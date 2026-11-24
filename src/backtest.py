import numpy as np
import pandas as pd
import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.ticker import StrMethodFormatter

import config
from rolling_pnl import compute_rolling_pnl

# Weekly moving-average pairs and percentiles used in the analyses.
SHORT_WINDOWS = range(1, 5)
LONG_WINDOWS = range(8, 27)
PAIRS = [(m, n) for m in SHORT_WINDOWS for n in LONG_WINDOWS]
PERCENTILES = np.arange(5, 96, 5)


def weekly_positions(positions: pd.DataFrame, ticker: str) -> pd.DataFrame:
    # Managed-money net positioning is longs minus shorts.
    columns = [f"{ticker}_m_money_long", f"{ticker}_m_money_short"]
    
    reports = positions[columns].dropna().sort_index().copy()
    if reports.index.has_duplicates:
        raise ValueError(f"{ticker}: duplicate positioning report dates.")
    
    weekend = reports.index.day_of_week >= 5
    if weekend.any():
        dates = reports.index[weekend].strftime("%Y-%m-%d").tolist()
        print(f"Warning: {ticker}: ignored weekend report dates: {dates}")
    reports = reports.loc[~weekend]
    if reports.empty:
        raise ValueError(f"{ticker}: no weekday managed-money reports available.")
    
    reports["net"] = reports[columns[0]] - reports[columns[1]]
    
    return reports[["net"]]


def crossover_signals(reports: pd.DataFrame, pairs: list[tuple[int, int]]) -> pd.DataFrame:
    """Calculate signals from the weekday reports returned by weekly_positions."""
    if not pairs or any(m < 1 or n <= m for m, n in pairs):
        raise ValueError("Moving-average windows must satisfy 1 <= m < n.")
    
    reports = reports.sort_index()
    if reports.empty:
        raise ValueError("No weekday managed-money reports available.")
    
    weeks = reports.index.normalize() - pd.to_timedelta(reports.index.day_of_week, unit="D")
    latest = ~weeks.duplicated(keep="last")
    reports = reports.loc[latest]
    weeks = weeks[latest]
    
    # Use calendar-week windows, ignoring missing reports after the initial warm-up.
    net = pd.Series(reports["net"].to_numpy(), index=weeks).asfreq("W-MON")
    averages = {}
    for window in sorted({window for pair in pairs for window in pair}):
        average = net.rolling(window, min_periods=1).mean()
        average.iloc[:window - 1] = np.nan
        averages[window] = average.reindex(weeks).to_numpy()
        
    # The sign of the crossover determines long, short, or flat exposure.
    return pd.DataFrame(
        {pair: np.nan_to_num(np.sign(averages[pair[0]] - averages[pair[1]]), nan=0.0)
         for pair in pairs},
        index=reports.index,
    )


def weekly_schedule(
    index: pd.DatetimeIndex, reports: pd.DataFrame, signals: pd.DataFrame,
) -> tuple[pd.DataFrame, pd.Series]:
    """Schedule signals using the weekday reports returned by weekly_positions."""
    # Use the latest weekday report from the previous week, assuming Friday release.
    week = index.normalize() - pd.to_timedelta(index.day_of_week, unit="D")
    rebalance_weeks = week.drop_duplicates()
    
    eligible = reports.copy()
    eligible["report_date"] = eligible.index
    eligible.index = eligible.index.normalize() - pd.to_timedelta(eligible.index.day_of_week, unit="D")
    eligible = eligible.loc[~eligible.index.duplicated(keep="last")]
    
    previous_weeks = rebalance_weeks - pd.Timedelta(days=7)
    selected = eligible.reindex(previous_weeks)
    report_dates = pd.DatetimeIndex(selected["report_date"])
    
    # Missing reports produce a flat position rather than a stale signal.
    selected_signals = signals.reindex(report_dates).fillna(0)
    selected_signals.index = rebalance_weeks
    selected_dates = pd.Series(report_dates, index=rebalance_weeks)
    
    return selected_signals, selected_dates


def apply_schedule(
    rolling: pd.DataFrame, schedule: pd.DataFrame, dates: pd.Series,
    pair: tuple[int, int],
) -> pd.DataFrame:
    rolling = rolling.copy()
    weeks = rolling.index.normalize() - pd.to_timedelta(rolling.index.day_of_week, unit="D")
    
    rolling["position_at_close"] = schedule[pair].reindex(weeks).to_numpy()
    rolling["report_date"] = pd.NaT
    
    first = ~weeks.duplicated()
    rolling.loc[first, "report_date"] = dates.reindex(weeks[first]).to_numpy()
    
    # Today's P&L belongs to the position held at the previous close.
    rolling["strategy_pnl"] = rolling["pnl"].fillna(0) * rolling["position_at_close"].shift(fill_value=0)
    
    return rolling


def backtest(
    prices: pd.DataFrame, expirations: pd.DataFrame, positions: pd.DataFrame,
    ticker: str, multiplier: float, m: int = 1, n: int = 8,
) -> pd.DataFrame:
    rolling = compute_rolling_pnl(ticker, prices, expirations, multiplier)
    reports = weekly_positions(positions, ticker)
    signals = crossover_signals(reports, [(m, n)])
    schedule, dates = weekly_schedule(rolling.index, reports, signals)
    rolling = apply_schedule(rolling, schedule, dates, (m, n))
    rolling["cumulative_pnl"] = rolling["strategy_pnl"].cumsum()
    
    return rolling


def weekly_pnl_matrix(
    rolling: pd.DataFrame, schedule: pd.DataFrame, start: str, end: str,
) -> pd.DataFrame:
    # Group P&L by the week of the previous close, including rebalance-day P&L.
    index = rolling.index
    weeks = index.normalize() - pd.to_timedelta(index.day_of_week, unit="D")
    held_week = pd.Series(weeks, index=index).shift()
    sample = (index >= pd.Timestamp(start)) & (index <= pd.Timestamp(end)) & held_week.notna()
    
    daily = rolling.loc[sample, "pnl"].fillna(0)
    if daily.empty:
        raise ValueError("No P&L observations in the requested period.")
    held = held_week.loc[sample]
    grouped = daily.groupby(held).sum()
    last_dates = pd.Series(index[sample], index=index[sample]).groupby(held).last()
    
    # Each column represents the weekly P&L for one (m, n) pair.
    exposure = schedule.reindex(pd.DatetimeIndex(grouped.index)).to_numpy()
    result = pd.DataFrame(exposure * grouped.to_numpy()[:, None],
                          index=pd.DatetimeIndex(last_dates), columns=schedule.columns)
    result.index.name = "date"
    
    return result


def sensitivity(weekly_pnl: pd.DataFrame) -> pd.DataFrame:
    # Annualize the weekly mean-to-sample-standard-deviation ratio.
    std = weekly_pnl.std(ddof=1).replace(0, np.nan)
    sharpe = weekly_pnl.mean() / std * np.sqrt(365 / 7)
    
    return pd.DataFrame(
        [[sharpe[(m, n)] for n in LONG_WINDOWS] for m in reversed(SHORT_WINDOWS)],
        index=list(reversed(SHORT_WINDOWS)), columns=list(LONG_WINDOWS),
    )


def monte_carlo_percentiles(
    weekly_pnl: pd.DataFrame, trials: int, seed: int,
) -> pd.DataFrame:
    if trials < 1:
        raise ValueError("Monte Carlo trials must be positive.")
    
    rng = np.random.default_rng(seed)
    cumulative = np.zeros(trials, dtype=np.float64)
    results = np.empty((len(weekly_pnl), len(PERCENTILES)))
    values = weekly_pnl.to_numpy(dtype=float)
    
    # Draw independently for every trial each week, retaining cumulative totals.
    for i, week in enumerate(values):
        cumulative += week[rng.integers(0, len(week), size=trials)]
        results[i] = np.percentile(cumulative, PERCENTILES, overwrite_input=False)
        
    return pd.DataFrame(results, index=weekly_pnl.index, columns=PERCENTILES)


def save_figures(ticker: str, sharpe: pd.DataFrame, percentiles: pd.DataFrame) -> None:
    # Plot the parameter sensitivity heatmap and simulated percentile paths.
    fig, ax = plt.subplots(figsize=(15, 7), constrained_layout=True)
    image = ax.imshow(sharpe.to_numpy(dtype=float), cmap="YlGnBu", vmin=-0.8, vmax=0.8, aspect="equal")
    ax.set_xticks(np.arange(len(sharpe.columns)), sharpe.columns)
    ax.set_yticks(np.arange(len(sharpe.index)), sharpe.index)
    ax.set_xlabel("Look-back period N (weeks)")
    ax.set_ylabel("Look-back period M (weeks)")
    ax.set_title(f"Follow {ticker} MM: Sharpe ratios for M and N", pad=16)
    fig.colorbar(image, ax=ax, orientation="horizontal", pad=0.16, label="Annualized weekly Sharpe")
    fig.savefig(config.OUTPUTS_DIR / f"{ticker}_sensitivity.png", dpi=180)
    plt.close(fig)

    fig, ax = plt.subplots(figsize=(12, 6))
    for percentile in percentiles.columns:
        key = percentile in (5, 10, 50, 90, 95)
        ax.plot(percentiles.index, percentiles[percentile],
                label=f"{percentile}th" if key else None,
                linewidth=2 if key else 1, alpha=1 if key else 0.4,
                linestyle="-" if key else "--", color=None if key else "black")
    ax.set_title(f"Follow {ticker} MM: simulated cumulative P&L percentiles")
    ax.set_xlabel("Date")
    ax.set_ylabel("Cumulative P&L ($)")
    ax.yaxis.set_major_formatter(StrMethodFormatter("{x:,.0f}"))
    ax.legend(title="Percentile")
    ax.grid()
    fig.tight_layout()
    fig.savefig(config.OUTPUTS_DIR / f"{ticker}_monte_carlo.png", dpi=180)
    plt.close(fig)


def main() -> None:
    # Keep pre-start history for contract rolling and signal warm-up.
    prices = pd.read_csv(config.PROCESSED_DATA_FILE, index_col="date", parse_dates=True)
    prices = prices.loc[:config.PERIOD_END]
    expirations = pd.read_csv(config.EXPIRATION_FILE, parse_dates=["CO", "CL"])
    positions = pd.read_csv(config.POSITIONS_FILE, index_col="date", parse_dates=True)
    config.OUTPUTS_DIR.mkdir(parents=True, exist_ok=True)
    
    for ticker in ("CO", "CL"):
        rolling = compute_rolling_pnl(ticker, prices, expirations, 1000)
        reports = weekly_positions(positions, ticker)
        pairs = list(dict.fromkeys([*PAIRS, (config.MA_SHORT, config.MA_LONG)]))
        signals = crossover_signals(reports, pairs)
        schedule, dates = weekly_schedule(rolling.index, reports, signals)

        # Restrict output to the reporting period after building the strategy.
        rolling = apply_schedule(rolling, schedule, dates, (config.MA_SHORT, config.MA_LONG))
        result = rolling.loc[config.PERIOD_START:config.PERIOD_END].copy()
        if result.empty:
            raise ValueError(f"{ticker}: no data in the requested period.")
        result["cumulative_pnl"] = result["strategy_pnl"].cumsum()
        result.to_csv(config.OUTPUTS_DIR / f"{ticker}_backtest.csv", index_label="date")

        # Analyze all parameter pairs over the same reporting period.
        weekly = weekly_pnl_matrix(rolling, schedule[PAIRS], config.PERIOD_START, config.PERIOD_END)
        sharpe = sensitivity(weekly)
        percentiles = monte_carlo_percentiles(weekly, config.MONTE_CARLO_TRIALS, config.RANDOM_SEED)
        
        sharpe.to_csv(config.OUTPUTS_DIR / f"{ticker}_sensitivity.csv", index_label="m")
        percentiles.to_csv(config.OUTPUTS_DIR / f"{ticker}_monte_carlo_percentiles.csv", index_label="date")
        
        save_figures(ticker, sharpe, percentiles)
        
        print(f"{ticker}: {len(result)} closes, P&L ${result['strategy_pnl'].sum():,.2f}", flush=True)


if __name__ == "__main__":
    main()

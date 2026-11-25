import numpy as np
import pandas as pd
import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.ticker import StrMethodFormatter

import config
from performance import (
    add_transaction_costs, cost_period_table, median_path_metrics, reporting_periods,
)
from rolling_pnl import compute_rolling_pnl

# Weekly moving-average pairs and percentiles used in the analyses.
PAIRS = [(m, n) for m in config.SHORT_WINDOWS for n in config.LONG_WINDOWS]
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


def liquidate_at_end(rolling: pd.DataFrame) -> pd.DataFrame:
    """Close the existing exposure instead of rebalancing at the final close."""
    if rolling.empty:
        raise ValueError("Cannot liquidate an empty backtest.")
    
    rolling = rolling.copy()
    last = rolling.index[-1]
    rolling.at[last, "position_at_close"] = 0.0
    rolling.at[last, "roll_at_close"] = False
    
    return rolling


def backtest(
    prices: pd.DataFrame, expirations: pd.DataFrame, positions: pd.DataFrame,
    ticker: str, multiplier: float, m: int, n: int,
) -> pd.DataFrame:
    rolling = compute_rolling_pnl(ticker, prices, expirations, multiplier)
    reports = weekly_positions(positions, ticker)
    signals = crossover_signals(reports, [(m, n)])
    schedule, dates = weekly_schedule(rolling.index, reports, signals)
    rolling = apply_schedule(rolling, schedule, dates, (m, n))
    rolling = liquidate_at_end(rolling)
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
        [[sharpe[(m, n)] for n in config.LONG_WINDOWS] for m in reversed(config.SHORT_WINDOWS)],
        index=list(reversed(config.SHORT_WINDOWS)), columns=list(config.LONG_WINDOWS),
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


def monte_carlo_cost_period_report(
    rolling: pd.DataFrame, schedule: pd.DataFrame, dates: pd.Series,
    periods: pd.DataFrame, costs: tuple[float, ...], multiplier: float,
    trials: int, seed: int,
) -> pd.DataFrame:
    """Report period metrics for weekly random choices of independently costed pairs.

    Switching pairs does not recalculate trades between their actual positions.
    """
    if trials < 1:
        raise ValueError("Monte Carlo trials must be positive.")

    # Keep gross P&L and standalone trade legs separate so every cost scenario
    # uses the same simulated choices.
    gross = {}
    turnover = {}
    for pair in schedule.columns:
        daily = apply_schedule(rolling, schedule, dates, pair)
        daily = add_transaction_costs(liquidate_at_end(daily), (0.0,), multiplier)
        gross[pair] = daily["strategy_pnl"]
        turnover[pair] = daily["total_turnover"]
    gross = pd.DataFrame(gross)
    turnover = pd.DataFrame(turnover)

    # A day's P&L belongs to the position at the previous close. Both its P&L
    # and turnover are grouped under that close's holding week, including on
    # rebalance days.
    week = rolling.index.normalize() - pd.to_timedelta(rolling.index.day_of_week, unit="D")
    held_week = pd.Series(week, index=rolling.index).shift()
    full = periods.loc[periods["period"] == "full_sample"].iloc[0]
    sample = (rolling.index >= pd.Timestamp(full["start"])) & (
        rolling.index <= pd.Timestamp(full["end"])
    ) & held_week.notna()
    weeks = pd.DatetimeIndex(held_week.loc[sample].drop_duplicates())
    if weeks.empty:
        raise ValueError("No Monte Carlo observations in the requested period.")

    # Filter by actual date before grouping: a holding week can contribute to
    # two different reporting periods. Align all periods to the same weeks.
    grouped = []
    for period in periods.drop_duplicates("period").itertuples(index=False):
        mask = sample & (rolling.index >= pd.Timestamp(period.start)) & (
            rolling.index <= pd.Timestamp(period.end)
        )
        weekly_gross = gross.loc[mask].groupby(held_week.loc[mask]).sum().reindex(weeks, fill_value=0)
        weekly_turnover = turnover.loc[mask].groupby(held_week.loc[mask]).sum().reindex(weeks, fill_value=0)
        # Distinguish observed zero-P&L weeks from weeks outside this period.
        active = held_week.loc[mask].value_counts().reindex(weeks, fill_value=0).to_numpy() > 0
        years = ((pd.Timestamp(period.end) - pd.Timestamp(period.start)).days + 1) / 365.25
        grouped.append((period.period, period.start, period.end, years,
                        weekly_gross.to_numpy(), weekly_turnover.to_numpy(), active))

    cumulative_gross = np.zeros((len(grouped), trials))
    cumulative_turnover = np.zeros((len(grouped), trials))
    median_paths = [np.empty((len(weeks), len(costs))) for _ in grouped]
    rng = np.random.default_rng(seed)
    for week_index in range(len(weeks)):
        # One independent choice per trial; reuse it across periods and costs.
        choices = rng.integers(0, len(schedule.columns), size=trials)
        for i, (_, _, _, _, weekly_gross, weekly_turnover, active) in enumerate(grouped):
            if not active[week_index]:
                continue
            profit, legs = weekly_gross[week_index], weekly_turnover[week_index]
            cumulative_gross[i] += profit[choices]
            cumulative_turnover[i] += legs[choices]
            for j, cost in enumerate(costs):
                net = cumulative_gross[i] - cumulative_turnover[i] * multiplier * cost
                # The pointwise median may come from different trials each week.
                median_paths[i][week_index, j] = np.median(net, overwrite_input=True)

    rows = []
    for i, (period, start, end, years, _, _, active) in enumerate(grouped):
        for j, cost in enumerate(costs):
            metrics = median_path_metrics(median_paths[i][active, j], years)
            rows.append({
                "period": period, "start": pd.Timestamp(start).date(), "end": pd.Timestamp(end).date(),
                "cost_per_barrel_per_leg": cost, **metrics,
            })
            
    return pd.DataFrame(rows)


def save_figures(ticker: str, sharpe: pd.DataFrame, percentiles: pd.DataFrame) -> None:
    # Plot the parameter sensitivity heatmap and simulated percentile paths.
    fig, ax = plt.subplots(figsize=(15, 7), constrained_layout=True)
    image = ax.imshow(
        sharpe.to_numpy(dtype=float), cmap="YlGnBu",
        vmin=-0.8, vmax=0.8, aspect="equal",
    )
    ax.set_xticks(np.arange(len(sharpe.columns)), sharpe.columns, fontsize=14)
    ax.set_yticks(np.arange(len(sharpe.index)), sharpe.index, fontsize=14)
    ax.set_xlabel("Look-back period N (weeks)", fontsize=17, labelpad=12)
    ax.set_ylabel("Look-back period M (weeks)", fontsize=17, labelpad=12)
    ax.set_title(f"Follow {ticker} MM: Sharpe ratios for M and N",
                 fontsize=21, pad=20, fontweight="bold")
    colorbar = fig.colorbar(
        image, ax=ax, orientation="horizontal", pad=0.18,
        label="Annualized weekly Sharpe",
    )
    colorbar.set_label("Annualized weekly Sharpe", fontsize=15, labelpad=10)
    colorbar.ax.tick_params(labelsize=13)
    fig.savefig(config.OUTPUTS_DIR / f"{ticker}_sensitivity.png", dpi=220)
    plt.close(fig)

    fig, ax = plt.subplots(figsize=(16, 8), constrained_layout=True)
    for percentile in percentiles.columns:
        key = percentile in (5, 10, 50, 90, 95)
        ax.plot(percentiles.index, percentiles[percentile],
                label=f"{percentile}th" if key else None,
                linewidth=2.8 if key else 1.2, alpha=1 if key else 0.45,
                linestyle="-" if key else "--", color=None if key else "black")
    ax.set_title(f"Follow {ticker} MM: simulated cumulative P&L percentiles",
                 fontsize=21, pad=18, fontweight="bold")
    ax.set_xlabel("Date", fontsize=17, labelpad=10)
    ax.set_ylabel("Cumulative P&L ($)", fontsize=17, labelpad=10)
    ax.yaxis.set_major_formatter(StrMethodFormatter("{x:,.0f}"))
    ax.tick_params(axis="both", labelsize=14)
    ax.legend(title="Percentile", title_fontsize=15, fontsize=14, framealpha=0.95)
    ax.grid(alpha=0.25, linewidth=0.8)
    fig.savefig(config.OUTPUTS_DIR / f"{ticker}_monte_carlo.png", dpi=200)
    plt.close(fig)


def main() -> None:
    """Analyze both markets and write sensitivity and Monte Carlo outputs."""
    # Keep pre-start history for contract rolling and signal warm-up.
    prices = pd.read_csv(config.PROCESSED_DATA_FILE, index_col="date", parse_dates=True)
    prices = prices.loc[:config.PERIOD_END]
    expirations = pd.read_csv(config.EXPIRATION_FILE, parse_dates=["CO", "CL"])
    positions = pd.read_csv(config.POSITIONS_FILE, index_col="date", parse_dates=True)
    config.OUTPUTS_DIR.mkdir(parents=True, exist_ok=True)
    
    # Markets are analyzed separately; rolling P&L is for one long contract.
    for ticker in ("CO", "CL"):
        rolling = compute_rolling_pnl(ticker, prices, expirations, 1000)
        reports = weekly_positions(positions, ticker)
        signals = crossover_signals(reports, PAIRS)
        schedule, dates = weekly_schedule(rolling.index, reports, signals)

        sample = rolling.loc[config.PERIOD_START:config.PERIOD_END]
        if sample.empty:
            raise ValueError(f"{ticker}: no data in the requested period.")
        periods = reporting_periods(
            sample.index, config.PERIOD_START, config.PERIOD_END, config.IRAN_WAR_WINDOW_START,
        )

        # Each column is one pair's gross P&L, grouped by the holding week.
        weekly = weekly_pnl_matrix(rolling, schedule[PAIRS], config.PERIOD_START, config.PERIOD_END)

        # Sensitivity compares pairs using annualized weekly Sharpe ratios.
        sharpe = sensitivity(weekly)

        # Randomize the pair each week to build cumulative zero-cost percentiles.
        percentiles_no_cost = monte_carlo_percentiles(weekly, config.MONTE_CARLO_TRIALS, config.RANDOM_SEED)

        # Evaluate the same random choices using each pair's standalone turnover
        # for every configured cost, including zero, and reporting period.
        medians_with_cost = monte_carlo_cost_period_report(
            rolling, schedule[PAIRS], dates, periods, config.TRANSACTION_COSTS,
            1000, config.MONTE_CARLO_TRIALS, config.RANDOM_SEED,
        )

        # Save median-path metrics and a table of annualized median net P&L.
        medians_with_cost.to_csv(config.OUTPUTS_DIR / f"{ticker}_performance.csv", index=False)
        cost_period_table(medians_with_cost).to_csv(config.OUTPUTS_DIR / f"{ticker}_cost_period_table.csv")
        
        # Save the zero-cost sensitivity and percentile data used by the plots.
        sharpe.to_csv(config.OUTPUTS_DIR / f"{ticker}_sensitivity.csv", index_label="m")
        percentiles_no_cost.to_csv(config.OUTPUTS_DIR / f"{ticker}_monte_carlo_percentiles.csv", index_label="date")
        
        save_figures(ticker, sharpe, percentiles_no_cost)
        
        print(f"{ticker}: {len(sample)} closes analyzed", flush=True)


if __name__ == "__main__":
    main()

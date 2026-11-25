"""Cost accounting and period-specific Monte Carlo median-path statistics."""

import numpy as np
import pandas as pd


def cost_suffix(cost: float) -> str:
    return f"{cost:.2f}"


def add_transaction_costs(
    rolling: pd.DataFrame, costs: tuple[float, ...], multiplier: float,
) -> pd.DataFrame:
    if multiplier <= 0 or any(not np.isfinite(cost) or cost < 0 for cost in costs):
        raise ValueError("Multiplier must be positive and transaction costs finite and nonnegative.")
    
    result = rolling.copy()
    position = result["position_at_close"]
    previous = position.shift(fill_value=0)
    result["position_turnover"] = (position - previous).abs()
    
    # On a roll, close the old contract and open the new one. Do not also
    # charge the position change separately for those same executions.
    roll_legs = previous.abs() + position.abs()
    result["roll_turnover"] = np.where(
        result["roll_at_close"], roll_legs - result["position_turnover"], 0.0
    )
    result["total_turnover"] = result["position_turnover"] + result["roll_turnover"]
    
    for cost in costs:
        suffix = cost_suffix(cost)
        result[f"transaction_cost_{suffix}"] = result["total_turnover"] * multiplier * cost
        result[f"net_pnl_{suffix}"] = result["strategy_pnl"] - result[f"transaction_cost_{suffix}"]
    
    return result


def mean_ci(weekly: pd.Series, annualization: float) -> tuple[float, float]:
    """Normal-approximation 95% CI for annualized mean, with Newey–West/HAC SE."""
    values = weekly.to_numpy(dtype=float)
    
    n = len(values)
    if n < 8:
        return np.nan, np.nan
    
    centered = values - values.mean()
    lag = min(n - 1, int(np.floor(4 * (n / 100) ** (2 / 9))))
    long_run_variance = np.dot(centered, centered) / n
    
    for k in range(1, lag + 1):
        covariance = np.dot(centered[k:], centered[:-k]) / n
        long_run_variance += 2 * (1 - k / (lag + 1)) * covariance
    
    se = np.sqrt(max(long_run_variance, 0) / n) * annualization
    mean = values.mean() * annualization
    
    return mean - 1.96 * se, mean + 1.96 * se


def reporting_periods(index: pd.DatetimeIndex, start: str, end: str, iran_start: str) -> pd.DataFrame:
    sample_start, sample_end = pd.Timestamp(start), pd.Timestamp(end)
    war_start = pd.Timestamp(iran_start)
    periods = [("full_sample", sample_start, sample_end)]
    periods.append(("pre_2022", sample_start, pd.Timestamp("2021-12-31")))
    periods.extend(
        (str(year), pd.Timestamp(f"{year}-01-01"), pd.Timestamp(f"{year}-12-31"))
        for year in range(2022, 2026)
    )
    periods.extend([
        ("2026_jan_aug", pd.Timestamp("2026-01-01"), pd.Timestamp("2026-08-31")),
        ("iran_war_window", war_start, sample_end),
    ])
    
    rows = []
    for label, period_start, period_end in periods:
        period_start = max(period_start, sample_start)
        period_end = min(period_end, sample_end)
        if period_start > period_end:
            continue
        if not ((index >= period_start) & (index <= period_end)).any():
            continue
        rows.append({"period": label, "start": period_start, "end": period_end})
    
    return pd.DataFrame(rows, columns=["period", "start", "end"])


def median_path_metrics(cumulative: np.ndarray, years: float) -> dict[str, float]:
    """Descriptive performance of one period's weekly pointwise median path."""
    if len(cumulative) == 0 or years <= 0 or not np.isfinite(cumulative).all():
        raise ValueError("Median path must be nonempty and finite, with a positive period length.")
    
    path = np.r_[0.0, cumulative]
    weekly = np.diff(path)
    annualization = len(weekly) / years
    std = np.std(weekly, ddof=1) if len(weekly) > 1 else np.nan
    annual_vol = std * np.sqrt(annualization)
    profit = weekly[weekly > 0].sum()
    loss = -weekly[weekly < 0].sum()
    ci_low, ci_high = mean_ci(pd.Series(weekly), annualization)
    
    return {
        "weeks": len(weekly),
        "net_pnl": cumulative[-1],
        "annualized_net_pnl": cumulative[-1] / years,
        "annualized_weekly_vol": annual_vol,
        "weekly_sharpe": cumulative[-1] / years / annual_vol if annual_vol > 0 else np.nan,
        "weekly_max_drawdown": np.max(np.maximum.accumulate(path) - path),
        "weekly_profit_factor": profit / loss if loss > 0 else np.nan,
        "descriptive_hac_ci95_low": ci_low,
        "descriptive_hac_ci95_high": ci_high,
    }


def cost_period_table(report: pd.DataFrame) -> pd.DataFrame:
    periods = [
        "pre_2022", "2022", "2023", "2024", "2025",
        "iran_war_window", "2026_jan_aug", "full_sample",
    ]
    
    table = report.pivot(
        index="cost_per_barrel_per_leg", columns="period", values="annualized_net_pnl"
    )
    table = table.reindex(columns=periods).sort_index()
    table.columns.name = None
    
    return table

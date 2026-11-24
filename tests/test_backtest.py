import sys
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from backtest import (
    apply_schedule, backtest, crossover_signals, monte_carlo_percentiles,
    weekly_pnl_matrix, weekly_positions, weekly_schedule,
)
from rolling_pnl import compute_rolling_pnl


def prices(dates, first, second, third):
    return pd.DataFrame(
        {"CL1 Comdty": first, "CL2 Comdty": second, "CL3 Comdty": third},
        index=pd.to_datetime(dates),
    )


class RollingPnlTests(unittest.TestCase):
    def test_roll_uses_same_contract_and_allows_negative_prices(self):
        data = prices(
            ["2020-12-30", "2020-12-31", "2021-01-04", "2021-01-05", "2021-01-06"],
            [10, 11, 12, 13, -2], [20, 21, 22, 23, 24], [30, 31, 32, 33, 34],
        )
        expiry = pd.DataFrame({"CL": pd.to_datetime(["2021-01-05", "2021-02-05"])})
        result = compute_rolling_pnl("CL", data, expiry, 1000)
        # December close rolls to M2; after expiry yesterday's M2 becomes M1.
        self.assertEqual(result["contract_at_close"].tolist(), [1, 2, 2, 2, 1])
        self.assertEqual(result["pnl"].iloc[1:].tolist(), [1000, 1000, 1000, -25000])

    def test_missing_expiry_settlement_is_not_silently_bridged(self):
        data = prices(["2021-01-04", "2021-01-06"], [1, 2], [2, 3], [3, 4])
        expiry = pd.DataFrame({"CL": pd.to_datetime(["2021-01-05", "2021-02-05"])})
        with self.assertRaisesRegex(ValueError, "expiration without a settlement"):
            compute_rolling_pnl("CL", data, expiry, 1000)

    def test_partial_missing_prices_are_forward_filled(self):
        data = prices(["2021-01-01", "2021-01-04", "2021-01-05", "2021-01-06", "2021-01-07"],
                      [None, 10, None, 12, None], [19, 20, None, 22, 23], [29, 30, 31, 32, 33])
        expiry = pd.DataFrame({"CL": pd.to_datetime(["2021-01-25", "2021-02-25"])})
        output = StringIO()
        with redirect_stdout(output):
            result = compute_rolling_pnl("CL", data, expiry, 1000)
        self.assertEqual(result["pnl"].iloc[1:].tolist(), [0, 2000])
        self.assertEqual(output.getvalue(),
                         "Warning: CL: forward-filled 2021-01-05: CL1 Comdty, CL2 Comdty\n")


class SignalTests(unittest.TestCase):
    def test_weekend_reports_are_excluded_from_signals_and_schedule(self):
        data = pd.DataFrame(
            {"CL_m_money_long": [10, 20, 0], "CL_m_money_short": [0, 0, 0]},
            index=pd.to_datetime(["2021-01-05", "2021-01-12", "2021-01-16"]),
        )
        output = StringIO()
        with redirect_stdout(output):
            reports = weekly_positions(data, "CL")
        self.assertIn("2021-01-16", output.getvalue())
        signals = crossover_signals(reports, [(1, 2)])
        schedule, dates = weekly_schedule(pd.to_datetime(["2021-01-18"]), reports, signals)
        self.assertEqual(schedule[(1, 2)].iloc[0], 1)
        self.assertEqual(dates.iloc[0], pd.Timestamp("2021-01-12"))

    def test_only_weekend_reports_raise(self):
        data = pd.DataFrame(
            {"CL_m_money_long": [10], "CL_m_money_short": [0]},
            index=pd.to_datetime(["2021-01-16"]),
        )
        with redirect_stdout(StringIO()):
            with self.assertRaisesRegex(ValueError, "no weekday managed-money reports"):
                weekly_positions(data, "CL")

    def test_monday_close_entry_and_next_available_close_exit(self):
        dates = ["2021-01-11", "2021-01-12", "2021-01-15", "2021-01-19", "2021-01-20"]
        data = prices(dates, [10, 11, 12, 13, 14], [20, 21, 22, 23, 24], [30, 31, 32, 33, 34])
        expiry = pd.DataFrame({"CL": pd.to_datetime(["2021-01-25", "2021-02-25"])})
        reports = pd.DataFrame(
            {"CL_m_money_long": [100, 110, 105], "CL_m_money_short": [50, 50, 50]},
            index=pd.to_datetime(["2020-12-29", "2021-01-05", "2021-01-12"]),
        )
        result = backtest(data, expiry, reports, "CL", 1000, m=1, n=2)
        self.assertEqual(result["position_at_close"].tolist(), [1, 1, 1, -1, -1])
        self.assertEqual(result["strategy_pnl"].tolist(), [0, 1000, 1000, 1000, -1000])
        self.assertEqual(result.at[pd.Timestamp("2021-01-11"), "report_date"], pd.Timestamp("2021-01-05"))
        self.assertEqual(result.at[pd.Timestamp("2021-01-19"), "report_date"], pd.Timestamp("2021-01-12"))

    def test_missing_week_does_not_create_a_signal(self):
        data = prices(["2021-01-11", "2021-01-12"], [10, 11], [20, 21], [30, 31])
        expiry = pd.DataFrame({"CL": pd.to_datetime(["2021-01-25", "2021-02-25"])})
        reports = pd.DataFrame(
            {"CL_m_money_long": [100, 200], "CL_m_money_short": [50, 50]},
            index=pd.to_datetime(["2020-12-29", "2021-01-12"]),
        )
        result = backtest(data, expiry, reports, "CL", 1000, m=1, n=2)
        self.assertEqual(result["position_at_close"].tolist(), [0, 0])
        self.assertEqual(result["strategy_pnl"].sum(), 0)

    def test_calendar_window_skips_missing_report_without_restarting(self):
        dates = pd.to_datetime([
            "2021-01-05", "2021-01-12", "2021-01-19", "2021-01-26",
            "2021-02-02", "2021-02-09", "2021-02-16", "2021-03-02",
        ])
        reports = pd.DataFrame({"net": [1000, 0, 0, 0, 0, 0, 0, 100]}, index=dates)
        signals = crossover_signals(reports, [(1, 8)])
        self.assertTrue((signals.iloc[:-1, 0] == 0).all())
        # The eighth calendar-week window excludes January 5 and has seven reports.
        self.assertEqual(signals.iloc[-1, 0], 1)

    def test_initial_warmup_counts_weeks_not_reports(self):
        dates = pd.to_datetime([
            "2021-01-05", "2021-01-12", "2021-01-26", "2021-02-02",
            "2021-02-09", "2021-02-16", "2021-02-23",
        ])
        reports = pd.DataFrame({"net": [0, 0, 0, 0, 0, 0, 70]}, index=dates)
        signals = crossover_signals(reports, [(1, 8)])
        self.assertTrue((signals.iloc[:-1, 0] == 0).all())
        self.assertEqual(signals.iloc[-1, 0], 1)

    def test_missing_week_after_warmup_stays_flat_then_resumes(self):
        dates = pd.to_datetime(["2021-01-05", "2021-01-12", "2021-01-19", "2021-02-02"])
        reports = pd.DataFrame({"net": [10, 20, 30, 50]}, index=dates)
        signals = crossover_signals(reports, [(1, 3)])
        self.assertEqual(signals.iloc[:, 0].tolist(), [0, 0, 1, 1])
        trade_dates = pd.to_datetime(["2021-02-01", "2021-02-08"])
        schedule, _ = weekly_schedule(trade_dates, reports, signals)
        self.assertEqual(schedule[(1, 3)].tolist(), [0, 1])

    def test_weekly_pnl_matches_daily_pnl_with_start_inside_week(self):
        dates = pd.to_datetime(["2021-01-11", "2021-01-12", "2021-01-15",
                                "2021-01-19", "2021-01-20", "2021-01-25"])
        rolling = pd.DataFrame({"pnl": [np.nan, 100, 200, 300, 400, 500],
                                "contract_at_close": 1}, index=dates)
        reports = pd.DataFrame({"net": [10, 20, 5]}, index=pd.to_datetime(
            ["2020-12-29", "2021-01-05", "2021-01-12"]))
        signals = crossover_signals(reports, [(1, 2)])
        schedule, report_dates = weekly_schedule(dates, reports, signals)
        daily = apply_schedule(rolling, schedule, report_dates, (1, 2))
        weekly = weekly_pnl_matrix(daily, schedule, "2021-01-15", "2021-01-25")
        self.assertEqual(weekly[(1, 2)].tolist(), [500, -900])
        self.assertEqual(weekly[(1, 2)].sum(), daily.loc["2021-01-15":, "strategy_pnl"].sum())

    def test_monte_carlo_selects_new_pair_each_week(self):
        weekly = pd.DataFrame([[1.0, -1.0], [10.0, -10.0]],
                              index=pd.to_datetime(["2021-01-11", "2021-01-18"]))
        actual = monte_carlo_percentiles(weekly, 100, 42)
        rng = np.random.default_rng(42)
        cumulative = np.zeros(100)
        for i in range(len(weekly)):
            cumulative += weekly.iloc[i].to_numpy()[rng.integers(0, 2, size=100)]
            np.testing.assert_allclose(actual.iloc[i].to_numpy(),
                                       np.percentile(cumulative, actual.columns))


if __name__ == "__main__":
    unittest.main()

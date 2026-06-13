import csv
import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

import portfolio_v2_advisory_pipeline as pipeline


class PortfolioV2PercentageScaleFixTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.cwd = os.getcwd()
        os.chdir(self.tmp.name)
        self.addCleanup(lambda: os.chdir(self.cwd))
        self.env = mock.patch.dict(os.environ, {}, clear=True)
        self.env.start()
        self.addCleanup(self.env.stop)
        self.now = datetime(2026, 6, 13, 10, 0, tzinfo=timezone.utc)

    def write_csv(self, rows, age_minutes=5):
        path = Path("data/ml_portfolio_allocation_v2_scale_test.csv")
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
            writer.writeheader()
            writer.writerows(rows)
        modified = (self.now - timedelta(minutes=age_minutes)).timestamp()
        os.utime(path, (modified, modified))
        return str(path)

    def test_capital_pct_v2_values_below_one_remain_percentage_points(self):
        rows = [
            {"symbol": "BEATUSDT", "capital_pct_v2": "11.44", "ev_pct": "0.9005", "winrate": "0.4767"},
            {"symbol": "SOLUSDT", "capital_pct_v2": "0.8", "ev_pct": "0.0672", "winrate": "0.5128"},
            {"symbol": "XAUUSDT", "capital_pct_v2": "0.57", "ev_pct": "0.0469", "winrate": "0.4949"},
            {"symbol": "BTCUSDT", "capital_pct_v2": "0.3", "ev_pct": "0.0251", "winrate": "0.5262"},
            {"symbol": "LINKUSDT", "capital_pct_v2": "0.27", "ev_pct": "0.0226", "winrate": "0.4898"},
        ]
        report = pipeline.build_report(allocation_path=self.write_csv(rows), now=self.now)
        allocations = {row["symbol"]: row["allocation_pct"] for row in report["top_allocations"]}
        self.assertEqual(allocations["SOLUSDT"], 0.8)
        self.assertEqual(allocations["XAUUSDT"], 0.57)
        self.assertEqual(allocations["BTCUSDT"], 0.3)
        self.assertEqual(allocations["LINKUSDT"], 0.27)
        self.assertEqual(report["top_allocations"][0]["symbol"], "BEATUSDT")

    def test_fraction_allocation_column_is_still_scaled(self):
        rows = [
            {"symbol": "AUSDT", "allocation": "0.25", "ev": "1", "winrate": "0.5"},
            {"symbol": "BUSDT", "allocation": "0.20", "ev": "1", "winrate": "0.5"},
            {"symbol": "CUSDT", "allocation": "0.15", "ev": "1", "winrate": "0.5"},
            {"symbol": "DUSDT", "allocation": "0.10", "ev": "1", "winrate": "0.5"},
            {"symbol": "EUSDT", "allocation": "0.05", "ev": "1", "winrate": "0.5"},
        ]
        report = pipeline.build_report(allocation_path=self.write_csv(rows), now=self.now)
        self.assertEqual(report["top_allocations"][0]["allocation_pct"], 25.0)

    def test_large_allocation_dataset_must_total_near_one_hundred_percent(self):
        rows = [
            {"symbol": f"S{i}USDT", "capital_pct_v2": "10", "ev_pct": "1", "winrate": "0.5"}
            for i in range(15)
        ]
        report = pipeline.build_report(allocation_path=self.write_csv(rows), now=self.now)
        self.assertEqual(report["status"], "BLOCKED_DATA_QUALITY")
        self.assertTrue(any("allocation total 150.00%" in reason for reason in report["blocked_reasons"]))

    def test_stale_explicit_health_and_rebalancing_are_not_reused(self):
        rows = [
            {"symbol": "BEATUSDT", "capital_pct_v2": "30", "ev_pct": "1", "winrate": "0.5"},
            {"symbol": "HYPEUSDT", "capital_pct_v2": "25", "ev_pct": "1", "winrate": "0.5"},
            {"symbol": "WLDUSDT", "capital_pct_v2": "20", "ev_pct": "1", "winrate": "0.5"},
            {"symbol": "NEARUSDT", "capital_pct_v2": "15", "ev_pct": "1", "winrate": "0.5"},
            {"symbol": "GRASSUSDT", "capital_pct_v2": "10", "ev_pct": "1", "winrate": "0.5"},
        ]
        allocation = self.write_csv(rows, age_minutes=5)
        health_path = Path("logs/phase5c.json")
        rebalance_path = Path("logs/phase5d.json")
        health_path.parent.mkdir(parents=True, exist_ok=True)
        health_path.write_text(json.dumps({"portfolio_health": "GREEN", "largest_exposure": "WRONGUSDT"}), encoding="utf-8")
        rebalance_path.write_text(json.dumps({"buy_more": ["WRONGUSDT"]}), encoding="utf-8")
        old = (self.now - timedelta(minutes=500)).timestamp()
        os.utime(health_path, (old, old))
        os.utime(rebalance_path, (old, old))
        report = pipeline.build_report(
            allocation_path=allocation,
            health_path=str(health_path),
            rebalancing_path=str(rebalance_path),
            max_age_minutes=60,
            now=self.now,
        )
        self.assertIn("DERIVED_FROM_ALLOCATION", report["portfolio_health"]["health_source"])
        self.assertIn("DERIVED_FROM_ALLOCATION", report["rebalancing_source"])
        self.assertEqual(report["rebalancing"]["BUY MORE"][0]["symbol"], "BEATUSDT")

    def test_dry_run_status_is_explicit_and_never_sends(self):
        rows = [
            {"symbol": "AUSDT", "capital_pct_v2": "25", "ev_pct": "1", "winrate": "0.5"},
            {"symbol": "BUSDT", "capital_pct_v2": "20", "ev_pct": "1", "winrate": "0.5"},
            {"symbol": "CUSDT", "capital_pct_v2": "15", "ev_pct": "1", "winrate": "0.5"},
            {"symbol": "DUSDT", "capital_pct_v2": "10", "ev_pct": "1", "winrate": "0.5"},
            {"symbol": "EUSDT", "capital_pct_v2": "5", "ev_pct": "1", "winrate": "0.5"},
        ]
        report = pipeline.build_report(allocation_path=self.write_csv(rows), now=self.now)
        with mock.patch.object(pipeline, "send_telegram", side_effect=AssertionError("send called")):
            result = pipeline.send_or_preview(
                report,
                send_requested=False,
                dry_run=True,
                ignore_cooldown=False,
                cooldown_seconds=60,
                state_path="logs/state.json",
            )
        self.assertEqual(result["status"], "BLOCKED_DRY_RUN")
        self.assertFalse(result["send_attempted"])


if __name__ == "__main__":
    unittest.main()

import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

import pandas as pd

import refresh_portfolio_v2_artifacts as refresh


class RefreshPortfolioV2ArtifactsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.cwd = os.getcwd()
        os.chdir(self.tmp.name)
        self.addCleanup(lambda: os.chdir(self.cwd))
        self.now = datetime(2026, 6, 13, 10, 0, tzinfo=timezone.utc)

    def write_source(self, path, age_minutes=5, symbols=6, rows_per_symbol=40):
        records = []
        for index in range(symbols):
            symbol = f"S{index}USDT"
            for row in range(rows_per_symbol):
                won = row % 2 == 0
                records.append(
                    {
                        "symbol": symbol,
                        "win_loss": "WIN" if won else "LOSS",
                        "pnl_pct": 2.0 + index * 0.1 if won else -1.0,
                        "position_size_multiplier": 0.5 + index * 0.01,
                    }
                )
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        pd.DataFrame(records).to_csv(target, index=False)
        modified = (self.now - timedelta(minutes=age_minutes)).timestamp()
        os.utime(target, (modified, modified))
        return str(target)

    def test_fresh_source_writes_timestamped_artifacts(self):
        source = self.write_source("data/ml_calibration_with_position_sizing_20260613.csv")
        result = refresh.run_refresh(
            source_path=source,
            output_tag="20260613T100000Z",
            now=self.now,
        )
        self.assertEqual(result["status"], "REFRESHED")
        self.assertTrue(result["outputs_written"])
        self.assertEqual(result["allocation_rows"], 6)
        self.assertAlmostEqual(result["allocation_total_pct"], 100.0, places=2)
        for path in result["output_paths"].values():
            self.assertTrue(Path(path).exists())
        self.assertFalse(result["safety"]["runtime_v1_changed"])
        self.assertFalse(result["safety"]["broker_routing"])
        self.assertFalse(result["safety"]["order_attempted"])

    def test_dry_run_writes_nothing(self):
        source = self.write_source("data/ml_calibration_with_position_sizing_20260613.csv")
        result = refresh.run_refresh(
            source_path=source,
            output_tag="dryrun",
            dry_run=True,
            now=self.now,
        )
        self.assertEqual(result["status"], "DRY_RUN_READY")
        self.assertFalse(result["outputs_written"])
        for path in result["output_paths"].values():
            self.assertFalse(Path(path).exists())

    def test_stale_source_is_blocked_without_output(self):
        source = self.write_source(
            "data/ml_calibration_with_position_sizing_20260610.csv",
            age_minutes=500,
        )
        result = refresh.run_refresh(
            source_path=source,
            source_max_age_minutes=60,
            output_tag="blocked",
            now=self.now,
        )
        self.assertEqual(result["status"], "BLOCKED_STALE_SOURCE")
        self.assertFalse(result["outputs_written"])
        self.assertIn("stale", result["blocked_reasons"][0])

    def test_latest_compatible_source_is_selected(self):
        old_source = self.write_source(
            "data/ml_calibration_with_position_sizing_old.csv",
            age_minutes=30,
        )
        new_source = self.write_source(
            "data/ml_calibration_with_position_sizing_new.csv",
            age_minutes=2,
        )
        invalid = Path("data/ml_calibration_with_position_sizing_invalid.csv")
        pd.DataFrame([{"symbol": "BAD"}]).to_csv(invalid, index=False)
        os.utime(invalid, (self.now.timestamp(), self.now.timestamp()))
        selected = refresh.discover_latest_compatible_source()
        self.assertEqual(selected, new_source)
        self.assertNotEqual(selected, old_source)
        self.assertNotEqual(selected, str(invalid))

    def test_negative_ev_symbols_are_removed(self):
        source = self.write_source("data/ml_calibration_with_position_sizing_20260613.csv")
        frame = pd.read_csv(source)
        mask = frame["symbol"] == "S5USDT"
        frame.loc[mask & (frame["win_loss"] == "WIN"), "pnl_pct"] = 0.1
        frame.loc[mask & (frame["win_loss"] == "LOSS"), "pnl_pct"] = -2.0
        frame.to_csv(source, index=False)
        os.utime(source, (self.now.timestamp(), self.now.timestamp()))
        allocation = refresh.build_allocation(source)
        row = allocation[allocation["symbol"] == "S5USDT"].iloc[0]
        self.assertEqual(float(row["capital_pct_v2"]), 0.0)
        rebalancing = refresh.build_rebalancing(allocation)
        removed = {item["symbol"] for item in rebalancing["remove"]}
        self.assertIn("S5USDT", removed)

    def test_missing_required_columns_is_blocked(self):
        source = Path("data/ml_calibration_with_position_sizing_bad.csv")
        source.parent.mkdir(parents=True, exist_ok=True)
        pd.DataFrame([{"symbol": "AUSDT", "win_loss": "WIN"}]).to_csv(source, index=False)
        os.utime(source, (self.now.timestamp(), self.now.timestamp()))
        result = refresh.run_refresh(
            source_path=str(source),
            output_tag="bad",
            now=self.now,
        )
        self.assertEqual(result["status"], "BLOCKED_SOURCE_INVALID")
        self.assertTrue(any("missing required columns" in item for item in result["blocked_reasons"]))


if __name__ == "__main__":
    unittest.main()

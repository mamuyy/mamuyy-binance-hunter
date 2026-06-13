import csv
import os
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

import portfolio_v2_live_advisory as live


class PortfolioV2LiveAdvisoryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.cwd = os.getcwd()
        os.chdir(self.tmp.name)
        self.addCleanup(lambda: os.chdir(self.cwd))
        self.now = datetime(2026, 6, 13, 10, 0, tzinfo=timezone.utc)

    def write_allocation(self):
        path = Path("data/ml_portfolio_allocation_v2_20260610.csv")
        path.parent.mkdir(parents=True, exist_ok=True)
        rows = [
            ("BEATUSDT", 30, 0.9, 0.47),
            ("HYPEUSDT", 25, 0.8, 0.60),
            ("WLDUSDT", 20, 0.6, 0.52),
            ("NEARUSDT", 15, 0.7, 0.55),
            ("ETHUSDT", 10, -0.2, 0.40),
            ("DOGEUSDT", 0, 1.2, 0.70),
        ]
        with path.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.writer(handle)
            writer.writerow(["symbol", "capital_pct_v2", "ev_pct", "winrate"])
            writer.writerows(rows)
        old = (self.now - timedelta(days=5)).timestamp()
        os.utime(path, (old, old))
        return str(path)

    def write_db(self, heartbeat_age=2, signal_age=5, internal_symbol=None):
        path = "mamuyy_hunter.db"
        with sqlite3.connect(path) as connection:
            connection.executescript("""
                CREATE TABLE runtime_heartbeats(id INTEGER PRIMARY KEY, timestamp TEXT, source TEXT, state TEXT, system_health_score REAL, scheduler TEXT, uptime_seconds REAL, message TEXT);
                CREATE TABLE regime_logs(id INTEGER PRIMARY KEY, timestamp TEXT, regime_name TEXT, regime_score REAL);
                CREATE TABLE signals(id INTEGER PRIMARY KEY, timestamp TEXT, symbol TEXT, score REAL, calculated_score REAL, adaptive_confidence_score REAL, regime_name TEXT);
                CREATE TABLE paper_trades(id INTEGER PRIMARY KEY, timestamp TEXT, symbol TEXT, status TEXT);
                CREATE TABLE internal_paper_trades(id INTEGER PRIMARY KEY, timestamp TEXT, symbol TEXT, status TEXT);
            """)
            connection.execute("INSERT INTO runtime_heartbeats VALUES(1,?,?,?,?,?,?,?)", ((self.now-timedelta(minutes=heartbeat_age)).isoformat(), "orchestrator", "IDLE", 100, "NORMAL", 1000, "ok"))
            connection.execute("INSERT INTO regime_logs VALUES(1,?,?,?)", (self.now.isoformat(), "TRENDING BULL", 80))
            timestamp = (self.now-timedelta(minutes=signal_age)).isoformat()
            for index, (symbol, score) in enumerate([("BEATUSDT",80),("HYPEUSDT",75),("WLDUSDT",60),("NEARUSDT",50),("ETHUSDT",40),("DOGEUSDT",99)], 1):
                connection.execute("INSERT INTO signals VALUES(?,?,?,?,?,?,?)", (index, timestamp, symbol, score, score, score, "TRENDING BULL"))
            connection.execute("INSERT INTO paper_trades VALUES(1,?,?,?)", (timestamp, "BEATUSDT", "OPEN"))
            if internal_symbol:
                connection.execute("INSERT INTO internal_paper_trades VALUES(1,?,?,?)", (timestamp, internal_symbol, "OPEN"))
            connection.commit()
        return path

    def test_old_baseline_is_valid_with_fresh_live_data(self):
        report = live.build_report(self.write_allocation(), self.write_db(), now=self.now)
        self.assertEqual(report["status"], "READY")
        self.assertGreater(report["research_baseline"]["file_age_minutes"], 1000)
        actions = {row["symbol"]: row["advisory_action"] for row in report["overlay_rankings"]}
        self.assertEqual(actions["BEATUSDT"], "HOLD_NO_ADD")
        self.assertEqual(actions["HYPEUSDT"], "TOP_WATCH")
        self.assertEqual(actions["DOGEUSDT"], "BASELINE_EXCLUDED")
        self.assertFalse(report["research_baseline_mutated"])
        self.assertFalse(report["runtime_v1_changed"])
        self.assertFalse(report["broker_routing_enabled"])
        self.assertFalse(report["order_attempted"])

    def test_zero_allocation_symbol_never_enters_top_watch(self):
        report = live.build_report(self.write_allocation(), self.write_db(), now=self.now)
        top_watch = [
            row["symbol"]
            for row in report["overlay_rankings"]
            if row["advisory_action"] in {"TOP_WATCH", "WATCH"}
        ]
        self.assertNotIn("DOGEUSDT", top_watch)

    def test_exposure_reads_both_paper_tables(self):
        report = live.build_report(
            self.write_allocation(),
            self.write_db(internal_symbol="HYPEUSDT"),
            now=self.now,
        )
        exposures = report["live_overlay"]["active_exposures"]
        self.assertEqual(exposures["BEATUSDT"], 1)
        self.assertEqual(exposures["HYPEUSDT"], 1)
        self.assertEqual(
            report["live_overlay"]["exposure_sources"],
            ["paper_trades", "internal_paper_trades"],
        )

    def test_stale_heartbeat_blocks_live_overlay(self):
        report = live.build_report(self.write_allocation(), self.write_db(heartbeat_age=60), now=self.now)
        self.assertEqual(report["status"], "BLOCKED_LIVE_OVERLAY")
        self.assertTrue(any("heartbeat stale" in reason for reason in report["blocked_reasons"]))

    def test_stale_signals_block_live_overlay(self):
        report = live.build_report(self.write_allocation(), self.write_db(signal_age=500), now=self.now)
        self.assertEqual(report["status"], "BLOCKED_LIVE_OVERLAY")
        self.assertTrue(any("signals stale" in reason for reason in report["blocked_reasons"]))

    def test_missing_runtime_database_blocks(self):
        report = live.build_report(self.write_allocation(), "missing.db", now=self.now)
        self.assertEqual(report["status"], "BLOCKED_LIVE_OVERLAY")


if __name__ == "__main__":
    unittest.main()

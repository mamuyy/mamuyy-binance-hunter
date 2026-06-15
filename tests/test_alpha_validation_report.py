import json
import os
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path

import alpha_validation_report as avr


class AlphaValidationReportTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.cwd = os.getcwd()
        os.chdir(self.tmp.name)
        self.addCleanup(lambda: os.chdir(self.cwd))

    def write_db(self, rows, extra_cols=""):
        path = Path("mamuyy_hunter.db")
        with sqlite3.connect(path) as con:
            con.execute(
                f"""
                CREATE TABLE internal_paper_trades(
                    id INTEGER PRIMARY KEY,
                    timestamp TEXT,
                    symbol TEXT,
                    status TEXT,
                    pnl REAL,
                    market_regime TEXT,
                    ml_confidence REAL
                    {extra_cols}
                )
                """
            )
            placeholders = ",".join("?" for _ in rows[0])
            con.executemany(f"INSERT INTO internal_paper_trades VALUES({placeholders})", rows)
            con.commit()
        return str(path)

    def test_absolute_drawdown_not_compared_to_percentage_gate_without_baseline(self):
        rows = [(1, "2026-01-01", "ETHUSDT", "CLOSED", 100.0, "BULL", 90.0), (2, "2026-01-02", "ETHUSDT", "CLOSED", -50.0, "BULL", 90.0)]
        report = avr.build_report(self.write_db(rows))
        self.assertEqual(report["core_performance"]["maximum_drawdown_absolute"], 50.0)
        self.assertIsNone(report["core_performance"]["maximum_drawdown_pct"])
        self.assertEqual(report["readiness_references"]["maximum_drawdown_pct_le_15"]["passed"], avr.UNKNOWN)

    def test_low_sample_regimes_excluded_from_headline_best_worst(self):
        rows = []
        for i in range(1, 20):
            rows.append((i, f"2026-01-{i:02d}", "MOON", "CLOSED", 10.0, "LOW_SAMPLE_WINNER", 80.0))
        for i in range(20, 45):
            rows.append((i, f"2026-02-{i-19:02d}", "ETH", "CLOSED", 1.0, "VALID", 80.0))
        report = avr.build_report(self.write_db(rows))
        regime = report["edge_segmentation"]["market_regime"]
        self.assertEqual(regime["groups"]["LOW_SAMPLE_WINNER"]["sample_flag"], "LOW_SAMPLE")
        self.assertEqual(regime["headline_best"], "VALID")
        self.assertEqual(regime["headline_worst"], "VALID")

    def test_confidence_bucketing(self):
        rows = [
            (1, "2026-01-01", "ETH", "CLOSED", 1.0, "BULL", 0.74),
            (2, "2026-01-02", "ETH", "CLOSED", 1.0, "BULL", 77.0),
            (3, "2026-01-03", "ETH", "CLOSED", 1.0, "BULL", 82.0),
            (4, "2026-01-04", "ETH", "CLOSED", 1.0, "BULL", 88.0),
            (5, "2026-01-05", "ETH", "CLOSED", 1.0, "BULL", 93.0),
            (6, "2026-01-06", "ETH", "CLOSED", 1.0, "BULL", 99.0),
        ]
        report = avr.build_report(self.write_db(rows))
        groups = report["edge_segmentation"]["ml_confidence"]["groups"]
        self.assertEqual(set(groups), {"<75", "75-80", "80-85", "85-90", "90-95", "95-100"})
        self.assertTrue(all(group["sample_flag"] == "LOW_SAMPLE" for group in groups.values()))

    def test_strict_json_without_infinity_nan(self):
        rows = [(1, "2026-01-01", "ETH", "CLOSED", 1.0, "BULL", 90.0), (2, "2026-01-02", "ETH", "CLOSED", 2.0, "BULL", 90.0)]
        report = avr.build_report(self.write_db(rows))
        self.assertIsNone(report["core_performance"]["profit_factor"])
        self.assertEqual(report["core_performance"]["profit_factor_reason"], "no_losses")
        json_path, _ = avr.write_outputs(report)
        text = json_path.read_text()
        self.assertNotIn("Infinity", text)
        self.assertNotIn("NaN", text)
        json.loads(text, parse_constant=lambda value: (_ for _ in ()).throw(ValueError(value)))

    def test_compact_markdown(self):
        rows = [(i, f"2026-01-{((i - 1) % 28) + 1:02d}", "ETH", "CLOSED", 1.0, "BULL", 90.0) for i in range(1, 245)]
        report = avr.build_report(self.write_db(rows))
        _, md_path = avr.write_outputs(report)
        lines = md_path.read_text().splitlines()
        self.assertLess(len(lines), 300)
        self.assertNotIn("```json", md_path.read_text())

    def test_latest_50_degradation_preserved_without_forcing_negative_edge(self):
        rows = []
        for i in range(1, 151):
            pnl = 2.0 if i <= 100 else -0.5
            rows.append((i, (datetime(2026, 1, 1) + timedelta(days=i)).date().isoformat(), "ETH", "CLOSED", pnl, "BULL", 90.0))
        report = avr.build_report(self.write_db(rows))
        self.assertEqual(report["stability"]["assessment"], "DEGRADING")
        self.assertLess(report["stability"]["latest_50"]["expectancy_per_trade"], 0)
        self.assertEqual(report["verdict"]["research_audit_verdict"], "INCONCLUSIVE")
        self.assertIn("monitoring", report["stability"]["recent_degradation_note"])

    def test_database_remains_read_only(self):
        rows = [(1, "2026-01-01", "ETH", "CLOSED", 1.0, "BULL", 90.0), (2, "2026-01-02", "ETH", "OPEN", 2.0, "BULL", 90.0)]
        db = self.write_db(rows)
        before = sqlite3.connect(db).execute("SELECT COUNT(*) FROM internal_paper_trades").fetchone()[0]
        report = avr.build_report(db)
        after = sqlite3.connect(db).execute("SELECT COUNT(*) FROM internal_paper_trades").fetchone()[0]
        self.assertEqual(before, after)
        self.assertEqual(report["data_quality"]["non_closed_rows"], 1)
        self.assertEqual(report["data_quality"]["invalid_unknown_status_count"], 0)
        with sqlite3.connect(f"file:{Path(db).resolve()}?mode=ro", uri=True) as con:
            with self.assertRaises(sqlite3.OperationalError):
                con.execute("INSERT INTO internal_paper_trades(id) VALUES(99)")


if __name__ == "__main__":
    unittest.main()

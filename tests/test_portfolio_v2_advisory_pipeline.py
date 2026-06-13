import csv
import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import portfolio_v2_advisory_pipeline as pipeline


class PortfolioV2AdvisoryPipelineTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.cwd = os.getcwd()
        os.chdir(self.tmp.name)
        self.addCleanup(lambda: os.chdir(self.cwd))
        self.env = mock.patch.dict(os.environ, {}, clear=True)
        self.env.start()
        self.addCleanup(self.env.stop)
        self.now = datetime(2026, 6, 13, 8, 0, tzinfo=timezone.utc)

    def write_allocation(self, path="data/ml_portfolio_allocation_v2_test.csv", rows=None, age_minutes=5):
        records = rows or [
            {"symbol": "BEATUSDT", "capital_pct_v2": "11.44", "ev_pct": "0.9005", "winrate": "47.67"},
            {"symbol": "HYPEUSDT", "capital_pct_v2": "9.67", "ev_pct": "0.7950", "winrate": "60.47"},
            {"symbol": "WLDUSDT", "capital_pct_v2": "8.96", "ev_pct": "0.6680", "winrate": "52.73"},
            {"symbol": "NEARUSDT", "capital_pct_v2": "8.48", "ev_pct": "0.7000", "winrate": "55.35"},
            {"symbol": "GRASSUSDT", "capital_pct_v2": "7.06", "ev_pct": "0.5716", "winrate": "53.05"},
            {"symbol": "ONDOUSDT", "capital_pct_v2": "6.43", "ev_pct": "0.5513", "winrate": "54.31"},
            {"symbol": "INUSDT", "capital_pct_v2": "6.31", "ev_pct": "0.5019", "winrate": "52.97"},
            {"symbol": "TAOUSDT", "capital_pct_v2": "4.96", "ev_pct": "0.4147", "winrate": "55.17"},
            {"symbol": "ETHUSDT", "capital_pct_v2": "0", "ev_pct": "-0.20", "winrate": "40"},
            {"symbol": "DOGEUSDT", "capital_pct_v2": "0", "ev_pct": "-0.15", "winrate": "41"},
        ]
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=list(records[0]))
            writer.writeheader()
            writer.writerows(records)
        modified = (self.now - timedelta(minutes=age_minutes)).timestamp()
        os.utime(target, (modified, modified))
        return str(target)

    def write_json(self, path, payload, age_minutes=5):
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(payload), encoding="utf-8")
        modified = (self.now - timedelta(minutes=age_minutes)).timestamp()
        os.utime(target, (modified, modified))
        return str(target)

    def test_fresh_allocation_builds_ready_advisory(self):
        allocation = self.write_allocation()
        report = pipeline.build_report(allocation_path=allocation, now=self.now)
        self.assertEqual(report["status"], "READY")
        self.assertEqual(report["rows_evaluated"], 10)
        self.assertEqual(report["top_allocations"][0]["symbol"], "BEATUSDT")
        self.assertEqual(report["rebalancing"]["BUY MORE"][0]["symbol"], "BEATUSDT")
        self.assertEqual(
            [item["symbol"] for item in report["rebalancing"]["REMOVE"]],
            ["DOGEUSDT", "ETHUSDT"],
        )
        self.assertIn("PORTFOLIO ENGINE V2 — ADVISORY", report["payload_text"])
        self.assertFalse(report["runtime_v1_changed"])
        self.assertFalse(report["broker_routing_enabled"])
        self.assertFalse(report["order_attempted"])

    def test_stale_allocation_is_blocked(self):
        allocation = self.write_allocation(age_minutes=500)
        report = pipeline.build_report(
            allocation_path=allocation,
            max_age_minutes=60,
            now=self.now,
        )
        self.assertEqual(report["status"], "BLOCKED_STALE_DATA")
        self.assertTrue(any("stale" in reason for reason in report["blocked_reasons"]))
        self.assertIn("Advisory blocked", report["payload_text"])

    def test_duplicate_normalized_symbol_is_blocked(self):
        rows = [
            {"symbol": "ETH-USDT", "capital_pct_v2": "20", "ev_pct": "1", "winrate": "50"},
            {"symbol": "ETHUSDT", "capital_pct_v2": "10", "ev_pct": "1", "winrate": "50"},
            {"symbol": "BTCUSDT", "capital_pct_v2": "20", "ev_pct": "1", "winrate": "50"},
            {"symbol": "SOLUSDT", "capital_pct_v2": "20", "ev_pct": "1", "winrate": "50"},
            {"symbol": "BNBUSDT", "capital_pct_v2": "20", "ev_pct": "1", "winrate": "50"},
        ]
        allocation = self.write_allocation(rows=rows)
        report = pipeline.build_report(allocation_path=allocation, now=self.now)
        self.assertEqual(report["status"], "BLOCKED_DATA_QUALITY")
        self.assertTrue(any("duplicate normalized symbols" in reason for reason in report["blocked_reasons"]))

    def test_explicit_health_and_rebalancing_are_preferred(self):
        allocation = self.write_allocation()
        health = self.write_json(
            "logs/phase5c_portfolio_health.json",
            {
                "portfolio_health": "GREEN",
                "risk_score": 11.44,
                "diversification_score": 86.67,
                "largest_exposure_symbol": "BEATUSDT",
                "largest_exposure_pct": 11.44,
            },
        )
        rebalancing = self.write_json(
            "logs/phase5d_portfolio_rebalancing.json",
            {
                "buy_more": [
                    {"symbol": "BEATUSDT", "capital_pct_v2": 11.44},
                    {"symbol": "HYPEUSDT", "capital_pct_v2": 9.67},
                ],
                "reduce": [{"symbol": "BNBUSDT", "capital_pct_v2": 1.03}],
                "remove": ["ETHUSDT", "DOGEUSDT"],
            },
        )
        report = pipeline.build_report(
            allocation_path=allocation,
            health_path=health,
            rebalancing_path=rebalancing,
            now=self.now,
        )
        self.assertEqual(report["portfolio_health"]["diversification_score"], 86.67)
        self.assertEqual(report["rebalancing_source"], rebalancing)
        self.assertEqual(report["rebalancing"]["REDUCE"][0]["symbol"], "BNBUSDT")
        self.assertEqual(report["rebalancing"]["REMOVE"][0]["symbol"], "ETHUSDT")

    def test_latest_allocation_source_is_selected(self):
        old_path = self.write_allocation("data/ml_portfolio_allocation_v2_old.csv", age_minutes=30)
        new_path = self.write_allocation("data/ml_portfolio_allocation_v2_new.csv", age_minutes=2)
        selected = pipeline.discover_latest(pipeline.ALLOCATION_PATTERNS)
        self.assertEqual(selected, new_path)
        self.assertNotEqual(selected, old_path)

    def test_active_execution_gate_blocks_advisory(self):
        allocation = self.write_allocation()
        os.environ["ALLOW_TESTNET_ORDER"] = "true"
        report = pipeline.build_report(allocation_path=allocation, now=self.now)
        self.assertEqual(report["status"], "BLOCKED_EXECUTION_GATES_ACTIVE")
        self.assertIn("ALLOW_TESTNET_ORDER", report["active_execution_gates"])

    def test_preview_never_sends(self):
        allocation = self.write_allocation()
        report = pipeline.build_report(allocation_path=allocation, now=self.now)
        with mock.patch.object(pipeline, "send_telegram", side_effect=AssertionError("send called")):
            result = pipeline.send_or_preview(
                report,
                send_requested=False,
                dry_run=False,
                ignore_cooldown=False,
                cooldown_seconds=100,
                state_path="logs/state.json",
            )
        self.assertEqual(result["status"], "PREVIEW_ONLY")
        self.assertFalse(result["send_attempted"])

    def test_manual_gate_is_required_for_send(self):
        allocation = self.write_allocation()
        report = pipeline.build_report(allocation_path=allocation, now=self.now)
        with mock.patch.object(
            pipeline,
            "config",
            SimpleNamespace(
                telegram_enabled=True,
                telegram_bot_token="redacted",
                telegram_chat_id="redacted",
                request_timeout_seconds=1,
            ),
        ), mock.patch.object(pipeline, "send_telegram", side_effect=AssertionError("send called")):
            result = pipeline.send_or_preview(
                report,
                send_requested=True,
                dry_run=False,
                ignore_cooldown=False,
                cooldown_seconds=100,
                state_path="logs/state.json",
            )
        self.assertEqual(result["status"], "BLOCKED_MANUAL_GATE")
        self.assertFalse(result["send_attempted"])

    def test_successful_manual_send_records_cooldown_and_blocks_duplicate(self):
        allocation = self.write_allocation()
        report = pipeline.build_report(allocation_path=allocation, now=self.now)
        os.environ[pipeline.ALLOW_SEND_ENV] = "1"
        fake_config = SimpleNamespace(
            telegram_enabled=True,
            telegram_bot_token="redacted",
            telegram_chat_id="redacted",
            request_timeout_seconds=1,
        )
        state_path = "logs/state.json"
        with mock.patch.object(pipeline, "config", fake_config), mock.patch.object(
            pipeline, "send_telegram", return_value=True
        ) as sender:
            first = pipeline.send_or_preview(
                report,
                send_requested=True,
                dry_run=False,
                ignore_cooldown=False,
                cooldown_seconds=3600,
                state_path=state_path,
            )
            second = pipeline.send_or_preview(
                report,
                send_requested=True,
                dry_run=False,
                ignore_cooldown=False,
                cooldown_seconds=3600,
                state_path=state_path,
            )
        self.assertEqual(first["status"], "SENT")
        self.assertEqual(second["status"], "BLOCKED_COOLDOWN")
        self.assertEqual(sender.call_count, 1)
        state = json.loads(Path(state_path).read_text(encoding="utf-8"))
        self.assertEqual(state["payload_sha256"], report["payload_sha256"])

    def test_blocked_report_cannot_send(self):
        allocation = self.write_allocation(age_minutes=500)
        report = pipeline.build_report(
            allocation_path=allocation,
            max_age_minutes=60,
            now=self.now,
        )
        os.environ[pipeline.ALLOW_SEND_ENV] = "1"
        with mock.patch.object(pipeline, "send_telegram", side_effect=AssertionError("send called")):
            result = pipeline.send_or_preview(
                report,
                send_requested=True,
                dry_run=False,
                ignore_cooldown=True,
                cooldown_seconds=0,
                state_path="logs/state.json",
            )
        self.assertEqual(result["status"], "BLOCKED_REPORT")
        self.assertFalse(result["send_attempted"])


if __name__ == "__main__":
    unittest.main()

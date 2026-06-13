import contextlib
import hashlib
import io
import json
import os
import subprocess
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path
from unittest import mock

import testnet_stability_limit_expansion_gate as gate
import testnet_stability_policy as policy


def write_json(path: Path, payload):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload) + "\n", encoding="utf-8")


def write_jsonl(path: Path, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")


class StabilityGateTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.cwd = os.getcwd()
        os.chdir(self.tmp.name)
        self.addCleanup(lambda: os.chdir(self.cwd))
        self.write_operations_result()

    def write_operations_result(self, **changes):
        payload = {
            "verdict": "SAFE_IDLE",
            "final_flat_live_verified": True,
            "symbol_position_amt": "0",
            "symbol_open_order_count": 0,
            "other_nonzero_positions": [],
            "execution_halt_active": False,
            "execution_lock_active": False,
            "real_binance_enabled": False,
            "allow_auto_testnet_order": False,
            "allow_testnet_order": False,
            "allow_manual_actual_roundtrip": False,
        }
        payload.update(changes)
        write_json(Path(gate.OPERATIONS_RESULT_PATH), payload)

    def make_session(
        self,
        number,
        day,
        duplicate=False,
        emergency=False,
        checksum_valid=True,
        completed=True,
    ):
        directory = Path("evidence") / f"operator_{number:02d}"
        start = f"{day.isoformat()}T00:00:00+00:00"
        finish = f"{day.isoformat()}T00:03:00+00:00"
        plan = {
            "actual_roundtrip_plan_id": f"plan-{number}",
            "generated_at": start,
            "completed_at": finish,
            "consumed": True,
            "completed": completed,
            "actual_roundtrip_payload": {
                "symbol": "ETHUSDT",
                "entry_side": "BUY",
                "entry_quantity": "0.013",
            },
        }
        state = {"state": "COMPLETED" if completed else "ENTRY_SENT"}
        orders = [{
            "generated_at": start,
            "mode": "actual_order",
            "symbol": "ETHUSDT",
            "side": "BUY",
            "quantity": "0.013",
            "reduce_only": False,
            "order_success": True,
            "order_test": False,
            "dry_run": False,
        }]
        if duplicate:
            orders.append({
                "generated_at": f"{day.isoformat()}T00:01:00+00:00",
                "mode": "actual_order",
                "symbol": "ETHUSDT",
                "side": "BUY",
                "quantity": "0.013",
                "reduce_only": False,
                "order_success": True,
                "order_test": False,
                "dry_run": False,
            })
        orders.append({
            "generated_at": f"{day.isoformat()}T00:02:00+00:00",
            "mode": "actual_close_position",
            "symbol": "ETHUSDT",
            "side": "SELL",
            "quantity": "0.013",
            "reduce_only": True,
            "order_success": True,
            "order_test": False,
            "dry_run": False,
            "position_before_amt": "0.013",
            "position_after_amt": "0",
            "blocked_reason": None,
        })
        events = [
            "entry intent",
            "entry result",
            "entry verification",
            "close intent",
            "close result",
            "flat verification",
            "completion",
        ]
        if emergency:
            events.append("emergency recovery")
        audit = [
            {
                "generated_at": f"{day.isoformat()}T00:01:30+00:00",
                "actual_roundtrip_plan_id": f"plan-{number}",
                "event": event,
            }
            for event in events
        ]
        write_json(directory / policy.PLAN_FILE, plan)
        write_json(directory / policy.STATE_FILE, state)
        write_jsonl(directory / policy.ORDERS_FILE, orders)
        write_jsonl(directory / policy.AUDIT_FILE, audit)
        lines = []
        for filename in policy.REQUIRED_FILES:
            digest = hashlib.sha256((directory / filename).read_bytes()).hexdigest()
            if not checksum_valid and filename == policy.PLAN_FILE:
                digest = "0" * 64
            lines.append(f"{digest}  {filename}\n")
        (directory / "SHA256SUMS").write_text("".join(lines), encoding="utf-8")

    def evaluate(self, current_limit=3):
        with contextlib.redirect_stdout(io.StringIO()):
            return gate.evaluate(configured_limit=current_limit)

    def test_one_clean_roundtrip_holds_at_three(self):
        self.make_session(1, date(2026, 6, 13))
        result = self.evaluate()
        self.assertEqual(result["verdict"], "HOLD_AT_3")
        self.assertEqual(result["valid_roundtrips"], 1)
        self.assertEqual(result["distinct_utc_days"], 1)
        self.assertFalse(result["limit_expansion_authorized"])
        self.assertFalse(result["configuration_changed"])

    def test_three_clean_days_are_eligible_for_five_review(self):
        start = date(2026, 6, 13)
        for index in range(3):
            self.make_session(index + 1, start + timedelta(days=index))
        result = self.evaluate()
        self.assertEqual(result["verdict"], "ELIGIBLE_FOR_5_REVIEW")
        self.assertEqual(result["recommended_daily_order_limit"], 5)
        self.assertTrue(result["human_review_required"])
        self.assertFalse(result["limit_expansion_authorized"])

    def test_three_roundtrips_on_one_day_do_not_qualify(self):
        day = date(2026, 6, 13)
        for index in range(3):
            self.make_session(index + 1, day)
        result = self.evaluate()
        self.assertEqual(result["verdict"], "HOLD_AT_3")
        self.assertEqual(result["distinct_utc_days"], 1)

    def test_duplicate_emergency_and_checksum_failure_freeze(self):
        cases = [
            {"duplicate": True},
            {"emergency": True},
            {"checksum_valid": False},
        ]
        for index, kwargs in enumerate(cases, 1):
            with self.subTest(kwargs=kwargs):
                import shutil
                shutil.rmtree("evidence", ignore_errors=True)
                self.make_session(index, date(2026, 6, 13), **kwargs)
                self.assertEqual(self.evaluate()["verdict"], "FREEZE_LIMIT")

    def test_unsafe_current_operations_state_freezes(self):
        self.make_session(1, date(2026, 6, 13))
        self.write_operations_result(verdict="HALTED", final_flat_live_verified=False)
        result = self.evaluate()
        self.assertEqual(result["verdict"], "FREEZE_LIMIT")
        self.assertFalse(result["current_safety_passed"])

    def test_ten_clean_roundtrips_across_seven_days_qualify_for_ten_review(self):
        start = date(2026, 6, 13)
        for index in range(10):
            self.make_session(index + 1, start + timedelta(days=index % 7))
        result = self.evaluate(current_limit=5)
        self.assertEqual(result["verdict"], "ELIGIBLE_FOR_10_REVIEW")
        self.assertEqual(result["recommended_daily_order_limit"], 10)
        self.assertEqual(result["valid_roundtrips"], 10)
        self.assertEqual(result["distinct_utc_days"], 7)

    def test_limit_ten_holds_and_unknown_limit_freezes(self):
        self.make_session(1, date(2026, 6, 13))
        self.assertEqual(self.evaluate(current_limit=10)["verdict"], "HOLD_AT_10")
        self.assertEqual(self.evaluate(current_limit=4)["verdict"], "FREEZE_LIMIT")

    def test_no_evidence_holds_and_reports_requirements(self):
        result = self.evaluate()
        self.assertEqual(result["verdict"], "HOLD_AT_3")
        self.assertEqual(result["valid_roundtrips"], 0)
        self.assertTrue(result["failed_requirements"])

    def test_preview_is_file_only_and_no_subprocess_is_used(self):
        self.make_session(1, date(2026, 6, 13))
        with mock.patch.object(gate, "evaluate", wraps=gate.evaluate), mock.patch.object(
            subprocess, "run", side_effect=AssertionError("subprocess invoked")
        ):
            with contextlib.redirect_stdout(io.StringIO()):
                result = gate.run(make_preview=True)
        self.assertEqual(result["verdict"], "HOLD_AT_3")
        self.assertTrue(Path(gate.RESULT_PATH).exists())
        self.assertTrue(Path(gate.TELEGRAM_PREVIEW_PATH).exists())
        preview = json.loads(Path(gate.TELEGRAM_PREVIEW_PATH).read_text(encoding="utf-8"))
        self.assertIn("No order sent", preview["preview"])
        self.assertIn("No limit changed", preview["preview"])

    def test_result_contains_no_full_plan_identifiers(self):
        self.make_session(1, date(2026, 6, 13))
        text = json.dumps(self.evaluate())
        self.assertNotIn("plan-1", text)
        self.assertNotIn("actual_roundtrip_plan_id", text)


if __name__ == "__main__":
    unittest.main()

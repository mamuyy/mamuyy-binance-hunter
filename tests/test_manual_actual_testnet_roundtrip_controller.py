import argparse
import copy
import fcntl
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import manual_actual_testnet_roundtrip_controller as arc
from testnet_approval_identity import canonical_bridge_signal_metadata

NOW = "2026-06-13T12:00:00+00:00"

BRIDGE = {
    "generated_at": NOW,
    "status": "WOULD_ORDER",
    "symbol": "ETHUSDT",
    "side": "BUY",
    "quantity": "0.014",
    "estimated_notional_usdt": 22.4,
    "signal_score": 95,
    "overlay_decision": "LONG / TESTNET_READY",
    "trade_rank": "HIGH_QUALITY",
    "suggested_risk": "NORMAL",
    "overlay_report_path": "tests/fixtures/roundtrip_overlay.json",
}

EXCHANGE_INFO = {
    "symbols": [
        {"symbol": "ETHUSDT", "status": "TRADING", "filters": [{"filterType": "LOT_SIZE", "minQty": "0.001", "stepSize": "0.001"}]}
    ]
}


def approval_request(side="BUY", used=False, expires_at="2026-06-13T12:10:00+00:00"):
    bridge = copy.deepcopy(BRIDGE)
    bridge["side"] = side
    payload = {
        "symbol": "ETHUSDT",
        "side": side,
        "quantity": "0.014",
        "approved_quantity": "0.014",
        "order_type": "MARKET",
        "estimated_notional_usdt": 22.4,
        "bridge_signal_metadata": canonical_bridge_signal_metadata(bridge),
    }
    return {
        "request_id": "request-actual-1",
        "generated_at": NOW,
        "expires_at": expires_at,
        "used": used,
        "payload_sha256": arc.payload_sha256(payload),
        "approval_payload": payload,
    }


def supervisor(side="BUY", daily_count=0):
    return {
        "generated_at": NOW,
        "status": "READY_FOR_MANUAL_DUMMY_ORDER",
        "read_only": True,
        "execution_permitted": False,
        "manual_execution_required": True,
        "request_integrity_passed": True,
        "bridge_payload_matches": True,
        "payload_sha256_matches": True,
        "request_expired": False,
        "request_used": False,
        "position_limit_passed": True,
        "exposure_limit_passed": True,
        "open_order_guard_passed": True,
        "duplicate_guard_passed": True,
        "notional_policy_passed": True,
        "quantity_filter_passed": True,
        "execution_halt_active": False,
        "blocked_reasons": [],
        "open_position_count": 0,
        "open_order_count": 0,
        "daily_actual_order_count": daily_count,
        "daily_order_limit": 3,
        "symbol": "ETHUSDT",
        "side": side,
        "approved_quantity": "0.014",
        "max_open_positions": 1,
        "max_total_exposure_usdt": 25.0,
        "dedupe_key": "dedupe-actual",
    }


class FakeClient:
    def __init__(self, positions=None, open_orders=None, mark="1600", can_trade=True):
        self.positions = positions if positions is not None else [{"symbol": "ETHUSDT", "positionAmt": "0"}]
        self.open_orders = open_orders if open_orders is not None else []
        self.mark = mark
        self.can_trade = can_trade

    def get_account(self):
        return {"canTrade": self.can_trade}

    def get_position_risk(self, symbol=None):
        return copy.deepcopy(self.positions)

    def get_open_orders(self, symbol=None):
        return copy.deepcopy(self.open_orders)

    def get_mark_price(self, symbol):
        return self.mark

    def get_exchange_info(self):
        return copy.deepcopy(EXCHANGE_INFO)


class ActualRoundtripControllerTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.paths = mock.patch.multiple(
            arc,
            SUPERVISOR_RESULT_PATH=str(self.root / "logs" / "testnet_execution_safety_supervisor_result.json"),
            APPROVAL_REQUEST_PATH=str(self.root / "logs" / "manual_testnet_approval_request.json"),
            BRIDGE_RESULT_PATH=str(self.root / "logs" / "semi_auto_testnet_bridge_result.json"),
            EXECUTOR_RESULT_PATH=str(self.root / "logs" / "binance_testnet_executor_result.json"),
            PLAN_PATH=str(self.root / "logs" / "manual_actual_testnet_roundtrip_plan.json"),
            RESULT_PATH=str(self.root / "logs" / "manual_actual_testnet_roundtrip_result.json"),
            STATE_PATH=str(self.root / "logs" / "manual_actual_testnet_roundtrip_state.json"),
            AUDIT_PATH=str(self.root / "logs" / "manual_actual_testnet_roundtrip_audit.jsonl"),
            LOCK_FILE_PATH=str(self.root / "runtime" / "MANUAL_ACTUAL_TESTNET_ROUNDTRIP.lock"),
            HALT_FILE_PATH=str(self.root / "runtime" / "TESTNET_EXECUTION_HALT"),
        )
        self.paths.start()
        self.env = mock.patch.dict(
            os.environ,
            {
                "MANUAL_ACTUAL_TESTNET_ROUNDTRIP_NOW": NOW,
                "BROKER_MODE": arc.BROKER_MODE_REQUIRED,
                "REAL_BINANCE_ENABLED": "false",
                "ALLOW_REAL_BINANCE_ORDER": "false",
                "ALLOW_AUTO_TESTNET_ORDER": "false",
                "ALLOW_TESTNET_ORDER": "false",
                "BINANCE_FUTURES_TESTNET_BASE_URL": arc.DEMO_FUTURES_BASE_URL,
                "TESTNET_EXECUTION_HALT": "false",
            },
            clear=False,
        )
        self.env.start()
        self.client = FakeClient()
        self.client_patch = mock.patch.object(arc, "client", return_value=self.client)
        self.client_patch.start()
        self.sleep_patch = mock.patch.object(arc.time, "sleep", return_value=None)
        self.sleep_patch.start()
        self.write_sources()

    def tearDown(self):
        self.sleep_patch.stop()
        self.client_patch.stop()
        self.env.stop()
        self.paths.stop()
        self.tmp.cleanup()

    def write_json(self, path, payload):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        Path(path).write_text(json.dumps(payload), encoding="utf-8")

    def read_result(self):
        return json.loads(Path(arc.RESULT_PATH).read_text())

    def read_plan(self):
        return json.loads(Path(arc.PLAN_PATH).read_text())

    def write_sources(self, side="BUY", daily_count=0):
        bridge = copy.deepcopy(BRIDGE)
        bridge["side"] = side
        self.write_json(arc.BRIDGE_RESULT_PATH, bridge)
        self.write_json(arc.APPROVAL_REQUEST_PATH, approval_request(side=side))
        self.write_json(arc.SUPERVISOR_RESULT_PATH, supervisor(side=side, daily_count=daily_count))

    def prepare_args(self):
        return argparse.Namespace(
            prepare=True,
            status=False,
            execute_roundtrip=False,
            recover_close=False,
            symbol="ETHUSDT",
            approve=None,
            confirm_sha256=None,
            confirm_action=None,
            supervisor_result_path=arc.SUPERVISOR_RESULT_PATH,
            approval_request_path=arc.APPROVAL_REQUEST_PATH,
            bridge_result_path=arc.BRIDGE_RESULT_PATH,
        )

    def exec_args(self, plan=None, action="OPEN_AND_CLOSE_BINANCE_FUTURES_DEMO_POSITION"):
        plan = plan or self.read_plan()
        return argparse.Namespace(
            prepare=False,
            status=False,
            execute_roundtrip=True,
            recover_close=False,
            symbol="ETHUSDT",
            approve=plan["actual_roundtrip_plan_id"],
            confirm_sha256=plan["actual_roundtrip_payload_sha256"],
            confirm_action=action,
            supervisor_result_path=arc.SUPERVISOR_RESULT_PATH,
            approval_request_path=arc.APPROVAL_REQUEST_PATH,
            bridge_result_path=arc.BRIDGE_RESULT_PATH,
        )

    def recover_args(self, plan=None):
        args = self.exec_args(plan, "REDUCE_ONLY_EMERGENCY_CLOSE")
        args.execute_roundtrip = False
        args.recover_close = True
        return args

    def prepare_plan(self):
        self.assertEqual(arc.prepare(self.prepare_args()), 0)
        return self.read_plan()

    def mock_run_sequence(self, position_after_entry="0.014", final_position="0", entry_rc=0, close_rc=0):
        calls = []

        def fake_run(command, capture_output, text, timeout, check):
            calls.append(command)
            mode = "actual_order" if "--close-position" not in command else "actual_close_position"
            success = (entry_rc == 0) if mode == "actual_order" else (close_rc == 0)
            rc = entry_rc if mode == "actual_order" else close_rc
            self.write_json(arc.EXECUTOR_RESULT_PATH, {"mode": mode, "order_success": success, "api_key": "secret"})
            if mode == "actual_order":
                self.client.positions = [{"symbol": "ETHUSDT", "positionAmt": position_after_entry}]
            else:
                self.client.positions = [{"symbol": "ETHUSDT", "positionAmt": final_position}]
            return subprocess_completed(rc)

        return calls, mock.patch.object(arc.subprocess, "run", side_effect=fake_run)

    def test_valid_plan_preparation(self):
        self.assertEqual(arc.prepare(self.prepare_args()), 0)
        plan = self.read_plan()
        self.assertTrue(plan["actual_testnet_only"])
        self.assertFalse(plan["execution_started"])
        self.assertFalse(plan["consumed"])
        self.assertEqual(plan["required_daily_order_slots"], 3)
        self.assertEqual(plan["actual_roundtrip_payload"]["base_url"], arc.DEMO_FUTURES_BASE_URL)

    def test_execution_gate_blocks(self):
        plan = self.prepare_plan()
        with mock.patch.dict(os.environ, {"ALLOW_TESTNET_ORDER": "true"}, clear=False):
            self.assertEqual(arc.execute_roundtrip(self.exec_args(plan)), 1)
        self.assertIn("ALLOW_MANUAL_ACTUAL_TESTNET_ROUNDTRIP=1 is required", self.read_result()["blocked_reasons"])

    def test_testnet_order_gate_blocks(self):
        plan = self.prepare_plan()
        with mock.patch.dict(os.environ, {"ALLOW_MANUAL_ACTUAL_TESTNET_ROUNDTRIP": "1"}, clear=False):
            self.assertEqual(arc.execute_roundtrip(self.exec_args(plan)), 1)
        self.assertIn("ALLOW_TESTNET_ORDER=true is required", self.read_result()["blocked_reasons"])

    def test_auto_real_and_production_url_block(self):
        cases = [
            {"ALLOW_AUTO_TESTNET_ORDER": "true"},
            {"REAL_BINANCE_ENABLED": "true"},
            {"BINANCE_FUTURES_TESTNET_BASE_URL": "https://fapi.binance.com"},
        ]
        for updates in cases:
            with self.subTest(updates=updates):
                self.write_sources()
                with mock.patch.dict(os.environ, updates, clear=False):
                    self.assertEqual(arc.prepare(self.prepare_args()), 1)
                    self.assertTrue(self.read_result()["blocked_reasons"])

    def test_halt_active_before_entry_blocks(self):
        plan = self.prepare_plan()
        Path(arc.HALT_FILE_PATH).parent.mkdir(parents=True, exist_ok=True)
        Path(arc.HALT_FILE_PATH).write_text("halt", encoding="utf-8")
        with mock.patch.dict(os.environ, {"ALLOW_MANUAL_ACTUAL_TESTNET_ROUNDTRIP": "1", "ALLOW_TESTNET_ORDER": "true"}, clear=False):
            self.assertEqual(arc.execute_roundtrip(self.exec_args(plan)), 1)
        self.assertTrue(any("execution halt active before entry" in reason for reason in self.read_result()["blocked_reasons"]))

    def test_existing_position_blocks_entry(self):
        plan = self.prepare_plan()
        self.client.positions = [{"symbol": "ETHUSDT", "positionAmt": "0.014"}]
        with mock.patch.dict(os.environ, {"ALLOW_MANUAL_ACTUAL_TESTNET_ROUNDTRIP": "1", "ALLOW_TESTNET_ORDER": "true"}, clear=False):
            self.assertEqual(arc.execute_roundtrip(self.exec_args(plan)), 1)
        self.assertTrue(any("symbol position must be exactly zero" in reason for reason in self.read_result()["blocked_reasons"]))

    def test_existing_open_order_blocks_entry(self):
        plan = self.prepare_plan()
        self.client.open_orders = [{"symbol": "ETHUSDT", "orderId": 1}]
        with mock.patch.dict(os.environ, {"ALLOW_MANUAL_ACTUAL_TESTNET_ROUNDTRIP": "1", "ALLOW_TESTNET_ORDER": "true"}, clear=False):
            self.assertEqual(arc.execute_roundtrip(self.exec_args(plan)), 1)
        self.assertTrue(any("symbol open orders must be zero" in reason for reason in self.read_result()["blocked_reasons"]))

    def test_insufficient_daily_capacity_blocks_prepare(self):
        self.write_sources(daily_count=1)
        self.assertEqual(arc.prepare(self.prepare_args()), 1)
        self.assertTrue(any("remaining_daily_order_slots must be at least 3" in reason for reason in self.read_result()["blocked_reasons"]))

    def test_wrong_id_sha_expired_and_consumed_block(self):
        plan = self.prepare_plan()
        with mock.patch.dict(os.environ, {"ALLOW_MANUAL_ACTUAL_TESTNET_ROUNDTRIP": "1", "ALLOW_TESTNET_ORDER": "true"}, clear=False):
            args = self.exec_args(plan)
            args.approve = "wrong"
            self.assertEqual(arc.execute_roundtrip(args), 1)
            args = self.exec_args(plan)
            args.confirm_sha256 = "bad"
            self.assertEqual(arc.execute_roundtrip(args), 1)
            plan["expires_at"] = "2026-06-13T11:59:00+00:00"
            self.write_json(arc.PLAN_PATH, plan)
            self.assertEqual(arc.execute_roundtrip(self.exec_args(plan)), 1)
            plan["expires_at"] = "2026-06-13T12:10:00+00:00"
            plan["consumed"] = True
            self.write_json(arc.PLAN_PATH, plan)
            self.assertEqual(arc.execute_roundtrip(self.exec_args(plan)), 1)

    def test_concurrent_lock_blocks(self):
        plan = self.prepare_plan()
        Path(arc.LOCK_FILE_PATH).parent.mkdir(parents=True, exist_ok=True)
        fh = open(arc.LOCK_FILE_PATH, "a+", encoding="utf-8")
        try:
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            with mock.patch.dict(os.environ, {"ALLOW_MANUAL_ACTUAL_TESTNET_ROUNDTRIP": "1", "ALLOW_TESTNET_ORDER": "true"}, clear=False):
                self.assertEqual(arc.execute_roundtrip(self.exec_args(plan)), 1)
            self.assertIn("another actual roundtrip controller is running", self.read_result()["blocked_reasons"])
        finally:
            fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
            fh.close()

    def test_successful_entry_and_primary_reduce_only_close(self):
        plan = self.prepare_plan()
        calls, patcher = self.mock_run_sequence()
        with patcher, mock.patch.dict(os.environ, {"ALLOW_MANUAL_ACTUAL_TESTNET_ROUNDTRIP": "1", "ALLOW_TESTNET_ORDER": "true"}, clear=False):
            self.assertEqual(arc.execute_roundtrip(self.exec_args(plan)), 0)
        result = self.read_result()
        self.assertTrue(result["final_flat_verified"])
        self.assertEqual(result["entry_attempt_count"], 1)
        self.assertEqual(result["primary_close_attempt_count"], 1)
        self.assertEqual(result["emergency_close_attempt_count"], 0)
        self.assertIn("--close-position", calls[1])
        self.assertNotIn("--order-test", " ".join(" ".join(c) for c in calls))

    def test_entry_executor_failure_zero_ends_entry_failed(self):
        plan = self.prepare_plan()
        calls, patcher = self.mock_run_sequence(position_after_entry="0", entry_rc=1)
        with patcher, mock.patch.dict(os.environ, {"ALLOW_MANUAL_ACTUAL_TESTNET_ROUNDTRIP": "1", "ALLOW_TESTNET_ORDER": "true"}, clear=False):
            self.assertEqual(arc.execute_roundtrip(self.exec_args(plan)), 1)
        self.assertEqual(self.read_result()["state"], arc.ENTRY_FAILED)
        self.assertEqual(len(calls), 1)

    def test_ambiguous_entry_nonzero_closes_never_retries_entry(self):
        plan = self.prepare_plan()
        calls, patcher = self.mock_run_sequence(position_after_entry="0.013", entry_rc=1)
        with patcher, mock.patch.dict(os.environ, {"ALLOW_MANUAL_ACTUAL_TESTNET_ROUNDTRIP": "1", "ALLOW_TESTNET_ORDER": "true"}, clear=False):
            self.assertEqual(arc.execute_roundtrip(self.exec_args(plan)), 0)
        self.assertEqual(len([c for c in calls if "--close-position" not in c]), 1)
        self.assertEqual(self.read_result()["live_position_after_entry"], "0.013")

    def test_entry_verification_failure_never_retries_entry(self):
        plan = self.prepare_plan()
        calls, patcher = self.mock_run_sequence(position_after_entry="0", entry_rc=0)
        with patcher, mock.patch.dict(os.environ, {"ALLOW_MANUAL_ACTUAL_TESTNET_ROUNDTRIP": "1", "ALLOW_TESTNET_ORDER": "true"}, clear=False):
            self.assertEqual(arc.execute_roundtrip(self.exec_args(plan)), 1)
        self.assertEqual(len(calls), 1)
        self.assertEqual(self.read_result()["state"], arc.ENTRY_FAILED)

    def test_primary_close_failure_triggers_one_emergency_success_and_halt(self):
        plan = self.prepare_plan()
        calls = []

        def fake_run(command, **kwargs):
            calls.append(command)
            is_close = "--close-position" in command
            if not is_close:
                self.write_json(arc.EXECUTOR_RESULT_PATH, {"order_success": True})
                self.client.positions = [{"symbol": "ETHUSDT", "positionAmt": "0.014"}]
                return subprocess_completed(0)
            self.write_json(arc.EXECUTOR_RESULT_PATH, {"order_success": len(calls) == 3})
            self.client.positions = [{"symbol": "ETHUSDT", "positionAmt": "0" if len(calls) == 3 else "0.014"}]
            return subprocess_completed(0 if len(calls) == 3 else 1)

        with mock.patch.object(arc.subprocess, "run", side_effect=fake_run), mock.patch.dict(os.environ, {"ALLOW_MANUAL_ACTUAL_TESTNET_ROUNDTRIP": "1", "ALLOW_TESTNET_ORDER": "true"}, clear=False):
            self.assertEqual(arc.execute_roundtrip(self.exec_args(plan)), 0)
        result = self.read_result()
        self.assertEqual(result["emergency_close_attempt_count"], 1)
        self.assertTrue(Path(arc.HALT_FILE_PATH).exists())

    def test_emergency_close_failure_manual_action_required(self):
        plan = self.prepare_plan()
        calls = []

        def fake_run(command, **kwargs):
            calls.append(command)
            self.write_json(arc.EXECUTOR_RESULT_PATH, {"order_success": "--close-position" not in command})
            self.client.positions = [{"symbol": "ETHUSDT", "positionAmt": "0.014"}]
            return subprocess_completed(0 if "--close-position" not in command else 1)

        with mock.patch.object(arc.subprocess, "run", side_effect=fake_run), mock.patch.dict(os.environ, {"ALLOW_MANUAL_ACTUAL_TESTNET_ROUNDTRIP": "1", "ALLOW_TESTNET_ORDER": "true"}, clear=False):
            self.assertEqual(arc.execute_roundtrip(self.exec_args(plan)), 1)
        result = self.read_result()
        self.assertEqual(result["state"], arc.EMERGENCY_MANUAL_ACTION_REQUIRED)
        self.assertEqual(result["emergency_close_attempt_count"], 1)

    def test_flat_after_ambiguous_primary_close_skips_emergency(self):
        plan = self.prepare_plan()
        calls, patcher = self.mock_run_sequence(final_position="0", close_rc=1)
        with patcher, mock.patch.dict(os.environ, {"ALLOW_MANUAL_ACTUAL_TESTNET_ROUNDTRIP": "1", "ALLOW_TESTNET_ORDER": "true"}, clear=False):
            self.assertEqual(arc.execute_roundtrip(self.exec_args(plan)), 0)
        self.assertEqual(self.read_result()["emergency_close_attempt_count"], 0)

    def test_crash_after_entry_intent_prevents_entry_replay(self):
        plan = self.prepare_plan()
        plan.update({"execution_started": True, "consumed": True})
        self.write_json(arc.PLAN_PATH, plan)
        with mock.patch.object(arc.subprocess, "run") as run, mock.patch.dict(os.environ, {"ALLOW_MANUAL_ACTUAL_TESTNET_ROUNDTRIP": "1", "ALLOW_TESTNET_ORDER": "true"}, clear=False):
            self.assertEqual(arc.execute_roundtrip(self.exec_args(plan)), 1)
        run.assert_not_called()

    def test_recovery_mode_never_submits_entry_and_uses_live_position(self):
        plan = self.prepare_plan()
        plan.update({"execution_started": True, "consumed": True})
        self.write_json(arc.PLAN_PATH, plan)
        self.client.positions = [{"symbol": "ETHUSDT", "positionAmt": "0.013"}]
        calls, patcher = self.mock_run_sequence(final_position="0", close_rc=0)
        with patcher, mock.patch.dict(os.environ, {"ALLOW_MANUAL_TESTNET_EMERGENCY_CLOSE": "1", "ALLOW_TESTNET_ORDER": "true"}, clear=False):
            self.assertEqual(arc.recover_close(self.recover_args(plan)), 0)
        self.assertEqual(len(calls), 1)
        self.assertIn("--close-position", calls[0])
        self.assertEqual(self.read_plan().get("last_live_close_quantity"), "0.013")

    def test_buy_and_sell_close_sides_from_live_position(self):
        self.assertEqual(arc.close_side_for_position("0.014"), "SELL")
        self.assertEqual(arc.close_side_for_position("-0.014"), "BUY")

    def test_static_safety_no_cron_unbounded_loop_or_production_support_and_redaction(self):
        source = Path("manual_actual_testnet_roundtrip_controller.py").read_text()
        self.assertNotIn("/fapi/v1/order", source)
        self.assertNotIn("/fapi/v1/order/test", source)
        self.assertNotIn("while True", source)
        self.assertNotIn("cron", source.lower())
        self.assertIn(arc.DEMO_FUTURES_BASE_URL, source)
        self.assertEqual(arc.redact({"api_key": "abc", "nested": {"telegram_token": "def"}})["api_key"], "REDACTED")


def subprocess_completed(returncode):
    return argparse.Namespace(returncode=returncode, stdout="ok", stderr="")


if __name__ == "__main__":
    unittest.main()

import argparse
import copy
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import manual_testnet_roundtrip_controller as rtc
from testnet_approval_identity import canonical_bridge_signal_metadata

NOW = "2026-06-13T12:00:00+00:00"

BRIDGE = {
    "generated_at": NOW,
    "status": "WOULD_ORDER",
    "safety_passed": True,
    "policy_passed": True,
    "order_attempted": False,
    "order_success": False,
    "dry_run": True,
    "send_requested": False,
    "real_binance_enabled": False,
    "allow_auto_testnet_order": False,
    "allow_testnet_order": False,
    "symbol": "ETHUSDT",
    "direction": "LONG",
    "side": "BUY",
    "quantity": "0.014",
    "estimated_notional_usdt": 22.4,
    "signal_score": 95,
    "overlay_decision": "LONG / TESTNET_READY",
    "trade_rank": "HIGH_QUALITY",
    "suggested_risk": "NORMAL",
    "overlay_report_path": "tests/fixtures/roundtrip_overlay.json",
    "broker_mode": rtc.BROKER_MODE_REQUIRED,
    "daily_actual_order_count": 0,
    "daily_order_limit": 3,
}


def approval_request(side="BUY", quantity="0.014", expires_at="2026-06-13T12:10:00+00:00", used=False):
    bridge = copy.deepcopy(BRIDGE)
    bridge["side"] = side
    bridge["quantity"] = quantity
    payload = {
        "symbol": "ETHUSDT",
        "side": side,
        "quantity": quantity,
        "approved_quantity": quantity,
        "order_type": "MARKET",
        "estimated_notional_usdt": 22.4,
        "bridge_signal_metadata": canonical_bridge_signal_metadata(bridge),
    }
    return {
        "request_id": "request-1234567890",
        "generated_at": NOW,
        "expires_at": expires_at,
        "used": used,
        "payload_sha256": rtc.payload_sha256(payload),
        "approval_payload": payload,
        "proposed_order_test_payload": {
            "symbol": "ETHUSDT",
            "side": side,
            "quantity": quantity,
            "order_type": "MARKET",
            "order_test": True,
            "send": True,
            "base_url": rtc.DEMO_FUTURES_BASE_URL,
        },
    }


def supervisor(side="BUY", status="READY_FOR_MANUAL_DUMMY_ORDER", blocked_reasons=None, daily_count=0, halt=False, gate=False):
    return {
        "generated_at": NOW,
        "mode": "preflight",
        "status": status,
        "read_only": True,
        "execution_permitted": False,
        "manual_execution_required": True,
        "order_attempted": False,
        "order_success": False,
        "base_url": rtc.DEMO_FUTURES_BASE_URL,
        "broker_mode": rtc.BROKER_MODE_REQUIRED,
        "real_binance_enabled": False,
        "allow_real_binance_order": False,
        "allow_auto_testnet_order": False,
        "allow_testnet_order": gate,
        "allow_manual_testnet_approval": False,
        "execution_halt_active": halt,
        "open_position_count": 0,
        "max_open_positions": 1,
        "position_limit_passed": True,
        "current_total_exposure_usdt": 0.0,
        "proposed_notional_usdt": 22.4,
        "live_proposed_notional_usdt": 22.4,
        "projected_total_exposure_usdt": 22.4,
        "max_total_exposure_usdt": 25.0,
        "exposure_limit_passed": True,
        "open_order_count": 0,
        "symbol_open_order_count": 0,
        "open_order_guard_passed": True,
        "daily_actual_order_count": daily_count,
        "daily_order_limit": 3,
        "daily_limit_passed": True,
        "request_expired": False,
        "request_used": False,
        "request_integrity_passed": True,
        "payload_sha256_matches": True,
        "bridge_payload_matches": True,
        "symbol": "ETHUSDT",
        "side": side,
        "approved_quantity": "0.014",
        "min_notional_usdt": 20.0,
        "max_notional_usdt": 25.0,
        "notional_policy_passed": True,
        "quantity_filter_passed": True,
        "dedupe_key": "dedupe-abc",
        "duplicate_guard_passed": True,
        "blocked_reasons": [] if blocked_reasons is None else blocked_reasons,
    }


class RoundtripControllerTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.paths = mock.patch.multiple(
            rtc,
            SUPERVISOR_RESULT_PATH=str(self.root / "logs" / "testnet_execution_safety_supervisor_result.json"),
            APPROVAL_REQUEST_PATH=str(self.root / "logs" / "manual_testnet_approval_request.json"),
            BRIDGE_RESULT_PATH=str(self.root / "logs" / "semi_auto_testnet_bridge_result.json"),
            PLAN_PATH=str(self.root / "logs" / "manual_testnet_roundtrip_plan.json"),
            RESULT_PATH=str(self.root / "logs" / "manual_testnet_roundtrip_result.json"),
            STATE_PATH=str(self.root / "logs" / "manual_testnet_roundtrip_state.json"),
            AUDIT_PATH=str(self.root / "logs" / "manual_testnet_roundtrip_audit.jsonl"),
            TELEGRAM_PREVIEW_PATH=str(self.root / "logs" / "manual_testnet_roundtrip_telegram_preview.json"),
            HALT_FILE_PATH=str(self.root / "runtime" / "TESTNET_EXECUTION_HALT"),
        )
        self.paths.start()
        self.env = mock.patch.dict(
            os.environ,
            {
                "MANUAL_TESTNET_ROUNDTRIP_NOW": NOW,
                "BROKER_MODE": rtc.BROKER_MODE_REQUIRED,
                "REAL_BINANCE_ENABLED": "false",
                "ALLOW_REAL_BINANCE_ORDER": "false",
                "ALLOW_AUTO_TESTNET_ORDER": "false",
                "ALLOW_TESTNET_ORDER": "false",
                "ALLOW_MANUAL_TESTNET_APPROVAL": "false",
                "BINANCE_FUTURES_TESTNET_BASE_URL": rtc.DEMO_FUTURES_BASE_URL,
                "TESTNET_EXECUTION_HALT": "false",
            },
            clear=False,
        )
        self.env.start()
        self.write_sources()
        self.original_approval_text = Path(rtc.APPROVAL_REQUEST_PATH).read_text()

    def tearDown(self):
        self.env.stop()
        self.paths.stop()
        self.tmp.cleanup()

    def write_json(self, path, payload):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        Path(path).write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")

    def write_sources(self, sup=None, req=None, bridge=None):
        self.write_json(rtc.SUPERVISOR_RESULT_PATH, supervisor() if sup is None else sup)
        self.write_json(rtc.APPROVAL_REQUEST_PATH, approval_request() if req is None else req)
        self.write_json(rtc.BRIDGE_RESULT_PATH, copy.deepcopy(BRIDGE) if bridge is None else bridge)

    def prepare_args(self):
        return argparse.Namespace(
            symbol="ETHUSDT",
            supervisor_result_path=rtc.SUPERVISOR_RESULT_PATH,
            approval_request_path=rtc.APPROVAL_REQUEST_PATH,
            bridge_result_path=rtc.BRIDGE_RESULT_PATH,
        )

    def sim_args(self, plan=None, sha=None, **flags):
        plan = plan or json.loads(Path(rtc.PLAN_PATH).read_text())
        defaults = {
            "approve": plan["roundtrip_plan_id"],
            "confirm_sha256": sha or plan["roundtrip_payload_sha256"],
            "supervisor_result_path": rtc.SUPERVISOR_RESULT_PATH,
            "approval_request_path": rtc.APPROVAL_REQUEST_PATH,
            "bridge_result_path": rtc.BRIDGE_RESULT_PATH,
            "mock_entry_failure": False,
            "mock_position_verification_failure": False,
            "mock_close_failure": False,
            "mock_final_flat_failure": False,
            "telegram_preview": False,
        }
        defaults.update(flags)
        return argparse.Namespace(**defaults)

    def read_result(self):
        return json.loads(Path(rtc.RESULT_PATH).read_text())

    def prepare_ok(self):
        self.assertEqual(rtc.prepare(self.prepare_args()), 0)
        return json.loads(Path(rtc.PLAN_PATH).read_text())

    def assert_safety_invariants(self, result):
        self.assertTrue(result["simulation_only"])
        self.assertFalse(result["actual_execution_enabled"])
        self.assertFalse(result["order_attempted"])
        self.assertFalse(result["order_success"])
        self.assertEqual(result["actual_order_count_increment"], 0)
        self.assertNotIn("binance", json.dumps(result).lower().replace("no binance order sent", ""))
        self.assertEqual(Path(rtc.APPROVAL_REQUEST_PATH).read_text(), self.original_approval_text)

    def test_valid_prepare(self):
        plan = self.prepare_ok()
        result = self.read_result()
        self.assertEqual(result["state"], "PREPARED")
        self.assertTrue(plan["simulation_only"])
        self.assertFalse(plan["actual_execution_enabled"])
        self.assert_safety_invariants(result)

    def test_valid_complete_simulation(self):
        plan = self.prepare_ok()
        with mock.patch.dict(os.environ, {"ALLOW_MANUAL_TESTNET_ROUNDTRIP_SIMULATION": "1"}, clear=False):
            self.assertEqual(rtc.simulate(self.sim_args(plan)), 0)
        result = self.read_result()
        used_plan = json.loads(Path(rtc.PLAN_PATH).read_text())
        self.assertEqual(result["state"], "COMPLETED")
        self.assertTrue(result["final_flat_verified"])
        self.assertTrue(result["close_reduce_only_verified"])
        self.assertTrue(used_plan["used"])
        self.assert_safety_invariants(result)

    def test_wrong_plan_id_blocked(self):
        plan = self.prepare_ok()
        with mock.patch.dict(os.environ, {"ALLOW_MANUAL_TESTNET_ROUNDTRIP_SIMULATION": "1"}, clear=False):
            self.assertEqual(rtc.simulate(self.sim_args(plan, approve="wrong-id")), 1)
        result = self.read_result()
        self.assertEqual(result["state"], "BLOCKED")
        self.assertFalse(result["plan_id_matches"])
        self.assert_safety_invariants(result)

    def test_wrong_sha256_blocked(self):
        plan = self.prepare_ok()
        with mock.patch.dict(os.environ, {"ALLOW_MANUAL_TESTNET_ROUNDTRIP_SIMULATION": "1"}, clear=False):
            self.assertEqual(rtc.simulate(self.sim_args(plan, sha="0" * 64)), 1)
        result = self.read_result()
        self.assertEqual(result["state"], "BLOCKED")
        self.assertFalse(result["payload_sha256_matches"])
        self.assert_safety_invariants(result)

    def test_expired_plan_blocked(self):
        plan = self.prepare_ok()
        plan["expires_at"] = "2026-06-13T11:59:00+00:00"
        self.write_json(rtc.PLAN_PATH, plan)
        with mock.patch.dict(os.environ, {"ALLOW_MANUAL_TESTNET_ROUNDTRIP_SIMULATION": "1"}, clear=False):
            self.assertEqual(rtc.simulate(self.sim_args(plan)), 1)
        result = self.read_result()
        self.assertEqual(result["state"], "BLOCKED")
        self.assertTrue(result["plan_expired"])
        self.assert_safety_invariants(result)

    def test_used_plan_replay_blocked(self):
        plan = self.prepare_ok()
        with mock.patch.dict(os.environ, {"ALLOW_MANUAL_TESTNET_ROUNDTRIP_SIMULATION": "1"}, clear=False):
            self.assertEqual(rtc.simulate(self.sim_args(plan)), 0)
            self.assertEqual(rtc.simulate(self.sim_args(plan)), 1)
        result = self.read_result()
        self.assertEqual(result["state"], "BLOCKED")
        self.assertIn("roundtrip plan already used", result["blocked_reasons"])
        self.assert_safety_invariants(result)

    def test_supervisor_not_ready_blocked(self):
        self.write_sources(sup=supervisor(status="BLOCKED"))
        self.assertEqual(rtc.prepare(self.prepare_args()), 1)
        result = self.read_result()
        self.assertEqual(result["state"], "BLOCKED")
        self.assert_safety_invariants(result)

    def test_supervisor_blocked_reasons_present_blocked(self):
        self.write_sources(sup=supervisor(blocked_reasons=["x"]))
        self.assertEqual(rtc.prepare(self.prepare_args()), 1)
        result = self.read_result()
        self.assertEqual(result["state"], "BLOCKED")
        self.assert_safety_invariants(result)

    def test_source_approval_expired_blocked(self):
        self.write_sources(req=approval_request(expires_at="2026-06-13T11:59:00+00:00"))
        self.original_approval_text = Path(rtc.APPROVAL_REQUEST_PATH).read_text()
        self.assertEqual(rtc.prepare(self.prepare_args()), 1)
        result = self.read_result()
        self.assertEqual(result["state"], "BLOCKED")
        self.assert_safety_invariants(result)

    def test_source_approval_used_blocked(self):
        self.write_sources(req=approval_request(used=True))
        self.original_approval_text = Path(rtc.APPROVAL_REQUEST_PATH).read_text()
        self.assertEqual(rtc.prepare(self.prepare_args()), 1)
        result = self.read_result()
        self.assertEqual(result["state"], "BLOCKED")
        self.assert_safety_invariants(result)

    def test_halt_active_blocked(self):
        self.write_sources(sup=supervisor(halt=True))
        self.assertEqual(rtc.prepare(self.prepare_args()), 1)
        result = self.read_result()
        self.assertEqual(result["state"], "BLOCKED")
        self.assert_safety_invariants(result)

    def test_execution_gate_active_blocked(self):
        self.write_sources(sup=supervisor(gate=True))
        self.assertEqual(rtc.prepare(self.prepare_args()), 1)
        result = self.read_result()
        self.assertEqual(result["state"], "BLOCKED")
        self.assert_safety_invariants(result)

    def test_fewer_than_two_daily_slots_blocked(self):
        self.write_sources(sup=supervisor(daily_count=2))
        self.assertEqual(rtc.prepare(self.prepare_args()), 1)
        result = self.read_result()
        self.assertEqual(result["state"], "BLOCKED")
        self.assertFalse(result["roundtrip_daily_capacity_passed"])
        self.assert_safety_invariants(result)

    def test_mock_entry_failure(self):
        plan = self.prepare_ok()
        with mock.patch.dict(os.environ, {"ALLOW_MANUAL_TESTNET_ROUNDTRIP_SIMULATION": "1"}, clear=False):
            self.assertEqual(rtc.simulate(self.sim_args(plan, mock_entry_failure=True)), 1)
        result = self.read_result()
        self.assertEqual(result["state"], "FAILED_ENTRY")
        self.assertEqual(result["simulated_position_after_entry"], "0")
        self.assert_safety_invariants(result)

    def test_mock_position_verification_failure(self):
        plan = self.prepare_ok()
        with mock.patch.dict(os.environ, {"ALLOW_MANUAL_TESTNET_ROUNDTRIP_SIMULATION": "1"}, clear=False):
            self.assertEqual(rtc.simulate(self.sim_args(plan, mock_position_verification_failure=True)), 1)
        result = self.read_result()
        self.assertEqual(result["state"], "FAILED_POSITION_VERIFICATION")
        self.assertTrue(result["halt_required"])
        self.assert_safety_invariants(result)

    def test_mock_close_failure(self):
        plan = self.prepare_ok()
        with mock.patch.dict(os.environ, {"ALLOW_MANUAL_TESTNET_ROUNDTRIP_SIMULATION": "1"}, clear=False):
            self.assertEqual(rtc.simulate(self.sim_args(plan, mock_close_failure=True)), 1)
        result = self.read_result()
        self.assertEqual(result["state"], "FAILED_CLOSE")
        self.assertEqual(result["simulated_position_after_close"], "0.014")
        self.assertTrue(result["emergency_close_required"])
        self.assert_safety_invariants(result)

    def test_mock_final_flat_failure(self):
        plan = self.prepare_ok()
        with mock.patch.dict(os.environ, {"ALLOW_MANUAL_TESTNET_ROUNDTRIP_SIMULATION": "1"}, clear=False):
            self.assertEqual(rtc.simulate(self.sim_args(plan, mock_final_flat_failure=True)), 1)
        result = self.read_result()
        self.assertEqual(result["state"], "FAILED_FINAL_FLAT_VERIFICATION")
        self.assertTrue(result["halt_required"])
        self.assert_safety_invariants(result)

    def test_buy_roundtrip_close_sell_reduce_only(self):
        plan = self.prepare_ok()
        payload = plan["roundtrip_payload"]
        self.assertEqual(payload["entry_side"], "BUY")
        self.assertEqual(payload["close_side"], "SELL")
        self.assertTrue(payload["close_reduce_only"])
        self.assert_safety_invariants(self.read_result())

    def test_sell_roundtrip_close_buy_reduce_only(self):
        sup = supervisor(side="SELL")
        bridge = copy.deepcopy(BRIDGE)
        bridge["side"] = "SELL"
        self.write_sources(sup=sup, req=approval_request(side="SELL"), bridge=bridge)
        self.original_approval_text = Path(rtc.APPROVAL_REQUEST_PATH).read_text()
        plan = self.prepare_ok()
        payload = plan["roundtrip_payload"]
        self.assertEqual(payload["entry_side"], "SELL")
        self.assertEqual(payload["close_side"], "BUY")
        self.assertTrue(payload["close_reduce_only"])
        self.assert_safety_invariants(self.read_result())

    def test_source_approval_request_remains_unchanged_after_failed_and_successful_simulations(self):
        plan = self.prepare_ok()
        with mock.patch.dict(os.environ, {"ALLOW_MANUAL_TESTNET_ROUNDTRIP_SIMULATION": "1"}, clear=False):
            self.assertEqual(rtc.simulate(self.sim_args(plan, mock_close_failure=True)), 1)
        self.assertEqual(Path(rtc.APPROVAL_REQUEST_PATH).read_text(), self.original_approval_text)
        plan = self.prepare_ok()
        with mock.patch.dict(os.environ, {"ALLOW_MANUAL_TESTNET_ROUNDTRIP_SIMULATION": "1"}, clear=False):
            self.assertEqual(rtc.simulate(self.sim_args(plan)), 0)
        self.assertEqual(Path(rtc.APPROVAL_REQUEST_PATH).read_text(), self.original_approval_text)
        self.assert_safety_invariants(self.read_result())


if __name__ == "__main__":
    unittest.main()

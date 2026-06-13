import argparse
import copy
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import manual_testnet_approval_gate as gate


BASE_BRIDGE = {
    "status": "WOULD_ORDER",
    "safety_passed": True,
    "policy_passed": True,
    "order_attempted": False,
    "order_success": False,
    "dry_run": True,
    "send_requested": False,
    "real_binance_enabled": False,
    "allow_auto_testnet_order": False,
    "symbol": "ETHUSDT",
    "side": "BUY",
    "quantity": "0.009",
    "estimated_notional_usdt": 21.6,
    "signal_score": 95,
    "overlay_decision": "LONG / TESTNET_READY",
    "trade_rank": "HIGH_QUALITY",
    "suggested_risk": "NORMAL",
    "overlay_report_path": "tests/fixtures/manual_approval_pass_ethusdt_long.json",
}


class ManualApprovalGateTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.env = mock.patch.dict(
            os.environ,
            {
                "BROKER_MODE": "BINANCE_FUTURES_TESTNET_ONLY",
                "REAL_BINANCE_ENABLED": "false",
                "ALLOW_REAL_BINANCE_ORDER": "false",
                "ALLOW_AUTO_TESTNET_ORDER": "false",
                "ALLOW_TESTNET_ORDER": "true",
                "ALLOW_MANUAL_TESTNET_APPROVAL": "1",
                "TESTNET_MIN_NOTIONAL_USDT": "20",
                "TESTNET_MAX_NOTIONAL_USDT": "25",
                "BINANCE_FUTURES_TESTNET_BASE_URL": gate.DEMO_FUTURES_BASE_URL,
                "TESTNET_EXECUTION_HALT": "false",
            },
            clear=False,
        )
        self.env.start()
        self.paths = mock.patch.multiple(
            gate,
            BRIDGE_RESULT_PATH=str(self.root / "logs" / "semi_auto_testnet_bridge_result.json"),
            REQUEST_PATH=str(self.root / "logs" / "manual_testnet_approval_request.json"),
            RESULT_PATH=str(self.root / "logs" / "manual_testnet_approval_result.json"),
            AUDIT_PATH=str(self.root / "logs" / "manual_testnet_approval_audit.jsonl"),
            STATE_PATH=str(self.root / "logs" / "manual_testnet_approval_state.json"),
            HALT_FILE_PATH=str(self.root / "runtime" / "TESTNET_EXECUTION_HALT"),
        )
        self.paths.start()

    def tearDown(self):
        self.paths.stop()
        self.env.stop()
        self.tmp.cleanup()

    def write_bridge(self, **updates):
        payload = copy.deepcopy(BASE_BRIDGE)
        payload.update(updates)
        Path(gate.BRIDGE_RESULT_PATH).parent.mkdir(parents=True, exist_ok=True)
        Path(gate.BRIDGE_RESULT_PATH).write_text(json.dumps(payload))
        return payload

    def read_result(self):
        return json.loads(Path(gate.RESULT_PATH).read_text())

    def approve_args(self, request, sha=None):
        return argparse.Namespace(
            approve=request["request_id"],
            confirm_sha256=sha or request["payload_sha256"],
            order_test=True,
            send=True,
        )

    def test_prepare_blocks_below_minimum_before_executor_is_relevant(self):
        self.write_bridge(quantity="0.002", estimated_notional_usdt=5.0)

        rc = gate.prepare()
        result = self.read_result()

        self.assertEqual(rc, 1)
        self.assertEqual(result["status"], "BLOCKED")
        self.assertFalse(result["order_attempted"])
        self.assertFalse(Path(gate.REQUEST_PATH).exists())
        self.assertFalse(result["minimum_notional_passed"])
        self.assertIn(gate.MIN_NOTIONAL_BLOCKED_REASON, result["blocked_reason"])

    def test_prepare_positive_fixture_generates_approvable_request(self):
        self.write_bridge()

        rc = gate.prepare()
        result = self.read_result()
        request = json.loads(Path(gate.REQUEST_PATH).read_text())

        self.assertEqual(rc, 0)
        self.assertEqual(result["status"], "PREPARED")
        self.assertTrue(result["minimum_notional_passed"])
        self.assertTrue(result["maximum_notional_passed"])
        self.assertTrue(result["notional_policy_passed"])
        self.assertGreaterEqual(result["estimated_notional_usdt"], result["min_notional_usdt"])
        self.assertLessEqual(result["estimated_notional_usdt"], result["max_notional_usdt"])
        self.assertEqual(request["approval_payload"]["quantity"], "0.009")

    def test_valid_approval_marks_used_after_mocked_order_test_success(self):
        self.write_bridge()
        self.assertEqual(gate.prepare(), 0)
        request = json.loads(Path(gate.REQUEST_PATH).read_text())
        captured = {}

        def fake_executor(payload):
            command = [
                "python3",
                "binance_testnet_executor.py",
                "--symbol",
                payload["symbol"],
                "--side",
                payload["side"],
                "--quantity",
                payload["quantity"],
                "--order-type",
                "MARKET",
                "--order-test",
                "--send",
            ]
            captured["command"] = command
            return 0, {
                "command": command,
                "order_test": True,
                "order_attempted": True,
                "order_success": True,
                "position_opened": False,
                "actual_order_enabled": False,
            }

        with mock.patch.object(gate, "run_executor", side_effect=fake_executor):
            rc = gate.approve(self.approve_args(request))

        self.assertEqual(rc, 0)
        self.assertIn("--order-test", captured["command"])
        self.assertIn("--send", captured["command"])
        self.assertNotIn("/fapi/v1/order", " ".join(captured["command"]))
        used_request = json.loads(Path(gate.REQUEST_PATH).read_text())
        self.assertTrue(used_request["used"])
        result = self.read_result()
        self.assertTrue(result["request_used"])
        self.assertFalse(result["position_opened"])
        self.assertFalse(result["actual_order_enabled"])

    def test_executor_failure_does_not_mark_request_used(self):
        self.write_bridge()
        self.assertEqual(gate.prepare(), 0)
        request = json.loads(Path(gate.REQUEST_PATH).read_text())

        with mock.patch.object(
            gate,
            "run_executor",
            return_value=(1, {"order_test": True, "order_attempted": True, "order_success": False}),
        ):
            rc = gate.approve(self.approve_args(request))

        self.assertEqual(rc, 1)
        failed_request = json.loads(Path(gate.REQUEST_PATH).read_text())
        result = self.read_result()
        self.assertFalse(failed_request["used"])
        self.assertFalse(result["request_used"])
        self.assertFalse(result["position_opened"])
        self.assertFalse(result["actual_order_enabled"])

    def test_wrong_sha_expired_halt_replay_and_missing_gates_fail_closed(self):
        self.write_bridge()
        self.assertEqual(gate.prepare(), 0)
        request = json.loads(Path(gate.REQUEST_PATH).read_text())

        with mock.patch.object(gate, "run_executor") as executor:
            self.assertEqual(gate.approve(self.approve_args(request, sha="0" * 64)), 1)
            executor.assert_not_called()

        expired = copy.deepcopy(request)
        expired["expires_at"] = "2000-01-01T00:00:00+00:00"
        Path(gate.REQUEST_PATH).write_text(json.dumps(expired))
        with mock.patch.object(gate, "run_executor") as executor:
            self.assertEqual(gate.approve(self.approve_args(expired)), 1)
            executor.assert_not_called()

        Path(gate.REQUEST_PATH).write_text(json.dumps(request))
        with mock.patch.dict(os.environ, {"TESTNET_EXECUTION_HALT": "true"}, clear=False):
            with mock.patch.object(gate, "run_executor") as executor:
                self.assertEqual(gate.approve(self.approve_args(request)), 1)
                executor.assert_not_called()

        gate.mark_used(request["request_id"])
        with mock.patch.object(gate, "run_executor") as executor:
            self.assertEqual(gate.approve(self.approve_args(request)), 1)
            executor.assert_not_called()

        fresh = copy.deepcopy(request)
        fresh["request_id"] = "fresh-request"
        fresh["used"] = False
        Path(gate.REQUEST_PATH).write_text(json.dumps(fresh))
        Path(gate.STATE_PATH).write_text(json.dumps({"used_request_ids": []}))
        with mock.patch.dict(os.environ, {"ALLOW_MANUAL_TESTNET_APPROVAL": "0"}, clear=False):
            with mock.patch.object(gate, "run_executor") as executor:
                self.assertEqual(gate.approve(self.approve_args(fresh)), 1)
                executor.assert_not_called()


if __name__ == "__main__":
    unittest.main()

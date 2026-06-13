import argparse
import copy
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import manual_testnet_approval_gate as gate
import binance_testnet_executor as executor


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


EXCHANGE_INFO = {
    "symbols": [
        {
            "symbol": "ETHUSDT",
            "status": "TRADING",
            "quantityPrecision": 3,
            "filters": [
                {"filterType": "LOT_SIZE", "minQty": "0.001", "maxQty": "1000", "stepSize": "0.001"},
                {"filterType": "MARKET_LOT_SIZE", "minQty": "0.001", "maxQty": "1000", "stepSize": "0.001"},
            ],
        }
    ]
}


class FakeClient:
    def __init__(self, mark_price="1664.45", exchange_info=None, fail_mark=False, fail_exchange=False):
        self.mark_price = mark_price
        self.exchange_info = exchange_info if exchange_info is not None else copy.deepcopy(EXCHANGE_INFO)
        self.fail_mark = fail_mark
        self.fail_exchange = fail_exchange

    def get_mark_price(self, symbol):
        if self.fail_mark:
            raise gate.BinanceFuturesTestnetClientError("mark failed")
        return self.mark_price

    def get_exchange_info(self):
        if self.fail_exchange:
            raise gate.BinanceFuturesTestnetClientError("exchange failed")
        return self.exchange_info


class FakeExecutorClient:
    base_url = gate.DEMO_FUTURES_BASE_URL

    def get_exchange_info(self):
        return copy.deepcopy(EXCHANGE_INFO)

    def get_mark_price(self, symbol):
        return "1664.45"

    def get_ticker_price(self, symbol):
        return "1664.45"

    def place_test_order(self, *args, **kwargs):
        raise AssertionError("dry-run must not call order-test")

    def place_order(self, *args, **kwargs):
        raise AssertionError("dry-run must not call actual order")


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
        self.client_patch = mock.patch.object(gate, "live_client", return_value=FakeClient())
        self.client_patch.start()

    def tearDown(self):
        self.client_patch.stop()
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

    def test_quantity_below_minimum_policy_fails(self):
        notional_policy = gate.notional_policy_fields(14.98)

        self.assertFalse(notional_policy["minimum_notional_passed"])
        self.assertTrue(notional_policy["maximum_notional_passed"])
        self.assertFalse(notional_policy["notional_policy_passed"])
        self.assertIn(gate.MIN_NOTIONAL_BLOCKED_REASON, notional_policy["notional_policy_reason"])

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
        self.assertGreaterEqual(float(result["estimated_notional_usdt"]), float(result["min_notional_usdt"]))
        self.assertLessEqual(float(result["estimated_notional_usdt"]), float(result["max_notional_usdt"]))
        self.assertEqual(request["approval_payload"]["source_bridge_quantity"], "0.009")
        self.assertNotEqual(request["approval_payload"]["approved_quantity"], "0.009")
        self.assertEqual(request["approval_payload"]["approved_quantity"], "0.014")

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


    def test_prepare_blocks_mark_price_and_exchange_filter_failures(self):
        self.write_bridge()
        with mock.patch.object(gate, "live_client", return_value=FakeClient(fail_mark=True)):
            self.assertEqual(gate.prepare(), 1)
            self.assertIn("live mark price unavailable", self.read_result()["blocked_reason"])

        with mock.patch.object(gate, "live_client", return_value=FakeClient(fail_exchange=True)):
            self.assertEqual(gate.prepare(), 1)
            self.assertIn("exchange filters unavailable", self.read_result()["blocked_reason"])

    def test_approval_time_price_drift_outside_limits_blocks(self):
        self.write_bridge()
        self.assertEqual(gate.prepare(), 0)
        request = json.loads(Path(gate.REQUEST_PATH).read_text())
        with mock.patch.object(gate, "live_client", return_value=FakeClient(mark_price="1900")):
            with mock.patch.object(gate, "run_executor") as executor:
                self.assertEqual(gate.approve(self.approve_args(request)), 1)
                executor.assert_not_called()
        result = self.read_result()
        self.assertFalse(result["notional_policy_passed"])
        self.assertIn("exceeds", result["blocked_reason"])

    def test_request_used_only_after_successful_mocked_order_test(self):
        self.write_bridge()
        self.assertEqual(gate.prepare(), 0)
        request = json.loads(Path(gate.REQUEST_PATH).read_text())
        with mock.patch.object(gate, "run_executor", return_value=(0, {"order_test": True, "order_attempted": True, "order_success": True})):
            self.assertEqual(gate.approve(self.approve_args(request)), 0)
        used_request = json.loads(Path(gate.REQUEST_PATH).read_text())
        self.assertTrue(used_request["used"])


    def test_executor_dry_run_reports_minimum_notional_failure_without_order(self):
        result_path = str(self.root / "logs" / "binance_testnet_executor_result.json")
        orders_path = str(self.root / "logs" / "binance_testnet_orders.jsonl")
        args = argparse.Namespace(
            status=False,
            account=False,
            positions=False,
            symbol="ETHUSDT",
            side="BUY",
            quantity="0.009",
            order_type="MARKET",
            price=None,
            dry_run=True,
            send=False,
            order_test=False,
            reduce_only=False,
            close_position=False,
            from_overlay=False,
            auto_from_overlay=False,
            allow_need_review=False,
        )
        with mock.patch.multiple(executor, RESULT_PATH=result_path, ORDERS_PATH=orders_path):
            with mock.patch.object(executor, "client", return_value=FakeExecutorClient()):
                rc = executor.run_order_action(args)
        self.assertEqual(rc, 0)
        result = json.loads(Path(result_path).read_text())
        self.assertAlmostEqual(result["estimated_notional_usdt"], 14.98005, places=5)
        self.assertFalse(result["minimum_notional_passed"])
        self.assertTrue(result["maximum_notional_passed"])
        self.assertFalse(result["notional_policy_passed"])
        self.assertFalse(result["notional_limit_passed"])
        self.assertFalse(result["order_attempted"])

    def test_manual_executor_command_is_order_test_only(self):
        payload = {"symbol": "ETHUSDT", "side": "BUY", "quantity": "0.014"}
        with mock.patch.object(gate.subprocess, "run") as run:
            run.return_value = argparse.Namespace(returncode=0, stdout="", stderr="")
            gate.run_executor(payload)
        command = run.call_args.args[0]
        self.assertIn("--order-test", command)
        self.assertIn("--send", command)
        self.assertNotIn("/fapi/v1/order", " ".join(command))


if __name__ == "__main__":
    unittest.main()

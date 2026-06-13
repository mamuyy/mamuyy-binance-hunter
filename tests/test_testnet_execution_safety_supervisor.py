import argparse
import copy
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import testnet_execution_safety_supervisor as sup

NOW = "2026-06-13T12:00:00+00:00"

BRIDGE = {
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
    "overlay_report_path": "fixture.json",
}

EXCHANGE_INFO = {
    "symbols": [
        {
            "symbol": "ETHUSDT",
            "status": "TRADING",
            "filters": [
                {"filterType": "LOT_SIZE", "minQty": "0.001", "maxQty": "1000", "stepSize": "0.001"},
                {"filterType": "MARKET_LOT_SIZE", "minQty": "0.001", "maxQty": "1000", "stepSize": "0.001"},
            ],
        }
    ]
}


class FakeClient:
    def __init__(self, *, mark="1664.45", positions=None, open_orders=None, can_trade=True, exchange_info=None):
        self.base_url = sup.DEMO_FUTURES_BASE_URL
        self.mark = mark
        self.positions = positions if positions is not None else []
        self.open_orders = open_orders if open_orders is not None else []
        self.can_trade = can_trade
        self.exchange_info = exchange_info if exchange_info is not None else copy.deepcopy(EXCHANGE_INFO)
        self.forbidden_calls = []

    def get_account(self):
        return {"canTrade": self.can_trade, "totalWalletBalance": "100", "availableBalance": "100"}

    def get_position_risk(self, symbol=None):
        return copy.deepcopy(self.positions)

    def get_open_orders(self, symbol=None):
        return copy.deepcopy(self.open_orders)

    def get_mark_price(self, symbol):
        return self.mark

    def get_exchange_info(self):
        return copy.deepcopy(self.exchange_info)

    def place_order(self, *args, **kwargs):
        self.forbidden_calls.append("place_order")
        raise AssertionError("supervisor must not call order endpoint")

    def place_test_order(self, *args, **kwargs):
        self.forbidden_calls.append("place_test_order")
        raise AssertionError("supervisor must not call order-test endpoint")

    def cancel_order(self, *args, **kwargs):
        self.forbidden_calls.append("cancel_order")
        raise AssertionError("supervisor must not cancel orders")


def approval_request(**payload_updates):
    payload = {
        "symbol": "ETHUSDT",
        "side": "BUY",
        "quantity": "0.014",
        "approved_quantity": "0.014",
        "order_type": "MARKET",
        "bridge_signal_metadata": sup.bridge_signal_metadata(BRIDGE),
    }
    payload.update(payload_updates)
    return {
        "request_id": "request-1234567890",
        "generated_at": NOW,
        "expires_at": "2026-06-13T12:10:00+00:00",
        "used": False,
        "payload_sha256": sup.payload_sha256(payload),
        "approval_payload": payload,
        "proposed_order_test_payload": {
            "symbol": payload["symbol"],
            "side": payload["side"],
            "quantity": payload.get("approved_quantity"),
            "order_type": "MARKET",
            "order_test": True,
            "send": True,
            "base_url": sup.DEMO_FUTURES_BASE_URL,
        },
    }


class SupervisorTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.paths = {
            "RESULT_PATH": str(self.root / "logs" / "testnet_execution_safety_supervisor_result.json"),
            "AUDIT_PATH": str(self.root / "logs" / "testnet_execution_safety_supervisor_audit.jsonl"),
            "TELEGRAM_PREVIEW_PATH": str(self.root / "logs" / "testnet_execution_safety_supervisor_telegram_preview.json"),
            "HALT_FILE_PATH": str(self.root / "runtime" / "TESTNET_EXECUTION_HALT"),
        }
        self.patch_paths = mock.patch.multiple(sup, **self.paths)
        self.patch_paths.start()
        self.env = mock.patch.dict(
            os.environ,
            {
                "BROKER_MODE": sup.BROKER_MODE_REQUIRED,
                "REAL_BINANCE_ENABLED": "false",
                "ALLOW_REAL_BINANCE_ORDER": "false",
                "ALLOW_AUTO_TESTNET_ORDER": "false",
                "ALLOW_TESTNET_ORDER": "false",
                "ALLOW_MANUAL_TESTNET_APPROVAL": "false",
                "BINANCE_FUTURES_TESTNET_BASE_URL": sup.DEMO_FUTURES_BASE_URL,
                "TESTNET_EXECUTION_HALT": "false",
                "TESTNET_MIN_NOTIONAL_USDT": "20",
                "TESTNET_MAX_NOTIONAL_USDT": "25",
                "TESTNET_MAX_TOTAL_EXPOSURE_USDT": "25",
                "TESTNET_MAX_OPEN_POSITIONS": "1",
                "TESTNET_MAX_OPEN_ORDERS_BEFORE_ENTRY": "0",
                "TESTNET_MAX_ORDERS_PER_DAY": "3",
                "TESTNET_SUPERVISOR_NOW": NOW,
            },
            clear=False,
        )
        self.env.start()
        self.client = FakeClient()
        self.client_patch = mock.patch.object(sup, "client", return_value=self.client)
        self.client_patch.start()
        self.bridge_path = self.root / "logs" / "semi_auto_testnet_bridge_result.json"
        self.request_path = self.root / "logs" / "manual_testnet_approval_request.json"
        self.orders_path = self.root / "logs" / "binance_testnet_orders.jsonl"
        self.write_json(self.bridge_path, BRIDGE)
        self.write_json(self.request_path, approval_request())

    def tearDown(self):
        self.client_patch.stop()
        self.env.stop()
        self.patch_paths.stop()
        self.tmp.cleanup()

    def write_json(self, path, payload):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload))

    def args(self, preflight=True):
        return argparse.Namespace(
            preflight=preflight,
            status=not preflight,
            symbol="ETHUSDT" if preflight else None,
            telegram_preview=False,
            bridge_result_path=str(self.bridge_path),
            approval_request_path=str(self.request_path),
            orders_log_path=str(self.orders_path),
        )

    def run_case(self, preflight=True):
        request_before = self.request_path.read_text() if self.request_path.exists() else None
        positions_before = copy.deepcopy(self.client.positions)
        open_orders_before = copy.deepcopy(self.client.open_orders)
        result = sup.run(self.args(preflight=preflight))
        self.assertFalse(result["order_attempted"])
        self.assertFalse(result["order_success"])
        self.assertFalse(result["execution_permitted"])
        self.assertEqual(self.client.forbidden_calls, [])
        self.assertEqual(request_before, self.request_path.read_text() if self.request_path.exists() else None)
        self.assertEqual(positions_before, self.client.positions)
        self.assertEqual(open_orders_before, self.client.open_orders)
        return result

    def test_flat_account_no_proposal_safe_idle(self):
        result = self.run_case(preflight=False)
        self.assertEqual(result["status"], "SAFE_IDLE")

    def test_valid_fresh_proposal_ready(self):
        result = self.run_case()
        self.assertEqual(result["status"], "READY_FOR_MANUAL_DUMMY_ORDER")
        self.assertFalse(result["execution_permitted"])
        self.assertFalse(result["order_attempted"])

    def test_existing_position_blocks(self):
        self.client.positions = [{"symbol": "ETHUSDT", "positionAmt": "0.01", "markPrice": "1664.45"}]
        result = self.run_case()
        self.assertEqual(result["status"], "BLOCKED")
        self.assertIn("position", "; ".join(result["blocked_reasons"]).lower())

    def test_existing_open_order_for_symbol_blocks(self):
        self.client.open_orders = [{"symbol": "ETHUSDT", "orderId": 1}]
        result = self.run_case()
        self.assertEqual(result["status"], "BLOCKED")
        self.assertIn("open Binance order", "; ".join(result["blocked_reasons"]))

    def test_halt_environment_active_blocks(self):
        with mock.patch.dict(os.environ, {"TESTNET_EXECUTION_HALT": "true"}, clear=False):
            result = self.run_case()
        self.assertEqual(result["status"], "BLOCKED")

    def test_halt_file_active_blocks(self):
        Path(sup.HALT_FILE_PATH).parent.mkdir(parents=True, exist_ok=True)
        Path(sup.HALT_FILE_PATH).write_text("halt")
        result = self.run_case()
        self.assertEqual(result["status"], "BLOCKED")

    def test_real_binance_flag_enabled_blocks(self):
        with mock.patch.dict(os.environ, {"REAL_BINANCE_ENABLED": "true"}, clear=False):
            result = self.run_case()
        self.assertEqual(result["status"], "BLOCKED")

    def test_auto_testnet_flag_enabled_blocks(self):
        with mock.patch.dict(os.environ, {"ALLOW_AUTO_TESTNET_ORDER": "true"}, clear=False):
            result = self.run_case()
        self.assertEqual(result["status"], "BLOCKED")

    def test_execution_gate_unexpectedly_enabled_blocks(self):
        with mock.patch.dict(os.environ, {"ALLOW_TESTNET_ORDER": "true", "ALLOW_MANUAL_TESTNET_APPROVAL": "true"}, clear=False):
            result = self.run_case()
        self.assertEqual(result["status"], "BLOCKED")

    def test_request_expired_blocks(self):
        req = approval_request()
        req["expires_at"] = "2026-06-13T11:59:00+00:00"
        self.write_json(self.request_path, req)
        result = self.run_case()
        self.assertEqual(result["status"], "BLOCKED")
        self.assertTrue(result["request_expired"])

    def test_request_already_used_blocks(self):
        req = approval_request()
        req["used"] = True
        self.write_json(self.request_path, req)
        result = self.run_case()
        self.assertEqual(result["status"], "BLOCKED")
        self.assertTrue(result["request_used"])

    def test_corrupted_sha256_blocks(self):
        req = approval_request()
        req["payload_sha256"] = "0" * 64
        self.write_json(self.request_path, req)
        result = self.run_case()
        self.assertEqual(result["status"], "BLOCKED")
        self.assertFalse(result["payload_sha256_matches"])

    def test_live_notional_below_minimum_blocks(self):
        self.client.mark = "1000"
        result = self.run_case()
        self.assertEqual(result["status"], "BLOCKED")
        self.assertFalse(result["minimum_notional_passed"])

    def test_live_notional_above_maximum_blocks(self):
        self.client.mark = "2000"
        result = self.run_case()
        self.assertEqual(result["status"], "BLOCKED")
        self.assertFalse(result["maximum_notional_passed"])

    def test_quantity_filter_invalid_blocks(self):
        self.write_json(self.request_path, approval_request(approved_quantity="0.0145", quantity="0.0145"))
        result = self.run_case()
        self.assertEqual(result["status"], "BLOCKED")
        self.assertFalse(result["quantity_filter_passed"])

    def test_daily_actual_order_limit_reached_blocks(self):
        self.orders_path.parent.mkdir(parents=True, exist_ok=True)
        rows = [
            {"generated_at": NOW, "mode": "actual_order", "order_success": True, "order_test": False, "dry_run": False}
            for _ in range(3)
        ]
        self.orders_path.write_text("\n".join(json.dumps(row) for row in rows) + "\n")
        result = self.run_case()
        self.assertEqual(result["status"], "BLOCKED")
        self.assertFalse(result["daily_limit_passed"])

    def test_duplicate_proposal_blocks(self):
        req = approval_request()
        dedupe = sup.make_dedupe_key(req["approval_payload"])
        self.orders_path.parent.mkdir(parents=True, exist_ok=True)
        self.orders_path.write_text(
            json.dumps(
                {
                    "generated_at": NOW,
                    "mode": "actual_order",
                    "order_success": True,
                    "order_test": False,
                    "dry_run": False,
                    "dedupe_key": dedupe,
                }
            )
            + "\n"
        )
        result = self.run_case()
        self.assertEqual(result["status"], "BLOCKED")
        self.assertTrue(result["duplicate_detected"])


if __name__ == "__main__":
    unittest.main()

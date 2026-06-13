import contextlib
import hashlib
import io
import json
import os
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

import testnet_operations_evidence_supervisor as sup

TODAY = datetime.now(timezone.utc).date().isoformat()


def write_json(path, payload):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload) + "\n", encoding="utf-8")


def append_jsonl(path, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")


class ReadOnlyClient:
    def __init__(self, base_url=None):
        self.base_url = base_url
        self.calls = []

    def get_account(self):
        self.calls.append("get_account")
        return {"canTrade": True, "totalWalletBalance": "100.123456", "availableBalance": "90.123456"}

    def get_position_risk(self):
        self.calls.append("get_position_risk")
        return [{"symbol": "ETHUSDT", "positionAmt": "0", "notional": "0"}]

    def get_open_orders(self, symbol=None):
        self.calls.append("get_open_orders")
        return []

    def get_mark_price(self, symbol):
        self.calls.append("get_mark_price")
        return "2000"

    def get_exchange_info(self):
        self.calls.append("get_exchange_info")
        return {"symbols": []}

    def place_order(self, *a, **k):
        raise AssertionError("order method invoked")

    def place_test_order(self, *a, **k):
        raise AssertionError("test order method invoked")

    def cancel_order(self, *a, **k):
        raise AssertionError("cancel method invoked")


class SupervisorTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.cwd = os.getcwd()
        os.chdir(self.tmp.name)
        self.addCleanup(lambda: os.chdir(self.cwd))
        self.env = mock.patch.dict(os.environ, {
            "BROKER_MODE": sup.BROKER_MODE_REQUIRED,
            "BINANCE_FUTURES_TESTNET_BASE_URL": sup.DEMO_FUTURES_BASE_URL,
            "REAL_BINANCE_ENABLED": "false",
            "ALLOW_REAL_BINANCE_ORDER": "false",
            "ALLOW_AUTO_TESTNET_ORDER": "false",
            "ALLOW_TESTNET_ORDER": "false",
            "ALLOW_MANUAL_ACTUAL_TESTNET_ROUNDTRIP": "false",
            "TESTNET_MAX_ORDERS_PER_DAY": "3",
        }, clear=False)
        self.env.start()
        self.addCleanup(self.env.stop)
        self.client = ReadOnlyClient
        self.make_artifacts()

    def make_artifacts(self, checksum=True, checksum_valid=True, entry=True, close=True, duplicate=False, close_after="0", close_side="SELL", close_qty="0.013", close_symbol="ETHUSDT"):
        plan = {"consumed": True, "completed": True, "actual_roundtrip_payload": {"symbol": "ETHUSDT", "entry_side": "BUY", "entry_quantity": "0.013"}}
        write_json(Path(sup.PLAN_PATH), plan)
        write_json(Path(sup.STATE_PATH), {"state": "COMPLETED"})
        write_json(Path(sup.RESULT_PATH), {"state": "COMPLETED"})
        write_json(Path(sup.STATUS_PATH), {"state": "COMPLETED"})
        orders = []
        if entry:
            orders.append({"generated_at": TODAY+"T00:00:00+00:00", "mode": "actual_order", "symbol": "ETHUSDT", "side": "BUY", "quantity": "0.013", "reduce_only": False, "order_success": True, "order_test": False, "dry_run": False})
        if duplicate:
            orders.append({"generated_at": TODAY+"T00:01:00+00:00", "mode": "actual_order", "symbol": "ETHUSDT", "side": "BUY", "quantity": "0.013", "reduce_only": False, "order_success": True, "order_test": False, "dry_run": False})
        if close:
            orders.append({"generated_at": TODAY+"T00:02:00+00:00", "mode": "actual_close_position", "symbol": close_symbol, "side": close_side, "quantity": close_qty, "reduce_only": True, "order_success": True, "order_test": False, "dry_run": False, "position_before_amt": "0.013", "position_after_amt": close_after, "blocked_reason": None})
        append_jsonl(Path(sup.ORDERS_PATH), orders)
        append_jsonl(Path(sup.AUDIT_PATH), [{"event": e} for e in ["ENTRY_INTENT_RECORDED", "ENTRY_SENT", "ENTRY_CONFIRMED", "PRIMARY_CLOSE_INTENT_RECORDED", "PRIMARY_CLOSE_SENT", "COMPLETED"]])
        ev = Path("evidence/phase2_97b_test")
        ev.mkdir(parents=True, exist_ok=True)
        for src in [sup.PLAN_PATH, sup.STATE_PATH, sup.AUDIT_PATH, sup.ORDERS_PATH]:
            (ev / Path(src).name).write_text(Path(src).read_text(encoding="utf-8"), encoding="utf-8")
        if checksum:
            lines = []
            for file in sup.REQUIRED_EVIDENCE_FILES:
                data = (ev / file).read_bytes()
                digest = hashlib.sha256(data).hexdigest()
                if not checksum_valid and file == sup.REQUIRED_EVIDENCE_FILES[0]:
                    digest = "0" * 64
                lines.append(f"{digest}  {file}\n")
            (ev / "SHA256SUMS").write_text("".join(lines), encoding="utf-8")

    def run_supervisor(self, mode="full"):
        with mock.patch.object(sup, "BinanceFuturesTestnetClient", self.client), contextlib.redirect_stdout(io.StringIO()) as out:
            result = sup.run(mode, "ETHUSDT", False)
        return result, out.getvalue()

    def test_completed_flat_roundtrip_safe_idle_and_capacity(self):
        result, _ = self.run_supervisor()
        self.assertEqual(result["verdict"], "SAFE_IDLE")
        self.assertEqual(result["daily_actual_order_count"], 2)
        self.assertEqual(result["remaining_daily_order_slots"], 1)
        self.assertTrue(result["emergency_close_slot_available"])
        self.assertFalse(result["full_roundtrip_capacity_passed"])

    def test_halt_file_and_env_halted(self):
        Path(sup.HALT_FILE_PATH).parent.mkdir(exist_ok=True)
        Path(sup.HALT_FILE_PATH).write_text("halt")
        self.assertEqual(self.run_supervisor()[0]["verdict"], "HALTED")
        Path(sup.HALT_FILE_PATH).unlink()
        with mock.patch.dict(os.environ, {"TESTNET_EXECUTION_HALT": "true"}):
            self.assertEqual(self.run_supervisor()[0]["verdict"], "HALTED")

    def test_live_position_open_order_other_position_halt(self):
        class Pos(ReadOnlyClient):
            def get_position_risk(self): return [{"symbol":"ETHUSDT","positionAmt":"0.1","notional":"20"}]
        self.client = Pos; self.assertEqual(self.run_supervisor()[0]["verdict"], "HALTED")
        class Ord(ReadOnlyClient):
            def get_open_orders(self, symbol=None): return [{"symbol":"ETHUSDT"}]
        self.client = Ord; self.assertEqual(self.run_supervisor()[0]["verdict"], "HALTED")
        class Other(ReadOnlyClient):
            def get_position_risk(self): return [{"symbol":"ETHUSDT","positionAmt":"0"},{"symbol":"BTCUSDT","positionAmt":"0.1"}]
        self.client = Other; self.assertEqual(self.run_supervisor()[0]["verdict"], "HALTED")

    def test_env_safety_halts(self):
        for key, value in [("REAL_BINANCE_ENABLED","true"),("ALLOW_AUTO_TESTNET_ORDER","true"),("BINANCE_FUTURES_TESTNET_BASE_URL","https://fapi.binance.com")]:
            with self.subTest(key=key), mock.patch.dict(os.environ, {key: value}):
                self.assertEqual(self.run_supervisor()[0]["verdict"], "HALTED")

    def test_evidence_directory_and_checksum_review(self):
        import shutil
        shutil.rmtree("evidence")
        self.assertEqual(self.run_supervisor()[0]["verdict"], "REVIEW_REQUIRED")
        self.make_artifacts(checksum=False)
        self.assertEqual(self.run_supervisor()[0]["checksum_status"], "MISSING")
        self.assertEqual(self.run_supervisor()[0]["verdict"], "REVIEW_REQUIRED")
        self.make_artifacts(checksum=True, checksum_valid=False)
        self.assertEqual(self.run_supervisor()[0]["checksum_status"], "MISMATCH")

    def test_required_file_missing_review(self):
        Path("evidence/phase2_97b_test/binance_testnet_orders.jsonl").unlink()
        self.assertEqual(self.run_supervisor()[0]["verdict"], "REVIEW_REQUIRED")

    def test_order_evidence_variants(self):
        for kwargs in [dict(entry=False), dict(close=False), dict(duplicate=True), dict(close_symbol="BTCUSDT"), dict(close_qty="0.014"), dict(close_side="BUY")]:
            with self.subTest(kwargs=kwargs):
                self.make_artifacts(**kwargs)
                self.assertIn(self.run_supervisor()[0]["verdict"], {"REVIEW_REQUIRED", "HALTED"})
        self.make_artifacts(close_after="0.001")
        self.assertEqual(self.run_supervisor()[0]["verdict"], "HALTED")

    def test_malformed_jsonl_status_no_network_telegram_and_no_secrets(self):
        Path(sup.ORDERS_PATH).write_text("not-json\n", encoding="utf-8")
        result, output = self.run_supervisor("status")
        self.assertEqual(result["verdict"], "REVIEW_REQUIRED")
        self.assertFalse(result["live_check_performed"])
        self.assertNotIn("API", output)
        with mock.patch.object(sup, "BinanceFuturesTestnetClient", side_effect=AssertionError("network")):
            with contextlib.redirect_stdout(io.StringIO()):
                sup.run("status", "ETHUSDT", True)
        self.assertTrue(Path(sup.TELEGRAM_PREVIEW_PATH).exists())

    def test_no_subprocess_or_source_mutation_and_only_get_methods(self):
        source = Path(sup.__file__)
        before = source.read_text(encoding="utf-8")
        with mock.patch("subprocess.run", side_effect=AssertionError("subprocess")):
            result, _ = self.run_supervisor()
        self.assertEqual(result["verdict"], "SAFE_IDLE")
        self.assertEqual(before, source.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()

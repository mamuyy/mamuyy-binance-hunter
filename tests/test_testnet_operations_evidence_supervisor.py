import contextlib
import hashlib
import io
import json
import os
import fcntl
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
        plan = {"actual_roundtrip_plan_id": "plan-current", "generated_at": TODAY+"T00:00:00+00:00", "completed_at": TODAY+"T00:03:00+00:00", "consumed": True, "completed": True, "actual_roundtrip_payload": {"symbol": "ETHUSDT", "entry_side": "BUY", "entry_quantity": "0.013"}}
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
        append_jsonl(Path(sup.AUDIT_PATH), [{"generated_at": TODAY+"T00:01:00+00:00", "actual_roundtrip_plan_id": "plan-current", "event": e} for e in ["execution locked", "entry intent", "entry result", "entry verification", "close intent", "close result", "flat verification", "completion"]])
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


    def refresh_evidence_checksums(self):
        ev = Path("evidence/phase2_97b_test")
        lines = []
        for file in sup.REQUIRED_EVIDENCE_FILES:
            lines.append(f"{hashlib.sha256((ev / file).read_bytes()).hexdigest()}  {file}\n")
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
        for key, value in [
            ("REAL_BINANCE_ENABLED", "true"),
            ("ALLOW_REAL_BINANCE_ORDER", "true"),
            ("ALLOW_AUTO_TESTNET_ORDER", "true"),
            ("ALLOW_MANUAL_ACTUAL_TESTNET_ROUNDTRIP", "true"),
            ("BINANCE_FUTURES_TESTNET_BASE_URL", "https://fapi.binance.com"),
        ]:
            with self.subTest(key=key), mock.patch.dict(os.environ, {key: value}):
                self.assertEqual(self.run_supervisor()[0]["verdict"], "HALTED")

    def test_phase3_armed_testnet_gate_alone_is_safe_idle_with_soft_warning(self):
        # CP-044A: ALLOW_TESTNET_ORDER=true is the approved Phase 3 semi-manual
        # steady state and must not force HALTED on its own.
        with mock.patch.dict(os.environ, {"ALLOW_TESTNET_ORDER": "true"}):
            result, output = self.run_supervisor()
        self.assertEqual(result["verdict"], "SAFE_IDLE")
        self.assertTrue(result["phase3_armed"])
        self.assertTrue(result["allow_testnet_order"])
        self.assertNotIn("Testnet order gate is enabled", result["blocked_reasons"])
        self.assertIn(
            "Testnet order gate is enabled (Phase 3 semi-manual armed state)",
            result["soft_safety_warnings"],
        )
        self.assertIn("Phase 3 Armed: YES", output)

    def test_phase3_armed_does_not_weaken_hard_blockers(self):
        for key, value in [
            ("REAL_BINANCE_ENABLED", "true"),
            ("ALLOW_REAL_BINANCE_ORDER", "true"),
            ("ALLOW_AUTO_TESTNET_ORDER", "true"),
            ("ALLOW_MANUAL_ACTUAL_TESTNET_ROUNDTRIP", "true"),
        ]:
            with self.subTest(key=key), mock.patch.dict(
                os.environ, {"ALLOW_TESTNET_ORDER": "true", key: value}
            ):
                result, _ = self.run_supervisor()
                self.assertEqual(result["verdict"], "HALTED")
                self.assertFalse(result["blocked_reasons"] == [])

    def test_phase3_armed_halt_file_still_halts(self):
        Path(sup.HALT_FILE_PATH).parent.mkdir(exist_ok=True)
        Path(sup.HALT_FILE_PATH).write_text("halt")
        self.addCleanup(lambda: Path(sup.HALT_FILE_PATH).unlink(missing_ok=True))
        with mock.patch.dict(os.environ, {"ALLOW_TESTNET_ORDER": "true"}):
            self.assertEqual(self.run_supervisor()[0]["verdict"], "HALTED")

    def test_phase3_armed_open_order_and_position_still_halt(self):
        class Ord(ReadOnlyClient):
            def get_open_orders(self, symbol=None): return [{"symbol": "ETHUSDT"}]
        class Pos(ReadOnlyClient):
            def get_position_risk(self): return [{"symbol": "ETHUSDT", "positionAmt": "0.1", "notional": "20"}]
        for client in (Ord, Pos):
            with self.subTest(client=client.__name__), mock.patch.dict(
                os.environ, {"ALLOW_TESTNET_ORDER": "true"}
            ):
                self.client = client
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

    def test_lock_file_missing_stale_active_and_bytes_unchanged(self):
        self.assertFalse(self.run_supervisor()[0]["execution_lock_file_present"])
        lock = Path(sup.LOCK_FILE_PATH)
        lock.parent.mkdir(exist_ok=True)
        payload = b"left behind by exited controller\n"
        lock.write_bytes(payload)
        result, _ = self.run_supervisor()
        self.assertEqual(result["verdict"], "SAFE_IDLE")
        self.assertTrue(result["execution_lock_file_present"])
        self.assertFalse(result["execution_lock_active"])
        self.assertTrue(result["execution_lock_stale_or_free"])
        self.assertEqual(lock.read_bytes(), payload)
        with lock.open("rb") as handle:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            active, _ = self.run_supervisor()
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        self.assertEqual(active["verdict"], "HALTED")
        self.assertTrue(active["execution_lock_active"])
        self.assertEqual(lock.read_bytes(), payload)

    def test_historical_live_entry_outside_window_not_duplicate_and_evidence_snapshot_used(self):
        live_rows = [
            {"generated_at": TODAY+"T00:00:00+00:00", "mode": "actual_order", "symbol": "ETHUSDT", "side": "BUY", "quantity": "0.013", "reduce_only": False, "order_success": True, "order_test": False, "dry_run": False},
            {"generated_at": TODAY+"T00:02:00+00:00", "mode": "actual_close_position", "symbol": "ETHUSDT", "side": "SELL", "quantity": "0.013", "reduce_only": True, "order_success": True, "order_test": False, "dry_run": False, "position_before_amt": "0.013", "position_after_amt": "0", "blocked_reason": None},
            {"generated_at": TODAY+"T05:00:00+00:00", "mode": "actual_order", "symbol": "ETHUSDT", "side": "BUY", "quantity": "0.013", "reduce_only": False, "order_success": True, "order_test": False, "dry_run": False},
        ]
        append_jsonl(Path(sup.ORDERS_PATH), live_rows)
        result, _ = self.run_supervisor()
        self.assertEqual(result["roundtrip_evidence_source"], "EVIDENCE_DIRECTORY")
        self.assertEqual(result["daily_capacity_source"], "LIVE_ORDER_LOG")
        self.assertEqual(result["successful_entry_count"], 1)
        self.assertFalse(result["duplicate_entry_detected"])
        self.assertEqual(result["daily_actual_order_count"], 3)

    def test_two_entries_inside_current_evidence_window_duplicate(self):
        ev_orders = Path("evidence/phase2_97b_test/binance_testnet_orders.jsonl")
        rows = [json.loads(line) for line in ev_orders.read_text(encoding="utf-8").splitlines()]
        rows.insert(1, {"generated_at": TODAY+"T00:01:00+00:00", "mode": "actual_order", "symbol": "ETHUSDT", "side": "BUY", "quantity": "0.013", "reduce_only": False, "order_success": True, "order_test": False, "dry_run": False})
        append_jsonl(ev_orders, rows)
        self.refresh_evidence_checksums()
        result, _ = self.run_supervisor()
        self.assertEqual(result["successful_entry_count"], 2)
        self.assertTrue(result["duplicate_entry_detected"])
        self.assertEqual(result["verdict"], "REVIEW_REQUIRED")

    def test_audit_other_plan_ignored(self):
        ev_audit = Path("evidence/phase2_97b_test/manual_actual_testnet_roundtrip_audit.jsonl")
        old = [{"generated_at": TODAY+"T00:01:00+00:00", "actual_roundtrip_plan_id": "older", "event": e} for e in ["entry intent", "entry result", "entry verification", "close intent", "close result", "flat verification", "completion"]]
        current = [{"generated_at": TODAY+"T00:01:00+00:00", "actual_roundtrip_plan_id": "plan-current", "event": e} for e in ["entry intent", "entry result", "entry verification", "close intent", "close result", "flat verification", "completion"]]
        append_jsonl(ev_audit, old + current)
        self.refresh_evidence_checksums()
        result, _ = self.run_supervisor()
        self.assertEqual(result["audit_total_rows_in_snapshot"], 14)
        self.assertEqual(result["audit_rows_for_current_plan"], 7)
        self.assertTrue(result["audit_lifecycle_passed"])
        self.assertNotIn("older", " ".join(result["audit_event_names_for_current_plan"]))

    def test_order_endpoints_and_execution_gate_not_used(self):
        with mock.patch.object(ReadOnlyClient, "place_order", side_effect=AssertionError("order endpoint")), \
             mock.patch.object(ReadOnlyClient, "place_test_order", side_effect=AssertionError("order-test endpoint")):
            result, _ = self.run_supervisor()
        self.assertEqual(result["verdict"], "SAFE_IDLE")
        self.assertFalse(result["allow_testnet_order"])
        self.assertFalse(result["allow_manual_actual_roundtrip"])


if __name__ == "__main__":
    unittest.main()

import os
import unittest
from pathlib import Path
from unittest import mock

import phase3_runtime_status as status_module
from phase3_runtime_status import resolve_phase3_status
from telegram_queue_notifier import build_message


FLAG_KEYS = {
    "ALLOW_TESTNET_ORDER": "",
    "ALLOW_REAL_BINANCE_ORDER": "",
    "REAL_BINANCE_ENABLED": "",
}


def _env(**overrides):
    values = dict(FLAG_KEYS)
    values.update(overrides)
    return mock.patch.dict(os.environ, values)


def _no_halt():
    return mock.patch.object(status_module, "HALT_FILE_PATH", Path("runtime/__no_such_halt_file__"))


class ResolvePhase3StatusTests(unittest.TestCase):
    def test_default_is_paper_only_fail_closed(self):
        with _env(), _no_halt():
            result = resolve_phase3_status()
        self.assertEqual(result["phase3"], "NOT_UNLOCKED")
        self.assertEqual(result["mode"], "PAPER_ONLY")
        self.assertEqual(result["execution"], "NOT_ALLOWED")
        self.assertEqual(result["real_trading"], "LOCKED")

    def test_testnet_order_flag_unlocks_semi_manual(self):
        with _env(ALLOW_TESTNET_ORDER="true"), _no_halt():
            result = resolve_phase3_status()
        self.assertEqual(result["phase3"], "UNLOCKED_TESTNET_SEMI_MANUAL")
        self.assertEqual(result["mode"], "TESTNET_SEMI_MANUAL")
        self.assertEqual(result["execution"], "MANUAL_APPROVAL_ONLY")
        self.assertEqual(result["real_trading"], "LOCKED")

    def test_real_order_flag_forces_fail_closed_with_warning(self):
        with _env(ALLOW_TESTNET_ORDER="true", ALLOW_REAL_BINANCE_ORDER="true"), _no_halt():
            result = resolve_phase3_status()
        self.assertEqual(result["phase3"], "NOT_UNLOCKED")
        self.assertEqual(result["execution"], "NOT_ALLOWED")
        self.assertEqual(result["real_trading"], "FLAG_DETECTED_REVIEW_REQUIRED")
        self.assertTrue(result["warnings"])

    def test_halt_file_forces_fail_closed(self):
        with _env(ALLOW_TESTNET_ORDER="true"), mock.patch.object(
            status_module.Path, "exists", return_value=True
        ):
            result = resolve_phase3_status()
        self.assertEqual(result["phase3"], "NOT_UNLOCKED")
        self.assertEqual(result["execution"], "NOT_ALLOWED")

    def test_malformed_flag_value_is_fail_closed(self):
        with _env(ALLOW_TESTNET_ORDER="maybe"), _no_halt():
            result = resolve_phase3_status()
        self.assertEqual(result["phase3"], "NOT_UNLOCKED")


class QueueNotifierFooterTests(unittest.TestCase):
    QUEUE_DATA = {
        "generated_at": "2026-07-02T00:00:00+00:00",
        "mode": "READ_ONLY_PROPOSAL",
        "rules": {"max_signal_age_hours": 6},
        "candidate_count": 0,
        "candidates": [],
    }

    def test_footer_paper_only(self):
        with _env(), _no_halt():
            message = build_message(dict(self.QUEUE_DATA))
        self.assertIn("Execution: NOT_ALLOWED", message)
        self.assertIn("Phase 3: NOT_UNLOCKED", message)
        self.assertIn("Real Trading: LOCKED", message)

    def test_footer_testnet_semi_manual(self):
        with _env(ALLOW_TESTNET_ORDER="true"), _no_halt():
            message = build_message(dict(self.QUEUE_DATA))
        self.assertIn("Execution: MANUAL_APPROVAL_ONLY", message)
        self.assertIn("Phase 3: UNLOCKED_TESTNET_SEMI_MANUAL", message)
        self.assertIn("Real Trading: LOCKED", message)
        self.assertIn("Status: PROPOSAL_ONLY", message)
        self.assertIn("Binance: NOT_CALLED", message)

    def test_footer_accepts_injected_status(self):
        status = {
            "phase3": "NOT_UNLOCKED",
            "execution": "NOT_ALLOWED",
            "real_trading": "LOCKED",
            "warnings": ["TESTNET_EXECUTION_HALT is active."],
        }
        message = build_message(dict(self.QUEUE_DATA), status=status)
        self.assertIn("⚠️ TESTNET_EXECUTION_HALT is active.", message)


if __name__ == "__main__":
    unittest.main()

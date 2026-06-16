import json
import tempfile
import unittest
from pathlib import Path

import hunter_completion_gate as gate


class CompletionGateTests(unittest.TestCase):
    def database(self, status="OK"):
        return {
            "status": status,
            "path": "mamuyy_hunter.db",
            "quick_check": ["ok"] if status == "OK" else None,
            "tables": ["internal_paper_trades", "runtime_heartbeats", "signals"],
            "table_count": 3,
            "error": None,
        }

    def alpha(self, verdict="INCONCLUSIVE", usable=100, critical=None):
        return {
            "critical_data_quality_failure": critical,
            "data_quality": {"rows_usable_for_calculation": usable},
            "verdict": {
                "research_audit_verdict": verdict,
                "phase_3": "NOT UNLOCKED",
                "real_trading": "LOCKED",
            },
            "readiness_references": {
                "closed_trades_500": usable >= 500,
                "rolling_win_rate_ge_45": {"value": 0.51, "passed": True},
                "rolling_profit_factor_ge_1_3": {"value": 1.42, "passed": True},
                "maximum_drawdown_pct_le_15": {"value": 8.0, "passed": True},
            },
        }

    def portfolio(self, status="READY", safe=True):
        return {
            "status": status,
            "blocked_reasons": [] if status == "READY" else ["runtime heartbeat stale"],
            "execution_gates_safe": safe,
            "broker_routing_enabled": False,
            "order_attempted": False,
        }

    def test_runtime_ready_but_inconclusive_alpha_is_research_hold(self):
        result = gate.evaluate_completion(
            self.database(), self.alpha("INCONCLUSIVE", 100), self.portfolio()
        )
        self.assertEqual(result["final_verdict"], "PAPER_OPERATIONAL_RESEARCH_HOLD")
        self.assertTrue(result["paper_operations_complete"])
        self.assertFalse(result["research_promotion_ready"])
        self.assertEqual(result["real_trading"], "LOCKED")

    def test_alpha_positive_is_still_paper_only(self):
        result = gate.evaluate_completion(
            self.database(), self.alpha("ALPHA_POSITIVE", 600), self.portfolio()
        )
        self.assertEqual(result["final_verdict"], "PAPER_OPERATIONAL_ALPHA_POSITIVE")
        self.assertTrue(result["paper_operations_complete"])
        self.assertTrue(result["research_promotion_ready"])
        self.assertEqual(result["phase_3_unlock"], "NOT_AUTHORIZED")
        self.assertEqual(result["real_trading"], "LOCKED")

    def test_missing_database_blocks_runtime(self):
        result = gate.evaluate_completion(
            self.database("MISSING"), self.alpha("ALPHA_POSITIVE", 600), self.portfolio()
        )
        self.assertEqual(result["final_verdict"], "BLOCKED_RUNTIME")
        self.assertFalse(result["paper_operations_complete"])

    def test_stale_live_overlay_blocks_runtime(self):
        result = gate.evaluate_completion(
            self.database(), self.alpha("ALPHA_POSITIVE", 600), self.portfolio("BLOCKED_LIVE_OVERLAY")
        )
        self.assertEqual(result["final_verdict"], "BLOCKED_RUNTIME")
        self.assertIn("runtime heartbeat stale", " ".join(result["blocking_reasons"]))

    def test_execution_gate_failure_blocks_safety(self):
        result = gate.evaluate_completion(
            self.database(), self.alpha("ALPHA_POSITIVE", 600), self.portfolio(safe=False)
        )
        self.assertEqual(result["final_verdict"], "BLOCKED_SAFETY")
        self.assertFalse(result["safety_ok"])
        self.assertFalse(result["paper_operations_complete"])

    def test_no_usable_trades_is_data_hold(self):
        result = gate.evaluate_completion(
            self.database(), self.alpha("INCONCLUSIVE", 0, "primary_table_not_found"), self.portfolio()
        )
        self.assertEqual(result["final_verdict"], "PAPER_OPERATIONAL_DATA_HOLD")
        self.assertTrue(result["paper_operations_complete"])
        self.assertFalse(result["alpha_data_ready"])

    def test_json_output_is_strict_and_markdown_keeps_locks(self):
        report = {
            "evaluation": gate.evaluate_completion(
                self.database(), self.alpha("INCONCLUSIVE", 100), self.portfolio()
            ),
            "database": self.database(),
            "alpha_validation": {
                "stability": {"assessment": "STABLE"},
            },
            "portfolio_v2_live_advisory": self.portfolio(),
        }
        with tempfile.TemporaryDirectory() as tmp:
            json_path = Path(tmp) / "gate.json"
            md_path = Path(tmp) / "gate.md"
            gate.write_json(report, json_path)
            gate.write_markdown(report, md_path)
            payload = json.loads(json_path.read_text(encoding="utf-8"))
            markdown = md_path.read_text(encoding="utf-8")

        self.assertEqual(
            payload["evaluation"]["final_verdict"],
            "PAPER_OPERATIONAL_RESEARCH_HOLD",
        )
        self.assertIn("Real trading remains **LOCKED**", markdown)
        self.assertIn("NO BROKER ROUTING", markdown)


if __name__ == "__main__":
    unittest.main()

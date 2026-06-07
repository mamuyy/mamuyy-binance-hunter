import csv
import json
from pathlib import Path
from datetime import datetime, timezone

ROOT = Path(__file__).resolve().parent
LOGS = ROOT / "logs"
RESULTS_CSV = LOGS / "strategy_genome_results.csv"
BLOCKED_JSON = LOGS / "genome_promotion_blocked.json"
REPORT_JSON = LOGS / "genome_promotion_gate_report.json"

MIN_SHADOW_TRADES = 30
MIN_WALKFORWARD_FOLDS = 3
MIN_FORWARD_PF = 1.5

def to_float(value, default=0.0):
    try:
        return float(value)
    except Exception:
        return default

def to_int(value, default=0):
    try:
        return int(float(value))
    except Exception:
        return default

def load_rows():
    if not RESULTS_CSV.exists():
        return []
    with RESULTS_CSV.open("r", newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))

def evaluate_row(row):
    strategy_id = row.get("strategy_id", "UNKNOWN")
    status = row.get("status", "UNKNOWN")

    shadow_trade_count = to_int(row.get("trade_count"))
    forward_period_pf = to_float(row.get("profit_factor"))
    walkforward_folds = to_int(row.get("walkforward_folds"), 0)

    checks = {
        "shadow_trade_count": {
            "value": shadow_trade_count,
            "required": MIN_SHADOW_TRADES,
            "passed": shadow_trade_count >= MIN_SHADOW_TRADES
        },
        "walkforward_folds": {
            "value": walkforward_folds,
            "required": MIN_WALKFORWARD_FOLDS,
            "passed": walkforward_folds >= MIN_WALKFORWARD_FOLDS
        },
        "forward_period_pf": {
            "value": forward_period_pf,
            "required": MIN_FORWARD_PF,
            "passed": forward_period_pf >= MIN_FORWARD_PF
        }
    }

    passed = all(item["passed"] for item in checks.values())

    return {
        "strategy_id": strategy_id,
        "strategy_name": row.get("strategy_name", ""),
        "current_status": status,
        "gate_passed": passed,
        "checks": checks,
        "recommended_action": "ALLOW_FUTURE_PROMOTION" if passed else "BLOCK_FUTURE_PROMOTION"
    }

def main():
    LOGS.mkdir(exist_ok=True)

    rows = load_rows()
    evaluated = [evaluate_row(row) for row in rows]

    blocked = [item for item in evaluated if not item["gate_passed"]]
    passed = [item for item in evaluated if item["gate_passed"]]

    report = {
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "mode": "READ_ONLY_GENOME_PROMOTION_GATE_CHECK",
        "safety": {
            "paper_only": True,
            "db_write": False,
            "execution_change": False,
            "production_scoring_change": False,
            "existing_promoted_genomes_changed": False
        },
        "gate_rules": {
            "shadow_trade_count_min": MIN_SHADOW_TRADES,
            "walkforward_folds_min": MIN_WALKFORWARD_FOLDS,
            "forward_period_pf_min": MIN_FORWARD_PF
        },
        "source_file": str(RESULTS_CSV),
        "total_rows": len(rows),
        "passed_count": len(passed),
        "blocked_count": len(blocked),
        "passed": passed,
        "blocked": blocked,
        "overall_status": "REVIEW" if blocked else "PASS"
    }

    REPORT_JSON.write_text(json.dumps(report, indent=2), encoding="utf-8")
    BLOCKED_JSON.write_text(json.dumps(blocked, indent=2), encoding="utf-8")

    print("GENOME PROMOTION GATE CHECK")
    print(f"Rows checked : {len(rows)}")
    print(f"Passed       : {len(passed)}")
    print(f"Blocked      : {len(blocked)}")
    print(f"Report       : {REPORT_JSON}")
    print(f"Blocked log  : {BLOCKED_JSON}")
    print(f"Status       : {report['overall_status']}")

if __name__ == "__main__":
    main()

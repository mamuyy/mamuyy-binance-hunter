#!/usr/bin/env python3
"""CP-043 Score95 Paper-only Watchlist Gate Draft.

Read-only governance/reporting artifact generator. This script consumes the
already-committed CP-041 and CP-042 evidence reports when present and writes a
paper-only policy draft for review. It does not touch runtime code, execution
logic, Telegram behavior, candidate queue behavior, model registries, or model
weights.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from typing import Any

CP_ID = "CP-043"
POLICY_NAME = "Score95 Paper-only Watchlist Gate Draft"
VERDICT = "POLICY_DRAFT_REVIEW"
REPORT_DIR = "reports"
CP041_PATH = os.path.join(REPORT_DIR, "cp041_ranking_ev_lifecycle_pivot.json")
CP042_PATH = os.path.join(REPORT_DIR, "cp042_score95_forward_validation.json")
OUT_JSON = os.path.join(REPORT_DIR, "cp043_score95_paper_watchlist_policy.json")
OUT_MD = os.path.join(REPORT_DIR, "cp043_score95_paper_watchlist_policy.md")


def _load_json(path: str) -> tuple[dict[str, Any] | None, bool, str | None]:
    if not os.path.exists(path):
        return None, False, None
    try:
        with open(path, "r", encoding="utf-8") as handle:
            data = json.load(handle)
        return data, True, None
    except (OSError, json.JSONDecodeError) as exc:
        return None, True, str(exc)


def _get(data: dict[str, Any] | None, *paths: str) -> Any:
    if not data:
        return None
    for path in paths:
        current: Any = data
        found = True
        for part in path.split("."):
            if isinstance(current, dict) and part in current:
                current = current[part]
            else:
                found = False
                break
        if found:
            return current
    return None


def _counts_from_statuses(items: Any) -> dict[str, int] | None:
    if not isinstance(items, list):
        return None
    counts = {"PASS": 0, "FAIL": 0, "LOW_SAMPLE": 0}
    for item in items:
        status = item.get("status") if isinstance(item, dict) else None
        if status in counts:
            counts[status] += 1
    counts["total"] = len(items)
    return counts


def _score95_cp041(cp041: dict[str, Any] | None) -> dict[str, Any]:
    threshold_rows = _get(cp041, "threshold_audit", "thresholds", "score_thresholds")
    score95 = None
    if isinstance(threshold_rows, list):
        for row in threshold_rows:
            if isinstance(row, dict) and row.get("threshold") == 95:
                score95 = row
                break
    bucket_rows = _get(cp041, "score_bucket_audit", "score_buckets", "bucket_audit.score")
    ipt_bucket = None
    if isinstance(bucket_rows, list):
        for row in bucket_rows:
            if not isinstance(row, dict):
                continue
            source = row.get("source_artifact") or row.get("source_scope") or row.get("source")
            if row.get("bucket") == "95-100" and source in {"internal_paper_trades", "internal_paper_trades_only", "IPT"}:
                ipt_bucket = row
                break
    return {
        "verdict": _get(cp041, "verdict", "final_verdict", "governance.verdict"),
        "dataset_rows": _get(cp041, "dataset_rows", "summary.dataset_rows"),
        "source_distribution": _get(cp041, "source_distribution", "dataset.source_distribution", "summary.source_distribution"),
        "score95_kept_rows": _get(score95, "kept_rows", "rows_kept", "score95_rows"),
        "score95_kept_win_rate": _get(score95, "kept_win_rate", "win_rate", "score95_win_rate"),
        "score95_kept_loss_rate": _get(score95, "kept_loss_rate", "loss_rate", "score95_loss_rate"),
        "score95_loss_avoidance_delta": _get(score95, "loss_avoidance_delta"),
        "ipt_only_score_95_100": {
            "rows": _get(ipt_bucket, "rows"),
            "win_rate": _get(ipt_bucket, "win_rate"),
            "loss_rate": _get(ipt_bucket, "loss_rate"),
        },
    }


def _score95_cp042(cp042: dict[str, Any] | None) -> dict[str, Any]:
    forward = _get(cp042, "ipt_forward_validation", "forward_validation.ipt_only", "ipt_only_forward_validation")
    rolling = _get(cp042, "rolling_windows", "rolling_window_validation")
    return {
        "verdict": _get(cp042, "verdict", "final_verdict", "governance.verdict"),
        "dataset_rows": _get(cp042, "dataset_rows", "summary.dataset_rows"),
        "score95_rows": _get(cp042, "score95_rows", "summary.score95_rows", "aggregate.score95_rows"),
        "score95_source_distribution": _get(cp042, "score95_source_distribution", "summary.score95_source_distribution", "aggregate.score95_source_distribution"),
        "aggregate_score95_status": _get(cp042, "aggregate_score95_status", "aggregate.status", "score95_aggregate.status"),
        "ipt_forward_counts": _get(cp042, "ipt_forward_counts", "ipt_forward_validation.counts") or _counts_from_statuses(forward),
        "rolling_window_counts": _get(cp042, "rolling_window_counts", "rolling_windows.counts") or _counts_from_statuses(rolling),
        "major_forward_contradiction": _get(cp042, "major_forward_contradiction", "contradictions.major_forward_contradiction", "review.major_forward_contradiction"),
    }


def _format_value(value: Any) -> str:
    if value is None:
        return "missing"
    if isinstance(value, float):
        return f"{value:.4f}"
    return str(value)


def _build_markdown(report: dict[str, Any]) -> str:
    cp041 = report["evidence_summary"]["cp041"]
    cp042 = report["evidence_summary"]["cp042"]
    lines = [
        f"# {report['cp_id']} — {report['policy_name']}",
        "",
        "## Governance header",
        f"- Verdict: `{report['verdict']}`",
        "- Phase 3 status: `LOCKED`",
        "- Classifier gate: `FROZEN`",
        "- Model promotion: `HOLD`",
        "- PAPER_ONLY: `true`",
        "- Runtime/execution/Telegram/candidate queue/registry changed: `false`",
        "",
        "## Evidence summary",
        f"- CP-041 verdict: `{_format_value(cp041.get('verdict'))}`",
        f"- CP-041 score>=95: rows={_format_value(cp041.get('score95_kept_rows'))}, win_rate={_format_value(cp041.get('score95_kept_win_rate'))}, loss_rate={_format_value(cp041.get('score95_kept_loss_rate'))}, loss_avoidance_delta={_format_value(cp041.get('score95_loss_avoidance_delta'))}",
        f"- CP-041 IPT-only score 95-100: `{_format_value(cp041.get('ipt_only_score_95_100'))}`",
        f"- CP-042 verdict: `{_format_value(cp042.get('verdict'))}`",
        f"- CP-042 aggregate score95 status: `{_format_value(cp042.get('aggregate_score95_status'))}`",
        f"- CP-042 IPT forward counts: `{_format_value(cp042.get('ipt_forward_counts'))}`",
        f"- CP-042 rolling window counts: `{_format_value(cp042.get('rolling_window_counts'))}`",
        f"- CP-042 major forward contradiction: `{_format_value(cp042.get('major_forward_contradiction'))}`",
        "",
        "## Proposed paper-only watchlist policy",
    ]
    lines.extend(f"- {item}" for item in report["proposed_policy"])
    lines.extend(["", "## Required future evidence before any gate proposal"])
    lines.extend(f"- {item}" for item in report["required_future_evidence"])
    lines.extend(["", "## Explicit non-goals"])
    lines.extend(f"- {item}" for item in report["explicit_non_goals"])
    lines.extend(["", "## Final recommendation"])
    lines.extend(f"- {item}" for item in report["final_recommendation"])
    lines.extend(["", "## Missing evidence flags"])
    lines.extend(f"- {key}: `{value}`" for key, value in report["missing_evidence_flags"].items())
    lines.append("")
    return "\n".join(lines)


def main() -> None:
    cp041, cp041_found, cp041_error = _load_json(CP041_PATH)
    cp042, cp042_found, cp042_error = _load_json(CP042_PATH)
    missing_flags = {
        "cp041_report_missing": not cp041_found,
        "cp042_report_missing": not cp042_found,
        "cp041_report_unreadable": cp041_error is not None,
        "cp042_report_unreadable": cp042_error is not None,
    }
    evidence_summary = {"cp041": _score95_cp041(cp041), "cp042": _score95_cp042(cp042)}
    if any(missing_flags.values()):
        evidence_summary["draft_evidence_status"] = "REVIEW_REQUIRED"

    report = {
        "cp_id": CP_ID,
        "verdict": VERDICT,
        "policy_name": POLICY_NAME,
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "governance": {
            "phase3_status": "LOCKED",
            "classifier_gate": "FROZEN",
            "model_promotion": "HOLD",
            "paper_only": True,
            "runtime_changed": False,
            "execution_changed": False,
            "telegram_changed": False,
            "candidate_queue_changed": False,
            "registry_changed": False,
        },
        "evidence_summary": evidence_summary,
        "proposed_policy": [
            "A candidate may be marked score95_watchlist_candidate=true only if score >= 95.",
            "This marker is informational and PAPER_ONLY.",
            "It must not trigger execution.",
            "It must not bypass portfolio risk.",
            "It must not bypass market regime filters.",
            "It must not bypass freshness guard.",
            "It must not bypass daily order caps.",
            "It must not bypass safety supervisor.",
            "It must not unlock Phase 3.",
            "It must not promote classifier.",
            "It must not create live Binance orders.",
            "It must require ongoing source-aware forward evidence.",
        ],
        "required_future_evidence": [
            "Minimum additional IPT/live-like sample collection.",
            "Minimum rolling windows.",
            "No major forward contradiction.",
            "Stable loss avoidance.",
            "Positive avg profit.",
            "Drawdown / adverse outcome review.",
            "Symbol concentration review.",
            "Regime-specific review.",
            "Manual approval chain before any runtime proposal.",
        ],
        "explicit_non_goals": [
            "No execution.",
            "No runtime integration.",
            "No Telegram alert change.",
            "No dashboard change unless future separate CP.",
            "No live trading.",
            "No model promotion.",
            "No Phase 3 unlock.",
        ],
        "final_recommendation": [
            "Keep Phase 3 LOCKED.",
            "Keep classifier gate FROZEN.",
            "Continue PAPER_ONLY score95 observation.",
            "CP-044 optional: score95 shadow monitoring dashboard/reporting audit after more forward data exists.",
            "Do not open live execution.",
        ],
        "missing_evidence_flags": missing_flags,
    }

    os.makedirs(REPORT_DIR, exist_ok=True)
    with open(OUT_JSON, "w", encoding="utf-8") as handle:
        json.dump(report, handle, indent=2, sort_keys=True)
        handle.write("\n")
    with open(OUT_MD, "w", encoding="utf-8") as handle:
        handle.write(_build_markdown(report))

    print("CP-043 generated")
    print(f"evidence files found/missing: CP-041={'found' if cp041_found else 'missing'}, CP-042={'found' if cp042_found else 'missing'}")
    if cp041_error:
        print(f"CP-041 evidence read error: {cp041_error}")
    if cp042_error:
        print(f"CP-042 evidence read error: {cp042_error}")
    print(f"CP-041 verdict: {_format_value(evidence_summary['cp041'].get('verdict'))}")
    print(f"CP-042 verdict: {_format_value(evidence_summary['cp042'].get('verdict'))}")
    print(f"proposed policy status: {VERDICT}")
    print("Phase 3 remains LOCKED")
    print("No runtime/execution/model changes")


if __name__ == "__main__":
    main()

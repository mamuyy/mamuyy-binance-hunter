import hashlib
import json
import os
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

VALIDATION_PATH = Path("reports/candidate_validation_report.json")
LEDGER_PATH = Path("reports/candidate_evidence_ledger.jsonl")
SUMMARY_PATH = Path("reports/candidate_evidence_ledger_summary.json")
PHASE = "Phase 9D Candidate Evidence Ledger"
SOURCE_REPORT = "reports/candidate_validation_report.json"


def bucket_score(score: float) -> str:
    if score >= 95:
        return "95-100"
    if score >= 90:
        return "90-94"
    if score >= 85:
        return "85-89"
    return "<85"


def evidence_id_for(symbol: Any, signal_timestamp: Any, horizon: Any) -> str:
    raw = f"{symbol}|{signal_timestamp}|{horizon}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def load_existing_ledger(ledger_path: Path) -> tuple[set[str], list[dict[str, Any]], int]:
    evidence_ids: set[str] = set()
    records: list[dict[str, Any]] = []
    malformed = 0

    if not ledger_path.exists():
        return evidence_ids, records, malformed

    with ledger_path.open("r", encoding="utf-8") as handle:
        for line in handle:
            if not line.strip():
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                malformed += 1
                continue
            if not isinstance(record, dict):
                malformed += 1
                continue
            records.append(record)
            evidence_id = record.get("evidence_id")
            if evidence_id is not None:
                evidence_ids.add(str(evidence_id))

    return evidence_ids, records, malformed


def build_evidence_record(candidate: dict[str, Any], horizon: str, hdata: dict[str, Any], recorded_at: str) -> dict[str, Any]:
    score = float(candidate.get("score") or 0.0)
    symbol = candidate.get("symbol")
    signal_timestamp = candidate.get("signal_timestamp")
    return {
        "evidence_id": evidence_id_for(symbol, signal_timestamp, horizon),
        "recorded_at": recorded_at,
        "symbol": symbol,
        "rank": candidate.get("rank"),
        "signal_timestamp": signal_timestamp,
        "base_price": candidate.get("base_price"),
        "score": score,
        "score_bucket": bucket_score(score),
        "predicted_direction": candidate.get("predicted_direction"),
        "regime_name": candidate.get("regime_name"),
        "whale_activity": candidate.get("whale_activity"),
        "horizon": horizon,
        "target_timestamp": hdata.get("target_timestamp"),
        "observed_timestamp": hdata.get("observed_timestamp"),
        "observed_price": hdata.get("observed_price"),
        "return_pct": hdata.get("return_pct"),
        "direction_hit": hdata.get("direction_hit"),
        "source_phase": PHASE,
        "source_report": SOURCE_REPORT,
        "status": "RECORDED",
    }


def ledger_summary(records: list[dict[str, Any]]) -> dict[str, Any]:
    candidates = {(r.get("symbol"), r.get("signal_timestamp")) for r in records}
    symbols = {r.get("symbol") for r in records if r.get("symbol") is not None}
    return {
        "total_records": len(records),
        "unique_candidates": len(candidates),
        "unique_symbols": len(symbols),
        "by_horizon": dict(sorted(Counter(str(r.get("horizon")) for r in records).items())),
        "by_score_bucket": dict(sorted(Counter(str(r.get("score_bucket")) for r in records).items())),
        "by_regime": dict(sorted(Counter(str(r.get("regime_name") or "UNKNOWN") for r in records).items())),
    }


def write_summary_atomic(summary_path: Path, summary: dict[str, Any]) -> None:
    summary_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = summary_path.with_name(f".{summary_path.name}.tmp")
    tmp_path.write_text(json.dumps(summary, indent=2), encoding="utf-8")
    os.replace(tmp_path, summary_path)


def run(
    validation_path: Path = VALIDATION_PATH,
    ledger_path: Path = LEDGER_PATH,
    summary_path: Path = SUMMARY_PATH,
) -> dict[str, Any]:
    if not validation_path.exists():
        raise SystemExit("[FAIL] Missing candidate validation report. Run candidate_validator.py first.")

    data = json.loads(validation_path.read_text(encoding="utf-8"))
    candidates = data.get("results", [])
    existing_ids, existing_records, malformed_count = load_existing_ledger(ledger_path)

    ready_seen = 0
    pending_skipped = 0
    duplicate_skipped = 0
    new_records: list[dict[str, Any]] = []
    recorded_at = utc_now_iso()

    for candidate in candidates:
        for horizon, hdata in candidate.get("horizons", {}).items():
            status = hdata.get("status")
            if status == "PENDING":
                pending_skipped += 1
                continue
            if status != "READY":
                continue
            ready_seen += 1
            record = build_evidence_record(candidate, horizon, hdata, recorded_at)
            if record["evidence_id"] in existing_ids:
                duplicate_skipped += 1
                continue
            existing_ids.add(record["evidence_id"])
            new_records.append(record)

    ledger_path.parent.mkdir(parents=True, exist_ok=True)
    if new_records:
        with ledger_path.open("a", encoding="utf-8") as handle:
            for record in new_records:
                handle.write(json.dumps(record, sort_keys=True) + "\n")

    all_records = existing_records + new_records
    summary = {
        "phase": PHASE,
        "mode": "APPEND_ONLY_RESEARCH",
        "source_report": SOURCE_REPORT,
        "ledger_path": str(ledger_path),
        "run_summary": {
            "validation_candidates_read": len(candidates),
            "ready_horizons_seen": ready_seen,
            "new_records_appended": len(new_records),
            "duplicate_skipped": duplicate_skipped,
            "pending_skipped": pending_skipped,
            "malformed_line_count": malformed_count,
        },
        "ledger_summary": ledger_summary(all_records),
        "governance": {
            "paper_only": True,
            "append_only": True,
            "writes_to_database": False,
            "writes_to_broker": False,
            "execution_allowed": False,
            "automatic_promotion_allowed": False,
        },
    }
    write_summary_atomic(summary_path, summary)
    return summary


def main() -> None:
    print("=== MAMUYY HUNTER PHASE 9D - CANDIDATE EVIDENCE LEDGER ===")
    summary = run()
    run_summary = summary["run_summary"]
    ledger = summary["ledger_summary"]
    if run_summary["new_records_appended"] > 0:
        status = "RECORDED"
    elif run_summary["ready_horizons_seen"] > 0:
        status = "NO_NEW_EVIDENCE"
    else:
        status = "PENDING"
    print(f"Candidates Read: {run_summary['validation_candidates_read']}")
    print(f"Ready Horizons: {run_summary['ready_horizons_seen']}")
    print(f"New Evidence: {run_summary['new_records_appended']}")
    print(f"Duplicates Skipped: {run_summary['duplicate_skipped']}")
    print(f"Pending Skipped: {run_summary['pending_skipped']}")
    print(f"Total Ledger Records: {ledger['total_records']}")
    print(f"Status: {status}")


if __name__ == "__main__":
    main()

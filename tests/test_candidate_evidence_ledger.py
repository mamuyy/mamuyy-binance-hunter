import json
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from candidate_evidence_ledger import evidence_id_for, run
from candidate_validator import validate_candidate


def write_validation(path: Path, results: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"results": results}), encoding="utf-8")


def candidate(horizons: dict) -> dict:
    return {
        "rank": 1,
        "symbol": "BTCUSDT",
        "signal_timestamp": "2026-01-01T00:00:00+00:00",
        "base_price": 100.0,
        "score": 96.5,
        "predicted_direction": "UP",
        "regime_name": "BULL",
        "whale_activity": "HIGH",
        "horizons": horizons,
    }


def ready(return_pct=2.5) -> dict:
    return {
        "status": "READY",
        "target_timestamp": "2026-01-02T00:00:00+00:00",
        "observed_timestamp": "2026-01-02T00:01:00+00:00",
        "observed_price": 102.5,
        "return_pct": return_pct,
        "direction_hit": True,
    }


def pending() -> dict:
    return {
        "status": "PENDING",
        "target_timestamp": "2026-01-03T00:00:00+00:00",
        "observed_timestamp": None,
        "observed_price": None,
        "return_pct": None,
        "direction_hit": None,
    }


def read_jsonl(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def paths(tmp_path):
    reports = tmp_path / "reports"
    return (
        reports / "candidate_validation_report.json",
        reports / "candidate_evidence_ledger.jsonl",
        reports / "candidate_evidence_ledger_summary.json",
    )


def test_ready_horizon_appended_correctly(tmp_path):
    validation, ledger, summary_path = paths(tmp_path)
    write_validation(validation, [candidate({"24h": ready()})])

    summary = run(validation, ledger, summary_path)
    records = read_jsonl(ledger)

    assert len(records) == 1
    assert records[0]["symbol"] == "BTCUSDT"
    assert records[0]["horizon"] == "24h"
    assert records[0]["score_bucket"] == "95-100"
    assert records[0]["source_phase"] == "Phase 9D Candidate Evidence Ledger"
    assert records[0]["status"] == "RECORDED"
    assert summary["run_summary"]["new_records_appended"] == 1


def test_pending_horizon_not_appended(tmp_path):
    validation, ledger, summary_path = paths(tmp_path)
    write_validation(validation, [candidate({"24h": pending()})])

    summary = run(validation, ledger, summary_path)

    assert not ledger.exists()
    assert summary["run_summary"]["pending_skipped"] == 1
    assert summary["run_summary"]["ready_horizons_seen"] == 0


def test_deterministic_evidence_id(tmp_path):
    validation, ledger, summary_path = paths(tmp_path)
    write_validation(validation, [candidate({"24h": ready()})])

    run(validation, ledger, summary_path)
    record = read_jsonl(ledger)[0]

    assert record["evidence_id"] == evidence_id_for("BTCUSDT", "2026-01-01T00:00:00+00:00", "24h")
    assert evidence_id_for("BTCUSDT", "2026-01-01T00:00:00+00:00", "24h") == evidence_id_for(
        "BTCUSDT", "2026-01-01T00:00:00+00:00", "24h"
    )


def test_duplicate_run_does_not_create_duplicate_records(tmp_path):
    validation, ledger, summary_path = paths(tmp_path)
    write_validation(validation, [candidate({"24h": ready()})])

    run(validation, ledger, summary_path)
    second = run(validation, ledger, summary_path)

    assert len(read_jsonl(ledger)) == 1
    assert second["run_summary"]["new_records_appended"] == 0
    assert second["run_summary"]["duplicate_skipped"] == 1


def test_multiple_horizons_create_separate_evidence_records(tmp_path):
    validation, ledger, summary_path = paths(tmp_path)
    write_validation(validation, [candidate({"24h": ready(1.0), "48h": ready(2.0), "72h": pending()})])

    summary = run(validation, ledger, summary_path)
    records = read_jsonl(ledger)

    assert len(records) == 2
    assert {r["horizon"] for r in records} == {"24h", "48h"}
    assert len({r["evidence_id"] for r in records}) == 2
    assert summary["run_summary"]["pending_skipped"] == 1


def test_malformed_existing_jsonl_line_does_not_crash(tmp_path):
    validation, ledger, summary_path = paths(tmp_path)
    ledger.parent.mkdir(parents=True, exist_ok=True)
    ledger.write_text("not json\n", encoding="utf-8")
    write_validation(validation, [candidate({"24h": ready()})])

    summary = run(validation, ledger, summary_path)

    assert summary["run_summary"]["malformed_line_count"] == 1
    assert len(ledger.read_text(encoding="utf-8").splitlines()) == 2
    assert len(read_jsonl_lines_ignore_bad(ledger)) == 1


def read_jsonl_lines_ignore_bad(path: Path) -> list[dict]:
    records = []
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            records.append(json.loads(line))
        except json.JSONDecodeError:
            pass
    return records


def test_summary_counts_are_correct(tmp_path):
    validation, ledger, summary_path = paths(tmp_path)
    second = candidate({"24h": ready()})
    second["symbol"] = "ETHUSDT"
    second["signal_timestamp"] = "2026-01-01T01:00:00+00:00"
    second["score"] = 88
    second["regime_name"] = None
    write_validation(validation, [candidate({"24h": ready(), "48h": ready()}), second])

    summary = run(validation, ledger, summary_path)

    assert summary["ledger_summary"]["total_records"] == 3
    assert summary["ledger_summary"]["unique_candidates"] == 2
    assert summary["ledger_summary"]["unique_symbols"] == 2
    assert summary["ledger_summary"]["by_horizon"] == {"24h": 2, "48h": 1}
    assert summary["ledger_summary"]["by_score_bucket"] == {"85-89": 1, "95-100": 2}
    assert summary["ledger_summary"]["by_regime"] == {"BULL": 2, "UNKNOWN": 1}


def test_governance_locks_remain_false(tmp_path):
    validation, ledger, summary_path = paths(tmp_path)
    write_validation(validation, [candidate({"24h": ready()})])

    summary = run(validation, ledger, summary_path)
    governance = summary["governance"]

    assert governance["paper_only"] is True
    assert governance["append_only"] is True
    assert governance["writes_to_database"] is False
    assert governance["writes_to_broker"] is False
    assert governance["execution_allowed"] is False
    assert governance["automatic_promotion_allowed"] is False


def make_phase9b_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.execute(
        """
        CREATE TABLE historical_klines (
            symbol TEXT,
            timestamp TEXT,
            interval TEXT,
            close REAL
        )
        """
    )
    conn.executemany(
        "INSERT INTO historical_klines (symbol, timestamp, interval, close) VALUES (?, ?, ?, ?)",
        [
            ("BTCUSDT", "2026-01-02T00:00:00+00:00", "15m", 101.0),
            ("BTCUSDT", "2026-01-03T00:00:00+00:00", "15m", 102.0),
            ("BTCUSDT", "2026-01-04T00:00:00+00:00", "15m", 103.0),
        ],
    )
    return conn


def test_ledger_records_phase9b_regime_name_and_whale_activity_contract(tmp_path):
    validation, ledger, summary_path = paths(tmp_path)
    conn = make_phase9b_conn()
    phase9b_result = validate_candidate(
        conn,
        {
            "rank": 1,
            "symbol": "BTCUSDT",
            "timestamp": "2026-01-01T00:00:00+00:00",
            "price": 100.0,
            "score": 96.5,
            "regime_name": "BULL_EXPANSION",
            "whale_activity": "HIGH",
            "symbol_validation": {"symbol": "BTCUSDT", "valid": True, "reason": None},
        },
    )
    conn.close()
    write_validation(validation, [phase9b_result])

    run(validation, ledger, summary_path)
    records = read_jsonl(ledger)

    assert len(records) == 3
    assert {record["regime_name"] for record in records} == {"BULL_EXPANSION"}
    assert {record["whale_activity"] for record in records} == {"HIGH"}

import json
from collections import defaultdict
from pathlib import Path
from statistics import mean
from typing import Any

VALIDATION_PATH = Path("reports/candidate_validation_report.json")
OUTPUT_PATH = Path("reports/candidate_outcome_analytics.json")


def bucket_score(score: float) -> str:
    if score >= 95:
        return "95-100"
    if score >= 90:
        return "90-94"
    if score >= 85:
        return "85-89"
    return "<85"


def pct(numerator: int, denominator: int) -> float | None:
    if denominator <= 0:
        return None
    return round((numerator / denominator) * 100, 2)


def summarize_hits(records: list[dict[str, Any]]) -> dict[str, Any]:
    total = len(records)
    hits = sum(1 for r in records if r.get("direction_hit") is True)
    returns = [float(r["return_pct"]) for r in records if r.get("return_pct") is not None]

    return {
        "validated": total,
        "hits": hits,
        "misses": total - hits,
        "direction_accuracy": pct(hits, total),
        "avg_return_pct": round(mean(returns), 4) if returns else None,
        "best_return_pct": round(max(returns), 4) if returns else None,
        "worst_return_pct": round(min(returns), 4) if returns else None,
    }


def add_group(groups: dict[str, list[dict[str, Any]]], key: str | None, record: dict[str, Any]) -> None:
    groups[str(key or "UNKNOWN")].append(record)


def main() -> None:
    print("=== MAMUYY HUNTER PHASE 9C - CANDIDATE OUTCOME ANALYTICS ===")

    if not VALIDATION_PATH.exists():
        raise SystemExit("[FAIL] Missing candidate validation report. Run candidate_validator.py first.")

    data = json.loads(VALIDATION_PATH.read_text(encoding="utf-8"))
    results = data.get("results", [])

    horizon_records: dict[str, list[dict[str, Any]]] = defaultdict(list)
    regime_groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    whale_groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    score_groups: dict[str, list[dict[str, Any]]] = defaultdict(list)

    for candidate in results:
        score = float(candidate.get("score") or 0.0)
        regime = candidate.get("regime_name")
        whale = candidate.get("whale_activity")
        score_bucket = bucket_score(score)

        for horizon, hdata in candidate.get("horizons", {}).items():
            if hdata.get("status") != "READY":
                continue

            record = {
                "symbol": candidate.get("symbol"),
                "rank": candidate.get("rank"),
                "score": score,
                "score_bucket": score_bucket,
                "regime_name": regime,
                "whale_activity": whale,
                "horizon": horizon,
                "return_pct": hdata.get("return_pct"),
                "direction_hit": hdata.get("direction_hit"),
            }

            horizon_records[horizon].append(record)
            add_group(regime_groups, regime, record)
            add_group(whale_groups, whale, record)
            add_group(score_groups, score_bucket, record)

    by_horizon = {k: summarize_hits(v) for k, v in sorted(horizon_records.items())}
    by_regime = {k: summarize_hits(v) for k, v in sorted(regime_groups.items())}
    by_whale_activity = {k: summarize_hits(v) for k, v in sorted(whale_groups.items())}
    by_score_bucket = {k: summarize_hits(v) for k, v in sorted(score_groups.items())}

    total_validated = sum(item["validated"] for item in by_horizon.values())

    report = {
        "phase": "Phase 9C Candidate Outcome Analytics",
        "mode": "READ_ONLY_ANALYTICS",
        "source_validation_report": str(VALIDATION_PATH),
        "total_validated_horizons": total_validated,
        "candidate_count": len(results),
        "by_horizon": by_horizon,
        "by_regime": by_regime,
        "by_whale_activity": by_whale_activity,
        "by_score_bucket": by_score_bucket,
        "governance": {
            "paper_only": True,
            "writes_to_database": False,
            "writes_to_broker": False,
            "execution_allowed": False,
        },
    }

    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT_PATH.write_text(json.dumps(report, indent=2), encoding="utf-8")

    print(f"Candidates: {len(results)}")
    print(f"Validated Horizons: {total_validated}")
    print(f"Report generated: {OUTPUT_PATH}")

    if total_validated == 0:
        print("Status: PENDING - no READY validation horizons yet.")
    else:
        print("By Horizon:")
        for key, summary in by_horizon.items():
            print(f"{key}: accuracy={summary['direction_accuracy']} avg_return={summary['avg_return_pct']}")


if __name__ == "__main__":
    main()

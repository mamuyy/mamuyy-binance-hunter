import csv
import json
import os
from collections import Counter, defaultdict


def summarize(log_path: str = "orchestrator_log.csv") -> dict:
    summary = {
        "source": log_path,
        "top_recurring_warning_reasons": [],
        "grouped_by_category": {},
    }
    if not os.path.exists(log_path):
        return summary

    reason_counter = Counter()
    category_counter = defaultdict(int)

    with open(log_path, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            level = str(row.get("level") or "").upper()
            if level not in {"WARNING", "ERROR", "CRITICAL"}:
                continue
            message = str(row.get("message") or "").strip()
            if not message:
                continue
            reason_counter[message] += 1

            m = message.lower()
            if "lock" in m or "database is locked" in m:
                category_counter["DB_LOCK"] += 1
            elif "heartbeat" in m and "stale" in m:
                category_counter["HEARTBEAT_STALE"] += 1
            elif "broadcast" in m and ("reject" in m or "skip" in m):
                category_counter["BROADCAST_REJECTION"] += 1
            elif "stress" in m or "panic" in m:
                category_counter["REGIME_STRESS"] += 1
            elif "telegram" in m or "telemetry" in m:
                category_counter["TELEMETRY_DEGRADED"] += 1
            elif "guardian" in m and ("recover" in m or "halt" in m):
                category_counter["GUARDIAN_RECOVERY"] += 1
            elif "stale" in m or "freshness" in m:
                category_counter["DATA_FRESHNESS"] += 1
            elif "drawdown" in m or "resource" in m or "memory" in m or "cpu" in m:
                category_counter["RESOURCE_PRESSURE"] += 1

    summary["top_recurring_warning_reasons"] = [
        {"warning_reason": reason, "occurrence_count": count}
        for reason, count in reason_counter.most_common(20)
    ]
    summary["grouped_by_category"] = dict(sorted(category_counter.items(), key=lambda kv: kv[1], reverse=True))
    return summary


if __name__ == "__main__":
    print(json.dumps(summarize(), indent=2))

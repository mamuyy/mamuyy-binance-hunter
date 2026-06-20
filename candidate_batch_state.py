import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from json_utils import atomic_write_json

ACTIVE_STATUSES = {"OPEN", "WAITING_DATA"}
CLOSED_STATUSES = {"COMPLETE", "TERMINAL_INVALID"}
RETRIABLE_HORIZON_STATUSES = {"PENDING_NOT_MATURE", "BLOCKED_STALE_DATA", "BLOCKED_MISSING_DATA"}
TERMINAL_HORIZON_STATUSES = {"BLOCKED_INVALID_SYMBOL"}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def state_path_for_archive(archive_path: str | Path) -> Path:
    path = Path(archive_path)
    return path.with_name(path.stem + ".state.json")


def registry_path_for_state(state_path: str | Path) -> Path:
    return Path(state_path).parent / "registry.json"


def initial_state_for_report(report: dict[str, Any], archive_path: str | Path) -> dict[str, Any]:
    now = _now()
    return {
        "batch_id": report["batch_id"],
        "archive_path": str(archive_path),
        "lifecycle_status": "OPEN",
        "status": "OPEN",
        "opened_at": report.get("generated_at") or now,
        "updated_at": now,
        "closed_at": None,
        "ready_horizon_count": 0,
        "pending_horizon_count": 0,
        "retriable_blocked_horizon_count": 0,
        "terminal_invalid_horizon_count": 0,
        "total_required_horizon_count": int(report.get("candidate_count", 0)) * len(report.get("validation_horizons", [24, 48, 72])),
        "close_reason": None,
        "validation_report_path": None,
        "interval": report.get("interval") or report.get("rules", {}).get("interval"),
        "governance": report.get("governance", {}),
    }


def summarize_validation(report: dict[str, Any]) -> dict[str, int]:
    ready = pending = retriable = terminal = total = 0
    for result in report.get("results", []) or []:
        for horizon in (result.get("horizons") or {}).values():
            total += 1
            status = horizon.get("status")
            if status == "READY":
                ready += 1
            elif status in RETRIABLE_HORIZON_STATUSES:
                if status == "PENDING_NOT_MATURE":
                    pending += 1
                else:
                    retriable += 1
            elif status in TERMINAL_HORIZON_STATUSES:
                terminal += 1
            else:
                retriable += 1
    return {
        "ready_horizon_count": ready,
        "pending_horizon_count": pending,
        "retriable_blocked_horizon_count": retriable,
        "terminal_invalid_horizon_count": terminal,
        "total_required_horizon_count": total,
    }


def lifecycle_from_counts(counts: dict[str, int]) -> tuple[str, str | None]:
    total = counts["total_required_horizon_count"]
    ready = counts["ready_horizon_count"]
    pending = counts["pending_horizon_count"]
    retriable = counts["retriable_blocked_horizon_count"]
    terminal = counts["terminal_invalid_horizon_count"]
    if total > 0 and ready == total:
        return "COMPLETE", "ALL_HORIZONS_READY"
    if total > 0 and terminal > 0 and pending == 0 and retriable == 0 and ready + terminal == total:
        return "TERMINAL_INVALID", "ALL_UNRESOLVED_HORIZONS_TERMINAL_INVALID"
    if pending > 0 or retriable > 0 or ready > 0 or terminal > 0:
        return "WAITING_DATA", None
    return "OPEN", None


def update_registry(state: dict[str, Any], registry_path: str | Path) -> None:
    path = Path(registry_path)
    try:
        registry = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        registry = {"batches": []}
    batches = registry.setdefault("batches", [])
    entry = next((item for item in batches if item.get("batch_id") == state.get("batch_id")), None)
    if entry is None:
        entry = {"batch_id": state.get("batch_id")}
        batches.append(entry)
    entry.update({
        "batch_id": state.get("batch_id"),
        "archive_path": state.get("archive_path"),
        "state_path": str(Path(path).parent / f"{state.get('batch_id')}.state.json"),
        "lifecycle_status": state.get("lifecycle_status"),
        "status": state.get("lifecycle_status"),
        "updated_at": state.get("updated_at"),
        "candidate_count": int(state.get("total_required_horizon_count", 0)) // 3 if state.get("total_required_horizon_count") is not None else None,
    })
    registry["updated_at"] = _now()
    atomic_write_json(path, registry)


def update_state_from_validation(
    validation_report: dict[str, Any],
    validation_report_path: str | Path,
    archive_path: str | Path | None = None,
    state_path: str | Path | None = None,
) -> dict[str, Any] | None:
    source_queue = archive_path or validation_report.get("source_queue")
    if not source_queue:
        return None
    archive = Path(source_queue)
    state_file = Path(state_path) if state_path else state_path_for_archive(archive)
    try:
        state = json.loads(state_file.read_text(encoding="utf-8"))
    except Exception:
        state = {
            "batch_id": validation_report.get("batch_id") or archive.stem,
            "archive_path": str(archive),
            "lifecycle_status": "OPEN",
            "status": "OPEN",
            "opened_at": validation_report.get("generated_at") or _now(),
            "closed_at": None,
        }
    counts = summarize_validation(validation_report)
    lifecycle, close_reason = lifecycle_from_counts(counts)
    now = _now()
    state.update(counts)
    state.update({
        "batch_id": state.get("batch_id") or validation_report.get("batch_id") or archive.stem,
        "archive_path": state.get("archive_path") or str(archive),
        "lifecycle_status": lifecycle,
        "status": lifecycle,
        "opened_at": state.get("opened_at") or validation_report.get("generated_at") or now,
        "updated_at": now,
        "closed_at": now if lifecycle in CLOSED_STATUSES else None,
        "close_reason": close_reason,
        "validation_report_path": str(validation_report_path),
        "interval": validation_report.get("interval") or state.get("interval"),
    })
    atomic_write_json(state_file, state)
    update_registry(state, registry_path_for_state(state_file))
    return state


def load_active_batch_archives(reports_dir: str | Path = "reports") -> list[Path]:
    batch_dir = Path(reports_dir) / "candidate_batches"
    archives: dict[str, Path] = {}
    registry_path = batch_dir / "registry.json"
    try:
        registry = json.loads(registry_path.read_text(encoding="utf-8"))
        for entry in registry.get("batches", []) or []:
            status = entry.get("lifecycle_status") or entry.get("status")
            if status in ACTIVE_STATUSES and entry.get("archive_path"):
                archives[str(entry["archive_path"])] = Path(entry["archive_path"])
    except Exception:
        pass
    if batch_dir.exists():
        for state_file in batch_dir.glob("*.state.json"):
            try:
                state = json.loads(state_file.read_text(encoding="utf-8"))
            except Exception:
                continue
            status = state.get("lifecycle_status") or state.get("status")
            if status in ACTIVE_STATUSES and state.get("archive_path"):
                archives[str(state["archive_path"])] = Path(state["archive_path"])
    return sorted(archives.values(), key=lambda p: str(p))

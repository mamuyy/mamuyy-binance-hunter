import csv
import json
import os
import sqlite3
import time
import traceback
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Dict, List

from database import insert_runtime_heartbeat


SCHEDULER_PROFILES = {
    "FAST": {"scanner": 60, "regime": 60, "flow": 120, "ML": 3600, "walkforward": 7200, "portfolio": 300, "execution": 300, "shadow": 120},
    "NORMAL": {"scanner": 300, "regime": 300, "flow": 300, "ML": 3600, "walkforward": 14400, "portfolio": 900, "execution": 900, "shadow": 300},
    "SAFE": {"scanner": 900, "regime": 900, "flow": 900, "ML": 21600, "walkforward": 43200, "portfolio": 1800, "execution": 1800, "shadow": 900},
}

LOG_FIELDS = ["timestamp", "engine", "state", "execution_time", "failure_count", "restart_count", "avg_runtime", "last_success_timestamp", "message"]
PROCESS_START = time.time()
DIAGNOSTICS_LOG_PATH = "logs/orchestrator_diagnostics.log"
DIAGNOSTICS_JSON_PATH = "logs/orchestrator_diagnostics.json"


@dataclass
class EngineRuntime:
    name: str
    callback: Callable[[], Any]
    state: str = "IDLE"
    execution_times: List[float] = field(default_factory=list)
    failure_count: int = 0
    restart_count: int = 0
    last_success_timestamp: str = ""
    message: str = ""

    @property
    def avg_runtime(self) -> float:
        return sum(self.execution_times) / len(self.execution_times) if self.execution_times else 0.0


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _append_log(row: Dict[str, Any], path: str) -> None:
    try:
        exists = os.path.exists(path)
        with open(path, "a", newline="", encoding="utf-8") as csv_file:
            writer = csv.DictWriter(csv_file, fieldnames=LOG_FIELDS)
            if not exists:
                writer.writeheader()
            writer.writerow({field: row.get(field, "") for field in LOG_FIELDS})
    except OSError:
        return


def _read_diagnostic_events(path: str = DIAGNOSTICS_LOG_PATH, limit: int = 250) -> List[Dict[str, Any]]:
    if not os.path.exists(path):
        return []
    events: List[Dict[str, Any]] = []
    try:
        with open(path, encoding="utf-8") as log_file:
            for line in log_file:
                line = line.strip()
                if not line:
                    continue
                try:
                    event = json.loads(line)
                    if isinstance(event, dict):
                        events.append(event)
                except json.JSONDecodeError:
                    continue
    except OSError:
        return []
    return events[-limit:]


def _parse_timestamp(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)
    except ValueError:
        return None


def _crash_count_last_24h(events: List[Dict[str, Any]]) -> int:
    cutoff = datetime.now(timezone.utc) - timedelta(hours=24)
    count = 0
    for event in events:
        timestamp = _parse_timestamp(event.get("timestamp"))
        if timestamp and timestamp >= cutoff and event.get("exception_type"):
            count += 1
    return count


def _write_diagnostics(
    event_type: str,
    cycle: int | None = None,
    cycle_start: str = "",
    cycle_end: str = "",
    cycle_duration_seconds: float = 0.0,
    last_completed_step: str = "",
    exception: BaseException | None = None,
    heartbeat_written: bool | None = None,
    log_path: str = DIAGNOSTICS_LOG_PATH,
    json_path: str = DIAGNOSTICS_JSON_PATH,
) -> None:
    event = {
        "timestamp": _now(),
        "event_type": event_type,
        "cycle": cycle,
        "cycle_start": cycle_start,
        "cycle_end": cycle_end,
        "cycle_duration_seconds": round(float(cycle_duration_seconds or 0.0), 4),
        "last_completed_step": last_completed_step,
        "exception_type": type(exception).__name__ if exception else "",
        "exception_message": str(exception) if exception else "",
        "traceback_summary": "".join(traceback.format_exception_only(type(exception), exception)).strip() if exception else "",
        "heartbeat_written": heartbeat_written,
    }
    if exception:
        event["traceback_summary"] = "".join(traceback.format_exception(exception)).strip()[-4000:]

    try:
        os.makedirs(os.path.dirname(log_path) or ".", exist_ok=True)
        with open(log_path, "a", encoding="utf-8") as log_file:
            log_file.write(json.dumps(event, default=str) + "\n")

        events = _read_diagnostic_events(log_path, limit=100)
        last_error_event = next((item for item in reversed(events) if item.get("exception_type")), {})
        snapshot = {
            "updated_at": _now(),
            "last_event": event,
            "last_cycle_time": cycle_end or cycle_start or event["timestamp"],
            "last_completed_step": last_completed_step,
            "last_error": last_error_event.get("exception_message", ""),
            "last_error_type": last_error_event.get("exception_type", ""),
            "last_error_timestamp": last_error_event.get("timestamp", ""),
            "crash_count_last_24h": _crash_count_last_24h(events),
            "events": events,
        }
        with open(json_path, "w", encoding="utf-8") as json_file:
            json.dump(snapshot, json_file, indent=2, default=str)
    except OSError:
        return


def load_orchestrator_diagnostics(path: str = DIAGNOSTICS_JSON_PATH) -> Dict[str, Any]:
    if not os.path.exists(path):
        return {
            "updated_at": "",
            "last_event": {},
            "last_cycle_time": "",
            "last_completed_step": "",
            "last_error": "",
            "last_error_type": "",
            "crash_count_last_24h": 0,
            "events": [],
        }
    try:
        with open(path, encoding="utf-8") as json_file:
            payload = json.load(json_file)
        return payload if isinstance(payload, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def format_orchestrator_diagnostics(result: Dict[str, Any]) -> str:
    last_event = result.get("last_event") or {}
    return "\n".join(
        [
            "ORCHESTRATOR DIAGNOSTICS",
            f"Updated At: {result.get('updated_at') or '-'}",
            f"Last Cycle Time: {result.get('last_cycle_time') or '-'}",
            f"Last Completed Step: {result.get('last_completed_step') or last_event.get('last_completed_step') or '-'}",
            f"Last Error: {result.get('last_error') or '-'}",
            f"Last Error Type: {result.get('last_error_type') or '-'}",
            f"Crash Count Last 24h: {result.get('crash_count_last_24h', 0)}",
            f"Heartbeat Written: {last_event.get('heartbeat_written')}",
        ]
    )


def _record_runtime_heartbeat(
    db_path: str,
    state: str,
    message: str,
    scheduler: str,
    system_health_score: float | None = None,
    log_path: str = "orchestrator_log.csv",
) -> bool:
    uptime = uptime_seconds()
    try:
        insert_runtime_heartbeat(
            {
                "timestamp": _now(),
                "source": "orchestrator",
                "state": state,
                "system_health_score": system_health_score,
                "scheduler": scheduler,
                "uptime_seconds": uptime,
                "message": message,
            },
            db_path,
        )
        _append_log(
            {
                "timestamp": _now(),
                "engine": "heartbeat_db",
                "state": "IDLE",
                "execution_time": 0,
                "failure_count": 0,
                "restart_count": 0,
                "avg_runtime": 0,
                "last_success_timestamp": _now(),
                "message": "runtime_heartbeats write success",
            },
            log_path,
        )
        return True
    except Exception as exc:
        _append_log(
            {
                "timestamp": _now(),
                "engine": "heartbeat_db",
                "state": "FAILED",
                "execution_time": 0,
                "failure_count": 1,
                "restart_count": 0,
                "avg_runtime": 0,
                "last_success_timestamp": "",
                "message": f"runtime_heartbeats write failed: {exc}",
            },
            log_path,
        )
        return False


def rotate_log_if_needed(path: str, max_bytes: int = 5_000_000) -> str:
    try:
        if not os.path.exists(path):
            return ""
        mtime = datetime.fromtimestamp(os.path.getmtime(path)).date()
        today = datetime.now().date()
        if os.path.getsize(path) < max_bytes and mtime == today:
            return ""
        rotated = f"{path}.{datetime.now().strftime('%Y%m%d_%H%M%S')}"
        os.replace(path, rotated)
        return rotated
    except OSError:
        return ""


def cleanup_old_files(paths: List[str], retention_days: int = 14) -> int:
    cutoff = time.time() - retention_days * 86400
    removed = 0
    for path in paths:
        if os.path.isdir(path):
            try:
                names = os.listdir(path)
            except OSError:
                continue
            for name in names:
                full_path = os.path.join(path, name)
                try:
                    if os.path.isfile(full_path) and os.path.getmtime(full_path) < cutoff:
                        os.remove(full_path)
                        removed += 1
                except OSError:
                    continue
        else:
            try:
                if os.path.isfile(path) and os.path.getmtime(path) < cutoff:
                    os.remove(path)
                    removed += 1
            except OSError:
                continue
    return removed


def cleanup_old_db_records(db_path: str, retention_days: int = 90) -> int:
    cutoff = (datetime.now(timezone.utc) - timedelta(days=retention_days)).isoformat()
    tables = ["ml_results", "walkforward_results", "shadow_trades"]
    deleted = 0
    try:
        with sqlite3.connect(db_path) as connection:
            for table in tables:
                try:
                    cursor = connection.execute(
                        f"DELETE FROM {table} WHERE timestamp IS NOT NULL AND timestamp < ?",
                        (cutoff,),
                    )
                    deleted += cursor.rowcount if cursor.rowcount else 0
                except sqlite3.Error:
                    continue
            connection.commit()
    except sqlite3.Error:
        return deleted
    return deleted


def _db_latency(db_path: str) -> float:
    start = time.perf_counter()
    try:
        with sqlite3.connect(db_path) as connection:
            connection.execute("SELECT 1").fetchone()
    except Exception:
        return 999.0
    return time.perf_counter() - start


def _memory_warning() -> bool:
    try:
        import resource
        return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss > 1_500_000
    except Exception:
        return False


def uptime_seconds() -> int:
    return int(time.time() - PROCESS_START)


def _degrade_profile(profile: str, db_slow: bool, failures_high: bool, memory_high: bool) -> str:
    if not (db_slow or failures_high or memory_high):
        return profile
    return "NORMAL" if profile == "FAST" else "SAFE"


def _health_score(engines: Dict[str, EngineRuntime], db_seconds: float, memory_high: bool) -> int:
    failed = sum(1 for engine in engines.values() if engine.state == "FAILED")
    warnings = sum(1 for engine in engines.values() if engine.state in {"WARNING", "RECOVERING"})
    failures = sum(engine.failure_count for engine in engines.values())
    score = 100 - failed * 18 - warnings * 8 - min(30, failures * 4)
    if db_seconds > 0.5:
        score -= 15
    if memory_high:
        score -= 15
    return max(0, min(100, score))


def _run_engine(engine: EngineRuntime, retries: int, log_path: str) -> None:
    engine.state = "RUNNING"
    start = time.perf_counter()
    try:
        engine.callback()
        elapsed = time.perf_counter() - start
        engine.execution_times.append(elapsed)
        engine.state = "IDLE"
        engine.last_success_timestamp = _now()
        engine.message = "success"
    except Exception as exc:
        engine.failure_count += 1
        engine.state = "RECOVERING" if engine.failure_count <= retries else "FAILED"
        engine.message = str(exc)
        if engine.failure_count <= retries:
            engine.restart_count += 1
            try:
                retry_start = time.perf_counter()
                engine.callback()
                elapsed = time.perf_counter() - retry_start
                engine.execution_times.append(elapsed)
                engine.state = "IDLE"
                engine.last_success_timestamp = _now()
                engine.message = "recovered after retry"
            except Exception as retry_exc:
                engine.state = "FAILED"
                engine.message = f"retry failed: {retry_exc}"
    _append_log(
        {
            "timestamp": _now(),
            "engine": engine.name,
            "state": engine.state,
            "execution_time": engine.execution_times[-1] if engine.execution_times else 0,
            "failure_count": engine.failure_count,
            "restart_count": engine.restart_count,
            "avg_runtime": engine.avg_runtime,
            "last_success_timestamp": engine.last_success_timestamp,
            "message": engine.message,
        },
        log_path,
    )


def run_orchestrator(
    callbacks: Dict[str, Callable[[], Any]],
    profile: str = "NORMAL",
    db_path: str = "mamuyy_hunter.db",
    log_path: str = "orchestrator_log.csv",
    cycles: int = 1,
    retries: int = 1,
    retention_days: int = 14,
    db_retention_days: int = 90,
    max_log_bytes: int = 5_000_000,
) -> Dict[str, Any]:
    scheduler = profile if profile in SCHEDULER_PROFILES else "NORMAL"
    _write_diagnostics("orchestrator_start", last_completed_step="startup_begin")
    startup_heartbeat_written = _record_runtime_heartbeat(
        db_path,
        "STARTING",
        f"orchestrator_startup;uptime={uptime_seconds()}s",
        scheduler,
        log_path=log_path,
    )
    _write_diagnostics(
        "orchestrator_startup_heartbeat",
        last_completed_step="startup_heartbeat",
        heartbeat_written=startup_heartbeat_written,
    )
    rotated_log = rotate_log_if_needed(log_path, max_log_bytes)
    cleanup_count = cleanup_old_files(["charts", "db_backups"], retention_days)
    db_deleted = cleanup_old_db_records(db_path, db_retention_days)
    _write_diagnostics("orchestrator_startup_cleanup", last_completed_step="startup_cleanup")
    engines = {name: EngineRuntime(name=name, callback=callback) for name, callback in callbacks.items()}
    db_seconds = _db_latency(db_path)
    memory_high = _memory_warning()
    scheduler = _degrade_profile(scheduler, db_seconds > 0.5, False, memory_high)
    intervals = SCHEDULER_PROFILES[scheduler]
    recovery_actions = []

    for cycle in range(max(1, cycles)):
        cycle_number = cycle + 1
        cycle_start_time = time.perf_counter()
        cycle_start_timestamp = _now()
        last_completed_step = "cycle_start"
        heartbeat_written = False
        _write_diagnostics(
            "cycle_start",
            cycle=cycle_number,
            cycle_start=cycle_start_timestamp,
            last_completed_step=last_completed_step,
        )
        try:
            heartbeat_written = _record_runtime_heartbeat(
                db_path,
                "RUNNING",
                f"cycle={cycle_number};phase=before;uptime={uptime_seconds()}s",
                scheduler,
                log_path=log_path,
            )
            last_completed_step = "cycle_start_heartbeat"
            _write_diagnostics(
                "cycle_start_heartbeat",
                cycle=cycle_number,
                cycle_start=cycle_start_timestamp,
                last_completed_step=last_completed_step,
                heartbeat_written=heartbeat_written,
            )
            for name, engine in engines.items():
                if name not in intervals:
                    engine.state = "WARNING"
                    engine.message = "not scheduled in active profile"
                    last_completed_step = f"{name}:not_scheduled"
                    continue
                _write_diagnostics(
                    "engine_start",
                    cycle=cycle_number,
                    cycle_start=cycle_start_timestamp,
                    last_completed_step=f"{name}:start",
                )
                _run_engine(engine, retries, log_path)
                last_completed_step = f"{name}:{engine.state}"
                _write_diagnostics(
                    "engine_end",
                    cycle=cycle_number,
                    cycle_start=cycle_start_timestamp,
                    cycle_duration_seconds=time.perf_counter() - cycle_start_time,
                    last_completed_step=last_completed_step,
                    heartbeat_written=heartbeat_written,
                )
                if engine.restart_count:
                    recovery_actions.append(f"{name}: retry/restart simulation")
            _append_log(
                {
                    "timestamp": _now(),
                    "engine": "heartbeat",
                    "state": "IDLE",
                    "execution_time": db_seconds,
                    "failure_count": sum(engine.failure_count for engine in engines.values()),
                    "restart_count": sum(engine.restart_count for engine in engines.values()),
                    "avg_runtime": sum(engine.avg_runtime for engine in engines.values()) / max(len(engines), 1),
                    "last_success_timestamp": _now(),
                    "message": f"cycle={cycle_number};uptime={uptime_seconds()}s;rotated={rotated_log or '-'};cleanup_files={cleanup_count};db_deleted={db_deleted}",
                },
                log_path,
            )
            last_completed_step = "cycle_csv_heartbeat"
            heartbeat_written = _record_runtime_heartbeat(
                db_path,
                "IDLE",
                f"cycle={cycle_number};uptime={uptime_seconds()}s;rotated={rotated_log or '-'};cleanup_files={cleanup_count};db_deleted={db_deleted}",
                scheduler,
                log_path=log_path,
            )
            cycle_end_timestamp = _now()
            last_completed_step = "cycle_end_heartbeat"
            _write_diagnostics(
                "cycle_end",
                cycle=cycle_number,
                cycle_start=cycle_start_timestamp,
                cycle_end=cycle_end_timestamp,
                cycle_duration_seconds=time.perf_counter() - cycle_start_time,
                last_completed_step=last_completed_step,
                heartbeat_written=heartbeat_written,
            )
        except Exception as exc:
            _write_diagnostics(
                "cycle_exception",
                cycle=cycle_number,
                cycle_start=cycle_start_timestamp,
                cycle_end=_now(),
                cycle_duration_seconds=time.perf_counter() - cycle_start_time,
                last_completed_step=last_completed_step,
                exception=exc,
                heartbeat_written=heartbeat_written,
            )
            raise

    failures_high = sum(engine.failure_count for engine in engines.values()) >= 3
    scheduler = _degrade_profile(scheduler, db_seconds > 0.5, failures_high, memory_high)
    health = _health_score(engines, db_seconds, memory_high)
    running = [name for name, engine in engines.items() if engine.state == "RUNNING"]
    failed = [name for name, engine in engines.items() if engine.state == "FAILED"]

    _append_log(
        {
            "timestamp": _now(),
            "engine": "heartbeat",
            "state": "WARNING" if failed else "IDLE",
            "execution_time": db_seconds,
            "failure_count": sum(engine.failure_count for engine in engines.values()),
            "restart_count": sum(engine.restart_count for engine in engines.values()),
            "avg_runtime": sum(engine.avg_runtime for engine in engines.values()) / max(len(engines), 1),
            "last_success_timestamp": _now(),
            "message": f"system_health_score={health};scheduler={scheduler};uptime={uptime_seconds()}s",
        },
        log_path,
    )
    _record_runtime_heartbeat(
        db_path,
        "WARNING" if failed else "IDLE",
        f"system_health_score={health};scheduler={scheduler};uptime={uptime_seconds()}s",
        scheduler,
        health,
        log_path=log_path,
    )
    return {
        "system_health_score": health,
        "running_engines": running,
        "failed_engines": failed,
        "recovery_actions": recovery_actions or ["none"],
        "scheduler_mode": scheduler,
        "uptime_seconds": uptime_seconds(),
        "cleanup": {"rotated_log": rotated_log, "files_removed": cleanup_count, "db_records_deleted": db_deleted},
        "engine_states": {name: engine.state for name, engine in engines.items()},
        "runtime_metrics": {
            name: {
                "execution_time": engine.execution_times[-1] if engine.execution_times else 0,
                "failure_count": engine.failure_count,
                "restart_count": engine.restart_count,
                "avg_runtime": engine.avg_runtime,
                "last_success_timestamp": engine.last_success_timestamp,
            }
            for name, engine in engines.items()
        },
    }

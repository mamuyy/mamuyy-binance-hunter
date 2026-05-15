import csv
import os
import sqlite3
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List


SCHEDULER_PROFILES = {
    "FAST": {"scanner": 60, "regime": 60, "flow": 120, "ML": 3600, "walkforward": 7200, "portfolio": 300, "execution": 300, "shadow": 120},
    "NORMAL": {"scanner": 300, "regime": 300, "flow": 300, "ML": 3600, "walkforward": 14400, "portfolio": 900, "execution": 900, "shadow": 300},
    "SAFE": {"scanner": 900, "regime": 900, "flow": 900, "ML": 21600, "walkforward": 43200, "portfolio": 1800, "execution": 1800, "shadow": 900},
}

LOG_FIELDS = ["timestamp", "engine", "state", "execution_time", "failure_count", "restart_count", "avg_runtime", "last_success_timestamp", "message"]


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
    exists = os.path.exists(path)
    with open(path, "a", newline="", encoding="utf-8") as csv_file:
        writer = csv.DictWriter(csv_file, fieldnames=LOG_FIELDS)
        if not exists:
            writer.writeheader()
        writer.writerow({field: row.get(field, "") for field in LOG_FIELDS})


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
) -> Dict[str, Any]:
    scheduler = profile if profile in SCHEDULER_PROFILES else "NORMAL"
    engines = {name: EngineRuntime(name=name, callback=callback) for name, callback in callbacks.items()}
    db_seconds = _db_latency(db_path)
    memory_high = _memory_warning()
    scheduler = _degrade_profile(scheduler, db_seconds > 0.5, False, memory_high)
    intervals = SCHEDULER_PROFILES[scheduler]
    recovery_actions = []

    for _ in range(max(1, cycles)):
        for name, engine in engines.items():
            if name not in intervals:
                engine.state = "WARNING"
                engine.message = "not scheduled in active profile"
                continue
            _run_engine(engine, retries, log_path)
            if engine.restart_count:
                recovery_actions.append(f"{name}: retry/restart simulation")

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
            "message": f"system_health_score={health};scheduler={scheduler}",
        },
        log_path,
    )
    return {
        "system_health_score": health,
        "running_engines": running,
        "failed_engines": failed,
        "recovery_actions": recovery_actions or ["none"],
        "scheduler_mode": scheduler,
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

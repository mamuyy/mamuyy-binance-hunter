import csv
import json
import os
import shlex
import shutil
import sqlite3
import subprocess
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, List

from database import db_health_check, init_db


@dataclass(frozen=True)
class HealthGuardianConfig:
    database_path: str = "mamuyy_hunter.db"
    orchestrator_log_path: str = "orchestrator_log.csv"
    project_dir: str = "~/mamuyy-binance-hunter"
    hunter_session: str = "hunter"
    dashboard_session: str = "dashboard"
    stale_minutes: int = 10
    interval_seconds: int = 300
    dry_run: bool = True
    restart_dashboard: bool = False
    restart_cooldown_seconds: int = 300


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


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


def _latest_heartbeat(log_path: str) -> Dict[str, Any]:
    latest: Dict[str, Any] = {"timestamp": "", "age_minutes": 9999.0, "message": "", "source": "orchestrator_log"}
    if not os.path.exists(log_path):
        return latest

    try:
        with open(log_path, newline="", encoding="utf-8") as log_file:
            for row in csv.DictReader(log_file):
                if row.get("engine") == "heartbeat":
                    latest["timestamp"] = row.get("timestamp", "")
                    latest["message"] = row.get("message", "")
    except OSError:
        return latest

    timestamp = _parse_timestamp(latest["timestamp"])
    if timestamp:
        latest["age_minutes"] = round((datetime.now(timezone.utc) - timestamp).total_seconds() / 60, 2)
    return latest


def _age_minutes(timestamp_text: str) -> float:
    timestamp = _parse_timestamp(timestamp_text)
    if not timestamp:
        return 9999.0
    return round((datetime.now(timezone.utc) - timestamp).total_seconds() / 60, 2)


def _latest_db_heartbeat(database_path: str) -> Dict[str, Any]:
    latest: Dict[str, Any] = {"timestamp": "", "age_minutes": 9999.0, "message": "", "source": "heartbeat_table"}
    try:
        init_db(database_path)
        with sqlite3.connect(database_path) as connection:
            connection.row_factory = sqlite3.Row
            row = connection.execute(
                """
                SELECT timestamp, message
                FROM runtime_heartbeats
                ORDER BY id DESC
                LIMIT 1
                """
            ).fetchone()
    except sqlite3.Error:
        row = None
    if not row:
        return latest
    latest["timestamp"] = row["timestamp"] or ""
    latest["message"] = row["message"] or ""
    latest["age_minutes"] = _age_minutes(latest["timestamp"])
    return latest


def _latest_activity(database_path: str, table: str, source: str) -> Dict[str, Any]:
    latest: Dict[str, Any] = {"timestamp": "", "age_minutes": 9999.0, "message": "", "source": source}
    try:
        init_db(database_path)
        with sqlite3.connect(database_path) as connection:
            connection.row_factory = sqlite3.Row
            row = connection.execute(
                f"SELECT timestamp FROM {table} ORDER BY id DESC LIMIT 1"
            ).fetchone()
    except sqlite3.Error:
        row = None
    if not row:
        return latest
    latest["timestamp"] = row["timestamp"] or ""
    latest["message"] = f"fallback activity from {table}"
    latest["age_minutes"] = _age_minutes(latest["timestamp"])
    return latest


def resolve_runtime_heartbeat(
    database_path: str,
    log_path: str,
    stale_minutes: int = 10,
) -> Dict[str, Any]:
    db_heartbeat = _latest_db_heartbeat(database_path)
    if db_heartbeat["timestamp"] and db_heartbeat["age_minutes"] <= stale_minutes:
        return db_heartbeat

    log_heartbeat = _latest_heartbeat(log_path)
    if log_heartbeat["timestamp"] and log_heartbeat["age_minutes"] <= stale_minutes:
        return log_heartbeat

    primary = db_heartbeat if db_heartbeat["timestamp"] else log_heartbeat
    primary_missing = not primary["timestamp"]
    primary_stale = primary["age_minutes"] > stale_minutes

    if primary_missing or primary_stale:
        fallback_candidates = [
            _latest_activity(database_path, "flow_logs", "fallback_flow_logs"),
            _latest_activity(database_path, "regime_logs", "fallback_regime_logs"),
        ]
        recent_fallbacks = [
            item for item in fallback_candidates if item["timestamp"] and item["age_minutes"] <= stale_minutes
        ]
        if recent_fallbacks:
            return min(recent_fallbacks, key=lambda item: item["age_minutes"])

    return primary


def _tmux_available() -> bool:
    return shutil.which("tmux") is not None


def _tmux_session_exists(session_name: str) -> bool:
    if not _tmux_available():
        return False
    target = session_name.strip()
    try:
        completed = subprocess.run(
            ["tmux", "ls"],
            check=False,
            capture_output=True,
            text=True,
            timeout=5,
        )
        if completed.returncode != 0:
            return False
        for line in completed.stdout.splitlines():
            name = line.split(":", 1)[0].strip()
            if name == target:
                return True
        return False
    except (subprocess.SubprocessError, OSError):
        return False


def _start_tmux_session(session_name: str, command: str, dry_run: bool) -> Dict[str, Any]:
    if dry_run:
        return {"attempted": False, "started": False, "detail": f"DRY_RUN would start tmux session {session_name}"}
    if not _tmux_available():
        return {"attempted": False, "started": False, "detail": "tmux not available"}
    try:
        completed = subprocess.run(
            ["tmux", "new-session", "-d", "-s", session_name, "bash", "-lc", command],
            check=False,
            capture_output=True,
            text=True,
            timeout=10,
        )
        return {
            "attempted": True,
            "started": completed.returncode == 0,
            "detail": completed.stderr.strip() or completed.stdout.strip() or "started",
        }
    except (subprocess.SubprocessError, OSError) as exc:
        return {"attempted": True, "started": False, "detail": str(exc)}


def _guardian_command(project_dir: str, app_command: str) -> str:
    safe_project_dir = os.path.expanduser(project_dir or "~/mamuyy-binance-hunter")
    return f"cd {shlex.quote(safe_project_dir)} && . .venv/bin/activate && {app_command}"


def _recovery_cooldown_active(database_path: str, session_name: str, cooldown_seconds: int) -> bool:
    try:
        init_db(database_path)
        with sqlite3.connect(database_path) as connection:
            connection.row_factory = sqlite3.Row
            row = connection.execute(
                """
                SELECT timestamp
                FROM risk_events
                WHERE session_name = ?
                  AND action = 'start_tmux_session'
                  AND dry_run = 0
                ORDER BY id DESC
                LIMIT 1
                """,
                (session_name,),
            ).fetchone()
    except sqlite3.Error:
        return False
    if not row or not row["timestamp"]:
        return False
    timestamp = _parse_timestamp(row["timestamp"])
    if not timestamp:
        return False
    age_seconds = (datetime.now(timezone.utc) - timestamp).total_seconds()
    return age_seconds < cooldown_seconds


def _log_recovery_action(
    database_path: str,
    session_name: str,
    action: str,
    result: str,
    dry_run: bool,
    reason: str,
) -> None:
    init_db(database_path)
    with sqlite3.connect(database_path) as connection:
        connection.execute(
            """
            INSERT INTO risk_events (
                timestamp,
                status,
                safe,
                risk_score,
                position_multiplier,
                reasons_json,
                regime_name,
                session_name,
                action,
                result,
                dry_run,
                reason
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                _now_iso(),
                "WATCH" if dry_run else ("SAFE" if result == "started" else "HALT"),
                1 if result in {"started", "dry_run", "cooldown"} else 0,
                50,
                1.0,
                json.dumps([reason, result]),
                "HEALTH_GUARDIAN",
                session_name,
                action,
                result,
                int(dry_run),
                reason,
            ),
        )
        connection.commit()


def _recover_tmux_session(
    config: HealthGuardianConfig,
    session_name: str,
    command: str,
    reason: str,
) -> str:
    if not config.dry_run and _recovery_cooldown_active(
        config.database_path,
        session_name,
        config.restart_cooldown_seconds,
    ):
        detail = f"restart cooldown active for tmux session {session_name}"
        _log_recovery_action(
            config.database_path,
            session_name,
            "start_tmux_session",
            "cooldown",
            config.dry_run,
            reason,
        )
        return detail

    start_result = _start_tmux_session(session_name, command, config.dry_run)
    result = "dry_run" if config.dry_run else "started" if start_result.get("started") else "failed"
    _log_recovery_action(
        config.database_path,
        session_name,
        "start_tmux_session",
        result,
        config.dry_run,
        reason,
    )
    return start_result["detail"]


def _log_guardian_event(
    database_path: str,
    status: str,
    reasons: List[str],
    heartbeat_age_minutes: float,
) -> None:
    init_db(database_path)
    with sqlite3.connect(database_path) as connection:
        connection.execute(
            """
            INSERT INTO risk_events (
                timestamp,
                status,
                safe,
                risk_score,
                position_multiplier,
                reasons_json,
                ml_accuracy,
                model_confidence,
                drawdown,
                regime_name,
                heartbeat_age_minutes,
                open_trades,
                consecutive_losses,
                reason
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                _now_iso(),
                status,
                0 if status == "HALT" else 1,
                0 if status == "HALT" else 50,
                0.0 if status == "HALT" else 1.0,
                json.dumps(reasons),
                None,
                None,
                None,
                "HEALTH_GUARDIAN",
                heartbeat_age_minutes,
                None,
                None,
                "; ".join(reasons),
            ),
        )
        connection.commit()


def check_health_guardian_once(config: HealthGuardianConfig | None = None) -> Dict[str, Any]:
    guardian_config = config or HealthGuardianConfig()
    project_dir = guardian_config.project_dir or os.getcwd()
    reasons: List[str] = []
    recovery_actions: List[str] = []

    db_health = db_health_check(
        database_url=guardian_config.database_path,
        migrate_csv=False,
        backup=False,
    )
    if not db_health.get("ok"):
        reasons.append("SQLite health check failed")

    heartbeat = resolve_runtime_heartbeat(
        guardian_config.database_path,
        guardian_config.orchestrator_log_path,
        guardian_config.stale_minutes,
    )
    heartbeat_stale = heartbeat["age_minutes"] > guardian_config.stale_minutes
    if heartbeat_stale:
        reasons.append(
            f"Runtime heartbeat stale for {heartbeat['age_minutes']:.1f} minutes"
        )
    elif str(heartbeat.get("source", "")).startswith("fallback_"):
        reasons.append(f"Heartbeat table missing/stale; using {heartbeat['source']}")

    hunter_exists = _tmux_session_exists(guardian_config.hunter_session)
    dashboard_exists = _tmux_session_exists(guardian_config.dashboard_session)
    tmux_available = _tmux_available()

    if not tmux_available:
        reasons.append("tmux not available in current environment")

    if not hunter_exists:
        reason = f"tmux session missing: {guardian_config.hunter_session}"
        reasons.append(reason)
        detail = _recover_tmux_session(
            guardian_config,
            guardian_config.hunter_session,
            _guardian_command(project_dir, "python main.py --orchestrator"),
            reason,
        )
        recovery_actions.append(detail)

    if not dashboard_exists:
        reason = f"tmux session missing: {guardian_config.dashboard_session}"
        reasons.append(reason)
        if guardian_config.dry_run and not guardian_config.restart_dashboard:
            recovery_actions.append("dashboard restart disabled; warning logged only")
            _log_recovery_action(
                guardian_config.database_path,
                guardian_config.dashboard_session,
                "start_tmux_session",
                "disabled",
                guardian_config.dry_run,
                reason,
            )
        else:
            detail = _recover_tmux_session(
                guardian_config,
                guardian_config.dashboard_session,
                _guardian_command(
                    project_dir,
                    "streamlit run dashboard.py --server.address 127.0.0.1 --server.port 8501",
                ),
                reason,
            )
            recovery_actions.append(detail)

    status = "HALT" if heartbeat_stale or not db_health.get("ok") else "WATCH" if reasons else "SAFE"
    if reasons:
        _log_guardian_event(
            guardian_config.database_path,
            status,
            reasons + recovery_actions,
            heartbeat["age_minutes"],
        )

    result = {
        "ok": status != "HALT",
        "status": status,
        "dry_run": guardian_config.dry_run,
        "db_ok": bool(db_health.get("ok")),
        "heartbeat_timestamp": heartbeat["timestamp"] or "-",
        "heartbeat_age_minutes": heartbeat["age_minutes"],
        "heartbeat_source": heartbeat.get("source", "-"),
        "tmux_available": tmux_available,
        "hunter_session": "RUNNING" if hunter_exists else "MISSING",
        "dashboard_session": "RUNNING" if dashboard_exists else "MISSING",
        "recovery_actions": recovery_actions or ["none"],
        "reasons": reasons or ["none"],
    }
    return result


def run_health_guardian_loop(config: HealthGuardianConfig | None = None) -> None:
    guardian_config = config or HealthGuardianConfig()
    while True:
        result = check_health_guardian_once(guardian_config)
        print(format_health_guardian_result(result))
        time.sleep(max(30, guardian_config.interval_seconds))


def format_health_guardian_result(result: Dict[str, Any]) -> str:
    return "\n".join(
        [
            "HEALTH GUARDIAN",
            f"Status: {result.get('status')}",
            f"Dry Run: {result.get('dry_run')}",
            f"DB OK: {result.get('db_ok')}",
            f"Heartbeat: {result.get('heartbeat_timestamp')} ({result.get('heartbeat_age_minutes')}m)",
            f"Heartbeat Source: {result.get('heartbeat_source')}",
            f"tmux Available: {result.get('tmux_available')}",
            f"Hunter Session: {result.get('hunter_session')}",
            f"Dashboard Session: {result.get('dashboard_session')}",
            f"Recovery Actions: {result.get('recovery_actions')}",
            f"Reasons: {result.get('reasons')}",
        ]
    )

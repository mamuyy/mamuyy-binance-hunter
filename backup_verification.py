from __future__ import annotations

import json
import os
import sqlite3
from datetime import datetime, timezone
from glob import glob
from pathlib import Path
from typing import Any, Dict, List

from config import config

REPORT_PATH = "reports/backup_verification.json"
BACKUP_EXTENSIONS = ("*.db", "*.sqlite", "*.sqlite3")


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _age_hours(path: str) -> float | None:
    try:
        modified = datetime.fromtimestamp(os.path.getmtime(path), timezone.utc)
        return round((datetime.now(timezone.utc) - modified).total_seconds() / 3600, 4)
    except OSError:
        return None


def _sqlite_integrity(path: str) -> Dict[str, Any]:
    if not os.path.exists(path):
        return {"exists": False, "integrity": "missing", "ok": False, "error": "file not found"}
    if os.path.getsize(path) <= 0:
        return {"exists": True, "integrity": "empty", "ok": False, "error": "file is empty"}
    try:
        uri = f"file:{Path(path).resolve()}?mode=ro"
        with sqlite3.connect(uri, uri=True, timeout=30) as connection:
            integrity = connection.execute("PRAGMA integrity_check").fetchone()[0]
        return {"exists": True, "integrity": integrity, "ok": integrity == "ok", "error": ""}
    except sqlite3.Error as exc:
        return {"exists": True, "integrity": "error", "ok": False, "error": str(exc)}


def _backup_candidates(backup_dir: str) -> List[str]:
    if not os.path.isdir(backup_dir):
        return []
    candidates: List[str] = []
    for pattern in BACKUP_EXTENSIONS:
        candidates.extend(glob(os.path.join(backup_dir, pattern)))
    return sorted(set(candidates), key=os.path.getmtime, reverse=True)


def generate_backup_verification(
    db_path: str = "mamuyy_hunter.db",
    backup_dir: str = "db_backups",
    output_path: str = REPORT_PATH,
    write_report: bool = True,
) -> Dict[str, Any]:
    """Verify SQLite DB and backup evidence without creating or mutating backups."""
    db_status = _sqlite_integrity(db_path)
    backups = _backup_candidates(backup_dir)
    latest_backup = backups[0] if backups else ""
    backup_status = _sqlite_integrity(latest_backup) if latest_backup else {
        "exists": False,
        "integrity": "missing",
        "ok": False,
        "error": "no backup candidate found",
    }
    latest_backup_age_hours = _age_hours(latest_backup) if latest_backup else None
    backup_evidence_exists = bool(latest_backup and backup_status.get("exists"))
    valid = bool(db_status.get("ok") and backup_evidence_exists and backup_status.get("ok"))
    verdict = "PASS" if valid else "FAIL"
    blockers: List[str] = []
    if not db_status.get("ok"):
        blockers.append("Primary SQLite database is missing or failed integrity_check.")
    if not backup_evidence_exists:
        blockers.append("No SQLite backup artifact found in configured backup directory.")
    elif not backup_status.get("ok"):
        blockers.append("Latest backup artifact failed SQLite integrity_check.")

    report: Dict[str, Any] = {
        "generated_at": _now_iso(),
        "mode": "READ_ONLY_PAPER_ONLY_BACKUP_VERIFICATION",
        "paper_only": True,
        "read_only": True,
        "verdict": verdict,
        "valid": valid,
        "database": {
            "path": db_path,
            "exists": db_status.get("exists", False),
            "size_bytes": os.path.getsize(db_path) if os.path.exists(db_path) else 0,
            "integrity": db_status.get("integrity"),
            "integrity_ok": db_status.get("ok", False),
            "error": db_status.get("error", ""),
        },
        "backup": {
            "directory": backup_dir,
            "directory_exists": os.path.isdir(backup_dir),
            "candidate_count": len(backups),
            "latest_path": latest_backup,
            "latest_exists": backup_status.get("exists", False),
            "latest_size_bytes": os.path.getsize(latest_backup) if latest_backup and os.path.exists(latest_backup) else 0,
            "latest_age_hours": latest_backup_age_hours,
            "latest_integrity": backup_status.get("integrity"),
            "latest_integrity_ok": backup_status.get("ok", False),
            "error": backup_status.get("error", ""),
        },
        "blockers": blockers,
        "safety": [
            "PAPER_ONLY enforced",
            "Read-only SQLite integrity checks only",
            "No backup creation or database migration",
            "No broker routing or order placement",
        ],
    }
    if write_report:
        Path(output_path).parent.mkdir(parents=True, exist_ok=True)
        with open(output_path, "w", encoding="utf-8") as file:
            json.dump(report, file, indent=2, sort_keys=True)
            file.write("\n")
    return report


def format_backup_verification(report: Dict[str, Any]) -> str:
    backup = report.get("backup", {}) if isinstance(report.get("backup"), dict) else {}
    database = report.get("database", {}) if isinstance(report.get("database"), dict) else {}
    return (
        "BACKUP VERIFICATION\n"
        f"Verdict: {report.get('verdict', 'FAIL')}\n"
        f"Database Integrity: {database.get('integrity')}\n"
        f"Backup Candidates: {backup.get('candidate_count', 0)}\n"
        f"Latest Backup: {backup.get('latest_path') or 'missing'}\n"
        f"Latest Backup Integrity: {backup.get('latest_integrity')}\n"
        "PAPER_ONLY read-only verification. No DB mutation or backup creation performed."
    )


if __name__ == "__main__":
    result = generate_backup_verification(
        db_path=config.database_path,
        backup_dir=config.database_backup_dir,
        output_path=REPORT_PATH,
    )
    print(format_backup_verification(result))
    print(f"Report generated: {REPORT_PATH}")

#!/usr/bin/env python3
"""
sqlite_housekeeping_audit.py — Read-only SQLite governance and disk audit.

Never writes to the main DB. Never enables WAL. Never runs VACUUM.
If the live DB is locked, falls back to auditing a /tmp copy.

Outputs:
  reports/sqlite_housekeeping_audit.json
  Console summary
"""
import json
import os
import shutil
import sqlite3
import subprocess
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).parent.parent
DB_PATH = REPO / "mamuyy_hunter.db"
REPORT_PATH = REPO / "reports" / "sqlite_housekeeping_audit.json"
TMP_COPY = Path("/tmp/hunter_sqlite_audit.db")

SAFE_PRAGMAS = [
    "journal_mode",
    "busy_timeout",
    "page_count",
    "page_size",
    "freelist_count",
]


def utcnow() -> str:
    return datetime.now(tz=timezone.utc).isoformat()


def _pragma(conn: sqlite3.Connection, name: str):
    try:
        row = conn.execute(f"PRAGMA {name}").fetchone()
        return row[0] if row else None
    except Exception as e:
        return f"ERROR: {e}"


def _quick_check(conn: sqlite3.Connection) -> str:
    try:
        rows = conn.execute("PRAGMA quick_check").fetchall()
        return "; ".join(r[0] for r in rows[:5])
    except Exception as e:
        return f"ERROR: {e}"


def _integrity_check(conn: sqlite3.Connection) -> str:
    try:
        rows = conn.execute("PRAGMA integrity_check").fetchall()
        return "; ".join(r[0] for r in rows[:5])
    except Exception as e:
        return f"ERROR: {e}"


def _open_ro(path: Path) -> tuple[sqlite3.Connection | None, str]:
    """Try read-only URI. Returns (conn, source_label)."""
    uri = f"file:{path.resolve()}?mode=ro"
    try:
        conn = sqlite3.connect(uri, uri=True, timeout=5)
        conn.execute("SELECT 1").fetchone()  # probe
        return conn, "live"
    except Exception:
        return None, ""


def _dir_size_bytes(path: Path) -> int:
    total = 0
    try:
        for p in path.rglob("*"):
            if p.is_file():
                try:
                    total += p.stat().st_size
                except OSError:
                    pass
    except Exception:
        pass
    return total


def _top_files(path: Path, n: int = 20) -> list[dict]:
    files = []
    try:
        for p in path.rglob("*"):
            if p.is_file():
                try:
                    files.append({"path": str(p.relative_to(REPO)), "bytes": p.stat().st_size})
                except OSError:
                    pass
    except Exception:
        pass
    return sorted(files, key=lambda x: x["bytes"], reverse=True)[:n]


def _run(cmd: list[str]) -> str:
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=5)
        return (r.stdout + r.stderr).strip()
    except Exception as e:
        return f"unavailable: {e}"


def _lock_holders(db_path: Path) -> dict:
    path_str = str(db_path)
    lsof_out = _run(["lsof", path_str])
    fuser_out = _run(["fuser", path_str])
    return {"lsof": lsof_out or "none", "fuser": fuser_out or "none"}


def _recommendation(data: dict) -> tuple[str, list[str]]:
    reasons = []
    level = "PASS"

    jm = data.get("journal_mode", "")
    bt = data.get("busy_timeout", 0)
    disk_pct = data.get("disk_usage_percent", 0)
    freelist = data.get("freelist_count", 0)
    page_size = data.get("page_size", 4096)
    ic = data.get("integrity_check", "")
    qc = data.get("quick_check", "")
    wal_exists = data.get("wal_exists", False)
    logs_bytes = data.get("logs_dir_bytes", 0)
    backup_bytes = data.get("manual_audit_backups_bytes", 0)

    if jm == "delete":
        reasons.append("journal_mode=delete: concurrent readers/writers risk 'database is locked'. WAL recommended.")
        level = "ACTION_REQUIRED"
    if bt == 0:
        reasons.append("busy_timeout=0: any lock contention causes immediate failure. Set to 5000–30000ms.")
        level = "ACTION_REQUIRED"
    if disk_pct >= 85:
        reasons.append(f"Disk usage {disk_pct}% is critically high (>=85%). Risk of write failure.")
        level = "ACTION_REQUIRED"
    elif disk_pct >= 80:
        reasons.append(f"Disk usage {disk_pct}% is high (>=80%). Monitor closely.")
        if level == "PASS":
            level = "REVIEW"
    if freelist > 0:
        reclaimable_mb = round((freelist * page_size) / 1_048_576, 1)
        reasons.append(f"freelist_count={freelist} ({reclaimable_mb} MB reclaimable via VACUUM — do not run during live runtime).")
        if level == "PASS":
            level = "REVIEW"
    if wal_exists:
        reasons.append("Unexpected .db-wal file present despite journal_mode=delete. Investigate before enabling WAL.")
        level = "ACTION_REQUIRED"
    if ic and ic != "ok":
        reasons.append(f"integrity_check returned non-ok: {ic[:120]}")
        level = "ACTION_REQUIRED"
    if qc and qc != "ok":
        reasons.append(f"quick_check returned non-ok: {qc[:120]}")
        level = "ACTION_REQUIRED"
    if logs_bytes > 2 * 1_073_741_824:
        reasons.append(f"logs/ directory is {round(logs_bytes/1_073_741_824,1)} GB. Consider rotating old logs.")
        if level == "PASS":
            level = "REVIEW"
    if backup_bytes > 5 * 1_073_741_824:
        reasons.append(f"manual_audit_backups/ is {round(backup_bytes/1_073_741_824,1)} GB. Old backups may be prunable.")
        if level == "PASS":
            level = "REVIEW"
    if not reasons:
        reasons.append("No issues detected.")

    return level, reasons


def main() -> None:
    now = utcnow()
    audit_path = DB_PATH
    source_label = "live"
    conn = None

    # Try live read-only first
    conn, source_label = _open_ro(DB_PATH)
    if conn is None:
        # Fall back to tmp copy
        print(f"[WARN] Live DB locked — copying to {TMP_COPY} for audit...")
        shutil.copy2(DB_PATH, TMP_COPY)
        conn, source_label = _open_ro(TMP_COPY)
        audit_path = TMP_COPY
        if conn is None:
            print("[ERROR] Could not open DB even from tmp copy. Exiting.")
            return

    print(f"[INFO] Auditing DB from: {source_label} ({audit_path})")

    pragma_data = {p: _pragma(conn, p) for p in SAFE_PRAGMAS}
    quick = _quick_check(conn)
    integrity = _integrity_check(conn)
    conn.close()

    # File-level checks
    db_stat = DB_PATH.stat() if DB_PATH.exists() else None
    db_size = db_stat.st_size if db_stat else 0
    wal_path = DB_PATH.with_suffix(".db-wal")
    shm_path = DB_PATH.with_suffix(".db-shm")
    # Correct suffixes for .db files
    wal_path2 = Path(str(DB_PATH) + "-wal")
    shm_path2 = Path(str(DB_PATH) + "-shm")
    wal_exists = wal_path.exists() or wal_path2.exists()
    shm_exists = shm_path.exists() or shm_path2.exists()
    wal_size = (wal_path2.stat().st_size if wal_path2.exists() else
                wal_path.stat().st_size if wal_path.exists() else 0)
    shm_size = (shm_path2.stat().st_size if shm_path2.exists() else
                shm_path.stat().st_size if shm_path.exists() else 0)

    # Disk
    disk = shutil.disk_usage("/")
    disk_pct = round(disk.used / disk.total * 100, 1)
    free_bytes = disk.free

    # Directory sizes
    logs_dir = REPO / "logs"
    backup_dir = REPO / "manual_audit_backups"
    logs_bytes = _dir_size_bytes(logs_dir)
    backup_bytes = _dir_size_bytes(backup_dir)
    top_logs = _top_files(logs_dir)
    top_backups = _top_files(backup_dir)

    # Lock holders
    locks = _lock_holders(DB_PATH)

    report: dict = {
        "generated_at": now,
        "db_path": str(DB_PATH),
        "audit_source": source_label,
        "db_size_bytes": db_size,
        "db_size_mb": round(db_size / 1_048_576, 1),
        **pragma_data,
        "quick_check": quick,
        "integrity_check": integrity,
        "wal_exists": wal_exists,
        "shm_exists": shm_exists,
        "wal_size_bytes": wal_size,
        "shm_size_bytes": shm_size,
        "disk_usage_percent": disk_pct,
        "disk_free_bytes": free_bytes,
        "disk_free_gb": round(free_bytes / 1_073_741_824, 2),
        "logs_dir_bytes": logs_bytes,
        "logs_dir_mb": round(logs_bytes / 1_048_576, 1),
        "top_logs_files": top_logs,
        "manual_audit_backups_bytes": backup_bytes,
        "manual_audit_backups_mb": round(backup_bytes / 1_048_576, 1),
        "top_backup_files": top_backups,
        "lock_holders": locks,
    }

    recommendation, reasons = _recommendation(report)
    report["recommendation"] = recommendation
    report["reasons"] = reasons

    REPORT_PATH.parent.mkdir(parents=True, exist_ok=True)
    REPORT_PATH.write_text(json.dumps(report, indent=2))

    # ── Console summary ───────────────────────────────────────────────────────
    print("=" * 65)
    print("SQLITE HOUSEKEEPING AUDIT")
    print("=" * 65)
    print(f"Run at (UTC):       {now[:19]}")
    print(f"DB path:            {DB_PATH}")
    print(f"DB size:            {report['db_size_mb']} MB")
    print(f"journal_mode:       {pragma_data['journal_mode']}")
    print(f"busy_timeout:       {pragma_data['busy_timeout']} ms")
    print(f"page_count:         {pragma_data['page_count']}")
    print(f"page_size:          {pragma_data['page_size']} bytes")
    print(f"freelist_count:     {pragma_data['freelist_count']}")
    print(f"quick_check:        {quick}")
    print(f"integrity_check:    {integrity}")
    print(f"WAL file exists:    {wal_exists}  ({wal_size} bytes)")
    print(f"SHM file exists:    {shm_exists}  ({shm_size} bytes)")
    print(f"Disk usage:         {disk_pct}%  ({report['disk_free_gb']} GB free)")
    print(f"logs/ size:         {report['logs_dir_mb']} MB")
    print(f"backups/ size:      {report['manual_audit_backups_mb']} MB")
    print(f"Lock holders:")
    print(f"  lsof: {locks['lsof'][:120] if locks['lsof'] else 'none'}")
    print(f"  fuser: {locks['fuser'][:80] if locks['fuser'] else 'none'}")
    print()
    print(f"RECOMMENDATION:     {recommendation}")
    for r in reasons:
        print(f"  • {r}")
    print()
    print(f"Report written to:  {REPORT_PATH}")


if __name__ == "__main__":
    main()

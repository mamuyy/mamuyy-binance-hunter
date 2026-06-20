import os
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from json_utils import atomic_write_json

THRESHOLDS = [(70, "GREEN"), (80, "WATCH"), (85, "WARNING"), (90, "CRITICAL"), (101, "BLOCK_HEAVY_JOBS")]

def classify_usage(percent: float) -> str:
    if percent < 70: return "GREEN"
    if percent < 80: return "WATCH"
    if percent < 85: return "WARNING"
    if percent < 90: return "CRITICAL"
    return "BLOCK_HEAVY_JOBS"

def _dir_size(path: Path) -> int:
    if not path.exists(): return 0
    if path.is_file(): return path.stat().st_size
    total=0
    for root, _, files in os.walk(path):
        for name in files:
            try: total += (Path(root)/name).stat().st_size
            except OSError: pass
    return total

def build_capacity_report(project_dir: str|Path='.', db_path: str|Path='mamuyy_hunter.db') -> dict[str, Any]:
    project=Path(project_dir); total, used, free = shutil.disk_usage(project)
    pct = round((used/total)*100, 2) if total else 0.0
    status=classify_usage(pct)
    return {"checked_at": datetime.now(timezone.utc).isoformat(), "project_dir": str(project), "filesystem_total_bytes": total, "filesystem_used_bytes": used, "filesystem_available_bytes": free, "usage_percent": pct, "status": status, "database_file_size_bytes": _dir_size(Path(db_path)), "tmp_dir_size_bytes": _dir_size(project/'tmp'), "backup_dir_size_bytes": _dir_size(project/'db_backups'), "reports_dir_size_bytes": _dir_size(project/'reports'), "logs_dir_size_bytes": _dir_size(project/'logs'), "recommendations": ["Do not start heavy backfill when status is BLOCK_HEAVY_JOBS"] if status=="BLOCK_HEAVY_JOBS" else []}

def assert_heavy_job_allowed(project_dir: str|Path='.', db_path: str|Path='mamuyy_hunter.db') -> None:
    report=build_capacity_report(project_dir, db_path)
    if report["status"] == "BLOCK_HEAVY_JOBS":
        raise RuntimeError("Heavy backfill blocked: disk usage >= 90%")

def lightweight_sync_allowed(project_dir: str|Path='.', db_path: str|Path='mamuyy_hunter.db', min_free_bytes: int = 50_000_000, projected_write_bytes: int = 10_000_000) -> tuple[bool, dict[str, Any]]:
    report = build_capacity_report(project_dir, db_path)
    required = max(min_free_bytes, projected_write_bytes * 2)
    allowed = int(report["filesystem_available_bytes"]) >= required
    report["lightweight_sync_allowed"] = allowed
    report["lightweight_min_free_bytes"] = min_free_bytes
    report["lightweight_projected_write_bytes"] = projected_write_bytes
    if not allowed:
        report.setdefault("recommendations", []).append("Lightweight sync blocked: insufficient free space for bounded write margin")
    return allowed, report

def main(output='reports/infrastructure_capacity.json') -> int:
    report=build_capacity_report()
    atomic_write_json(output, report)
    print(f"Capacity status: {report['status']} usage={report['usage_percent']}%")
    return 0
if __name__ == '__main__': raise SystemExit(main())

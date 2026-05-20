"""Standalone resource monitor for local runtime health snapshots.

This module is intentionally independent from trading, ML, paper execution,
and orchestration code. It only reads host/resource metadata and writes monitor
outputs under logs/.
"""

from __future__ import annotations

import csv
import json
import os
import re
import subprocess
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


BASE_DIR = Path(__file__).resolve().parent
LOGS_DIR = BASE_DIR / "logs"
DATABASE_PATH = BASE_DIR / "mamuyy_hunter.db"
LATEST_JSON_PATH = LOGS_DIR / "resource_monitor_latest.json"
HISTORY_CSV_PATH = LOGS_DIR / "resource_monitor.csv"

CSV_FIELDS = [
    "timestamp_utc",
    "cpu_percent",
    "cpu_source",
    "ram_percent",
    "ram_source",
    "disk_percent",
    "disk_source",
    "load_1m",
    "load_5m",
    "load_15m",
    "load_source",
    "database_path",
    "database_exists",
    "database_size_bytes",
    "logs_path",
    "logs_exists",
    "logs_size_bytes",
]


def _load_psutil() -> Any | None:
    try:
        import psutil  # type: ignore
    except ImportError:
        return None
    return psutil


def _round_percent(value: float | int | None) -> float | None:
    if value is None:
        return None
    return round(float(value), 2)


def _load_average() -> tuple[float | None, float | None, float | None, str]:
    try:
        load_1m, load_5m, load_15m = os.getloadavg()
    except (AttributeError, OSError):
        return None, None, None, "unavailable"
    return round(load_1m, 2), round(load_5m, 2), round(load_15m, 2), "os.getloadavg"


def _cpu_percent(psutil: Any | None, load_1m: float | None) -> tuple[float | None, str]:
    if psutil is not None:
        try:
            return _round_percent(psutil.cpu_percent(interval=0.1)), "psutil"
        except Exception:
            pass

    cpu_count = os.cpu_count() or 1
    if load_1m is None:
        return None, "unavailable"
    estimated = min((load_1m / cpu_count) * 100.0, 100.0)
    return _round_percent(estimated), "load_average_estimate"


def _ram_percent(psutil: Any | None) -> tuple[float | None, str]:
    if psutil is not None:
        try:
            return _round_percent(psutil.virtual_memory().percent), "psutil"
        except Exception:
            pass

    meminfo = Path("/proc/meminfo")
    if meminfo.exists():
        try:
            values: dict[str, int] = {}
            for line in meminfo.read_text(encoding="utf-8").splitlines():
                key, raw_value = line.split(":", 1)
                values[key] = int(raw_value.strip().split()[0])
            total = values.get("MemTotal")
            available = values.get("MemAvailable")
            if total and available is not None:
                used_percent = ((total - available) / total) * 100.0
                return _round_percent(used_percent), "/proc/meminfo"
        except (OSError, ValueError):
            pass

    macos_ram = _macos_ram_percent()
    if macos_ram is not None:
        return macos_ram, "vm_stat"

    return None, "unavailable"


def _macos_ram_percent() -> float | None:
    try:
        vm_stat_output = subprocess.run(
            ["vm_stat"],
            check=True,
            capture_output=True,
            text=True,
            timeout=2,
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return None

    try:
        page_size_match = re.search(r"page size of (\d+) bytes", vm_stat_output)
        if page_size_match is None:
            return None
        page_size = int(page_size_match.group(1))
        wanted_keys = {
            "Pages free",
            "Pages speculative",
            "Pages active",
            "Pages inactive",
            "Pages wired down",
            "Pages occupied by compressor",
        }
        page_counts: dict[str, int] = {}
        for line in vm_stat_output.splitlines():
            if ":" not in line:
                continue
            key, raw_value = line.split(":", 1)
            clean_key = key.strip()
            if clean_key not in wanted_keys:
                continue
            clean_value = raw_value.strip().rstrip(".").replace(",", "")
            page_counts[clean_key] = int(clean_value)

        free_pages = page_counts.get("Pages free", 0)
        speculative_pages = page_counts.get("Pages speculative", 0)
        active_pages = page_counts.get("Pages active", 0)
        inactive_pages = page_counts.get("Pages inactive", 0)
        wired_pages = page_counts.get("Pages wired down", 0)
        compressed_pages = page_counts.get("Pages occupied by compressor", 0)

        used_pages = active_pages + inactive_pages + wired_pages + compressed_pages
        total_pages = used_pages + free_pages + speculative_pages
        if page_size <= 0 or total_pages <= 0:
            return None
        return _round_percent((used_pages / total_pages) * 100.0)
    except (KeyError, ValueError, ZeroDivisionError):
        return None


def _disk_percent(path: Path) -> tuple[float | None, str]:
    try:
        usage = shutil.disk_usage(path)
    except OSError:
        return None, "unavailable"
    if usage.total <= 0:
        return None, "unavailable"
    return _round_percent((usage.used / usage.total) * 100.0), "shutil.disk_usage"


def _file_size(path: Path) -> tuple[bool, int]:
    try:
        return path.exists(), path.stat().st_size if path.exists() else 0
    except OSError:
        return False, 0


def _directory_size(path: Path) -> tuple[bool, int]:
    if not path.exists():
        return False, 0

    total_size = 0
    for root, _, files in os.walk(path):
        for filename in files:
            file_path = Path(root) / filename
            try:
                total_size += file_path.stat().st_size
            except OSError:
                continue
    return True, total_size


def collect_snapshot() -> dict[str, Any]:
    LOGS_DIR.mkdir(parents=True, exist_ok=True)

    psutil = _load_psutil()
    load_1m, load_5m, load_15m, load_source = _load_average()
    cpu_percent, cpu_source = _cpu_percent(psutil, load_1m)
    ram_percent, ram_source = _ram_percent(psutil)
    disk_percent, disk_source = _disk_percent(BASE_DIR)
    database_exists, database_size_bytes = _file_size(DATABASE_PATH)
    logs_exists, logs_size_bytes = _directory_size(LOGS_DIR)

    return {
        "timestamp_utc": datetime.now(timezone.utc).isoformat(),
        "cpu_percent": cpu_percent,
        "cpu_source": cpu_source,
        "ram_percent": ram_percent,
        "ram_source": ram_source,
        "disk_percent": disk_percent,
        "disk_source": disk_source,
        "load_1m": load_1m,
        "load_5m": load_5m,
        "load_15m": load_15m,
        "load_source": load_source,
        "database_path": str(DATABASE_PATH.relative_to(BASE_DIR)),
        "database_exists": database_exists,
        "database_size_bytes": database_size_bytes,
        "logs_path": str(LOGS_DIR.relative_to(BASE_DIR)),
        "logs_exists": logs_exists,
        "logs_size_bytes": logs_size_bytes,
    }


def write_latest(snapshot: dict[str, Any]) -> None:
    LATEST_JSON_PATH.write_text(
        json.dumps(snapshot, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def append_history(snapshot: dict[str, Any]) -> None:
    file_exists = HISTORY_CSV_PATH.exists()
    with HISTORY_CSV_PATH.open("a", newline="", encoding="utf-8") as csv_file:
        writer = csv.DictWriter(csv_file, fieldnames=CSV_FIELDS)
        if not file_exists:
            writer.writeheader()
        writer.writerow({field: snapshot.get(field) for field in CSV_FIELDS})


def main() -> int:
    snapshot = collect_snapshot()
    write_latest(snapshot)
    append_history(snapshot)
    print(json.dumps(snapshot, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

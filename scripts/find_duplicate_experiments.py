#!/usr/bin/env python3
"""Lightweight read-only helper to detect potential duplicate experiments."""

from __future__ import annotations

import argparse
import re
from collections import defaultdict
from pathlib import Path
from typing import Iterable

CATEGORY_KEYWORDS = {
    "threshold_tuning": ["threshold", "tuning"],
    "calibration_rerun": ["calibration", "recalibration", "calib"],
    "robustness_rerun": ["robustness", "stress", "sensitivity"],
    "temporal_diagnosis": ["temporal", "regime", "window", "timesplit", "drift"],
    "nonlinear_exploration": ["nonlinear", "non-linear", "kernel", "tree", "interaction"],
}

DEFAULT_PATTERNS = [
    "docs/**/*",
    "reports/**/*",
    "scripts/**/*",
    "phase2*",
    "phase2*/**/*",
    "phase4*",
    "phase4*/**/*",
    "walkforward*",
    "walkforward*/**/*",
    "calibration*",
    "calibration*/**/*",
    "robustness*",
    "robustness*/**/*",
]

TOKEN_RE = re.compile(r"[a-z0-9]+")


def tokenize_name(path: Path) -> set[str]:
    raw = path.as_posix().lower()
    tokens = set(TOKEN_RE.findall(raw))
    return {t for t in tokens if len(t) > 2}


def detect_categories(tokens: set[str]) -> set[str]:
    detected: set[str] = set()
    for category, keywords in CATEGORY_KEYWORDS.items():
        if any(k in tokens for k in keywords):
            detected.add(category)
    return detected


def jaccard(a: set[str], b: set[str]) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def collect_files(root: Path, patterns: Iterable[str]) -> list[Path]:
    found: dict[str, Path] = {}
    for pattern in patterns:
        for p in root.glob(pattern):
            if p.is_file():
                found[p.as_posix()] = p
    return sorted(found.values(), key=lambda p: p.as_posix())


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", default=".", help="Repository root to scan (default: .)")
    parser.add_argument("--min-sim", type=float, default=0.35, help="Minimum Jaccard similarity")
    parser.add_argument("--top-k", type=int, default=20, help="Maximum candidate pairs to print")
    args = parser.parse_args()

    root = Path(args.root).resolve()
    files = collect_files(root, DEFAULT_PATTERNS)

    indexed = []
    category_to_indices: dict[str, list[int]] = defaultdict(list)
    for file_path in files:
        tokens = tokenize_name(file_path.relative_to(root))
        categories = detect_categories(tokens)
        if not categories:
            continue
        indexed.append((file_path, tokens, categories))
        idx = len(indexed) - 1
        for c in categories:
            category_to_indices[c].append(idx)

    candidates: list[tuple[float, str, Path, Path]] = []
    for category, indices in category_to_indices.items():
        for i in range(len(indices)):
            for j in range(i + 1, len(indices)):
                p1, t1, _ = indexed[indices[i]]
                p2, t2, _ = indexed[indices[j]]
                sim = jaccard(t1, t2)
                if sim >= args.min_sim:
                    candidates.append((sim, category, p1, p2))

    candidates.sort(key=lambda x: x[0], reverse=True)

    print("# Duplicate Experiment Candidate Scan (read-only)")
    print(f"Root: {root}")
    print(f"Scanned files: {len(files)}")
    print(f"Candidate pairs (min-sim={args.min_sim}): {len(candidates)}")

    if not candidates:
        print("No duplicate candidates detected.")
        return 0

    print("\nTop candidates:")
    for rank, (sim, category, p1, p2) in enumerate(candidates[: args.top_k], start=1):
        r1 = p1.relative_to(root)
        r2 = p2.relative_to(root)
        print(f"{rank:02d}. [{category}] sim={sim:.2f}")
        print(f"    - {r1}")
        print(f"    - {r2}")

    print("\nWARNING: Candidates are heuristic only; validate via experiment metadata.")
    print("PAPER_ONLY confirmed: governance helper does not execute strategies or modify artifacts.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""Merge partial duration JSON files into .test_durations, dropping outliers.

Called by batch-store-durations.sh to replace the inline Python heredoc:
    python3 "$SCRIPT_DIR/merge_durations.py" "$SRUN_DIR" "$PWD/.test_durations"

Also importable by other scripts.
"""

import glob
import json
import os
import statistics
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from durations_quality import MIN_ENTRIES, MIN_UNIQUE, MIN_CV, OUTLIER_CAP, quality_ok


def merge(src_dir: str, output_path: str) -> int:
    """Merge all *.json files from src_dir into output_path; return entry count."""
    merged = {}
    outlier_count = 0
    for path in sorted(glob.glob(os.path.join(src_dir, "*/*.json"))):
        with open(path) as f:
            data = json.load(f)
        pre = len(merged)
        for k, v in data.items():
            if v > OUTLIER_CAP:
                outlier_count += 1
                print(f"  SKIP outlier {v:.1f}s: {k}")
            else:
                merged[k] = v
        added = len(merged) - pre
        print(f"  {os.path.relpath(path, src_dir)}: {len(data)} entries ({added} new)")

    if outlier_count:
        print(f"  Dropped {outlier_count} outlier(s) > {OUTLIER_CAP}s (fixture-setup misattribution)")

    with open(output_path, "w") as f:
        json.dump(merged, f, indent=2, sort_keys=True)
        f.write("\n")

    vs = list(merged.values())
    n = len(vs)
    uv = len(set(vs))
    avg = statistics.mean(vs) if vs else 0.0
    sd = statistics.stdev(vs) if n > 1 else 0.0
    cv = sd / avg if avg > 0 else 0.0

    print()
    print(f"Total entries: {n}")
    print(f"Unique values: {uv}")
    print(f"Mean: {avg:.4f}s | SD: {sd:.4f}s | CV: {cv:.4f}")
    if vs:
        print(f"Min: {min(vs):.4f}s | Max: {max(vs):.4f}s")
    ok = quality_ok(n, uv, cv)
    print(f"Guard check: {'PASS' if ok else 'FAIL'} (needs n>={MIN_ENTRIES}, uv>={MIN_UNIQUE}, CV>{MIN_CV})")

    if not ok:
        if n < MIN_ENTRIES:
            print(f"  FAIL: entries: {n} < {MIN_ENTRIES}")
        if uv < MIN_UNIQUE:
            print(f"  FAIL: unique values: {uv} < {MIN_UNIQUE}")
        if cv <= MIN_CV:
            print(f"  FAIL: CV > {MIN_CV}: {cv:.4f}")

    return n


if __name__ == "__main__":
    if len(sys.argv) != 3:
        print(f"Usage: {sys.argv[0]} <src_dir> <output_path>", file=sys.stderr)
        sys.exit(1)
    count = merge(sys.argv[1], sys.argv[2])
    sys.exit(0 if count > 0 else 1)

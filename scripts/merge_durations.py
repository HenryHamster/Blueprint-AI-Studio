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
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))
from durations_quality import (
    IN_BAND_SETUP_GAP_MAX,
    IN_BAND_SETUP_MAX,
    IN_BAND_SETUP_MIN,
    MIN_CV,
    MIN_ENTRIES,
    MIN_UNIQUE,
    OUTLIER_CAP,
    in_band_setup_artifacts,
    quality_ok,
)
from testing.identity_extraction import extract_python_test_ids_from_file


def _valid_test_ids(path: Path) -> set[str]:
    """Return canonical IDs, treating unreadable source as no valid tests."""
    return extract_python_test_ids_from_file(path)


def filter_current_entries(data: dict, root: str | os.PathLike[str]) -> dict:
    """Drop entries whose test file or statically collectable ID is gone."""
    root_path = Path(root)
    valid_ids_by_file: dict[Path, set[str]] = {}
    current: dict = {}
    for key, value in data.items():
        file_key, separator, raw_node_id = key.partition("::")
        if not separator:
            continue
        test_path = root_path / file_key
        if not test_path.is_file():
            continue
        if test_path not in valid_ids_by_file:
            valid_ids_by_file[test_path] = _valid_test_ids(test_path)
        valid_ids = valid_ids_by_file[test_path]
        base_node_id = raw_node_id.split("[", 1)[0]
        if base_node_id not in valid_ids:
            continue
        current[key] = value
    return current


def load_duration_baseline(path: str) -> dict:
    """Load the existing artifact, retaining only entries in the current tree."""
    if not os.path.exists(path):
        return {}
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    if not isinstance(data, dict):
        raise ValueError(f"duration baseline must be a JSON object: {path}")
    return filter_current_entries(data, os.path.dirname(os.path.abspath(path)))


def sanitize_fresh_entries(fresh: dict) -> tuple[dict, set[str]]:
    """Return the fresh observations with their own setup plateau removed.

    The baseline is deliberately kept out of this decision. Sweeping
    baseline-and-fresh together lets a historical observation inside the
    diagnostic gap vouch for a freshly recorded plateau, and the artifacts are
    then persisted as the new baseline -- where they are indistinguishable from
    real measurements. The fresh entries are the only ones a merge can still
    reject, so they are judged on their own evidence first. Callers still sweep
    the merged view afterwards, which is what strips a plateau that is already
    sitting in the baseline.
    """
    band_artifacts = in_band_setup_artifacts(fresh)
    return {
        key: value for key, value in fresh.items() if key not in band_artifacts
    }, band_artifacts


def merge(src_dir: str, output_path: str) -> int:
    """Merge all *.json files from src_dir into output_path; return entry count."""
    root = os.path.dirname(os.path.abspath(output_path))
    merged = {}
    outlier_count = 0

    for k, v in load_duration_baseline(output_path).items():
        if v > OUTLIER_CAP:
            outlier_count += 1
        else:
            merged[k] = v

    # Fresh partial observations accumulate separately so the setup-plateau
    # sanitizer can judge them without the baseline vouching for them; `added`
    # still counts entries that are new to the baseline-and-partials view.
    fresh: dict = {}
    for path in sorted(glob.glob(os.path.join(src_dir, "*/*.json"))):
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        data = filter_current_entries(data, root)
        pre = len(merged.keys() | fresh.keys())
        for k, v in data.items():
            if v > OUTLIER_CAP:
                outlier_count += 1
                print(f"  SKIP outlier {v:.1f}s: {k}")
            else:
                fresh[k] = v
        added = len(merged.keys() | fresh.keys()) - pre
        print(f"  {os.path.relpath(path, src_dir)}: {len(data)} entries ({added} new)")

    fresh, fresh_band_artifacts = sanitize_fresh_entries(fresh)
    if fresh_band_artifacts:
        print(
            f"  Dropped {len(fresh_band_artifacts)} in-band setup artifacts from the "
            f"fresh observations ({IN_BAND_SETUP_MIN:g}-{IN_BAND_SETUP_MAX:g}s plateau; "
            f"no observations through {IN_BAND_SETUP_GAP_MAX:g}s)"
        )
    merged.update(fresh)

    band_artifacts = in_band_setup_artifacts(merged)
    for key in band_artifacts:
        del merged[key]
    if band_artifacts:
        print(
            f"  Dropped {len(band_artifacts)} in-band setup artifacts "
            f"({IN_BAND_SETUP_MIN:g}-{IN_BAND_SETUP_MAX:g}s plateau; "
            f"no observations through {IN_BAND_SETUP_GAP_MAX:g}s)"
        )

    if outlier_count:
        print(f"  Dropped {outlier_count} outlier(s) > {OUTLIER_CAP}s (fixture-setup misattribution)")

    # ``newline=""`` keeps the committed durations artifact LF-only when this
    # runs on a Windows host: platform translation would rewrite every one of
    # its thousands of lines as CRLF and produce a whole-file diff.
    with open(output_path, "w", encoding="utf-8", newline="") as f:
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

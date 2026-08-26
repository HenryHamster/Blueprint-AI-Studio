#!/usr/bin/env python3
"""Reconstruct backend/.test_durations from the per-shard durations CI already runs.

Motivation
----------
CI splits the backend suite 8 ways with pytest-split (``--splits 8 --group N``).
That balancing is only time-accurate while ``backend/.test_durations`` is fresh,
and the only existing way to refresh it -- ``scripts/store-backend-durations.sh``
-- reruns the WHOLE suite with ``--store-durations`` (15-20 min plus Postgres).
Too expensive to run per merge, so the file rots and shards drift out of balance.

The promotion PR already runs all 8 shards. pytest-split's ``--store-durations``
is fully compatible with ``--splits``/``--group`` (the two plugins register
independently in ``pytest_split.plugin.pytest_configure``), so each shard can
emit the timings for the tests IT ran at zero extra compute. Merging the 8
partials over the committed baseline reconstructs the full map for free.

The carry-over trap this tool exists to defend against
------------------------------------------------------
``PytestSplitCachePlugin`` does NOT write only what the shard ran. It seeds
``self.cached_durations`` by READING ``--durations-path`` at startup, then
writes that dict back updated with the new measurements. So if a shard points
``--durations-path`` at a copy of the committed baseline, its artifact is the
FULL map in which only its own ~1/8 of rows are fresh and the other ~7/8 are
stale carry-over. Naively ``dict.update()``-ing eight such files in glob order
lets shard 8's stale copy of a row silently overwrite shard 3's fresh
measurement -- producing a file that looks complete and is ~7/8 rot.

Two defences, belt and braces:
  1. The workflow points each shard at a FRESH ``--durations-path`` that does
     not exist yet, so the partial contains only measured rows.
  2. This tool does not trust that. An entry whose value is bit-identical to
     the baseline's value for the same key is treated as carry-over, not as a
     fresh measurement. Re-measured wall-clock floats do not collide exactly.

Determinism
-----------
Output is ``sort_keys=True, indent=2`` plus a trailing newline (the format
``scripts/merge_durations.py`` already writes, so the two generators agree).
When several shards report a fresh value for the same test the MAXIMUM is
taken -- order-independent, so the result does not depend on argument or glob
order, and conservative for shard balancing.

Duration policy (outlier cap, in-band setup-artifact strip, quality floors) is
imported from ``scripts/durations_quality.py`` -- the same module
``scripts/merge_durations.py`` uses -- so the two paths cannot drift apart.

Exit codes
----------
  0  success (or, with --check, the artifact is already up to date)
  1  --check only: the artifact WOULD change
  2  fail-closed error; nothing was written
"""

from __future__ import annotations

import argparse
import json
import math
import os
import statistics
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from durations_quality import (  # noqa: E402
    IN_BAND_SETUP_MAX,
    MIN_CV,
    MIN_ENTRIES,
    MIN_UNIQUE,
    OUTLIER_CAP,
    in_band_setup_artifacts,
    quality_ok,
)

EXIT_OK = 0
EXIT_WOULD_CHANGE = 1
EXIT_ERROR = 2


class MergeError(Exception):
    """Fail-closed condition: report loudly and write nothing."""


def _validate_mapping(data: object, source: str) -> dict[str, float]:
    """Return data as a str->finite-float mapping, or raise MergeError."""
    if not isinstance(data, dict):
        raise MergeError(f"{source}: expected a JSON object, got {type(data).__name__}")
    if not data:
        raise MergeError(f"{source}: duration file is empty")
    clean: dict[str, float] = {}
    for key, value in data.items():
        if not isinstance(key, str) or not key:
            raise MergeError(f"{source}: non-string or empty test id key: {key!r}")
        # bool is a subclass of int; a boolean duration is corruption, not data.
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise MergeError(f"{source}: non-numeric duration for {key!r}: {value!r}")
        value = float(value)
        # json.load parses NaN/Infinity by default -- reject them explicitly.
        if not math.isfinite(value):
            raise MergeError(f"{source}: non-finite duration for {key!r}: {value!r}")
        if value < 0:
            raise MergeError(f"{source}: negative duration for {key!r}: {value!r}")
        clean[key] = value
    return clean


def load_duration_file(path: str | os.PathLike[str]) -> dict[str, float]:
    """Load and validate one durations JSON file. Raises MergeError on any fault."""
    source = str(path)
    try:
        raw = Path(path).read_text(encoding="utf-8")
    except OSError as exc:
        raise MergeError(f"{source}: cannot read: {exc}") from exc
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise MergeError(f"{source}: malformed JSON: {exc}") from exc
    return _validate_mapping(data, source)


def collect_fresh(
    shard_paths: list[str],
    baseline: dict[str, float],
    log=print,
) -> tuple[dict[str, float], list[str]]:
    """Return (fresh observations, names of shards that were absent).

    A shard file that is absent is tolerated and reported loudly -- a skipped
    shard should leave the baseline's entries standing, not blow up the merge.
    A shard file that is PRESENT but malformed or empty is fail-closed: that
    means the run went wrong in a way we cannot reason about.
    """
    fresh: dict[str, float] = {}
    missing: list[str] = []
    for path in shard_paths:
        if not Path(path).exists():
            missing.append(path)
            log(f"  WARNING: shard file absent, skipping: {path}")
            continue
        data = load_duration_file(path)
        carried = 0
        new = 0
        for key, value in data.items():
            # Bit-identical to the baseline => pytest-split carry-over, not a
            # fresh measurement. See the carry-over trap in the module docstring.
            if key in baseline and value == baseline[key]:
                carried += 1
                continue
            # Max across shards keeps the merge independent of argument order.
            if key not in fresh or value > fresh[key]:
                fresh[key] = value
            new += 1
        note = f" ({carried} baseline carry-over ignored)" if carried else ""
        log(f"  {path}: {len(data)} entries, {new} freshly measured{note}")
    return fresh, missing


def build_merged(
    baseline: dict[str, float], fresh: dict[str, float], log=print
) -> dict[str, float]:
    """Overlay fresh observations on the baseline and apply duration policy."""
    merged = dict(baseline)
    merged.update(fresh)

    capped = sorted(k for k, v in merged.items() if v > OUTLIER_CAP)
    for key in capped:
        del merged[key]
    if capped:
        log(
            f"  Dropped {len(capped)} outlier(s) > {OUTLIER_CAP}s "
            "(fixture-setup misattribution)"
        )

    band = in_band_setup_artifacts(merged)
    for key in sorted(band):
        del merged[key]
    if band:
        log(
            f"  Dropped {len(band)} in-band setup artifacts "
            f"({IN_BAND_SETUP_MAX:g}s plateau with no following observations)"
        )
    return merged


def render(merged: dict[str, float]) -> str:
    """Serialize deterministically: sorted keys, indent 2, trailing newline."""
    return json.dumps(merged, indent=2, sort_keys=True) + "\n"


def summarize(merged: dict[str, float], log=print) -> bool:
    """Print distribution stats and return whether the quality floor is met."""
    values = list(merged.values())
    n = len(values)
    uv = len(set(values))
    avg = statistics.mean(values) if values else 0.0
    sd = statistics.stdev(values) if n > 1 else 0.0
    cv = sd / avg if avg > 0 else 0.0
    log(f"Total entries: {n}")
    log(f"Unique values: {uv}")
    log(f"Mean: {avg:.4f}s | SD: {sd:.4f}s | CV: {cv:.4f}")
    ok = quality_ok(n, uv, cv)
    log(
        f"Guard check: {'PASS' if ok else 'FAIL'} "
        f"(needs n>={MIN_ENTRIES}, uv>={MIN_UNIQUE}, CV>{MIN_CV})"
    )
    if not ok:
        if n < MIN_ENTRIES:
            log(f"  FAIL: entries {n} < {MIN_ENTRIES}")
        if uv < MIN_UNIQUE:
            log(f"  FAIL: unique values {uv} < {MIN_UNIQUE}")
        if cv <= MIN_CV:
            log(f"  FAIL: CV {cv:.4f} <= {MIN_CV}")
    return ok


def merge_shards(
    shard_paths: list[str],
    baseline_path: str,
    output_path: str | None = None,
    check: bool = False,
    prune_deleted: bool = False,
    allow_quality_failure: bool = False,
    log=print,
) -> int:
    """Merge shard partials over the baseline. Returns a process exit code."""
    if not shard_paths:
        raise MergeError("no shard duration files given")
    output_path = output_path or baseline_path

    baseline = load_duration_file(baseline_path)
    log(f"Baseline {baseline_path}: {len(baseline)} entries")

    if prune_deleted:
        from merge_durations import filter_current_entries

        root = os.path.dirname(os.path.abspath(baseline_path))
        before = len(baseline)
        baseline = filter_current_entries(baseline, root)
        if before != len(baseline):
            log(
                f"  Pruned {before - len(baseline)} entries for tests "
                "no longer in the tree"
            )

    fresh, missing = collect_fresh(shard_paths, baseline, log=log)
    if len(missing) == len(shard_paths):
        raise MergeError(
            f"all {len(shard_paths)} shard duration file(s) were absent; "
            "refusing to rewrite the artifact from no fresh measurements"
        )
    if not fresh:
        # Every present shard agreed bit-for-bit with the baseline: the artifact
        # has already converged. That is NOT a fault -- treating it as one would
        # make the tool non-idempotent and would break a --check guard running
        # against an already-up-to-date artifact. Carry on and write/compare the
        # unchanged map; no baseline entry is ever dropped, so nothing degrades.
        log("  No fresh measurements: every shard matched the baseline (already converged)")

    merged = build_merged(baseline, fresh, log=log)

    # Counted off the FINAL map so policy drops above are reflected honestly.
    # An unmeasured baseline entry must survive the merge; that is how this file
    # avoids quietly degrading when a shard is skipped.
    added = sum(1 for k in merged if k not in baseline)
    updated = sum(1 for k in merged if k in baseline and k in fresh)
    preserved = sum(1 for k in merged if k in baseline and k not in fresh)
    log(f"  {added} new test(s), {updated} refreshed, {preserved} preserved from baseline")

    ok = summarize(merged, log=log)
    if not ok and not allow_quality_failure:
        raise MergeError(
            "merged artifact fails the durations quality floor; refusing to write "
            "(pass --allow-quality-failure to override)"
        )

    payload = render(merged)
    existing = None
    if Path(output_path).exists():
        existing = Path(output_path).read_text(encoding="utf-8")

    if check:
        if existing == payload:
            log(f"--check: {output_path} is already up to date")
            return EXIT_OK
        log(f"--check: {output_path} WOULD change ({added} added, {updated} refreshed)")
        return EXIT_WOULD_CHANGE

    Path(output_path).write_text(payload, encoding="utf-8")
    log(f"Wrote {output_path} ({len(merged)} entries)")
    return EXIT_OK


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Merge per-shard pytest-split duration files over backend/.test_durations."
        ),
    )
    parser.add_argument("shards", nargs="+", help="per-shard durations JSON files")
    parser.add_argument(
        "--baseline", required=True, help="existing .test_durations to merge over"
    )
    parser.add_argument(
        "--output", default=None, help="where to write (default: the baseline path)"
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="report what would change and write nothing (exit 1 if it would change)",
    )
    parser.add_argument(
        "--prune-deleted",
        action="store_true",
        help="also drop baseline entries whose test no longer exists in the tree",
    )
    parser.add_argument(
        "--allow-quality-failure",
        action="store_true",
        help="write even if the merged artifact fails the quality floor",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return merge_shards(
            shard_paths=args.shards,
            baseline_path=args.baseline,
            output_path=args.output,
            check=args.check,
            prune_deleted=args.prune_deleted,
            allow_quality_failure=args.allow_quality_failure,
        )
    except MergeError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return EXIT_ERROR


if __name__ == "__main__":
    sys.exit(main())

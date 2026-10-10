#!/usr/bin/env python3
"""Generate the checked-in test-estate ranking.

The queue is a derived report, not a second source of test-estate facts.  Use
``--write`` to regenerate it after a test inventory change and ``--check`` in
guards/CI to require that the checked-in bytes match the current tree.

THE RENDERING IS LINE-LOCAL, AND THAT IS A CONTRACT
---------------------------------------------------
Every line this generator emits below the fixed preamble is a function of
exactly ONE test file.  Nothing here aggregates across files, and nothing is
numbered by position.  That is not a stylistic choice; it is what makes the
artifact mergeable, and it is pinned by
``test_adding_a_test_file_only_inserts_lines`` and
``test_two_independent_additions_merge_to_the_full_render`` in
``backend/testing/test_test_estate_ranked_queue.py``.

The report used to carry two GLOBAL fields, and between them they made this the
single largest source of merge conflicts in the repository -- eight of twenty
real ``origin/automain`` re-sync merges (2026-08-26), more than every source
conflict in that window combined:

* a ``Rank`` ordinal column, so inserting one row rewrote the ordinal of every
  row below it -- a whole-file rewrite for a one-file change; and
* an estate-summary table of per-area totals, so any two branches that both
  added a backend test rewrote the same summary line.

While a global field is present, no fix exists on the git side.  A custom merge
driver cannot help (git hands a driver three blobs, and the right answer is a
function of the MERGED TREE, which does not exist yet), and the built-in
``union`` driver cannot help either: GitHub's server-side merge does not honour
``.gitattributes`` merge attributes at all.  Measured 2026-08-26 -- a two-commit
probe whose only change was each side appending one line to a path marked
``merge=union`` merges cleanly under stock local git and is reported
``CONFLICTING`` by the GitHub API (throwaway PR #18024).

Removing the global fields fixes it at the source instead, for STOCK git and
therefore for GitHub too: two branches that each add a test file produce two
disjoint single-line insertions, git's ordinary three-way merge takes both, and
because every surviving line is still exactly the line the generator would emit
for its own file, the merged bytes equal a fresh ``--write`` from the merged
tree.  Rank survives as row ORDER, which is where a reader was reading it from
anyway; the estate totals moved to ``--summary``, which prints them on demand
rather than committing an aggregate that every branch rewrites.

Anything added here later must keep that property: no ordinals, no running
totals, no "N of M" counters in a row.
"""

from __future__ import annotations

import argparse
import difflib
import json
import sys
from dataclasses import dataclass
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))

from backend.testing.estate_inventory import (  # noqa: E402
    iter_backend_test_files,
    iter_vitest_test_files,
)
from backend.testing.identity_extraction import (  # noqa: E402
    extract_python_test_ids_from_file,
    extract_typescript_test_ids,
    frozen_typescript_tree,
)

try:  # absent from synthetic fixtures that copy scripts/ piecemeal
    from scripts.utf8_io import force_utf8_streams  # noqa: E402
except ImportError:  # pragma: no cover - fixture trees run on UTF-8 hosts
    def force_utf8_streams() -> None:
        return None

QUEUE_PATH = "backend/testing/test-estate-ranked-queue.md"
MANIFEST_PATH = "backend/testing/conversational_workday_manifest.json"
BACKEND_DURATIONS_PATH = "backend/.test_durations"
VITEST_DURATION_PATHS = {
    "admin-ui": "admin-ui/.test_durations.json",
    "agent": "agent/.test_durations.json",
}


@dataclass(frozen=True)
class TestFile:
    area: str
    path: str
    lines: int
    test_count: int
    threshold: str
    priority: str
    cost_seconds: float | None
    protected_declared: int | None


def _threshold(lines: int) -> tuple[str, str]:
    """Return the rubric threshold class and its queue priority."""
    if lines > 5000:
        return "critical (>5000 LOC)", "P2"
    if lines >= 3000:
        return "high (3000–5000 LOC)", "P3"
    if lines >= 1500:
        return "moderate (1500–2999 LOC)", "P5"
    return "under 1500 LOC", "G"


def _load_json(path: Path) -> dict:
    with path.open(encoding="utf-8") as handle:
        data = json.load(handle)
    if not isinstance(data, dict):
        raise ValueError(f"expected JSON object in {path}")
    return data


def _load_protected_counts(root: Path) -> dict[str, int]:
    data = _load_json(root / MANIFEST_PATH)
    entries = data.get("protect", [])
    if not isinstance(entries, list):
        raise ValueError(f"{MANIFEST_PATH} protect must be a list")
    protected: dict[str, int] = {}
    for entry in entries:
        if not isinstance(entry, dict) or not isinstance(entry.get("path"), str):
            raise ValueError(f"invalid protect entry in {MANIFEST_PATH}: {entry!r}")
        count = entry.get("test_count")
        if not isinstance(count, int) or count < 0:
            raise ValueError(f"invalid declared test_count in {MANIFEST_PATH}: {entry!r}")
        protected[entry["path"]] = count
    return protected


def _backend_costs(root: Path) -> dict[str, float]:
    data = _load_json(root / BACKEND_DURATIONS_PATH)
    costs: dict[str, float] = {}
    for key, value in data.items():
        file_path, separator, _ = key.partition("::")
        if separator and isinstance(value, (int, float)):
            repository_path = f"backend/{file_path}"
            costs[repository_path] = costs.get(repository_path, 0.0) + float(value)
    return costs


def _vitest_costs(root: Path, suite: str) -> dict[str, float]:
    data = _load_json(root / VITEST_DURATION_PATHS[suite])
    costs: dict[str, float] = {}
    for entry in data.get("files", []):
        if not isinstance(entry, dict):
            continue
        path = entry.get("file")
        duration = entry.get("duration_ms")
        if (
            isinstance(path, str)
            and entry.get("measured") is True
            and isinstance(duration, (int, float))
        ):
            repository_path = f"{suite}/{path}"
            costs[repository_path] = costs.get(repository_path, 0.0) + float(duration) / 1000
    return costs


def _record(
    area: str,
    path: Path,
    relative: str,
    protected: dict[str, int],
    costs: dict[str, float],
) -> TestFile:
    source = path.read_text(encoding="utf-8")
    if area == "backend":
        test_count = len(extract_python_test_ids_from_file(path))
    else:
        test_count = len(extract_typescript_test_ids(source, path))
    threshold, priority = _threshold(len(source.splitlines()))
    return TestFile(
        area=area,
        path=relative,
        lines=len(source.splitlines()),
        test_count=test_count,
        threshold=threshold,
        priority=priority,
        cost_seconds=costs.get(relative),
        protected_declared=protected.get(relative),
    )


def collect_test_files(root: Path) -> tuple[TestFile, ...]:
    """Collect all three current test inventories with canonical test counts.

    The sweep is read-only, so it runs under ``frozen_typescript_tree()``:
    resolving one Vitest file's transitive imports is the same work for
    hundreds of entry points, and revalidating each cached module with a stat
    every time costs ~46 stats per file actually read. See that context
    manager for the measurement.
    """
    protected = _load_protected_counts(root)
    records: list[TestFile] = []
    backend_costs = _backend_costs(root)
    with frozen_typescript_tree():
        for path in iter_backend_test_files(root):
            records.append(
                _record(
                    "backend",
                    path,
                    path.relative_to(root).as_posix(),
                    protected,
                    backend_costs,
                )
            )
        for suite in ("admin-ui", "admin-ui-viewport", "agent"):
            costs = (
                _vitest_costs(root, suite) if suite in VITEST_DURATION_PATHS else {}
            )
            for path in iter_vitest_test_files(root, suite):
                records.append(
                    _record(
                        suite,
                        path,
                        path.relative_to(root).as_posix(),
                        protected,
                        costs,
                    )
                )
    return tuple(records)


def _rank_key(record: TestFile) -> tuple[int, bool, float, int, str]:
    priority_order = {"P2": 0, "P3": 1, "P5": 2, "G": 3}
    return (
        priority_order[record.priority],
        record.cost_seconds is None,
        -(record.cost_seconds or 0.0),
        -record.lines,
        record.path,
    )


def _summary(records: tuple[TestFile, ...]) -> str:
    """Per-area estate totals, for ``--summary`` only.

    Deliberately NOT part of ``render_queue``. Every cell here aggregates over
    every file in an area, so any two branches that add a backend test rewrite
    the same line -- the exact shape that made the committed report conflict on
    40% of merges. It is still the most useful two seconds of reading in the
    estate, so it is printed on demand instead of committed.
    """
    lines = [
        "| Area | Files | Test LOC | Canonical tests | Measured cost | Unknown-cost files |",
        "|------|------:|---------:|----------------:|--------------:|-------------------:|",
    ]
    for area in ("backend", "admin-ui", "admin-ui-viewport", "agent"):
        group = [record for record in records if record.area == area]
        measured = [record.cost_seconds for record in group if record.cost_seconds is not None]
        lines.append(
            f"| {area} | {len(group)} | {sum(r.lines for r in group):,} | "
            f"{sum(r.test_count for r in group):,} | "
            f"{sum(measured):,.3f}s ({len(measured)} files) | "
            f"{len(group) - len(measured)} |"
        )
    return "\n".join(lines)


def _cost_display(record: TestFile) -> str:
    if record.cost_seconds is None:
        return "unknown (no committed measurement)"
    return f"{record.cost_seconds:,.3f}s measured"


def _row(record: TestFile) -> str:
    """One queue row, derived from *record* and nothing else.

    Carries no ordinal and no aggregate on purpose -- see the module docstring.
    A row must stay a total function of its own test file so that two branches
    adding two different files produce two disjoint insertions.
    """
    protection = (
        f"protected; declared floor {record.protected_declared}"
        if record.protected_declared is not None
        else "—"
    )
    return (
        f"| {record.priority} | {record.area} | `{record.path}` | "
        f"{record.lines:,} | {record.test_count:,} | {record.threshold} | "
        f"{_cost_display(record)} | {protection} |"
    )


def render_queue(root: Path) -> str:
    """Render the queue deterministically from the current checkout.

    Rank is carried by row ORDER (highest priority first) rather than by an
    ordinal column: an ordinal is a global field, and a global field is what
    made this report conflict across concurrent branches.
    """
    records = collect_test_files(root)
    ranked = sorted(records, key=_rank_key)
    rows = [
        "| Priority | Area | File | LOC | Tests | Threshold class | Cost signal | Workday protection |",
        "|:--------:|------|------|----:|------:|------------------|-------------|---------------------|",
    ]
    rows.extend(_row(record) for record in ranked)
    return (
        "# Test Estate Ranked Queue\n\n"
        "> GENERATED FILE. Do not hand-edit mutable estate facts.\n"
        "> Regenerate with `python3 scripts/generate_test_estate_ranked_queue.py --write`;\n"
        "> validate with `python3 scripts/generate_test_estate_ranked_queue.py --check`.\n"
        "> Estate totals are not committed here — they aggregate across every file,\n"
        "> so every branch would rewrite them. Print them with\n"
        "> `python3 scripts/generate_test_estate_ranked_queue.py --summary`.\n"
        "> On Windows the interpreter is `python`: the python.org installer ships\n"
        "> no `python3`, so the commands above cannot run there as written.\n"
        "> Policy and threshold definitions: `backend/testing/test-estate-rubric.md`.\n\n"
        "Rows are in rank order, highest priority first; there is deliberately no rank "
        "ordinal, because an ordinal renumbers every row below an insertion and made this "
        "report conflict on 40% of merges. Every row is a function of its own file alone, "
        "so concurrent branches insert disjoint lines and merge cleanly.\n\n"
        "The queue includes every currently collected backend, admin-ui unit, admin-ui viewport, and agent test file. "
        "Backend duration coverage is partial: an `unknown` cost is intentionally not treated "
        "as zero or ranked as cheap. Admin-ui and agent costs are complete directional signals "
        "from their committed artifacts. Protected Workday counts are copied verbatim from the "
        "manifest for reporting only; this generator never edits that manifest.\n\n"
        "## Ranked queue\n\n"
        + "\n".join(rows)
        + "\n"
    )


def render_summary(root: Path) -> str:
    """The per-area estate totals, rendered from the current checkout."""
    return _summary(collect_test_files(root))


def _check(root: Path, output: Path) -> int:
    expected = render_queue(root)
    actual = output.read_text(encoding="utf-8") if output.exists() else ""
    if actual == expected:
        return 0
    diff = difflib.unified_diff(
        actual.splitlines(keepends=True),
        expected.splitlines(keepends=True),
        fromfile=str(output),
        tofile="generated queue",
    )
    print(
        "Test-estate queue is stale. Regenerate it with "
        "`python3 scripts/generate_test_estate_ranked_queue.py --write` "
        "(`python` on Windows, which ships no `python3`).\n"
        + "".join(diff),
        file=sys.stderr,
    )
    return 1


def main(argv: list[str] | None = None) -> int:
    force_utf8_streams()
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--write", action="store_true", help="write the derived queue")
    mode.add_argument("--check", action="store_true", help="fail if queue bytes are stale")
    mode.add_argument(
        "--summary",
        action="store_true",
        help=(
            "print the per-area estate totals. They are not committed: every "
            "cell aggregates over a whole area, so a committed copy is rewritten "
            "by every branch that adds a test."
        ),
    )
    parser.add_argument("--root", type=Path, default=REPOSITORY_ROOT)
    args = parser.parse_args(argv)
    root = args.root.resolve()
    output = root / QUEUE_PATH
    if args.summary:
        print(render_summary(root))
        return 0
    if args.write:
        # newline="" suppresses platform newline translation: a Windows
        # --write otherwise emits CRLF for every one of the ~1200 rows,
        # a whole-file diff that conflicts with every concurrent branch.
        output.write_text(render_queue(root), encoding="utf-8", newline="")
        return 0
    return _check(root, output)


if __name__ == "__main__":
    raise SystemExit(main())

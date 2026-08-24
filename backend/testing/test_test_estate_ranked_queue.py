"""Regression tests for the derived test-estate queue."""

from __future__ import annotations

import importlib.util
import json
import os
import re
import sys
from pathlib import Path
from typing import Any

import pytest

from testing.fixgate_attribution import format_offender_marker


NL = chr(10)
GENERATOR_RELATIVE_PATH = Path("scripts") / "generate_test_estate_ranked_queue.py"


def _first_root_with_generator(candidates: "list[Path]") -> Path | None:
    """Return the first candidate that actually holds the generator, else None.

    Never raises. A miss here is resolved while pytest is still importing this
    module, and an exception at that point aborts the entire collection rather
    than this one file.
    """
    for root in candidates:
        try:
            if (root / GENERATOR_RELATIVE_PATH).is_file():
                return root
        except OSError:
            continue
    return None


def _resolve_repository_root() -> Path | None:
    """Locate the repo root across local + Docker test environments.

    Candidates, in order: explicit env override, the ``/repo`` bind-mount
    (``scripts/test-backend.sh`` and the CI shards mount the repo root
    read-only at ``/repo`` while the backend source is mounted separately at
    ``/app``, so ``parents[2]`` resolves to ``/`` inside the container), then
    the repo root two levels above this file (plain checkouts, non-Docker).
    """
    candidates: list[Path] = []
    env_override = os.getenv("BLUEPRINT_REPO_ROOT")
    if env_override:
        candidates.append(Path(env_override))
    candidates.append(Path("/repo"))
    candidates.append(Path(__file__).resolve().parents[2])
    return _first_root_with_generator(candidates)


REPOSITORY_ROOT = _resolve_repository_root()

_MISSING_ROOT_MESSAGE = (
    "scripts/generate_test_estate_ranked_queue.py not available in this test "
    "environment (checked BLUEPRINT_REPO_ROOT, the /repo bind-mount that "
    "scripts/test-backend.sh and the CI shards provide, and ../.. relative to "
    "this file)"
)

_GENERATOR: Any = None


def _require_generator() -> Any:
    """Import the generator or skip — except in CI, where a miss FAILS loudly.

    Resolution is deliberately deferred out of module import. Raising while
    pytest is still collecting aborts the entire run, not just this file, so a
    single unresolvable path takes every other test in the lane down with it.
    """
    global _GENERATOR
    if _GENERATOR is not None:
        return _GENERATOR
    if REPOSITORY_ROOT is None:
        if os.getenv("CI"):
            pytest.fail(
                _MISSING_ROOT_MESSAGE + " — refusing to skip in CI: mount the "
                "repo root at /repo or set BLUEPRINT_REPO_ROOT so this "
                "invariant enforces"
            )
        pytest.skip(_MISSING_ROOT_MESSAGE)
    spec = importlib.util.spec_from_file_location(
        "generate_test_estate_ranked_queue",
        REPOSITORY_ROOT / "scripts" / "generate_test_estate_ranked_queue.py",
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    _GENERATOR = module
    return module


def collect_test_files(*args: Any, **kwargs: Any) -> Any:
    return _require_generator().collect_test_files(*args, **kwargs)


def main(*args: Any, **kwargs: Any) -> Any:
    return _require_generator().main(*args, **kwargs)


def render_queue(*args: Any, **kwargs: Any) -> Any:
    return _require_generator().render_queue(*args, **kwargs)


def _write_fixture_root(root: Path) -> None:
    (root / "backend" / "testing").mkdir(parents=True)
    (root / "admin-ui" / "src").mkdir(parents=True)
    (root / "agent" / "src").mkdir(parents=True)
    (root / "backend" / ".test_durations").write_text("{}", encoding="utf-8")
    for suite in ("admin-ui", "agent"):
        (root / suite / ".test_durations.json").write_text(
            json.dumps(
                {
                    "files": [
                        {
                            "file": "src/example.test.ts",
                            "duration_ms": 125,
                            "measured": True,
                        }
                    ]
                }
            ),
            encoding="utf-8",
        )
    (root / "admin-ui" / "src" / "example.test.ts").write_text(
        'it("renders", () => expect(true).toBe(true));\n', encoding="utf-8"
    )
    (root / "agent" / "src" / "example.test.ts").write_text(
        'test("runs", () => expect(true).toBe(true));\n', encoding="utf-8"
    )
    (root / "backend" / "testing" / "test_example.py").write_text(
        "def test_example():\n    assert 1 == 1\n", encoding="utf-8"
    )
    (root / "backend" / "testing" / "conversational_workday_manifest.json").write_text(
        json.dumps({"protect": []}), encoding="utf-8"
    )


def test_repository_root_resolution_never_raises_on_a_missing_checkout(
    tmp_path: Path,
) -> None:
    """A miss returns None instead of raising, so collection survives it.

    The previous form resolved the generator from ``Path(__file__).parents[2]``
    at import time. Inside the test container the backend tree is mounted at
    ``/app``, so that expression yields ``/`` and the load raised
    FileNotFoundError while pytest was still collecting — which aborts the
    whole run, not just this file, and took all eight backend CI shards down.
    """
    assert _first_root_with_generator([tmp_path, tmp_path / "absent"]) is None
    assert REPOSITORY_ROOT is not None
    assert _first_root_with_generator([tmp_path, REPOSITORY_ROOT]) == REPOSITORY_ROOT


_RANKED_ROW = re.compile(r"^\| \d+ \| \S+ \| \S+ \| `([^`]+)` \|", re.MULTILINE)


# Measured cost of the one full sweep this guard runs: 15.8s on a native
# filesystem, 37.1s inside the standalone lane's container (Docker-on-Windows
# bind mount, ~2.3x slower at both I/O and CPU). The work is irreducible --
# 1,170 test files plus 1,061 transitively imported modules, 47 MB, read and
# parsed to derive canonical test counts. Two upstream fixes already cut the
# container cost from >120s to 37.1s (the node_modules walk and the
# per-reference revalidation stat); what is left is reading and parsing the
# tree, which the guard exists to do.
#
# So the 30s the standalone lane passes on the command line is not a budget
# this test can meet -- it left zero headroom over a 15.8s native cost and the
# guard died on a timeout, not an assertion, which reads as a red gate with no
# diagnosis. The explicit marker overrides it. Keep the number well clear of
# the measurement: a shared CI runner competing for four slots is slower again.
@pytest.mark.timeout(180)
def test_checked_in_queue_ranks_every_collected_test_file() -> None:
    """Every current test file is ranked, and no vanished file still is.

    Deliberately not byte-equality against a fresh render of the real tree.
    The LOC and test-count columns move whenever any of ~1000 test files is
    edited, and the rank column reshuffles when they do, so an exact match on
    a 991-row derived document is stale within hours of being written and
    turns a reporting artifact into a merge-conflict ratchet across every
    concurrent branch. Byte-exact rendering is still pinned, against the
    deterministic fixture tree in
    ``test_check_reports_threshold_crossing_until_queue_is_regenerated`` where
    it cannot rot. What must not rot here is coverage: a new test file missing
    from the queue, or a deleted one still ranked, is the drift this guards.
    """
    generator = _require_generator()
    assert REPOSITORY_ROOT is not None
    queue_text = (REPOSITORY_ROOT / "test-estate-ranked-queue.md").read_text(
        encoding="utf-8"
    )
    ranked = set(_RANKED_ROW.findall(queue_text))
    assert ranked, "the checked-in queue has no ranked rows at all"
    collected = {record.path for record in generator.collect_test_files(REPOSITORY_ROOT)}

    missing = sorted(collected - ranked)
    assert not missing, (
        "test files exist but are absent from test-estate-ranked-queue.md; "
        "regenerate with `python3 scripts/generate_test_estate_ranked_queue.py "
        "--write`:" + NL
        + NL.join(f"  - {path}" for path in missing)
        + NL
        + format_offender_marker(missing)
    )
    vanished = sorted(ranked - collected)
    assert not vanished, (
        "test-estate-ranked-queue.md ranks files that are no longer collected; "
        "regenerate with `python3 scripts/generate_test_estate_ranked_queue.py "
        "--write`:" + NL
        + NL.join(f"  - {path}" for path in vanished)
        + NL
        + format_offender_marker(vanished)
    )


def test_generator_uses_canonical_counts_and_preserves_manifest_floor(tmp_path: Path) -> None:
    _write_fixture_root(tmp_path)
    backend_file = tmp_path / "backend" / "testing" / "test_example.py"
    backend_file.write_text(
        "import unittest\n\n"
        "class Helper(unittest.TestCase):\n"
        "    def test_inherited_collector_shape(self):\n"
        "        assert True\n\n"
        "def test_function():\n"
        "    assert True\n",
        encoding="utf-8",
    )
    manifest = {
        "protect": [
            {
                "path": "backend/testing/test_example.py",
                "test_ids": ["test_function"],
                "test_count": 9,
            }
        ]
    }
    manifest_path = tmp_path / "backend/testing/conversational_workday_manifest.json"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    before = manifest_path.read_bytes()

    records = collect_test_files(tmp_path)
    backend_record = next(record for record in records if record.area == "backend")
    assert backend_record.test_count == 2
    assert backend_record.protected_declared == 9
    assert backend_record.cost_seconds is None
    output = render_queue(tmp_path)
    assert "protected; declared floor 9" in output
    assert "unknown (no committed measurement)" in output
    assert manifest_path.read_bytes() == before


def test_check_reports_threshold_crossing_until_queue_is_regenerated(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _write_fixture_root(tmp_path)
    assert main(["--write", "--root", str(tmp_path)]) == 0
    assert main(["--check", "--root", str(tmp_path)]) == 0

    fixture = tmp_path / "backend" / "testing" / "test_example.py"
    fixture.write_text("def test_example():\n    assert 1 == 1\n" + "# growth\n" * 1500)
    assert main(["--check", "--root", str(tmp_path)]) == 1
    error = capsys.readouterr().err
    assert "Test-estate queue is stale" in error
    assert "moderate (1500\u20132999 LOC)" in error

    assert main(["--write", "--root", str(tmp_path)]) == 0
    assert main(["--check", "--root", str(tmp_path)]) == 0
    assert "moderate (1500\u20132999 LOC)" in (
        tmp_path / "test-estate-ranked-queue.md"
    ).read_text(encoding="utf-8")


def test_measured_frontend_cost_is_directional_signal(tmp_path: Path) -> None:
    _write_fixture_root(tmp_path)
    output = render_queue(tmp_path)
    assert "admin-ui/src/example.test.ts" in output
    assert "0.125s measured" in output
    assert output.index("0.125s measured") < output.index(
        "backend/testing/test_example.py"
    )


def _each_row_ids(source: str, path: Path) -> set[str]:
    from testing.identity_extraction import extract_typescript_test_ids

    return extract_typescript_test_ids(source, path)


def test_typescript_import_graph_caches_are_invalidated_by_content(
    tmp_path: Path,
) -> None:
    """The import-graph memo must never outlive the file it summarizes.

    Resolving one test file's transitive relative imports is a pure function of
    the files' contents, so ``identity_extraction`` memoizes the read, the
    resolution and the per-module parse -- without which a single
    ``collect_test_files`` pass over the estate re-parses ~100k modules and
    blows the 60s suite timeout that
    ``test_checked_in_queue_ranks_every_collected_test_file`` runs under.

    A content-keyed cache is only safe if it actually notices content changing.
    This pins all three invalidation paths in one process, because that is the
    shape a stale hit would take: the fixture is rewritten and re-read without
    the interpreter restarting.
    """
    table = tmp_path / "table.ts"
    table.write_text(
        "export const CASES = [['a'], ['b']];\n", encoding="utf-8"
    )
    spec = tmp_path / "suite.test.ts"
    source = (
        "import { CASES } from './table';\n"
        "describe('suite', () => {\n"
        "  it.each(CASES)('case %s', () => {});\n"
        "});\n"
    )
    spec.write_text(source, encoding="utf-8")

    assert len(_each_row_ids(source, spec)) == 2, (
        "fixture check: a two-row imported table expands to two identities"
    )

    # 1. The imported module grows. Its row count must grow with it.
    table.write_text(
        "export const CASES = [['a'], ['b'], ['c'], ['d']];\n", encoding="utf-8"
    )
    os.utime(table, (0, 0))  # defeat a coarse mtime clock, not the cache
    assert len(_each_row_ids(source, spec)) == 4, (
        "a rewritten imported table was served from a stale source/parse cache"
    )

    # 2. The module disappears. The resolver must stop returning it, falling
    #    back to the conservative single row rather than the remembered four.
    table.unlink()
    assert len(_each_row_ids(source, spec)) == 1, (
        "a deleted module was still resolved from the module-resolution cache"
    )

    # 3. It comes back with different contents.
    table.write_text(
        "export const CASES = [['a'], ['b'], ['c']];\n", encoding="utf-8"
    )
    assert len(_each_row_ids(source, spec)) == 3, (
        "a recreated module was resolved to a stale remembered answer"
    )


def test_typescript_import_cycles_still_terminate(tmp_path: Path) -> None:
    """Memoizing the parse must not turn a cyclic import graph into a hang.

    Two modules importing each other is legal TypeScript and appears in the
    estate. The walk breaks the cycle on the visited set; the caches sit
    underneath that and must not resurrect the edge it dropped.
    """
    left = tmp_path / "left.ts"
    right = tmp_path / "right.ts"
    left.write_text(
        "import { RIGHT } from './right';\n"
        "export const LEFT = [[1], [2]];\n",
        encoding="utf-8",
    )
    right.write_text(
        "import { LEFT } from './left';\n"
        "export const RIGHT = [[1], [2], [3]];\n",
        encoding="utf-8",
    )
    source = (
        "import { LEFT } from './left';\n"
        "describe('cyclic', () => {\n"
        "  it.each(LEFT)('row %s', () => {});\n"
        "});\n"
    )
    spec = tmp_path / "cyclic.test.ts"
    spec.write_text(source, encoding="utf-8")

    assert len(_each_row_ids(source, spec)) == 2


def _table_fixture(tmp_path: Path, rows: int) -> tuple[str, Path]:
    """A spec whose it.each table lives in a separate imported module."""
    table = tmp_path / "table.ts"
    cases = ", ".join(f"['r{i}']" for i in range(rows))
    table.write_text(f"export const CASES = [{cases}];" + NL, encoding="utf-8")
    spec = tmp_path / "suite.test.ts"
    source = (
        "import { CASES } from './table';" + NL
        + "describe('suite', () => {" + NL
        + "  it.each(CASES)('case %s', () => {});" + NL
        + "});" + NL
    )
    spec.write_text(source, encoding="utf-8")
    return source, spec


def test_frozen_typescript_tree_is_opt_in_and_does_not_leak(tmp_path: Path) -> None:
    """The bulk-sweep fast path must be explicit, and must end when it ends.

    ``frozen_typescript_tree`` buys its speed by declaring the tree immutable
    and skipping the revalidation stat on every cache hit. That is only sound
    for a read-only sweep, so it is deliberately not the default -- being wrong
    here means silently reporting a stale test estate, which is exactly the
    class of bug the estate guards exist to catch.

    Two halves, and the second is the one that matters: inside the context a
    rewritten module is intentionally served from cache, and on exit
    revalidation must resume. A leaked flag would make every later caller in
    the same process blind to edits, including the fixtures in
    ``test_typescript_import_graph_caches_are_invalidated_by_content``.
    """
    from testing.identity_extraction import frozen_typescript_tree

    source, spec = _table_fixture(tmp_path, 2)
    table = tmp_path / "table.ts"

    assert len(_each_row_ids(source, spec)) == 2, "fixture check"

    with frozen_typescript_tree():
        table.write_text(
            "export const CASES = [['a'], ['b'], ['c'], ['d']];" + NL,
            encoding="utf-8",
        )
        assert len(_each_row_ids(source, spec)) == 2, (
            "inside the frozen context the cached parse is authoritative -- "
            "that is the entire point of the context, and the reason it is "
            "opt-in rather than the default"
        )

    assert len(_each_row_ids(source, spec)) == 4, (
        "frozen_typescript_tree leaked: revalidation must resume on exit"
    )


def test_frozen_typescript_tree_stops_paying_a_stat_per_reference(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Repeated resolution of one module must cost one stat, not one each.

    This is the measurement the context exists for. Over the real estate the
    unfrozen sweep spends 103,550 stat calls to perform 2,231 reads -- ~46
    revalidation stats per file actually opened -- because the same imported
    modules are reached from hundreds of entry points. On a native filesystem
    that is ~18s of waste. Over the Docker-on-Windows bind mount the standalone
    lane runs on, where stat is ~100x slower, it is what pushed
    ``test_checked_in_queue_ranks_every_collected_test_file`` past its timeout.

    Asserting "zero further stats" rather than "fewer stats" so that quietly
    reintroducing per-reference revalidation fails here instead of only showing
    up as a timeout in the container, which is where it is most expensive to
    diagnose.
    """
    from testing.identity_extraction import frozen_typescript_tree

    source, spec = _table_fixture(tmp_path, 3)

    calls: list[Path] = []
    real_stat = Path.stat

    def counting_stat(self: Path, *args: Any, **kwargs: Any) -> Any:
        calls.append(self)
        return real_stat(self, *args, **kwargs)

    monkeypatch.setattr(Path, "stat", counting_stat)

    with frozen_typescript_tree():
        assert len(_each_row_ids(source, spec)) == 3
        warm = len(calls)
        for _ in range(20):
            assert len(_each_row_ids(source, spec)) == 3
        extra = [str(path) for path in calls[warm:]]
        assert not extra, (
            f"20 repeat resolutions inside the frozen context made {len(extra)} "
            "extra stat call(s); the memo is supposed to be authoritative there"
            + NL
            + format_offender_marker(sorted(set(extra)))
        )

    before = len(calls)
    _each_row_ids(source, spec)
    assert len(calls) > before, (
        "outside the context every hit must still revalidate -- otherwise the "
        "default is unsafe, not just the fast path"
    )


def test_collect_test_files_runs_the_sweep_frozen(tmp_path: Path) -> None:
    """Pin that the bulk sweep actually enters the context.

    Without this the freeze could be dropped from ``collect_test_files`` and
    every test above would still pass, because they exercise the context
    directly. The cost would come back silently and reappear only as a
    container timeout, which is the most expensive place to diagnose it.

    The flag is read off ``frozen_typescript_tree.__module__`` rather than a
    plain ``import testing.identity_extraction``. The generator is loaded from
    a file path and imports ``backend.testing.identity_extraction``, while this
    file's own imports resolve ``testing.identity_extraction`` -- two distinct
    module objects with independent globals in the same process. Reading the
    wrong one reports every record as unfrozen even when the sweep is fully
    frozen, so the observation has to follow the generator's own binding.

    Runs against a tmp fixture root, not ``REPOSITORY_ROOT``: a second full
    sweep would add ~15s to a file that already spends ~16s on one, against a
    30s per-test budget.
    """
    _write_fixture_root(tmp_path)

    generator = _require_generator()
    identity = sys.modules[generator.frozen_typescript_tree.__module__]
    observed: list[bool] = []
    real_record = generator._record

    def observing_record(*args: Any, **kwargs: Any) -> Any:
        # args[2] is the repo-relative path _record stamps onto the row.
        observed.append((bool(identity._TYPESCRIPT_TREE_FROZEN), str(args[2])))
        return real_record(*args, **kwargs)

    generator._record = observing_record
    try:
        generator.collect_test_files(tmp_path)
    finally:
        generator._record = real_record

    assert observed, "collect_test_files recorded nothing"
    thawed = [relative for frozen, relative in observed if not frozen]
    assert not thawed, (
        f"{len(thawed)} of {len(observed)} records were built outside "
        "frozen_typescript_tree()"
        + NL
        + format_offender_marker(sorted(thawed))
    )
    assert not identity._TYPESCRIPT_TREE_FROZEN, (
        "collect_test_files left the tree frozen for everything that runs after it"
    )

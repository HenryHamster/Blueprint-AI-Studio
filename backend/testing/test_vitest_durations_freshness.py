"""
Standalone guard: agent/.test_durations.json and admin-ui/.test_durations.json
must exist, be valid, and contain real timing data so classification/cut work
has a ranked signal to consume.

Coverage is enforced on two axes: the artifact's file population must match the
on-disk inventory the Vitest config collects, AND every entry must carry a real
measurement. scripts/vitest-test-telemetry.mjs emits an inventory file that
produced no reporter result as `measured: false` with a null duration (and exits
non-zero), so a run aborted partway through the suite cannot be committed green.

Runs under --noconftest (no DB, no conftest fixtures).

To regenerate: node scripts/vitest-test-telemetry.mjs  (from workspace root)
"""
import json
import subprocess
import warnings
from pathlib import Path

from testing.estate_inventory import (
    is_vitest_test_path,
    vitest_test_file_inventory,
    vitest_test_file_inventory_from_paths,
)

def _resolve_workspace_root() -> Path:
    candidates = (Path(__file__).resolve().parents[2], Path("/repo"))
    for candidate in candidates:
        if (
            (candidate / "agent" / ".test_durations.json").is_file()
            and (candidate / "admin-ui" / ".test_durations.json").is_file()
        ):
            return candidate
    return candidates[0]


WORKSPACE_ROOT = _resolve_workspace_root()
AGENT_DURATIONS = WORKSPACE_ROOT / "agent" / ".test_durations.json"
ADMINUI_DURATIONS = WORKSPACE_ROOT / "admin-ui" / ".test_durations.json"


def _is_suite_test_path(suite: str, relative: str) -> bool:
    """Return whether a suite-relative posix path belongs to that suite's roster."""
    return is_vitest_test_path(suite, relative)


def _test_file_inventory_from_paths(suite: str, paths) -> set[str]:
    return vitest_test_file_inventory_from_paths(suite, paths)


def _test_file_inventory(suite: str) -> set[str]:
    """Return the files collected by the suite's committed Vitest config."""
    return vitest_test_file_inventory(WORKSPACE_ROOT, suite)


class BaselineUnavailable(RuntimeError):
    """Raised when the automain baseline cannot be resolved for attribution."""


def _git(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-c", f"safe.directory={WORKSPACE_ROOT}", *args],
        cwd=WORKSPACE_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )


def _resolve_baseline_ref(base: str = "automain") -> str:
    for candidate in (f"origin/{base}", base):
        if _git("rev-parse", "--verify", candidate).returncode == 0:
            return candidate
    raise BaselineUnavailable(f"cannot resolve base ref origin/{base} or {base}")


def _baseline_paths(suite: str, base_ref: str) -> set[str]:
    result = _git("ls-tree", "-r", "--name-only", base_ref, "--", f"{suite}/")
    if result.returncode != 0:
        raise BaselineUnavailable(
            f"git ls-tree failed for {suite}/ at {base_ref}: {result.stderr.strip()}"
        )
    prefix = f"{suite}/"
    return {
        line[len(prefix) :]
        for line in result.stdout.splitlines()
        if line.startswith(prefix)
    }


def _baseline_artifact(suite: str, base_ref: str) -> dict:
    result = _git("show", f"{base_ref}:{suite}/.test_durations.json")
    if result.returncode != 0:
        raise BaselineUnavailable(
            f"git show failed for {suite}/.test_durations.json at {base_ref}: "
            f"{result.stderr.strip()}"
        )
    return json.loads(result.stdout)


def _baseline_drift(suite: str, base_ref: str) -> tuple[set[str], set[str]]:
    """Return the (missing, stale) sets already present on the baseline ref."""
    expected = _test_file_inventory_from_paths(suite, _baseline_paths(suite, base_ref))
    actual = {entry.get("file") for entry in _baseline_artifact(suite, base_ref).get("files", [])}
    return expected - actual, actual - expected


def _warn_suppressed_baseline_drift(
    suite: str,
    base_ref: str,
    suppressed_missing: set[str],
    suppressed_stale: set[str],
) -> None:
    """Name every entry this guard is relying on the baseline to excuse.

    Attribution is a suppression: an entry that is red both here and on the
    baseline is subtracted and never reported. Left silent, inventory drift
    that lands on automain becomes invisible forever with no signal to
    regenerate -- and on the automain->main promotion PR (where the baseline
    IS the tree under test) the coverage guard would degrade to a no-op with
    nothing recorded. Emit a non-failing warning so the suppression is always
    visible in the pytest warnings summary.
    """
    if not suppressed_missing and not suppressed_stale:
        return
    missing = sorted(suppressed_missing)
    stale = sorted(suppressed_stale)
    warnings.warn(
        f"\n\n  SUPPRESSED pre-existing {suite}/.test_durations.json inventory drift "
        f"(already red at baseline {base_ref}):\n"
        f"    missing {len(missing)} file(s): {missing[:10]}\n"
        f"    stale {len(stale)} file(s): {stale[:10]}\n"
        f"  These are not attributed to this change, but the artifact stays out of "
        f"sync until someone regenerates it:\n"
        f"    node scripts/vitest-test-telemetry.mjs --suite {suite}\n",
        stacklevel=2,
    )


def _assert_artifact_covers_inventory(suite: str, data: dict) -> None:
    """Fail only on inventory drift the task itself introduced.

    A whole-suite artifact regeneration is expensive (a full Vitest run), so
    charging a task that merely adds/deletes/renames one test file with a
    pre-existing whole-artifact drift makes the guard unrepairable from that
    task. Attribute missing/stale entries against the automain baseline's own
    drift and fail only on entries that are new relative to it. If the
    baseline can't be resolved, fail closed to the original absolute check.
    """
    expected = _test_file_inventory(suite)
    actual = {entry.get("file") for entry in data.get("files", [])}
    missing = expected - actual
    stale = actual - expected

    try:
        base_ref = _resolve_baseline_ref()
        baseline_missing, baseline_stale = _baseline_drift(suite, base_ref)
    except BaselineUnavailable:
        new_missing, new_stale = missing, stale
    else:
        new_missing = missing - baseline_missing
        new_stale = stale - baseline_stale
        _warn_suppressed_baseline_drift(
            suite,
            base_ref,
            suppressed_missing=missing & baseline_missing,
            suppressed_stale=stale & baseline_stale,
        )

    new_missing = sorted(new_missing)
    new_stale = sorted(new_stale)
    assert not new_missing and not new_stale, (
        f"{suite}/.test_durations.json is out of sync with Vitest's on-disk test inventory. "
        f"Missing {len(new_missing)} file(s): {new_missing[:10]}; "
        f"stale {len(new_stale)} file(s): {new_stale[:10]}. "
        f"Regenerate: node scripts/vitest-test-telemetry.mjs --suite {suite}"
    )


def _assert_all_entries_measured(suite: str, data: dict) -> None:
    """Reject artifacts containing entries the generator never measured.

    An aborted or partial regeneration records the unexecuted files as
    ``measured: false`` / ``duration_ms: null``. Those entries carry no cost
    signal, so an artifact containing any of them must not be committed.
    """
    unmeasured = sorted(
        entry.get("file")
        for entry in data.get("files", [])
        if entry.get("measured") is not True or not isinstance(
            entry.get("duration_ms"), (int, float)
        )
    )
    assert not unmeasured, (
        f"{suite}/.test_durations.json contains {len(unmeasured)} unmeasured "
        f"entry/entries: {unmeasured[:10]}. The regeneration run did not execute "
        "these files (aborted run, crash, or OOM), so their cost is unknown. "
        f"Rerun until every file is measured: "
        f"node scripts/vitest-test-telemetry.mjs --suite {suite}"
    )


def test_measured_guard_rejects_unmeasured_entries() -> None:
    """An aborted regeneration marks unexecuted files — the guard must fail."""
    data = {
        "files": [
            {"file": "src/a.test.ts", "duration_ms": 120, "tests": 4, "measured": True},
            {
                "file": "src/b.test.ts",
                "duration_ms": None,
                "tests": None,
                "measured": False,
            },
        ]
    }
    try:
        _assert_all_entries_measured("agent", data)
    except AssertionError as exc:
        assert "src/b.test.ts" in str(exc)
    else:
        raise AssertionError("unmeasured entry was accepted by the freshness guard")


def test_measured_guard_rejects_zero_filled_entry_without_measured_flag() -> None:
    """A fabricated 0ms/0-test entry has no measured flag and must be rejected."""
    data = {
        "files": [
            {"file": "src/a.test.ts", "duration_ms": 120, "tests": 4, "measured": True},
            {"file": "src/b.test.ts", "duration_ms": 0, "tests": 0, "flaky": False},
        ]
    }
    try:
        _assert_all_entries_measured("admin-ui", data)
    except AssertionError as exc:
        assert "src/b.test.ts" in str(exc)
    else:
        raise AssertionError("zero-filled entry was accepted by the freshness guard")


def test_measured_guard_accepts_fully_measured_artifact() -> None:
    data = {
        "files": [
            {"file": "src/a.test.ts", "duration_ms": 120, "tests": 4, "measured": True},
            {"file": "src/b.test.ts", "duration_ms": 0, "tests": 1, "measured": True},
        ]
    }
    _assert_all_entries_measured("agent", data)


# ── baseline-attributed inventory coverage ──────────────────────────────────

def _patch_baseline(monkeypatch, missing: set[str], stale: set[str]) -> None:
    monkeypatch.setattr(
        "test_vitest_durations_freshness._resolve_baseline_ref",
        lambda base="automain": "origin/automain",
    )
    monkeypatch.setattr(
        "test_vitest_durations_freshness._baseline_drift",
        lambda suite, base_ref: (set(missing), set(stale)),
    )


def _patch_inventory(monkeypatch, expected: set[str]) -> None:
    monkeypatch.setattr(
        "test_vitest_durations_freshness._test_file_inventory",
        lambda suite: set(expected),
    )


def test_coverage_guard_ignores_preexisting_baseline_drift(monkeypatch) -> None:
    """A file missing on both the baseline and the current tree is pre-existing red."""
    _patch_inventory(monkeypatch, {"src/a.test.ts", "src/missing_on_base.test.ts"})
    _patch_baseline(monkeypatch, missing={"src/missing_on_base.test.ts"}, stale=set())
    data = {"files": [{"file": "src/a.test.ts", "duration_ms": 10, "measured": True}]}
    _assert_artifact_covers_inventory("agent", data)


def test_coverage_guard_rejects_missing_inventory_entry(monkeypatch) -> None:
    """A file collected by the inventory but absent from the artifact fails,
    even when the baseline itself has unrelated pre-existing drift.

    This is the case the parent investigation flagged as broken: a task that
    adds a JS/TS test file must still be caught if it forgets to regenerate
    the durations artifact, rather than being suppressed alongside whatever
    is already red on automain.
    """
    _patch_inventory(monkeypatch, {"src/a.test.ts", "src/b_task_added.test.ts"})
    _patch_baseline(monkeypatch, missing={"src/unrelated_preexisting_gap.test.ts"}, stale=set())
    data = {"files": [{"file": "src/a.test.ts", "duration_ms": 10, "measured": True}]}
    try:
        _assert_artifact_covers_inventory("agent", data)
    except AssertionError as exc:
        assert "src/b_task_added.test.ts" in str(exc)
        assert "src/unrelated_preexisting_gap.test.ts" not in str(exc)
    else:
        raise AssertionError("guard accepted an artifact missing a genuinely new inventory entry")


def test_coverage_guard_still_fails_on_task_introduced_stale_entry(monkeypatch) -> None:
    """A stale artifact entry absent from the baseline's own stale set fails."""
    _patch_inventory(monkeypatch, {"src/a.test.ts"})
    _patch_baseline(monkeypatch, missing=set(), stale=set())
    data = {
        "files": [
            {"file": "src/a.test.ts", "duration_ms": 10, "measured": True},
            {"file": "src/deleted_but_not_regenerated.test.ts", "duration_ms": 10, "measured": True},
        ]
    }
    try:
        _assert_artifact_covers_inventory("agent", data)
    except AssertionError as exc:
        assert "src/deleted_but_not_regenerated.test.ts" in str(exc)
    else:
        raise AssertionError("guard accepted a task-introduced stale artifact entry")


def test_coverage_guard_fails_closed_when_baseline_unresolvable(monkeypatch) -> None:
    """If automain can't be resolved, fall back to the original absolute check."""

    def _raise(base: str = "automain") -> str:
        raise BaselineUnavailable("no automain in this checkout")

    monkeypatch.setattr("test_vitest_durations_freshness._resolve_baseline_ref", _raise)
    _patch_inventory(monkeypatch, {"src/a.test.ts", "src/b.test.ts"})
    data = {"files": [{"file": "src/a.test.ts", "duration_ms": 10, "measured": True}]}
    try:
        _assert_artifact_covers_inventory("agent", data)
    except AssertionError as exc:
        assert "src/b.test.ts" in str(exc)
    else:
        raise AssertionError(
            "guard silently passed with unresolvable baseline instead of failing closed"
        )


def test_coverage_guard_warns_when_it_suppresses_baseline_drift(monkeypatch) -> None:
    """Suppressed pre-existing drift must be named, not silently subtracted."""
    _patch_inventory(monkeypatch, {"src/a.test.ts", "src/missing_on_base.test.ts"})
    _patch_baseline(
        monkeypatch,
        missing={"src/missing_on_base.test.ts"},
        stale={"src/stale_on_base.test.ts"},
    )
    data = {
        "files": [
            {"file": "src/a.test.ts", "duration_ms": 10, "measured": True},
            {"file": "src/stale_on_base.test.ts", "duration_ms": 10, "measured": True},
        ]
    }
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        _assert_artifact_covers_inventory("agent", data)
    messages = [str(w.message) for w in caught]
    assert messages, "suppressed baseline drift produced no warning at all"
    joined = "\n".join(messages)
    assert "src/missing_on_base.test.ts" in joined
    assert "src/stale_on_base.test.ts" in joined
    assert "origin/automain" in joined, "warning must name the base ref it relied on"


def test_coverage_guard_is_silent_when_nothing_is_suppressed(monkeypatch) -> None:
    """A clean artifact must not emit the suppression warning."""
    _patch_inventory(monkeypatch, {"src/a.test.ts"})
    _patch_baseline(monkeypatch, missing={"src/unrelated.test.ts"}, stale=set())
    data = {"files": [{"file": "src/a.test.ts", "duration_ms": 10, "measured": True}]}
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        _assert_artifact_covers_inventory("agent", data)
    assert not [str(w.message) for w in caught if "SUPPRESSED" in str(w.message)]


# ── real git tree fixture for the baseline attribution path ─────────────────

def _make_baseline_repo(tmp_path: Path) -> Path:
    """Build a throwaway git repo whose HEAD branch is named ``automain``.

    The tree mirrors the real layout: suite-prefixed paths (``agent/src/...``)
    plus a committed durations artifact, so the helpers exercise genuine
    ``git ls-tree`` / ``git show`` output rather than a stubbed return value.
    """
    root = tmp_path / "repo"
    (root / "agent" / "src").mkdir(parents=True)
    (root / "agent" / "src" / "a.test.ts").write_text("// covered\n")
    (root / "agent" / "src" / "b.test.ts").write_text("// missing from artifact\n")
    (root / "agent" / "src" / "helper.ts").write_text("// not a test file\n")
    (root / "agent" / "README.md").write_text("not a test file\n")
    (root / "agent" / ".test_durations.json").write_text(
        json.dumps(
            {
                "files": [
                    {"file": "src/a.test.ts", "duration_ms": 10, "measured": True},
                    {"file": "src/gone.test.ts", "duration_ms": 20, "measured": True},
                ]
            }
        )
    )
    env_args = [
        "-c",
        "user.email=guard@example.com",
        "-c",
        "user.name=Guard Fixture",
    ]
    subprocess.run(
        ["git", "init", "-q", "-b", "automain", str(root)],
        capture_output=True,
        text=True,
        check=True,
    )
    subprocess.run(
        ["git", *env_args, "add", "-A"], cwd=root, capture_output=True, text=True, check=True
    )
    subprocess.run(
        ["git", *env_args, "commit", "-q", "-m", "baseline"],
        cwd=root,
        capture_output=True,
        text=True,
        check=True,
    )
    return root


def test_baseline_paths_strips_the_suite_prefix_on_a_real_tree(monkeypatch, tmp_path) -> None:
    """``_baseline_paths`` must return suite-RELATIVE posix paths.

    If the prefix strip ever regressed, ``_test_file_inventory_from_paths``
    would classify nothing, the baseline expected set would be empty, and the
    guard would fail OPEN -- every stale entry in the current tree suppressed.
    Pin the stripped shape against real ``git ls-tree`` output.
    """
    root = _make_baseline_repo(tmp_path)
    monkeypatch.setattr("test_vitest_durations_freshness.WORKSPACE_ROOT", root)
    base_ref = _resolve_baseline_ref()
    assert base_ref == "automain"
    paths = _baseline_paths("agent", base_ref)
    assert "src/a.test.ts" in paths
    assert "src/b.test.ts" in paths
    assert not any(path.startswith("agent/") for path in paths), (
        f"suite prefix was not stripped: {sorted(paths)}"
    )
    expected = _test_file_inventory_from_paths("agent", paths)
    assert expected == {"src/a.test.ts", "src/b.test.ts"}, (
        "baseline expected-set must be non-empty and test-only; an empty set here "
        "is the fail-open mode this guard exists to prevent"
    )


def test_baseline_drift_reads_a_real_tree_and_artifact(monkeypatch, tmp_path) -> None:
    """End-to-end: ls-tree + git show + set algebra against a real commit."""
    root = _make_baseline_repo(tmp_path)
    monkeypatch.setattr("test_vitest_durations_freshness.WORKSPACE_ROOT", root)
    assert _baseline_artifact("agent", "automain")["files"][0]["file"] == "src/a.test.ts"
    missing, stale = _baseline_drift("agent", "automain")
    assert missing == {"src/b.test.ts"}
    assert stale == {"src/gone.test.ts"}


def test_baseline_helpers_raise_when_the_ref_is_absent(monkeypatch, tmp_path) -> None:
    """A resolvable-looking but absent ref must fail closed, not return empty."""
    root = _make_baseline_repo(tmp_path)
    monkeypatch.setattr("test_vitest_durations_freshness.WORKSPACE_ROOT", root)
    try:
        _resolve_baseline_ref("no-such-branch")
    except BaselineUnavailable:
        pass
    else:
        raise AssertionError("_resolve_baseline_ref accepted a nonexistent base branch")
    try:
        _baseline_paths("agent", "no-such-ref")
    except BaselineUnavailable:
        pass
    else:
        raise AssertionError("_baseline_paths returned instead of failing closed")
    try:
        _baseline_artifact("agent", "no-such-ref")
    except BaselineUnavailable:
        pass
    else:
        raise AssertionError("_baseline_artifact returned instead of failing closed")


# Minimum distinct duration values before the file is considered degenerate
# (synthetic placeholder artifacts would have 1 or 2 distinct values).
_DISTINCT_VALUE_FLOOR = 10

# Warn threshold — visible in pytest's warnings summary without failing.
_DEGENERATE_WARN_THRESHOLD = 5


def _load(path: Path) -> dict:
    assert path.exists(), (
        f"{path} not found. Regenerate with: "
        "node scripts/vitest-test-telemetry.mjs"
    )
    with path.open() as f:
        return json.load(f)


# ── agent suite ─────────────────────────────────────────────────────────────

def test_agent_artifact_exists() -> None:
    assert AGENT_DURATIONS.exists(), (
        f"agent/.test_durations.json missing at {AGENT_DURATIONS}. "
        "Regenerate: node scripts/vitest-test-telemetry.mjs --suite agent"
    )


def test_agent_artifact_is_valid_json() -> None:
    data = _load(AGENT_DURATIONS)
    assert isinstance(data, dict), f"Expected a JSON object, got {type(data).__name__}"
    assert "_meta" in data, "Missing '_meta' key"
    assert "files" in data, "Missing 'files' key"


def test_agent_artifact_meta_fields() -> None:
    data = _load(AGENT_DURATIONS)
    meta = data["_meta"]
    assert meta.get("suite") == "agent", f"Expected suite='agent', got {meta.get('suite')!r}"
    assert "generated" in meta, "Missing '_meta.generated'"
    assert "runs" in meta, "Missing '_meta.runs'"
    assert "flake_detection" in meta, "Missing '_meta.flake_detection'"


def test_agent_artifact_nonempty_files() -> None:
    data = _load(AGENT_DURATIONS)
    files = data.get("files", [])
    assert len(files) > 0, "agent/.test_durations.json has no file entries"
    _assert_artifact_covers_inventory("agent", data)
    _assert_all_entries_measured("agent", data)


def test_agent_artifact_file_schema() -> None:
    data = _load(AGENT_DURATIONS)
    for entry in data["files"][:10]:
        assert "file" in entry, f"Missing 'file' key in entry: {entry}"
        assert "duration_ms" in entry, f"Missing 'duration_ms' in entry: {entry}"
        assert "tests" in entry, f"Missing 'tests' in entry: {entry}"
        assert "flaky" in entry, f"Missing 'flaky' in entry: {entry}"
        assert "measured" in entry, f"Missing 'measured' in entry: {entry}"
        assert isinstance(entry["duration_ms"], (int, float)), (
            f"duration_ms must be numeric, got {type(entry['duration_ms']).__name__}"
        )


def test_agent_artifact_real_durations() -> None:
    """Catch synthetic placeholder artifacts (e.g. all values identical)."""
    data = _load(AGENT_DURATIONS)
    files = data.get("files", [])
    if not files:
        return
    durations = [f["duration_ms"] for f in files]
    distinct = len(set(durations))
    if distinct <= _DEGENERATE_WARN_THRESHOLD:
        warnings.warn(
            f"\n\n  SYNTHETIC agent/.test_durations.json DETECTED: only {distinct} "
            f"distinct duration value(s) across {len(files)} entries.\n"
            f"  Regenerate: node scripts/vitest-test-telemetry.mjs --suite agent\n",
            stacklevel=2,
        )
    assert distinct >= _DISTINCT_VALUE_FLOOR, (
        f"agent/.test_durations.json looks synthetic: only {distinct} distinct "
        f"duration value(s) across {len(files)} files (floor={_DISTINCT_VALUE_FLOOR}). "
        "Regenerate: node scripts/vitest-test-telemetry.mjs --suite agent"
    )


def test_agent_artifact_sorted_descending() -> None:
    """Files must be sorted slowest-first so consumers can slice the head."""
    data = _load(AGENT_DURATIONS)
    durations = [f["duration_ms"] for f in data.get("files", [])]
    assert durations == sorted(durations, reverse=True), (
        "agent/.test_durations.json files are not sorted descending by duration_ms. "
        "Regenerate: node scripts/vitest-test-telemetry.mjs --suite agent"
    )


# ── admin-ui suite ───────────────────────────────────────────────────────────

def test_adminui_artifact_exists() -> None:
    assert ADMINUI_DURATIONS.exists(), (
        f"admin-ui/.test_durations.json missing at {ADMINUI_DURATIONS}. "
        "Regenerate: node scripts/vitest-test-telemetry.mjs --suite admin-ui"
    )


def test_adminui_artifact_is_valid_json() -> None:
    data = _load(ADMINUI_DURATIONS)
    assert isinstance(data, dict), f"Expected a JSON object, got {type(data).__name__}"
    assert "_meta" in data, "Missing '_meta' key"
    assert "files" in data, "Missing 'files' key"


def test_adminui_artifact_meta_fields() -> None:
    data = _load(ADMINUI_DURATIONS)
    meta = data["_meta"]
    assert meta.get("suite") == "admin-ui", (
        f"Expected suite='admin-ui', got {meta.get('suite')!r}"
    )
    assert "generated" in meta, "Missing '_meta.generated'"
    assert "runs" in meta, "Missing '_meta.runs'"
    assert "flake_detection" in meta, "Missing '_meta.flake_detection'"


def test_adminui_artifact_nonempty_files() -> None:
    data = _load(ADMINUI_DURATIONS)
    files = data.get("files", [])
    assert len(files) > 0, "admin-ui/.test_durations.json has no file entries"
    _assert_artifact_covers_inventory("admin-ui", data)
    _assert_all_entries_measured("admin-ui", data)


def test_adminui_unit_and_viewport_inventories_are_disjoint() -> None:
    unit_inventory = _test_file_inventory("admin-ui")
    viewport_inventory = _test_file_inventory("admin-ui-viewport")
    assert unit_inventory
    assert viewport_inventory
    assert unit_inventory.isdisjoint(viewport_inventory)
    assert all(".viewport.test." not in path for path in unit_inventory)
    assert all(".viewport.test." in path for path in viewport_inventory)


def test_adminui_artifact_file_schema() -> None:
    data = _load(ADMINUI_DURATIONS)
    for entry in data["files"][:10]:
        assert "file" in entry, f"Missing 'file' key in entry: {entry}"
        assert "duration_ms" in entry, f"Missing 'duration_ms' in entry: {entry}"
        assert "tests" in entry, f"Missing 'tests' in entry: {entry}"
        assert "flaky" in entry, f"Missing 'flaky' in entry: {entry}"
        assert "measured" in entry, f"Missing 'measured' in entry: {entry}"
        assert isinstance(entry["duration_ms"], (int, float)), (
            f"duration_ms must be numeric, got {type(entry['duration_ms']).__name__}"
        )


def test_adminui_artifact_real_durations() -> None:
    """Catch synthetic placeholder artifacts (e.g. all values identical)."""
    data = _load(ADMINUI_DURATIONS)
    files = data.get("files", [])
    if not files:
        return
    durations = [f["duration_ms"] for f in files]
    distinct = len(set(durations))
    if distinct <= _DEGENERATE_WARN_THRESHOLD:
        warnings.warn(
            f"\n\n  SYNTHETIC admin-ui/.test_durations.json DETECTED: only {distinct} "
            f"distinct duration value(s) across {len(files)} entries.\n"
            f"  Regenerate: node scripts/vitest-test-telemetry.mjs --suite admin-ui\n",
            stacklevel=2,
        )
    assert distinct >= _DISTINCT_VALUE_FLOOR, (
        f"admin-ui/.test_durations.json looks synthetic: only {distinct} distinct "
        f"duration value(s) across {len(files)} files (floor={_DISTINCT_VALUE_FLOOR}). "
        "Regenerate: node scripts/vitest-test-telemetry.mjs --suite admin-ui"
    )


def test_adminui_artifact_sorted_descending() -> None:
    """Files must be sorted slowest-first so consumers can slice the head."""
    data = _load(ADMINUI_DURATIONS)
    durations = [f["duration_ms"] for f in data.get("files", [])]
    assert durations == sorted(durations, reverse=True), (
        "admin-ui/.test_durations.json files are not sorted descending by duration_ms. "
        "Regenerate: node scripts/vitest-test-telemetry.mjs --suite admin-ui"
    )

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
import warnings
from pathlib import Path

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


def _test_file_inventory(suite: str) -> set[str]:
    """Return the files collected by the suite's committed Vitest config."""
    suite_root = WORKSPACE_ROOT / suite
    files: set[str] = set()
    for path in suite_root.rglob("*"):
        if not path.is_file():
            continue
        relative = path.relative_to(suite_root).as_posix()
        if suite == "agent":
            included = relative.startswith("src/") and relative.endswith(".test.ts")
        elif suite == "admin-ui":
            included = (
                (relative.startswith("src/") and relative.endswith((".test.ts", ".test.tsx")))
                or (
                    relative.startswith("eslint-rules/")
                    and relative.endswith((".test.js", ".test.ts"))
                )
            )
            included = included and not (
                relative.startswith("src/") and ".viewport.test." in relative
            )
        else:
            raise ValueError(f"Unknown Vitest suite: {suite}")
        if included:
            files.add(relative)
    return files


def _assert_artifact_covers_inventory(suite: str, data: dict) -> None:
    expected = _test_file_inventory(suite)
    actual = {entry.get("file") for entry in data.get("files", [])}
    missing = sorted(expected - actual)
    stale = sorted(actual - expected)
    assert not missing and not stale, (
        f"{suite}/.test_durations.json is out of sync with Vitest's on-disk test inventory. "
        f"Missing {len(missing)} file(s): {missing[:10]}; "
        f"stale {len(stale)} file(s): {stale[:10]}. "
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


def test_adminui_inventory_excludes_viewport_files() -> None:
    inventory = _test_file_inventory("admin-ui")
    assert inventory
    assert all(".viewport.test." not in path for path in inventory)


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

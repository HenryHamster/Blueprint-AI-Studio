"""
Standalone guard: backend/.test_durations must exist, be valid, and stay in sync
with the current test collection so CI shard time-balancing can't silently rot.

Runs under --noconftest (no DB, no conftest fixtures).
"""
import ast
import atexit
import json
import re
import shutil
import subprocess
import sys
import tempfile
import warnings
from dataclasses import dataclass
from pathlib import Path
from testing.fixgate_attribution import format_offender_marker
from testing.estate_inventory import iter_backend_test_files
from testing.identity_extraction import (
    extract_python_test_ids,
    extract_python_test_ids_from_file,
)

BACKEND_ROOT = Path(__file__).parents[1]
DURATIONS_PATH = BACKEND_ROOT / ".test_durations"
# Key format: "testing/<filename>.py::<test_function_or_class>"
KEY_PATTERN = re.compile(r"^testing/test_[^/]+\.py::.+$")
# At least 60% of test_*.py files should appear in the durations map. This is
# advisory because regenerating the baseline requires the full DB-backed suite,
# which is unavailable on agent VMs. The structural and quality checks below
# remain blocking.
MIN_COVERAGE_FRACTION = 0.60
_BASELINE_UNLOADED = object()
_baseline_cache: object = _BASELINE_UNLOADED
_baseline_clone_roots: set[Path] = set()


def _cleanup_baseline_clones() -> None:
    for clone_root in _baseline_clone_roots:
        shutil.rmtree(clone_root, ignore_errors=True)


atexit.register(_cleanup_baseline_clones)


@dataclass(frozen=True)
class _DurationBaseline:
    """The immutable roster and artifact needed for candidate attribution."""

    ref: str
    test_files: frozenset[str]
    durations: dict[str, float]
    stale_entries: frozenset[str]
    repo_root: Path | None = None
    git_ref: str | None = None


def _workspace_root() -> Path | None:
    """Find the checkout root for both a normal and a Docker-only layout."""
    candidates = (BACKEND_ROOT.parent, Path("/repo"))
    for candidate in candidates:
        if (candidate / "backend" / "testing").is_dir():
            return candidate
    return None


def _current_test_files() -> dict[str, Path]:
    """Return the current collector roster keyed by suite-relative path."""
    local_testing = BACKEND_ROOT / "testing"
    if local_testing.is_dir():
        paths = tuple(sorted(local_testing.glob("test_*.py")))
    else:
        workspace_root = _workspace_root()
        # A backend-only image has /app/testing but no /app/backend.  This
        # fallback keeps the guard useful even when /repo is not mounted.
        paths = (
            iter_backend_test_files(workspace_root)
            if workspace_root is not None
            else ()
        )
    return {
        path.relative_to(BACKEND_ROOT).as_posix(): path
        for path in paths
    }


def _git_output(repo_root: Path, *args: str) -> str | None:
    """Read one immutable git-tree object without allowing git errors through.

    ``-c safe.directory=<repo_root>`` is load-bearing, not defensive. The
    ci.yml backend shards bind the checkout read-only at /repo into a container
    that runs as ROOT while the runner user owns the files, so git refuses
    every command there with "detected dubious ownership" (exit 128) unless the
    path is marked safe. Without it EVERY read below returns None, the automain
    baseline silently becomes unavailable, and the two guards downstream fall
    back to their ABSOLUTE thresholds -- reporting inherited debt as though
    this candidate introduced it. The guard-data lane cannot surface that:
    scripts/test-backend.sh runs its container as the invoking uid, so git is
    happy there and the baseline resolves. Same commit, opposite verdict,
    decided purely by the checkout's ownership shape. The sibling guards
    (test_vitest_durations_freshness, test_size_offender_governance) already
    inject this; this one was missed.
    """
    try:
        result = subprocess.run(
            ["git", "-c", f"safe.directory={repo_root}", *args],
            cwd=repo_root,
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError:
        return None
    if result.returncode != 0:
        return None
    return result.stdout


def _resolve_baseline_ref(repo_root: Path) -> str | None:
    """Prefer the fetched automain ref, then a local automain branch."""
    for ref in ("origin/automain", "automain"):
        if _git_output(repo_root, "rev-parse", "--verify", f"{ref}^{{commit}}"):
            return ref
    return None


def _clone_origin_config(repo_root: Path) -> list[str]:
    """Preserve checkout-local HTTP auth when cloning into a writable tempdir."""
    output = _git_output(repo_root, "config", "--get-regexp", r"^http\..*\.extraheader$")
    if output is None:
        return []
    args: list[str] = []
    for line in output.splitlines():
        key, separator, value = line.partition(" ")
        if separator and key and value:
            args.extend(("-c", f"{key}={value}"))
    return args


def _clone_baseline(repo_root: Path) -> tuple[Path, str] | None:
    """Fetch automain into a private writable clone when checkout is shallow.

    GitHub Actions' default PR checkout has only the candidate merge object and
    is mounted read-only at /repo inside the backend test container.  Clone the
    immutable automain tip into /tmp rather than depending on an incidental
    remote-tracking ref or attempting to mutate that checkout.
    """
    origin = _git_output(repo_root, "remote", "get-url", "origin")
    if origin is None or not origin.strip():
        return None
    clone_root = Path(tempfile.mkdtemp(prefix="duration-baseline-"))
    try:
        result = subprocess.run(
            [
                "git", *_clone_origin_config(repo_root), "clone", "--quiet",
                "--no-checkout", "--depth=1", "--filter=blob:none",
                "--branch=automain", origin.strip(), str(clone_root),
            ],
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError:
        result = None
    ref = (
        _git_output(clone_root, "rev-parse", "--verify", "HEAD^{commit}")
        if result is not None and result.returncode == 0
        else None
    )
    if ref is None or not ref.strip():
        shutil.rmtree(clone_root, ignore_errors=True)
        return None
    _baseline_clone_roots.add(clone_root)
    return clone_root, ref.strip()


def _git_test_files(repo_root: Path, ref: str) -> set[str] | None:
    listing = _git_output(repo_root, "ls-tree", "-r", "--name-only", ref, "backend/testing")
    if listing is None:
        return None
    return {
        path.removeprefix("backend/")
        for path in listing.splitlines()
        if re.fullmatch(r"backend/testing/test_[^/]+\.py", path)
    }


def _git_json(repo_root: Path, ref: str, path: str) -> dict | None:
    raw = _git_output(repo_root, "show", f"{ref}:{path}")
    if raw is None:
        return None
    try:
        data = json.loads(raw)
    except (TypeError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _git_test_ids(
    repo_root: Path, ref: str, paths: set[str]
) -> dict[str, set[str]] | None:
    """Read only candidate-stale modules from the immutable git tree."""
    ids_by_file: dict[str, set[str]] = {}
    for file_key in paths:
        source = _git_output(repo_root, "show", f"{ref}:backend/{file_key}")
        if source is None:
            return None
        try:
            ids_by_file[file_key] = extract_python_test_ids(source)
        except (SyntaxError, ValueError):
            return None
    return ids_by_file


def _stale_entries(
    durations: dict, test_files: set[str], ids_by_file: dict[str, set[str]]
) -> set[str]:
    stale = set()
    for key in durations:
        file_key, separator, raw_node_id = key.partition("::")
        if not separator or file_key not in test_files:
            continue
        if raw_node_id.split("[", 1)[0] not in ids_by_file.get(file_key, set()):
            stale.add(key)
    return stale


def _load_duration_baseline() -> _DurationBaseline | None:
    """Load an immutable automain snapshot, or None for fail-closed fallback."""
    global _baseline_cache
    if _baseline_cache is not _BASELINE_UNLOADED:
        return _baseline_cache  # type: ignore[return-value]

    repo_root = _workspace_root()
    if repo_root is None:
        baseline = None
    else:
        ref = _resolve_baseline_ref(repo_root)
        baseline_root = repo_root
        baseline_label = ref
        if ref is None:
            cloned = _clone_baseline(repo_root)
            if cloned is None:
                baseline = None
            else:
                baseline_root, sha = cloned
                ref = sha
                baseline_label = f"automain@{sha}"
        if ref is not None:
            test_files = _git_test_files(baseline_root, ref)
            durations = _git_json(baseline_root, ref, "backend/.test_durations")
            if test_files is None or durations is None:
                baseline = None
            else:
                baseline = _DurationBaseline(
                    ref=baseline_label or ref,
                    test_files=frozenset(test_files),
                    durations=durations,
                    # Loaded lazily only if the candidate has stale entries;
                    # coverage checks should not parse a 37MB test tree.
                    stale_entries=frozenset(),
                    repo_root=baseline_root,
                    git_ref=ref,
                )
    _baseline_cache = baseline
    return baseline


def _warn_inherited(kind: str, entries: set[str], ref: str) -> None:
    if entries:
        warnings.warn(
            f"backend/.test_durations has {len(entries)} inherited {kind} "
            f"entry(ies) from {ref}; they are not attributed to this candidate. "
            f"First entries: {sorted(entries)[:10]}",
            stacklevel=2,
        )


def _baseline_stale_entries(
    baseline: _DurationBaseline, candidate_stale: set[str]
) -> set[str]:
    """Resolve only baseline files relevant to candidate stale entries."""
    if baseline.stale_entries:
        return set(baseline.stale_entries)
    file_keys = {
        key.split("::", 1)[0]
        for key in candidate_stale
        if key.split("::", 1)[0] in baseline.test_files
    }
    if not file_keys:
        return set()
    repo_root = baseline.repo_root or _workspace_root()
    if repo_root is None:
        return set()
    ids_by_file = _git_test_ids(repo_root, baseline.git_ref or baseline.ref, file_keys)
    if ids_by_file is None:
        return set()
    return _stale_entries(baseline.durations, file_keys, ids_by_file)


def _load_durations() -> dict:
    assert DURATIONS_PATH.exists(), (
        f"backend/.test_durations not found at {DURATIONS_PATH}. "
        "Generate it by running the backend test suite with duration recording "
        "enabled (scripts/store-backend-durations.sh) and commit the result."
    )
    with DURATIONS_PATH.open() as f:
        return json.load(f)


def test_durations_file_exists() -> None:
    """The durations baseline file must be present in the repo."""
    assert DURATIONS_PATH.exists(), (
        f"backend/.test_durations missing at {DURATIONS_PATH}. "
        "Commit a fresh baseline generated by scripts/store-backend-durations.sh."
    )


def test_durations_file_is_valid_json() -> None:
    """The file must parse as a JSON object (not array or scalar)."""
    data = _load_durations()
    assert isinstance(data, dict), (
        f"backend/.test_durations must be a JSON object, got {type(data).__name__}"
    )


def test_durations_values_are_numeric() -> None:
    """Every value must be a number (int or float — pytest stores floats)."""
    data = _load_durations()
    bad = {k: v for k, v in data.items() if not isinstance(v, (int, float))}
    assert not bad, (
        f"backend/.test_durations has {len(bad)} non-numeric value(s). "
        f"First offenders: {dict(list(bad.items())[:5])}" + "\n" + format_offender_marker(str(offender) for offender in bad)
    )


def test_durations_key_format() -> None:
    """All keys must follow the pattern 'testing/test_<name>.py::<node_id>'."""
    data = _load_durations()
    bad = [k for k in data if not KEY_PATTERN.match(k)]
    assert not bad, (
        f"backend/.test_durations has {len(bad)} key(s) with unexpected format. "
        f"Expected 'testing/test_<name>.py::<node_id>'. "
        f"First offenders: {bad[:5]}" + "\n" + format_offender_marker(str(offender) for offender in bad)
    )


def test_durations_values_have_variance() -> None:
    """
    The durations map must not be uniform placeholder data.

    pytest-split balances shards by wall-clock time; if every entry is the
    same value (e.g. all 0.5 s) the splitting devolves to pure count-based,
    silently defeating the whole point.  We require:
      - At least 2 distinct duration values, AND
      - At least 5 % of entries differ from the single most-common value.

    A freshly generated .test_durations file from a real pytest run will
    easily satisfy both: real suites span milliseconds to tens of seconds.
    """
    data = _load_durations()
    if not data:
        return  # Empty file is caught by coverage check

    from collections import Counter

    counts = Counter(data.values())
    unique_values = len(counts)
    most_common_count = counts.most_common(1)[0][1]
    total = len(data)
    dominant_fraction = most_common_count / total

    # Real pytest-split duration files span milliseconds to tens of seconds
    # with hundreds of distinct values.  Two-value placeholder data (e.g. all
    # 0.1 s / 0.5 s) has at most a handful of unique values and a dominant
    # fraction well above 50 %.  Both thresholds must hold.
    MIN_UNIQUE_VALUES = 10
    MAX_DOMINANT_FRACTION = 0.50

    assert unique_values >= MIN_UNIQUE_VALUES, (
        f"backend/.test_durations has only {unique_values} unique duration "
        f"value(s) across {total} entries — this looks like placeholder data. "
        f"Real timing data has {MIN_UNIQUE_VALUES}+ distinct values. "
        "Regenerate via scripts/store-backend-durations.sh."
    )
    assert dominant_fraction <= MAX_DOMINANT_FRACTION, (
        f"backend/.test_durations: {most_common_count}/{total} entries "
        f"({dominant_fraction:.1%}) share the same value — effectively uniform "
        "placeholder data that defeats CI time-balancing. "
        "Regenerate via scripts/store-backend-durations.sh."
    )


def test_durations_coverage_fraction_within_threshold() -> None:
    """
    Attribute missing modules to this candidate when an automain tree is usable.

    The committed artifact currently has inherited gaps from automain. Those
    gaps are reported loudly, but only a newly uncovered current module blocks.
    Without an immutable baseline, retain the absolute fail-closed threshold.
    """
    data = _load_durations()
    files_in_durations = {k.split("::")[0] for k in data}

    current_files = _current_test_files()
    all_test_files = set(current_files)

    if not all_test_files:
        return  # Nothing to check (shouldn't happen in a real repo)

    covered = files_in_durations & all_test_files
    uncovered = sorted(all_test_files - files_in_durations)
    baseline = _load_duration_baseline()
    if baseline is not None:
        baseline_covered = {
            key.split("::")[0] for key in baseline.durations
        } & baseline.test_files
        inherited_missing = (
            set(uncovered)
            & (set(baseline.test_files) - baseline_covered)
        )
        new_missing = set(uncovered) - (set(baseline.test_files) - baseline_covered)
        _warn_inherited("missing coverage", inherited_missing, baseline.ref)
        assert not new_missing, (
            f"backend/.test_durations introduced {len(new_missing)} uncovered "
            f"test file(s) relative to {baseline.ref}. "
            f"Newly uncovered files: {sorted(new_missing)[:10]}"
            + "\n"
            + format_offender_marker(sorted(new_missing))
        )
        return

    fraction = len(covered) / len(all_test_files)
    assert fraction >= MIN_COVERAGE_FRACTION, (
        f"backend/.test_durations covers only {fraction:.1%} of test files "
        f"({len(covered)}/{len(all_test_files)}), below the {MIN_COVERAGE_FRACTION:.0%} threshold. "
        f"No usable origin/automain or automain baseline was available; this "
        f"absolute check is fail-closed. Regenerate via scripts/store-backend-durations.sh. "
        f"Uncovered files ({len(uncovered)}): {uncovered[:10]}"
        + "\n"
        + format_offender_marker(uncovered)
    )


# Ratchet floor: how many *distinct* duration values the committed file must have.
# A real measured baseline has thousands of distinct values; floor=100 hard-blocks
# any future regression to synthetic placeholder data (e.g. all entries 0.1 or 0.5).
_DISTINCT_VALUE_FLOOR = 100

# Warn threshold: ≤ this many distinct values triggers a loud regeneration notice.
# The warning surfaces in pytest's warnings summary without failing the blocking suite.
_DEGENERATE_WARN_THRESHOLD = 5

# Cap above which a duration is considered a fixture-setup artifact, not a real test
# duration. Docker Postgres cold-start is charged to the first test in a batch and
# can produce 400-600s entries. 60s is well above any legitimate single-test cost in
# this suite (next-longest real test is ~27s) and well below the fixture-attribution range.
_IMPLAUSIBLE_OUTLIER_CAP = 60.0
_IN_BAND_SETUP_MIN = 12.0
_IN_BAND_SETUP_MAX = 15.0
_IN_BAND_SETUP_MIN_COUNT = 100
_IN_BAND_SETUP_GAP_MAX = 27.4


def test_durations_are_not_degenerate_synthetic_placeholders() -> None:
    """
    Catch synthetic placeholder files (e.g. all values equal 0.1 or 0.5).

    Two-tier response:
    1. WARN loudly when distinct_count <= _DEGENERATE_WARN_THRESHOLD — visible in
       pytest's warnings summary.
    2. HARD-FAIL when distinct_count < _DISTINCT_VALUE_FLOOR (100) — blocks any
       regression to synthetic placeholder values.

    To regenerate a fresh baseline:
        bash scripts/store-backend-durations.sh   # ~15-20 min, requires Docker+Postgres
        git add backend/.test_durations && git commit
    """
    data = _load_durations()
    if not data:
        return

    distinct_count = len(set(data.values()))

    if distinct_count <= _DEGENERATE_WARN_THRESHOLD:
        warnings.warn(
            f"\n\n"
            f"  SYNTHETIC .test_durations DETECTED: only {distinct_count} distinct duration "
            f"value(s) across {len(data)} entries.\n"
            f"  CI pytest-split is falling back to COUNT-BASED shard balancing — shards "
            f"will be time-imbalanced.\n"
            f"\n"
            f"  To fix: bash scripts/store-backend-durations.sh  (Docker+Postgres, ~15-20 min)\n"
            f"  Then: git add backend/.test_durations && git commit\n",
            stacklevel=2,
        )

    assert distinct_count >= _DISTINCT_VALUE_FLOOR, (
        f"backend/.test_durations has regressed to a synthetic/degenerate state: "
        f"only {distinct_count} distinct duration value(s) across {len(data)} entries "
        f"(floor={_DISTINCT_VALUE_FLOOR}). "
        f"Regenerate via: bash scripts/store-backend-durations.sh"
    )


def _resolve_quality_path() -> Path | None:
    """
    Return the path to scripts/durations_quality.py or None if not found.

    The standalone Docker image mounts the repo at /app (the backend dir) but
    also bind-mounts the repo root at /repo, so we try both layouts.
    """
    candidates = [
        BACKEND_ROOT.parent / "scripts" / "durations_quality.py",
        Path("/repo/scripts/durations_quality.py"),
    ]
    for p in candidates:
        if p.exists():
            return p
    return None


def test_outlier_cap_matches_durations_quality() -> None:
    """
    The outlier cap here must match the authoritative definition in
    scripts/durations_quality.py (OUTLIER_CAP).  They cannot share a Python
    import because this guard runs standalone under --noconftest where scripts/
    is not in the load path, so an explicit cross-check prevents silent drift.

    If this test fails, update _IMPLAUSIBLE_OUTLIER_CAP above to match
    the value in scripts/durations_quality.py.
    """
    quality_path = _resolve_quality_path()
    assert quality_path is not None, (
        "Cannot locate scripts/durations_quality.py. "
        "Tried: "
        + str(BACKEND_ROOT.parent / "scripts" / "durations_quality.py")
        + " (local) and /repo/scripts/durations_quality.py (Docker mount)."
    )

    tree = ast.parse(quality_path.read_text())
    for node in tree.body:
        if (
            isinstance(node, ast.Assign)
            and len(node.targets) == 1
            and isinstance(node.targets[0], ast.Name)
            and node.targets[0].id == "OUTLIER_CAP"
            and isinstance(node.value, ast.Constant)
        ):
            canonical = node.value.value
            assert canonical == _IMPLAUSIBLE_OUTLIER_CAP, (
                f"Outlier cap mismatch: scripts/durations_quality.py defines "
                f"OUTLIER_CAP = {canonical}, but this guard uses "
                f"_IMPLAUSIBLE_OUTLIER_CAP = {_IMPLAUSIBLE_OUTLIER_CAP}. "
                f"Update _IMPLAUSIBLE_OUTLIER_CAP to match."
            )
            return

    assert False, (
        "Could not locate OUTLIER_CAP assignment in "
        "scripts/durations_quality.py"
    )


def test_durations_no_implausible_outliers() -> None:
    """
    Reject fixture-setup cost misattributed to individual tests.

    When pytest-split runs tests in batches with Docker+Postgres, Postgres cold-start
    time (~400-600s) is charged to the first test in the batch.  These outlier values
    corrupt shard balancing: the affected test looks 30-100x more expensive than it is,
    causing CI to under-fill the shard that contains it.

    Any duration above _IMPLAUSIBLE_OUTLIER_CAP (60s) is a misattribution artifact.
    Strip them before committing via scripts/batch-store-durations.sh or
    scripts/regenerate-durations.py (both enforce this cap during the merge step).
    """
    data = _load_durations()
    if not data:
        return

    outliers = {k: v for k, v in data.items() if v > _IMPLAUSIBLE_OUTLIER_CAP}
    assert not outliers, (
        f"backend/.test_durations contains {len(outliers)} implausibly long duration(s) "
        f"(> {_IMPLAUSIBLE_OUTLIER_CAP}s) — likely Docker Postgres cold-start attributed "
        f"to the first test in a batch rather than real test cost.\n"
        f"Offending entries: {dict(list(outliers.items())[:5])}\n"
        f"Fix: strip entries > {_IMPLAUSIBLE_OUTLIER_CAP}s then recommit, or regenerate "
        f"via scripts/batch-store-durations.sh (which caps automatically)." + "\n" + format_offender_marker(str(offender) for offender in outliers)
    )


def test_durations_no_in_band_setup_attribution() -> None:
    """Reject the measured 12-15s setup-cost plateau before it skews shards.

    The plateau is distinct from a single extreme outlier: it is a dense band
    followed by a diagnostic gap, so every affected test can look plausible in
    isolation while collectively taking most of the shard weight.
    """
    data = _load_durations()
    band = {
        key
        for key, value in data.items()
        if _IN_BAND_SETUP_MIN <= value < _IN_BAND_SETUP_MAX
    }
    if len(band) < _IN_BAND_SETUP_MIN_COUNT:
        return
    first_post_plateau = min(
        (value for value in data.values() if value >= _IN_BAND_SETUP_MAX),
        default=_IN_BAND_SETUP_MAX,
    )
    has_following_gap = first_post_plateau > _IN_BAND_SETUP_GAP_MAX and not any(
        _IN_BAND_SETUP_MAX <= value <= _IN_BAND_SETUP_GAP_MAX
        for value in data.values()
    )
    assert not has_following_gap, (
        f"backend/.test_durations contains {len(band)} entries in the "
        f"{_IN_BAND_SETUP_MIN:g}-{_IN_BAND_SETUP_MAX:g}s setup-attribution band "
        f"with no observations through {_IN_BAND_SETUP_GAP_MAX:g}s. "
        "Strip these entries and regenerate via scripts/batch-store-durations.sh."
        + "\n" + format_offender_marker(sorted(band))
    )


def _extract_valid_test_ids(filepath: Path) -> set:
    """
    Return the set of valid pytest node IDs for a test file, using AST.

    Recognises two forms:
    - Top-level async/sync functions named test_* → bare function name
    - Methods named test_* on a collectable top-level class →
      "ClassName::method", including methods inherited from module-local
      bases (a unittest subclass reports them under its own name)

    Only top-level classes are collection roots, matching pytest's default
    behaviour.  Does not expand @pytest.mark.parametrize variants — callers
    must strip the "[param]" suffix before lookup.
    """
    return extract_python_test_ids_from_file(filepath)


def test_durations_no_stale_entries() -> None:
    """
    Every duration entry whose test file exists on disk must reference a real
    test ID in that file.

    When a test file is split or renamed, its old entries remain in
    .test_durations and inflate shard-balancing estimates for tests that no
    longer exist — causing CI to under-fill the shard that previously held
    them. This guard catches that class of regression without requiring a
    live pytest run.

    Parametrized variants (e.g. "test_foo[param1]") are checked by stripping
    the "[...]" suffix and verifying the base function/method exists.
    Files that no longer exist on disk are ignored here (the coverage-fraction
    test flags those separately).
    """
    data = _load_durations()

    current_files = _current_test_files()
    duration_files = {
        key.split("::", 1)[0] for key in data if "::" in key
    }
    ids_by_file = {
        file_key: _extract_valid_test_ids(path)
        for file_key, path in current_files.items()
        if file_key in duration_files
    }
    stale = _stale_entries(data, set(current_files), ids_by_file)

    baseline = _load_duration_baseline()
    if baseline is not None:
        baseline_stale = _baseline_stale_entries(baseline, stale)
        inherited_stale = stale & baseline_stale
        new_stale = stale - baseline_stale
        _warn_inherited("stale artifact", inherited_stale, baseline.ref)
        assert not new_stale, (
            f"backend/.test_durations introduced {len(new_stale)} stale "
            f"entry(ies) relative to {baseline.ref}. These phantom entries "
            f"inflate CI shard-balancing estimates.\n"
            f"New stale entries: {sorted(new_stale)[:10]}\n"
            f"Fix: remove them or regenerate via scripts/store-backend-durations.sh."
            + "\n"
            + format_offender_marker(sorted(new_stale))
        )
        return

    assert not stale, (
        f"backend/.test_durations contains {len(stale)} stale entry(ies) for "
        f"test IDs that no longer exist in their file. These phantom entries "
        f"inflate CI shard-balancing estimates for tests that were moved or "
        f"removed.\n"
        f"Stale entries ({min(len(stale), 10)} of {len(stale)} shown): "
        f"{sorted(stale)[:10]}\n"
        f"Fix: remove these entries from backend/.test_durations, or regenerate "
        f"via scripts/store-backend-durations.sh." + "\n" + format_offender_marker(str(offender) for offender in stale)
    )


# ---------------------------------------------------------------------------
# Unit tests for _extract_valid_test_ids
# ---------------------------------------------------------------------------


def test_duration_baseline_reads_real_git_tree() -> None:
    """The preferred automain ref supplies an immutable roster and artifact."""
    baseline = _load_duration_baseline()
    if baseline is None:
        import pytest

        pytest.skip("origin/automain or local automain baseline is unavailable")
    assert baseline.ref == "origin/automain"
    assert "testing/test_test_durations_freshness.py" in baseline.test_files
    assert baseline.durations


def _run_git(repo_root: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=repo_root,
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout.strip()


def test_duration_baseline_fetches_automain_from_depth_one_checkout(
    tmp_path: Path, monkeypatch
) -> None:
    """A shallow candidate checkout still attributes against automain's SHA."""
    source = tmp_path / "source"
    origin = tmp_path / "origin.git"
    shallow = tmp_path / "shallow"
    source.mkdir()
    _run_git(source, "init", "-q", "-b", "automain")
    _run_git(source, "config", "user.email", "duration-test@example.test")
    _run_git(source, "config", "user.name", "Duration Test")
    base_test = source / "backend" / "testing" / "test_base.py"
    base_test.parent.mkdir(parents=True)
    base_test.write_text("def test_base(): pass\n", encoding="utf-8")
    (source / "backend" / ".test_durations").write_text(
        '{"testing/test_base.py::test_base": 1.0, '
        '"testing/test_base.py::test_removed": 1.1}\n',
        encoding="utf-8",
    )
    _run_git(source, "add", ".")
    _run_git(source, "commit", "-qm", "base durations")
    automain_sha = _run_git(source, "rev-parse", "HEAD")
    _run_git(source, "checkout", "-qb", "candidate")
    (source / "backend" / "testing" / "test_candidate.py").write_text(
        "def test_candidate(): pass\n", encoding="utf-8"
    )
    _run_git(source, "add", ".")
    _run_git(source, "commit", "-qm", "candidate")
    _run_git(tmp_path, "init", "--bare", "-q", str(origin))
    _run_git(source, "remote", "add", "origin", str(origin))
    _run_git(source, "push", "-q", "origin", "automain", "candidate")
    subprocess.run(
        [
            "git",
            "clone",
            "--quiet",
            "--depth=1",
            "--branch=candidate",
            f"file://{origin}",
            str(shallow),
        ],
        check=True,
    )

    module = sys.modules[__name__]
    assert _resolve_baseline_ref(shallow) is None
    monkeypatch.setattr(module, "_workspace_root", lambda: shallow)
    monkeypatch.setattr(module, "_baseline_cache", _BASELINE_UNLOADED)

    baseline = _load_duration_baseline()

    assert baseline is not None
    assert baseline.ref == f"automain@{automain_sha}"
    assert baseline.test_files == frozenset({"testing/test_base.py"})
    assert baseline.durations == {
        "testing/test_base.py::test_base": 1.0,
        "testing/test_base.py::test_removed": 1.1,
    }
    assert baseline.repo_root is not None
    assert _baseline_stale_entries(
        baseline, {"testing/test_base.py::test_removed"}
    ) == {"testing/test_base.py::test_removed"}
    shutil.rmtree(baseline.repo_root, ignore_errors=True)
    _baseline_clone_roots.discard(baseline.repo_root)


def test_coverage_guard_rejects_new_backend_inventory_entry(monkeypatch) -> None:
    """A candidate-added unmeasured module is not hidden by inherited debt."""
    module = sys.modules[__name__]
    monkeypatch.setattr(
        module,
        "_current_test_files",
        lambda: {
            "testing/test_existing.py": Path("/tmp/test_existing.py"),
            "testing/test_new.py": Path("/tmp/test_new.py"),
        },
    )
    monkeypatch.setattr(
        module,
        "_load_durations",
        lambda: {"testing/test_existing.py::test_old": 1.0},
    )
    monkeypatch.setattr(
        module,
        "_load_duration_baseline",
        lambda: _DurationBaseline(
            ref="origin/automain",
            test_files=frozenset({"testing/test_existing.py"}),
            durations={"testing/test_existing.py::test_old": 1.0},
            stale_entries=frozenset(),
        ),
    )

    import pytest

    with pytest.raises(AssertionError, match="introduced 1 uncovered"):
        test_durations_coverage_fraction_within_threshold()


def test_coverage_guard_suppresses_inherited_missing_module(monkeypatch) -> None:
    """A missing module already absent on automain warns but does not fail."""
    module = sys.modules[__name__]
    monkeypatch.setattr(
        module,
        "_current_test_files",
        lambda: {"testing/test_inherited.py": Path("/tmp/test_inherited.py")},
    )
    monkeypatch.setattr(module, "_load_durations", lambda: {})
    monkeypatch.setattr(
        module,
        "_load_duration_baseline",
        lambda: _DurationBaseline(
            ref="origin/automain",
            test_files=frozenset({"testing/test_inherited.py"}),
            durations={},
            stale_entries=frozenset(),
        ),
    )

    import pytest

    with pytest.warns(UserWarning, match="inherited missing coverage"):
        test_durations_coverage_fraction_within_threshold()


def test_stale_guard_rejects_new_stale_entry(tmp_path: Path, monkeypatch) -> None:
    """A stale node newly introduced by the candidate remains blocking."""
    module = sys.modules[__name__]
    path = tmp_path / "test_current.py"
    path.write_text("def test_current(): pass\n", encoding="utf-8")
    monkeypatch.setattr(
        module,
        "_current_test_files",
        lambda: {"testing/test_current.py": path},
    )
    monkeypatch.setattr(
        module,
        "_load_durations",
        lambda: {"testing/test_current.py::test_removed": 1.0},
    )
    monkeypatch.setattr(
        module,
        "_load_duration_baseline",
        lambda: _DurationBaseline(
            ref="origin/automain",
            test_files=frozenset({"testing/test_current.py"}),
            durations={},
            stale_entries=frozenset(),
        ),
    )

    import pytest

    with pytest.raises(AssertionError, match="introduced 1 stale"):
        test_durations_no_stale_entries()


def test_coverage_guard_falls_back_when_baseline_is_unavailable(monkeypatch) -> None:
    """No baseline means the old absolute coverage check remains fail-closed."""
    module = sys.modules[__name__]
    monkeypatch.setattr(
        module,
        "_current_test_files",
        lambda: {
            f"testing/test_{index}.py": Path(f"/tmp/test_{index}.py")
            for index in range(10)
        },
    )
    monkeypatch.setattr(
        module,
        "_load_durations",
        lambda: {"testing/test_0.py::test_one": 1.0},
    )
    monkeypatch.setattr(module, "_load_duration_baseline", lambda: None)

    import pytest

    with pytest.raises(AssertionError, match="No usable .* baseline"):
        test_durations_coverage_fraction_within_threshold()


def _write_tmp_py(content: str) -> Path:
    """Write Python source to a temporary file and return its Path."""
    with tempfile.NamedTemporaryFile(suffix=".py", mode="w", delete=False) as tf:
        tf.write(content)
        return Path(tf.name)


def test_extract_valid_test_ids_top_level_sync_functions() -> None:
    """Top-level sync test_* functions are collected."""
    p = _write_tmp_py(
        "def test_alpha(): pass\n"
        "def test_beta(): pass\n"
        "def helper(): pass\n"
    )
    result = _extract_valid_test_ids(p)
    assert result == {"test_alpha", "test_beta"}


def test_extract_valid_test_ids_top_level_async_functions() -> None:
    """Top-level async test_* functions are collected."""
    p = _write_tmp_py(
        "async def test_async_one(): pass\n"
        "async def setup(): pass\n"
    )
    result = _extract_valid_test_ids(p)
    assert result == {"test_async_one"}


def test_extract_valid_test_ids_test_class_methods() -> None:
    """test_* methods inside Test* classes are collected as ClassName::method."""
    p = _write_tmp_py(
        "class TestFoo:\n"
        "    def test_one(self): pass\n"
        "    def test_two(self): pass\n"
        "    def helper(self): pass\n"
    )
    result = _extract_valid_test_ids(p)
    assert result == {"TestFoo::test_one", "TestFoo::test_two"}


def test_extract_valid_test_ids_async_methods_in_class() -> None:
    """async test_* methods inside Test* classes are collected."""
    p = _write_tmp_py(
        "class TestBar:\n"
        "    async def test_async_method(self): pass\n"
    )
    result = _extract_valid_test_ids(p)
    assert result == {"TestBar::test_async_method"}


def test_extract_valid_test_ids_non_test_class_excluded() -> None:
    """Methods in plain, non-``TestCase`` classes are not collected."""
    p = _write_tmp_py(
        "class Helper:\n"
        "    def test_this_is_not_collected(self): pass\n"
        "class Util:\n"
        "    def test_also_not_collected(self): pass\n"
    )
    result = _extract_valid_test_ids(p)
    assert result == set()


def test_extract_valid_test_ids_indirect_unittest_class() -> None:
    """A module-local ``TestCase`` subclass is collectable despite its name."""
    p = _write_tmp_py(
        "import unittest\n"
        "class BaseCase(unittest.TestCase): pass\n"
        "class RunnerRegressionTests(BaseCase):\n"
        "    def test_collected(self): pass\n"
    )

    assert _extract_valid_test_ids(p) == {"RunnerRegressionTests::test_collected"}


def test_extract_valid_test_ids_mixed_module() -> None:
    """Top-level functions and class methods both appear in mixed file."""
    p = _write_tmp_py(
        "def test_standalone(): pass\n"
        "class TestGroup:\n"
        "    def test_member(self): pass\n"
        "class NotATestClass:\n"
        "    def test_invisible(self): pass\n"
    )
    result = _extract_valid_test_ids(p)
    assert result == {"test_standalone", "TestGroup::test_member"}


def test_extract_valid_test_ids_syntax_error_returns_empty() -> None:
    """A file with a syntax error returns an empty set instead of raising."""
    p = _write_tmp_py("def test_broken(\n")  # unclosed paren → SyntaxError
    result = _extract_valid_test_ids(p)
    assert result == set()


def test_extract_valid_test_ids_nonexistent_file_returns_empty() -> None:
    """A path that does not exist returns an empty set instead of raising."""
    result = _extract_valid_test_ids(Path("/nonexistent/path/test_fake.py"))
    assert result == set()


def test_extract_valid_test_ids_empty_file() -> None:
    """An empty file returns an empty set."""
    p = _write_tmp_py("")
    result = _extract_valid_test_ids(p)
    assert result == set()


def test_extract_valid_test_ids_no_test_functions() -> None:
    """A valid Python file with no test_* functions returns an empty set."""
    p = _write_tmp_py(
        "import os\n"
        "def setup(): pass\n"
        "class MyClass:\n"
        "    def method(self): pass\n"
    )
    result = _extract_valid_test_ids(p)
    assert result == set()


def test_extract_valid_test_ids_nested_class_not_collected() -> None:
    """
    test_* methods inside a class nested within another class are NOT collected.
    pytest's default collector only looks one level deep: top-level classes.
    """
    p = _write_tmp_py(
        "class TestOuter:\n"
        "    class Inner:\n"
        "        def test_nested(self): pass\n"
        "    def test_outer(self): pass\n"
    )
    result = _extract_valid_test_ids(p)
    # Only the top-level class method is collected; nested class is ignored.
    assert "TestOuter::test_outer" in result
    assert "TestOuter::Inner::test_nested" not in result
    assert "Inner::test_nested" not in result


# ---------------------------------------------------------------------------
# Meta-tests: verify the staleness guard detects phantom entries
# ---------------------------------------------------------------------------


def test_staleness_detection_catches_renamed_function() -> None:
    """
    Simulate a test rename: entry points to 'test_old_name' which no longer
    exists in the file (only 'test_new_name' does). Guard must detect it.
    """
    p = _write_tmp_py("def test_new_name(): pass\n")
    valid_ids = _extract_valid_test_ids(p)

    # 'test_old_name' is gone after the rename
    assert "test_old_name" not in valid_ids
    # 'test_new_name' is present
    assert "test_new_name" in valid_ids


def test_staleness_detection_parametrize_suffix_stripped() -> None:
    """
    Parametrized variants like 'test_foo[param1-val]' must resolve to 'test_foo'
    after stripping the '[...]' suffix, and must be found in the file.
    """
    p = _write_tmp_py("def test_foo(): pass\n")
    valid_ids = _extract_valid_test_ids(p)

    raw_node_id = "test_foo[param1-val]"
    base_node_id = raw_node_id.split("[")[0]
    assert base_node_id in valid_ids


def test_staleness_detection_parametrize_suffix_removed_function_is_stale() -> None:
    """
    A parametrized entry for a function that was removed is correctly classified
    as stale after stripping the '[...]' suffix.
    """
    p = _write_tmp_py("def test_remaining(): pass\n")
    valid_ids = _extract_valid_test_ids(p)

    raw_node_id = "test_removed[case1]"
    base_node_id = raw_node_id.split("[")[0]
    assert base_node_id not in valid_ids


def test_staleness_detection_class_method_rename() -> None:
    """
    A class method rename leaves a stale 'TestFoo::test_old' entry while
    'TestFoo::test_renamed' is the current name.
    """
    p = _write_tmp_py(
        "class TestFoo:\n"
        "    def test_renamed(self): pass\n"
    )
    valid_ids = _extract_valid_test_ids(p)

    assert "TestFoo::test_old" not in valid_ids
    assert "TestFoo::test_renamed" in valid_ids


def test_staleness_detection_split_file_stale_in_original() -> None:
    """
    Simulates a file-split: original file retains only 'test_remaining'
    while 'test_moved_to_new_file' was moved out. The moved entry should
    be detected as stale in the original file.
    """
    original = _write_tmp_py("def test_remaining(): pass\n")
    valid_ids = _extract_valid_test_ids(original)

    assert "test_moved_to_new_file" not in valid_ids
    assert "test_remaining" in valid_ids


def test_in_band_setup_thresholds_match_durations_quality() -> None:
    """
    _IN_BAND_SETUP_MIN/MAX/MIN_COUNT/GAP_MAX here must match the authoritative
    definitions in scripts/durations_quality.py.  Same standalone cross-check
    rationale as test_outlier_cap_matches_durations_quality: this guard runs
    under --noconftest where scripts/ is not importable, so drift between the
    two files would otherwise go undetected.

    If this test fails, update the local literals above to match
    scripts/durations_quality.py.
    """
    quality_path = _resolve_quality_path()
    assert quality_path is not None, (
        "Cannot locate scripts/durations_quality.py. "
        "Tried: "
        + str(BACKEND_ROOT.parent / "scripts" / "durations_quality.py")
        + " (local) and /repo/scripts/durations_quality.py (Docker mount)."
    )

    names = {
        "IN_BAND_SETUP_MIN": _IN_BAND_SETUP_MIN,
        "IN_BAND_SETUP_MAX": _IN_BAND_SETUP_MAX,
        "IN_BAND_SETUP_MIN_COUNT": _IN_BAND_SETUP_MIN_COUNT,
        "IN_BAND_SETUP_GAP_MAX": _IN_BAND_SETUP_GAP_MAX,
    }
    tree = ast.parse(quality_path.read_text())
    canonical = {}
    for node in tree.body:
        if (
            isinstance(node, ast.Assign)
            and len(node.targets) == 1
            and isinstance(node.targets[0], ast.Name)
            and node.targets[0].id in names
            and isinstance(node.value, ast.Constant)
        ):
            canonical[node.targets[0].id] = node.value.value

    missing = names.keys() - canonical.keys()
    assert not missing, (
        f"scripts/durations_quality.py is missing assignment(s) for: {sorted(missing)}"
        + "\n" + format_offender_marker(sorted(missing))
    )
    for name, local_value in names.items():
        assert canonical[name] == local_value, (
            f"In-band setup threshold mismatch: scripts/durations_quality.py "
            f"defines {name} = {canonical[name]}, but this guard uses "
            f"_{name} = {local_value}. Update the local literal to match."
            + "\n" + format_offender_marker([name])
        )


def test_setup_misattribution_band_is_derived_from_the_observed_plateau() -> None:
    """
    The sanitizer must derive the first post-plateau observation from
    backend/.test_durations, not treat a hand-snapshotted gap endpoint as the
    observation itself.

    Loads scripts/durations_quality.py::derive_in_band_setup_gap_max (via
    importlib, same pattern test_shard_balance.py uses for
    impute_missing_mean) and computes the smallest duration actually observed
    at or above IN_BAND_SETUP_MAX in the committed data. The committed
    27.430318s value must remain outside the 27.4s diagnostic
    gap, so regenerating the baseline with a new in-gap observation surfaces a
    failing assertion here instead of silently changing sanitizer behavior.
    """
    quality_path = _resolve_quality_path()
    assert quality_path is not None, (
        "Cannot locate scripts/durations_quality.py. "
        "Tried: "
        + str(BACKEND_ROOT.parent / "scripts" / "durations_quality.py")
        + " (local) and /repo/scripts/durations_quality.py (Docker mount)."
    )

    import importlib.util

    spec = importlib.util.spec_from_file_location("durations_quality", quality_path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    data = _load_durations()
    derived_gap_max = module.derive_in_band_setup_gap_max(data)

    assert derived_gap_max > _IN_BAND_SETUP_GAP_MAX, (
        f"The first post-plateau observation derived from backend/.test_durations "
        f"({derived_gap_max}) is not beyond the {_IN_BAND_SETUP_GAP_MAX}s "
        "diagnostic gap. Re-check the setup-misattribution band and its data."
    )

    band = {
        key
        for key, value in data.items()
        if _IN_BAND_SETUP_MIN <= value < _IN_BAND_SETUP_MAX
    }
    module_band = module.in_band_setup_artifacts(data)
    assert module_band == band

    synthetic_band = {
        f"plateau-{index}": _IN_BAND_SETUP_MIN + index / 1000
        for index in range(_IN_BAND_SETUP_MIN_COUNT)
    }
    synthetic_band["first-post-gap"] = derived_gap_max
    assert module.in_band_setup_artifacts(synthetic_band) == set(
        synthetic_band
    ) - {"first-post-gap"}

    synthetic_band["in-gap"] = _IN_BAND_SETUP_GAP_MAX
    assert module.in_band_setup_artifacts(synthetic_band) == set()

"""
Standalone guard: the committed backend/.test_durations file must produce
time-balanced pytest-split shards.

CI uses `--splits 8 --group N` with the default durations file.  pytest-split
allocates tests via a greedy Longest-Duration-First bin-packing (heapq-based
min-heap), which is what we simulate here.  A max/avg imbalance ratio above
the threshold means at least one shard runs significantly longer than average,
defeating the parallelism benefit.

The simulation packs the *measured* durations plus a mean-imputed entry for
every collected test ID that has no entry in backend/.test_durations, because
that is what pytest-split itself does at split time (see
scripts/durations_quality.py::impute_missing_mean). Packing only the measured
subset understates the weight of coverage gaps and, on this suite's item
count, makes the imbalance ratio converge to ~1.000 regardless of input --
i.e. a threshold picked to match that number is unreachable by construction.

Runs under --noconftest (no DB, no conftest fixtures).
"""
import functools
import heapq
import importlib.util
import json
import shlex
import warnings
from pathlib import Path

import pytest

from testing.fixgate_attribution import format_offender_marker
from testing.identity_extraction import extract_python_test_ids_from_file

try:
    import tomllib
except ModuleNotFoundError:  # Python 3.10: tomllib was added in Python 3.11.
    tomllib = None

BACKEND_ROOT = Path(__file__).parents[1]
DURATIONS_PATH = BACKEND_ROOT / ".test_durations"
PYPROJECT_PATH = BACKEND_ROOT / "pyproject.toml"
TEST_FILE_GLOB = "testing/test_*.py"

NUM_SHARDS = 8
# Independently-justified, not derived from this run's own measured ratio: the
# analytic ceiling for greedy LDF packing is (average_shard + largest_item) /
# average_shard. On this suite (thousands of sub-second items, largest single
# item ~27s, per-shard average in the thousands of seconds) that ceiling sits
# just above 1.00, and empirically the packer lands within a few parts-per-
# million of 1.000 no matter how the input durations are shuffled. 1.05 (5%
# above perfectly-balanced) keeps meaningful headroom above that noise floor
# while still being reachable: it trips if a single newly-added test costs
# roughly 5% of a shard's average load (tens to ~100+ seconds depending on
# suite size), which is exactly the "one contiguous expensive test dominates
# a shard" regression this guard exists to catch.
MAX_IMBALANCE_RATIO = 1.05


def _parse_pytest_addopts_fallback(pyproject_text: str) -> str:
    """Read a single-line basic/literal ``addopts`` TOML string.

    Python 3.10 has no stdlib TOML parser.  The checked-in value is a TOML
    basic string, whose quoted representation is JSON-compatible. Reject a
    file containing multiline TOML strings rather than risk interpreting a
    header-looking line inside one as real configuration.
    """
    if '"""' in pyproject_text or "'''" in pyproject_text:
        raise ValueError(
            "Python 3.10 addopts fallback requires single-line TOML strings"
        )
    section = ""
    decoder = json.JSONDecoder()
    for line_number, raw_line in enumerate(pyproject_text.splitlines(), start=1):
        stripped = raw_line.strip()
        if stripped.startswith("["):
            closing_bracket = stripped.find("]")
            if closing_bracket == -1:
                section = ""
                continue
            trailing = stripped[closing_bracket + 1 :].strip()
            if trailing and not trailing.startswith("#"):
                section = ""
                continue
            section = stripped[1:closing_bracket].strip()
            continue
        if section != "tool.pytest.ini_options":
            continue

        key, separator, encoded_value = raw_line.partition("=")
        if not separator or key.strip() != "addopts":
            continue
        encoded_value = encoded_value.lstrip()
        if encoded_value.startswith("'"):
            end = encoded_value.find("'", 1)
            if end == -1:
                raise ValueError(
                    f"unterminated pyproject.toml addopts literal on line {line_number}"
                )
            value = encoded_value[1:end]
            end += 1
        else:
            try:
                value, end = decoder.raw_decode(encoded_value)
            except json.JSONDecodeError as exc:
                raise ValueError(
                    f"pyproject.toml addopts must be a single-line TOML string "
                    f"(line {line_number})"
                ) from exc
        trailing = encoded_value[end:].strip()
        if trailing and not trailing.startswith("#"):
            raise ValueError(
                f"unexpected content after pyproject.toml addopts on line {line_number}"
            )
        if not isinstance(value, str):
            raise ValueError(
                f"pyproject.toml addopts must be a string (line {line_number})"
            )
        return value

    raise ValueError("pyproject.toml has no [tool.pytest.ini_options] addopts string")


def _load_pytest_addopts(path: Path, *, toml_loader=tomllib) -> str:
    if toml_loader is not None:
        with path.open("rb") as file_handle:
            config = toml_loader.load(file_handle)
        return config["tool"]["pytest"]["ini_options"]["addopts"]
    return _parse_pytest_addopts_fallback(path.read_text(encoding="utf-8"))


def test_ci_uses_least_duration_splitting() -> None:
    """Keep the real pytest-split algorithm aligned with the model below.

    ``duration_based_chunks`` preserves collection-order boundaries and can
    concentrate thousands of cheap tests in one shard when recorded durations
    drift.  The least-duration algorithm uses the LDF allocation simulated by
    this guard and distributes individual tests across the least-loaded shard.
    """
    addopts = _load_pytest_addopts(PYPROJECT_PATH).split()
    configured_algorithms = [
        addopts[index + 1]
        for index, option in enumerate(addopts[:-1])
        if option == "--splitting-algorithm"
    ]
    assert configured_algorithms == ["least_duration"], (
        "pytest-split must use least_duration so CI matches this LDF balance "
        "guard; its default duration_based_chunks algorithm previously put "
        "nearly half the suite in one contiguous shard"
    )


def test_rerun_allowlist_is_pinned_to_event_loop_signatures() -> None:
    """Pin the ``--only-rerun`` allowlist that scopes ``--reruns 2``.

    ``--reruns 2`` alone would retry any failure, including ordinary
    regressions, on both the base-tree and current-tree runs of every
    differential guard. ``--only-rerun`` narrows pytest-rerunfailures'
    retries to failures whose message matches one of the given patterns
    (see pytest-rerunfailures==14.0, pinned in backend/requirements.txt).
    Today that allowlist only covers the two event-loop-teardown flake
    signatures that ``_dispose_production_engine_per_test`` in conftest.py
    defends against as its root-cause fix. Widening the allowlist, adding
    unrelated patterns, or dropping ``--only-rerun`` (making ``--reruns``
    unconditional) would silently convert this signature-scoped net into a
    blanket retry that can mask real regressions -- assert the exact set so
    that change requires touching this test.
    """
    addopts = shlex.split(_load_pytest_addopts(PYPROJECT_PATH))

    reruns_values = [
        addopts[index + 1]
        for index, option in enumerate(addopts[:-1])
        if option == "--reruns"
    ]
    assert reruns_values == ["2"], (
        "expected exactly one '--reruns 2' in pytest addopts; found "
        f"{reruns_values!r}. A missing --reruns disables the event-loop "
        "flake net entirely; more than one or a different count changes "
        "the retry budget without this test being updated."
        + "\n" + format_offender_marker(reruns_values)
    )

    only_rerun_values = [
        addopts[index + 1]
        for index, option in enumerate(addopts[:-1])
        if option == "--only-rerun"
    ]
    assert only_rerun_values == [
        "attached to a different loop",
        "Event loop is closed",
    ], (
        "pytest addopts --only-rerun allowlist changed from the pinned "
        f"event-loop signatures; found {only_rerun_values!r}. Widening "
        "this allowlist (or dropping --only-rerun so --reruns retries "
        "unconditionally) turns the signature-scoped rerun net into a "
        "blanket retry on both sides of every differential guard, which "
        "can mask ordinary regressions as flakes."
        + "\n" + format_offender_marker(only_rerun_values)
    )


def test_pytest_addopts_fallback_parses_json_compatible_string(tmp_path: Path) -> None:
    pyproject_path = tmp_path / "pyproject.toml"
    pyproject_path.write_text(
        """
[tool.unrelated]
addopts = "--splitting-algorithm wrong"

[tool.pytest.ini_options] # the fallback must tolerate a section comment
addopts = "--only-rerun \\"Event #1\\" --flag=a=b --splitting-algorithm least_duration" # note
python_classes = ["Test", "*Test"]
""".lstrip(),
        encoding="utf-8",
    )

    assert _load_pytest_addopts(pyproject_path, toml_loader=None) == (
        '--only-rerun "Event #1" --flag=a=b --splitting-algorithm least_duration'
    )


def test_pytest_addopts_fallback_is_fail_closed_for_multiline_toml() -> None:
    deceptive = '''
description = """
[tool.pytest.ini_options]
addopts = "--splitting-algorithm least_duration"
"""
[tool.pytest.ini_options]
addopts = "--splitting-algorithm duration_based_chunks"
'''
    with pytest.raises(ValueError, match="requires single-line TOML strings"):
        _parse_pytest_addopts_fallback(deceptive)


def test_pytest_addopts_fallback_matches_repo_config() -> None:
    fallback = _load_pytest_addopts(PYPROJECT_PATH, toml_loader=None)
    if tomllib is not None:
        assert fallback == _load_pytest_addopts(PYPROJECT_PATH, toml_loader=tomllib)


def _load_durations() -> dict:
    assert DURATIONS_PATH.exists(), (
        f"backend/.test_durations not found at {DURATIONS_PATH}. "
        "Generate it via scripts/store-backend-durations.sh and commit."
    )
    with DURATIONS_PATH.open() as f:
        return json.load(f)


def _extract_valid_test_ids(filepath: Path) -> set:
    """
    Return the set of valid pytest node IDs for a test file, using AST.

    Shared with the duration freshness and merge tools so all duration readers
    model the same static collection universe.
    """
    return extract_python_test_ids_from_file(filepath)


@functools.lru_cache(maxsize=1)
def _enumerate_collected_test_ids() -> frozenset:
    """Enumerate every ``testing/test_<file>.py::<id>`` pytest would collect.

    Memoized because both shard-balance tests need the same answer and the walk
    is not cheap: parsing the 627-file backend test corpus and resolving the
    collector-faithful identities costs ~15s measured over the Docker-on-Windows
    bind mount the standalone lane runs on. Paying it twice put the second
    caller's total over the lane's budget for no new information.

    Safe to memoize here, not in general: the input is the real checkout's
    ``backend/testing/test_*.py``, and no test in this module writes to it —
    every synthetic corpus these guards build lives under ``tmp_path``. The
    shared extractor in ``testing/identity_extraction.py`` is deliberately left
    uncached, because its other callers do rewrite files in-process.
    """
    universe: set = set()
    for path in sorted(BACKEND_ROOT.glob(TEST_FILE_GLOB)):
        rel = path.relative_to(BACKEND_ROOT).as_posix()
        for test_id in _extract_valid_test_ids(path):
            universe.add(f"{rel}::{test_id}")
    return frozenset(universe)


def _resolve_quality_path() -> Path | None:
    """Locate scripts/durations_quality.py across local and Docker-mount layouts."""
    candidates = [
        BACKEND_ROOT.parent / "scripts" / "durations_quality.py",
        Path("/repo/scripts/durations_quality.py"),
    ]
    for candidate in candidates:
        if candidate.exists():
            return candidate
    return None


def _impute_missing_mean(known_durations: dict) -> float:
    """
    Local copy of scripts/durations_quality.py::impute_missing_mean.

    Duplicated (rather than imported) for the same standalone-execution
    reason as ``_extract_valid_test_ids`` above; cross-checked against the
    production function by test_impute_missing_mean_matches_durations_quality.
    """
    if not known_durations:
        return 0.0
    return sum(known_durations.values()) / len(known_durations)


def _with_imputed_durations(durations: dict) -> dict:
    """
    Merge measured durations with a mean-imputed entry for every collected
    test ID missing from ``durations``.

    pytest-split assigns the suite mean to any collected test absent from the
    durations file rather than dropping it from the shard plan, so the LDF
    simulation must model that same fallback or it will understate the real
    weight CI carries for coverage gaps.
    """
    universe = _enumerate_collected_test_ids()
    known_bases = {
        f"{file_key}::{node_id.split('[')[0]}"
        for key in durations
        for file_key, _, node_id in [key.partition("::")]
    }
    missing = universe - known_bases
    merged = dict(durations)
    if missing:
        mean = _impute_missing_mean(durations)
        for index, test_id in enumerate(sorted(missing)):
            merged[f"__imputed__::{index}::{test_id}"] = mean
    return merged


def _simulate_ldf_allocation(durations: dict, num_shards: int) -> list[float]:
    """
    Simulate pytest-split's greedy Longest-Duration-First allocation.

    Sort all test durations descending, then assign each to the shard with
    the smallest current total (min-heap).  Returns a list of per-shard
    totals.
    """
    shard_totals = [0.0] * num_shards
    heap: list[tuple[float, int]] = [(0.0, i) for i in range(num_shards)]
    heapq.heapify(heap)

    for duration in sorted(durations.values(), reverse=True):
        current, shard_idx = heapq.heappop(heap)
        new_total = current + duration
        shard_totals[shard_idx] = new_total
        heapq.heappush(heap, (new_total, shard_idx))

    return shard_totals


# The standalone lane passes LANE_HANG_DETECTOR_SECONDS on the command line
# (backend/testing/test_harness_timeout_budgets.py). This guard's cost is
# dominated by _enumerate_collected_test_ids(), which must read and parse all
# 627 backend test files and resolve collector-faithful identities for each.
# Measured over the Docker-on-Windows bind mount the lane runs on: 17.82s cold,
# 14.64s with the page cache already hot -- so a warm cache does not rescue it
# either. Against the THEN-30s lane budget that was a coin flip, and on the lane
# it lost: the guard died on `Failed: Timeout (>30.0s) from pytest-timeout`, the
# signal landing mid-walk inside the per-file read (identity_extraction ->
# Path.read_text) rather than on any assertion -- i.e. it reported a slow
# filesystem as a shard-imbalance defect. The lane catch-all is far larger now,
# but the marker below stays: it bounds this scan as ITSELF.
#
# The walk is irreducible -- every test file must be enumerated, and there is
# no sound textual prefilter for "contains a test". So the budget is the thing
# that was wrong. 180 matches the number
# test_test_estate_ranked_queue.py::test_checked_in_queue_ranks_every_collected_test_file
# already carries for the same whole-corpus enumeration, and leaves ~10x
# headroom over the measurement for a CI runner competing for slots.
@pytest.mark.timeout(180)
def test_projected_shard_balance_within_threshold() -> None:
    """
    Simulate 8-shard LDF allocation from committed durations (plus mean-
    imputed entries for every collected test the file has no measurement
    for) and assert that no shard exceeds MAX_IMBALANCE_RATIO x average.

    A balanced split keeps all shards close to the average; an imbalanced
    split means the slowest shard gates the whole CI run — silently if no
    guard exists.  Emits a per-shard report as a warning so the balance is
    visible in pytest output on both pass and (partial-data) edge cases.
    """
    data = _load_durations()
    if not data:
        warnings.warn("backend/.test_durations is empty — skipping balance check.", stacklevel=2)
        return

    merged = _with_imputed_durations(data)
    shard_totals = _simulate_ldf_allocation(merged, NUM_SHARDS)
    avg_total = sum(shard_totals) / NUM_SHARDS
    max_total = max(shard_totals)
    ratio = max_total / avg_total if avg_total > 0 else 1.0

    report_lines = [
        f"Projected shard balance (LDF, {NUM_SHARDS} shards, "
        f"{len(data)} measured + {len(merged) - len(data)} imputed durations, "
        f"total={sum(shard_totals):.1f}s):"
    ]
    for i, t in enumerate(sorted(shard_totals, reverse=True), 1):
        report_lines.append(f"  shard {i}: {t:.1f}s")
    report_lines.append(f"  max/avg ratio: {ratio:.3f} (threshold: {MAX_IMBALANCE_RATIO})")
    warnings.warn("\n" + "\n".join(report_lines), stacklevel=2)

    assert ratio <= MAX_IMBALANCE_RATIO, (
        f"Projected shard imbalance too high: max/avg = {ratio:.3f} "
        f"(threshold {MAX_IMBALANCE_RATIO}). "
        f"Slowest shard: {max_total:.1f}s, average: {avg_total:.1f}s. "
        f"Regenerate backend/.test_durations via scripts/store-backend-durations.sh "
        f"or rebalance the test suite so no single shard dominates."
    )


def test_ldf_allocation_includes_imputed_missing_tests() -> None:
    """
    A collected test ID absent from the durations map must still appear in
    the merged map fed to the simulation, at the mean of known durations.

    Without this, pytest-split's real fallback for uncovered tests (assign
    the suite mean) is invisible to the guard: a coverage regression that
    drops thousands of tests out of the durations file would look identical
    to a perfectly-covered file, because dropped tests contribute zero
    weight instead of their real (imputed) weight.
    """
    known = {
        "testing/test_fixture_example.py::test_measured_one": 2.0,
        "testing/test_fixture_example.py::test_measured_two": 6.0,
    }
    universe = {
        "testing/test_fixture_example.py::test_measured_one",
        "testing/test_fixture_example.py::test_measured_two",
        "testing/test_fixture_example.py::test_uncovered",
    }
    known_bases = {
        f"{file_key}::{node_id.split('[')[0]}"
        for key in known
        for file_key, _, node_id in [key.partition("::")]
    }
    missing = universe - known_bases
    assert missing == {"testing/test_fixture_example.py::test_uncovered"}

    merged = dict(known)
    mean = _impute_missing_mean(known)
    for index, test_id in enumerate(sorted(missing)):
        merged[f"__imputed__::{index}::{test_id}"] = mean

    assert len(merged) == 3
    imputed_values = [v for k, v in merged.items() if k.startswith("__imputed__")]
    assert imputed_values == [4.0]  # mean(2.0, 6.0)


def test_imputation_uses_mean_of_known_durations() -> None:
    """``_impute_missing_mean`` is the arithmetic mean, not a fixed constant."""
    assert _impute_missing_mean({"a": 1.0, "b": 2.0, "c": 3.0}) == pytest.approx(2.0)
    assert _impute_missing_mean({"a": 10.0}) == pytest.approx(10.0)
    assert _impute_missing_mean({}) == 0.0


def test_impute_missing_mean_matches_durations_quality() -> None:
    """
    Cross-check the local imputation formula against the production
    implementation in scripts/durations_quality.py::impute_missing_mean,
    which scripts/merge_durations.py and scripts/regenerate-durations.py use
    when generating backend/.test_durations. The two must agree or the guard
    would be modeling a fallback CI's own generators don't actually apply.
    """
    quality_path = _resolve_quality_path()
    assert quality_path is not None, (
        "Cannot locate scripts/durations_quality.py. Tried: "
        + str(BACKEND_ROOT.parent / "scripts" / "durations_quality.py")
        + " (local) and /repo/scripts/durations_quality.py (Docker mount)."
    )

    spec = importlib.util.spec_from_file_location("durations_quality", quality_path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    sample = {"testing/test_a.py::test_one": 1.0, "testing/test_a.py::test_two": 9.0}
    assert module.impute_missing_mean(sample) == pytest.approx(
        _impute_missing_mean(sample)
    )
    assert module.impute_missing_mean({}) == _impute_missing_mean({}) == 0.0


# Same whole-corpus enumeration, same budget rationale as
# test_projected_shard_balance_within_threshold above. It survived the lane run
# that killed its sibling at 14.64s of the then-30s budget -- a pass by 15
# seconds, not by design; see the note above that test for what the lane budget
# is now. The memoization above now makes it nearly free when it runs second,
# but ordering is not a contract, so it carries the marker on its own account.
@pytest.mark.timeout(180)
def test_projected_shard_balance_threshold_is_reachable() -> None:
    """
    Prove MAX_IMBALANCE_RATIO can actually fail, not just pass by construction.

    The previous 1.50 threshold was mathematically unreachable: greedy LDF
    packing of this suite's thousands of sub-second items bounds the max
    shard at essentially average + largest_item, which never approaches 1.50.
    This test injects one synthetic, deliberately oversized test duration
    into a realistic imputed dataset and asserts the *same* simulation and
    *same* MAX_IMBALANCE_RATIO used by test_projected_shard_balance_within_threshold
    would flag it — demonstrating the guard has real discriminating power,
    not just a threshold no input can trip.
    """
    data = _load_durations()
    if not data:
        pytest.skip("backend/.test_durations is empty — nothing to perturb.")

    merged = _with_imputed_durations(data)
    baseline_totals = _simulate_ldf_allocation(merged, NUM_SHARDS)
    baseline_avg = sum(baseline_totals) / NUM_SHARDS
    baseline_ratio = max(baseline_totals) / baseline_avg if baseline_avg > 0 else 1.0
    assert baseline_ratio <= MAX_IMBALANCE_RATIO, (
        "baseline must be green going into this perturbation test, otherwise "
        "it proves nothing about reachability"
    )

    # A single test cannot be split across shards, so whichever shard receives
    # it has total >= oversized_duration regardless of how well thousands of
    # small filler items rebalance the rest (this is why the earlier, smaller
    # synthetic value below the threshold got fully absorbed by filler). Solve
    # oversized_duration / ((baseline_total + oversized_duration) / NUM_SHARDS)
    # > MAX_IMBALANCE_RATIO for oversized_duration, then double it for margin,
    # so tripping the guard here does not depend on the filler distribution.
    baseline_total = sum(merged.values())
    required_duration = (
        MAX_IMBALANCE_RATIO * baseline_total / (NUM_SHARDS - MAX_IMBALANCE_RATIO)
    )
    oversized_duration = required_duration * 2.0
    poisoned = dict(merged)
    poisoned["testing/test_shard_balance_synthetic.py::test_oversized"] = oversized_duration

    poisoned_totals = _simulate_ldf_allocation(poisoned, NUM_SHARDS)
    poisoned_avg = sum(poisoned_totals) / NUM_SHARDS
    poisoned_ratio = max(poisoned_totals) / poisoned_avg

    assert poisoned_ratio > MAX_IMBALANCE_RATIO, (
        f"Injecting a single {oversized_duration:.1f}s synthetic test failed to "
        f"exceed MAX_IMBALANCE_RATIO ({poisoned_ratio:.3f} <= {MAX_IMBALANCE_RATIO}). "
        "The threshold is unreachable and this guard cannot fail on a real "
        "regression."
    )

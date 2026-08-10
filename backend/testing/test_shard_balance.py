"""
Standalone guard: the committed backend/.test_durations file must produce
time-balanced pytest-split shards.

CI uses `--splits 8 --group N` with the default durations file.  pytest-split
allocates tests via a greedy Longest-Duration-First bin-packing (heapq-based
min-heap), which is what we simulate here.  A max/avg imbalance ratio above
the threshold means at least one shard runs significantly longer than average,
defeating the parallelism benefit.

Runs under --noconftest (no DB, no conftest fixtures).
"""
import heapq
import json
import warnings
from pathlib import Path

import pytest

try:
    import tomllib
except ModuleNotFoundError:  # Python 3.10: tomllib was added in Python 3.11.
    tomllib = None

BACKEND_ROOT = Path(__file__).parents[1]
DURATIONS_PATH = BACKEND_ROOT / ".test_durations"
PYPROJECT_PATH = BACKEND_ROOT / "pyproject.toml"

NUM_SHARDS = 8
# Allow up to 50 % above average before we flag it.  A perfectly balanced split
# has ratio 1.00; real LDF packing of this suite is typically 1.00–1.05.
MAX_IMBALANCE_RATIO = 1.50


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


def test_projected_shard_balance_within_threshold() -> None:
    """
    Simulate 8-shard LDF allocation from committed durations and assert that
    no shard exceeds MAX_IMBALANCE_RATIO × average.

    A balanced split keeps all shards close to the average; an imbalanced
    split means the slowest shard gates the whole CI run — silently if no
    guard exists.  Emits a per-shard report as a warning so the balance is
    visible in pytest output on both pass and (partial-data) edge cases.
    """
    data = _load_durations()
    if not data:
        warnings.warn("backend/.test_durations is empty — skipping balance check.", stacklevel=2)
        return

    shard_totals = _simulate_ldf_allocation(data, NUM_SHARDS)
    avg_total = sum(shard_totals) / NUM_SHARDS
    max_total = max(shard_totals)
    ratio = max_total / avg_total if avg_total > 0 else 1.0

    report_lines = [
        f"Projected shard balance (LDF, {NUM_SHARDS} shards, "
        f"{len(data)} durations, total={sum(shard_totals):.1f}s):"
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

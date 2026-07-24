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

BACKEND_ROOT = Path(__file__).parents[1]
DURATIONS_PATH = BACKEND_ROOT / ".test_durations"

NUM_SHARDS = 8
# Allow up to 50 % above average before we flag it.  A perfectly balanced split
# has ratio 1.00; real LDF packing of this suite is typically 1.00–1.05.
MAX_IMBALANCE_RATIO = 1.50


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

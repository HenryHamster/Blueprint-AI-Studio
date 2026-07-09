"""Cost-telemetry reconciliation: verify emitted per-task LLM cost and cache-hit
numbers against a structurally independent recomputation path.

The reconciliation function computes LLM cost two independent ways and compares
them:

- **Snapshot path**: delegates to ``compute_llm_cost_breakdown`` (the same
  aggregation the efficiency snapshot uses), extracting ``totals.total_cost_usd``
  and cache-hit rate (``total_cache_read_input_tokens / total_input_tokens``).
- **Direct path**: independently iterates each task's per-attempt rows via
  ``_task_llm_attempts``, prices each attempt with ``LLM_TOKEN_PRICING`` /
  ``_price_llm_attempt``, and accumulates raw sums — a structurally different
  computation that should agree within the declared tolerance.

The two paths share the same pricing table and attempt-row iterators but
diverge in aggregation structure (nested loop with per-model/provder buckets
vs. flat iteration), so agreement within tolerance provides strong evidence
that the production cost telemetry is internally consistent.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List

from app.llm_cost_attribution import (
    LLM_TOKEN_PRICING,
    _price_llm_attempt,
    _task_llm_attempts,
    compute_llm_cost_breakdown,
)
from app.models import Task

# Acceptable absolute error band for cost reconciliation.
# Uses 1e-6 (one-hundredth of a US cent), which accommodates the cumulative
# rounding from per-attempt cost rounding (6 decimal places in the snapshot
# path's totals) and the single-rounding difference between the snapshot's
# per-model/provder buckets and the flat-direct summation. Values smaller than
# 1e-6 would false-positive on legitimate floating-point accumulation order
# differences between the two structurally divergent aggregation paths.
COST_TELEMETRY_TOLERANCE: float = 1e-6


@dataclass
class ReconciliationResult:
    """Outcome of comparing snapshot-path vs direct-path cost computation.

    Both cost and cache-hit rate are compared independently. The cache-hit
    rate comparison uses a direct difference threshold (delta <= 1e-10) since
    the rates are ratios derived from the same shared token counters and should
    match much more closely than absolute cost values that pass through
    per-attempt rounding.
    """

    # Snapshot-path totals (from compute_llm_cost_breakdown). These are what the
    # efficiency snapshot reports and operators see in dashboards.
    total_cost_snapshot_path: float = 0.0
    total_input_tokens_snapshot_path: int = 0
    total_cache_read_input_tokens_snapshot_path: int = 0
    total_cache_creation_input_tokens_snapshot_path: int = 0

    # Direct-path totals (flat iteration over per-attempt rows).
    total_cost_direct_path: float = 0.0
    total_input_tokens_direct_path: int = 0
    total_cache_read_input_tokens_direct_path: int = 0
    total_cache_creation_input_tokens_direct_path: int = 0

    # Cost reconciliation.
    absolute_delta: float = 0.0
    relative_delta: float | None = None
    within_tolerance: bool = True
    tolerance: float = COST_TELEMETRY_TOLERANCE

    # Cache-hit rate reconciliation (computed from the token counters above).
    cache_hit_rate_snapshot_path: float = 0.0
    cache_hit_rate_direct_path: float = 0.0
    cache_hit_rate_delta: float = 0.0
    cache_hit_rate_within_tolerance: bool = True

    # Task count (must match between both paths by construction; included for
    # caller convenience when validating the input).
    task_count: int = 0

    # Detailed per-task breakdown for diagnostics.
    per_task: List[Dict[str, Any]] = field(default_factory=list)

    def asdict(self) -> Dict[str, Any]:
        """Return a plain dict suitable for JSON serialization / snapshot storage."""
        return {
            "total_cost_snapshot_path": self.total_cost_snapshot_path,
            "total_cost_direct_path": self.total_cost_direct_path,
            "absolute_delta": self.absolute_delta,
            "relative_delta": self.relative_delta,
            "within_tolerance": self.within_tolerance,
            "tolerance": self.tolerance,
            "total_input_tokens_snapshot_path": self.total_input_tokens_snapshot_path,
            "total_input_tokens_direct_path": self.total_input_tokens_direct_path,
            "total_cache_read_input_tokens_snapshot_path": (
                self.total_cache_read_input_tokens_snapshot_path
            ),
            "total_cache_read_input_tokens_direct_path": (
                self.total_cache_read_input_tokens_direct_path
            ),
            "total_cache_creation_input_tokens_snapshot_path": (
                self.total_cache_creation_input_tokens_snapshot_path
            ),
            "total_cache_creation_input_tokens_direct_path": (
                self.total_cache_creation_input_tokens_direct_path
            ),
            "cache_hit_rate_snapshot_path": self.cache_hit_rate_snapshot_path,
            "cache_hit_rate_direct_path": self.cache_hit_rate_direct_path,
            "cache_hit_rate_delta": self.cache_hit_rate_delta,
            "cache_hit_rate_within_tolerance": self.cache_hit_rate_within_tolerance,
            "task_count": self.task_count,
            "per_task": self.per_task,
        }


def _compute_direct_path_costs(
    tasks: List[Task],
) -> tuple[float, int, int, int, List[Dict[str, Any]]]:
    """Compute total LLM cost and token counters via flat direct-path iteration.

    Iterates each task's per-attempt rows, prices each attempt using the shared
    pricing table, and accumulates raw sums. This path does NOT go through
    ``compute_llm_cost_breakdown``'s nested per-model/per-provder bucket
    aggregation, so it provides an independent ground-truth comparison.

    Returns:
        (total_cost, total_input_tokens, total_cache_read_input_tokens,
         total_cache_creation_input_tokens, per_task_details)
    """
    total_cost = 0.0
    total_input = 0
    total_cache_read = 0
    total_cache_creation = 0
    per_task: List[Dict[str, Any]] = []

    for task in tasks:
        task_cost = 0.0
        task_input = 0
        task_output = 0
        task_cache_read = 0
        task_cache_creation = 0
        attempt_details: List[Dict[str, Any]] = []

        for (
            provider,
            model,
            input_tokens,
            output_tokens,
            _call_count,
            cache_creation,
            cache_read,
        ) in _task_llm_attempts(task):
            if input_tokens <= 0 and output_tokens <= 0:
                continue
            input_tokens = int(input_tokens)
            output_tokens = int(output_tokens)
            cache_creation = int(cache_creation)
            cache_read = int(cache_read)

            task_input += input_tokens
            task_output += output_tokens
            task_cache_read += cache_read
            task_cache_creation += cache_creation

            cost = 0.0
            if provider and model:
                pricing = LLM_TOKEN_PRICING.get(
                    (provider.lower(), model.lower())
                )
                if pricing:
                    input_rate, output_rate = pricing
                    cost = _price_llm_attempt(
                        input_tokens,
                        output_tokens,
                        cache_creation,
                        cache_read,
                        input_rate,
                        output_rate,
                        provider=provider,
                        model=model,
                    )

            task_cost += cost
            attempt_details.append(
                {
                    "provider": provider,
                    "model": model,
                    "input_tokens": input_tokens,
                    "output_tokens": output_tokens,
                    "cache_creation": cache_creation,
                    "cache_read": cache_read,
                    "cost_usd": round(cost, 6),
                }
            )

        total_cost += task_cost
        total_input += task_input
        total_cache_read += task_cache_read
        total_cache_creation += task_cache_creation

        per_task.append(
            {
                "task_id": str(task.id) if hasattr(task, "id") and task.id else None,
                "total_cost_usd": round(task_cost, 6),
                "total_input_tokens": task_input,
                "total_output_tokens": task_output,
                "total_cache_read_input_tokens": task_cache_read,
                "total_cache_creation_input_tokens": task_cache_creation,
                "attempts": attempt_details,
            }
        )

    return total_cost, total_input, total_cache_read, total_cache_creation, per_task


def reconcile_llm_cost(tasks: List[Task]) -> ReconciliationResult:
    """Reconcile LLM cost computed via the snapshot aggregation path against an
    independent direct-path recomputation for the given tasks.

    Args:
        tasks: List of Task records to reconcile. Typically the same task window
            the efficiency snapshot loads (e.g., tasks completed in the last N
            days for a project).

    Returns:
        A ``ReconciliationResult`` with both paths' totals, deltas, and
        ``within_tolerance`` flags.
    """
    task_count = len(tasks)

    # --- Snapshot path: use the existing compute_llm_cost_breakdown ---
    breakdown = compute_llm_cost_breakdown(tasks)
    totals = breakdown["totals"]
    snapshot_cost = totals["total_cost_usd"]
    snapshot_input = totals["total_input_tokens"]
    snapshot_cache_read = totals["total_cache_read_input_tokens"]
    snapshot_cache_creation = totals["total_cache_creation_input_tokens"]

    # --- Direct path: flat iteration over per-attempt rows ---
    (
        direct_cost,
        direct_input,
        direct_cache_read,
        direct_cache_creation,
        per_task,
    ) = _compute_direct_path_costs(tasks)

    # --- Compute cache-hit rates ---
    snapshot_cache_hit_rate = (
        (snapshot_cache_read / snapshot_input)
        if snapshot_input > 0
        else 0.0
    )
    direct_cache_hit_rate = (
        (direct_cache_read / direct_input)
        if direct_input > 0
        else 0.0
    )

    # --- Compute cost deltas ---
    absolute_delta = abs(snapshot_cost - direct_cost)
    relative_delta: float | None = (
        (absolute_delta / snapshot_cost) if snapshot_cost > 0 else None
    )
    within_tolerance = absolute_delta <= COST_TELEMETRY_TOLERANCE

    # --- Compute cache-hit rate deltas ---
    cache_hit_rate_delta = abs(snapshot_cache_hit_rate - direct_cache_hit_rate)
    # Cache-hit rates derived from the same token counters should match very
    # closely; use 1e-10 as the threshold (essentially identical).
    cache_hit_rate_within_tolerance = cache_hit_rate_delta <= 1e-10

    return ReconciliationResult(
        total_cost_snapshot_path=snapshot_cost,
        total_input_tokens_snapshot_path=snapshot_input,
        total_cache_read_input_tokens_snapshot_path=snapshot_cache_read,
        total_cache_creation_input_tokens_snapshot_path=snapshot_cache_creation,
        total_cost_direct_path=round(direct_cost, 6),
        total_input_tokens_direct_path=direct_input,
        total_cache_read_input_tokens_direct_path=direct_cache_read,
        total_cache_creation_input_tokens_direct_path=direct_cache_creation,
        absolute_delta=absolute_delta,
        relative_delta=relative_delta,
        within_tolerance=within_tolerance,
        tolerance=COST_TELEMETRY_TOLERANCE,
        cache_hit_rate_snapshot_path=round(snapshot_cache_hit_rate, 10),
        cache_hit_rate_direct_path=round(direct_cache_hit_rate, 10),
        cache_hit_rate_delta=cache_hit_rate_delta,
        cache_hit_rate_within_tolerance=cache_hit_rate_within_tolerance,
        task_count=task_count,
        per_task=per_task,
    )


def reconcile_task_llm_cost(task: Task) -> ReconciliationResult:
    """Convenience wrapper that reconciles cost for a single task.

    Equivalent to ``reconcile_llm_cost([task])`` but avoids list-wrapping
    for callers that operate on one task at a time.
    """
    return reconcile_llm_cost([task])

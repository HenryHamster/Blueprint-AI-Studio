"""Cost-telemetry reconciliation: cross-check Blueprint-counted LLM usage against
provider-reported sources.

What this module CAN detect:
- Agent accumulation bugs: tasks where the agent's internal ``api_call_summary``
  token counter diverges from the per-call provider usage objects returned by
  the Anthropic Messages API (``provider_reported`` path -- Anthropic provider
  tasks only). The two accumulators use different code paths; agreement within
  0.1% provides genuine evidence that the agent counts tokens correctly.

What this module CANNOT detect:
- Pricing table errors: if ``LLM_TOKEN_PRICING`` has a wrong rate, the computed
  cost diverges from the actual provider invoice by the same factor regardless
  of which reconciliation path is used. Detecting this requires comparison
  against a provider billing invoice or cost API, which no supported provider
  currently exposes in a machine-queryable form.
- Non-Anthropic provider accuracy: OpenAI, Gemini, DeepSeek, and Z.AI do not
  expose per-call usage probes. Usage numbers for these providers are agent-
  self-reported and unverifiable without a provider billing API.

LEGACY PATH (``internal_recompute``, kept for backward compatibility):
The older ``reconcile_llm_cost`` function compares a snapshot-path aggregation
(``compute_llm_cost_breakdown``) against a flat direct-path recomputation.
Both paths share the same pricing table (``LLM_TOKEN_PRICING``) and attempt-row
iterators (``_task_llm_attempts``), so their agreement is arithmetically
tautological -- it proves only that floating-point accumulation order is stable.
This path is DEPRECATED and MUST NOT be linked to any acceptance criterion that
requires genuine independent verification.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List

from app.llm_cost_attribution import (
    LLM_TOKEN_PRICING,
    _is_provider_probe,
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
# DEPRECATED: used only by the legacy internal_recompute path (see module docstring).
# Will be removed once all callers migrate to provider_reported.
COST_TELEMETRY_TOLERANCE: float = 1e-6

# Acceptable relative delta for the provider-reported usage comparison.
# Uses 0.001 (0.1%) which accommodates marginal rounding differences between
# the agent's per-call inclusive-input accumulation and the raw provider-reported
# token sums. Values smaller than 0.1% would false-positive on legitimate minor
# differences between how the provider counts tokens at the API boundary and how
# the agent internally sums them.
PROVIDER_REPORTED_TOLERANCE: float = 0.001


@dataclass
class ReconciliationResult:
    """Outcome of comparing usage/cost across reconciliation paths.

    The ``source`` field distinguishes which comparison path produced this
    result. See module-level docstring for what each source can and cannot
    prove.
    """

    # ── Source identification ──────────────────────────────────────────────
    # ``internal_recompute`` = deprecated snapshot-vs-direct comparison (the
    # two paths share pricing tables and iterators — arithmetically tautological).
    # ``provider_reported`` = genuine cross-check against provider-reported
    # token counts from the Anthropic Messages API ``response.usage`` object.
    source: str = "internal_recompute"

    # ── Legacy internal_recompute fields ───────────────────────────────────
    # Snapshot-path totals (from compute_llm_cost_breakdown). These are what the
    # efficiency snapshot reports and operators see in dashboards. Only populated
    # when source == "internal_recompute".
    total_cost_snapshot_path: float = 0.0
    total_input_tokens_snapshot_path: int = 0
    total_cache_read_input_tokens_snapshot_path: int = 0
    total_cache_creation_input_tokens_snapshot_path: int = 0

    # Direct-path totals (flat iteration over per-attempt rows).
    total_cost_direct_path: float = 0.0
    total_input_tokens_direct_path: int = 0
    total_cache_read_input_tokens_direct_path: int = 0
    total_cache_creation_input_tokens_direct_path: int = 0

    # Cost reconciliation (legacy internal_recompute).
    absolute_delta: float = 0.0
    relative_delta: float | None = None
    within_tolerance: bool = True
    tolerance: float = COST_TELEMETRY_TOLERANCE

    # Cache-hit rate reconciliation.
    cache_hit_rate_snapshot_path: float = 0.0
    cache_hit_rate_direct_path: float = 0.0
    cache_hit_rate_delta: float = 0.0
    cache_hit_rate_within_tolerance: bool = True

    # ── Provider-reported path fields ──────────────────────────────────────
    # Only populated when source == "provider_reported". Compares the agent's
    # api_call_summary token totals against the provider_reported_usage values
    # from the Anthropic API response.usage object.
    provider_reported_input_tokens: int = 0
    provider_reported_output_tokens: int = 0
    provider_reported_cache_creation_input_tokens: int = 0
    provider_reported_cache_read_input_tokens: int = 0
    # Agent-accumulated inclusive totals from api_call_summary.
    agent_accumulated_input_tokens: int = 0
    agent_accumulated_output_tokens: int = 0
    agent_accumulated_cache_creation_input_tokens: int = 0
    agent_accumulated_cache_read_input_tokens: int = 0

    # Overall relative delta for provider-reported usage, defined as the larger
    # of the input-token and output-token deltas below.
    provider_reported_delta: float = 0.0
    # Relative delta between provider-reported total input (input_tokens +
    # cache_creation + cache_read) and agent-accumulated total_input_tokens.
    provider_reported_input_delta: float = 0.0
    # Relative delta between provider-reported output_tokens and
    # agent-accumulated total_output_tokens.
    provider_reported_output_delta: float = 0.0
    provider_reported_within_tolerance: bool = True
    provider_reported_tolerance: float = PROVIDER_REPORTED_TOLERANCE
    # Whether at least one task in the window actually carried a usable
    # provider-reported probe. A zero-coverage window reports
    # ``provider_reported_within_tolerance=False`` (the verdict is UNPROVEN, not
    # "passing"), and this flag lets callers distinguish "failed" from
    # "nothing was independently verified".
    provider_reported_verified: bool = False

    # ── Common fields ──────────────────────────────────────────────────────
    # ``task_count`` is the number of tasks that CONTRIBUTED to this result:
    # for the provider_reported path that is the number of tasks with a usable
    # provider-reported probe (i.e. == ``provider_reported_task_count``), NOT
    # the size of the input window. Kept unchanged for backward compatibility.
    task_count: int = 0

    # ── Reconciliation coverage ────────────────────────────────────────────
    # How much of the input task window carried an INDEPENDENT probe. For the
    # provider_reported path a 0%-coverage result (no Anthropic task had a
    # provider-reported usage object) is otherwise indistinguishable from
    # "nothing needed checking" — these fields make that explicit so callers /
    # operators can tell what fraction of a project's spend window is
    # independently verified vs purely self-reported.
    total_task_count: int = 0
    provider_reported_task_count: int = 0
    # provider_reported_task_count / total_task_count, or None when zero tasks.
    coverage_fraction: float | None = None

    # Detailed per-task breakdown for diagnostics.
    per_task: List[Dict[str, Any]] = field(default_factory=list)

    def asdict(self) -> Dict[str, Any]:
        """Return a plain dict suitable for JSON serialization / snapshot storage."""
        return {
            "source": self.source,
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
            "provider_reported_input_tokens": self.provider_reported_input_tokens,
            "provider_reported_output_tokens": self.provider_reported_output_tokens,
            "provider_reported_cache_creation_input_tokens": (
                self.provider_reported_cache_creation_input_tokens
            ),
            "provider_reported_cache_read_input_tokens": (
                self.provider_reported_cache_read_input_tokens
            ),
            "agent_accumulated_input_tokens": self.agent_accumulated_input_tokens,
            "agent_accumulated_output_tokens": self.agent_accumulated_output_tokens,
            "agent_accumulated_cache_creation_input_tokens": (
                self.agent_accumulated_cache_creation_input_tokens
            ),
            "agent_accumulated_cache_read_input_tokens": (
                self.agent_accumulated_cache_read_input_tokens
            ),
            "provider_reported_delta": self.provider_reported_delta,
            "provider_reported_input_delta": self.provider_reported_input_delta,
            "provider_reported_output_delta": self.provider_reported_output_delta,
            "provider_reported_within_tolerance": self.provider_reported_within_tolerance,
            "provider_reported_verified": self.provider_reported_verified,
            "provider_reported_tolerance": self.provider_reported_tolerance,
            "task_count": self.task_count,
            "total_task_count": self.total_task_count,
            "provider_reported_task_count": self.provider_reported_task_count,
            "coverage_fraction": self.coverage_fraction,
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
        source="internal_recompute",
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


def _coerce_reconcile_int(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def reconcile_provider_reported_usage(tasks: List[Task]) -> ReconciliationResult:
    """Cross-check the agent's ``api_call_summary`` token counters against the
    provider-reported usage values from the provider API (Anthropic Messages API,
    OpenAI/Codex, DeepSeek).

    This is a **genuinely independent** comparison: the agent accumulates
    ``provider_reported_usage`` through a separate code path from
    ``api_call_summary``, and the raw provider ``response.usage`` fields are
    captured before normalization. Divergence indicates an accumulation bug,
    double-counting, or dropped-call accounting in the agent.

    Accumulation across attempts: retried tasks bank each closed attempt's
    provider-reported usage into ``attempt_history`` (key ``provider_reported_usage``)
    alongside the agent-side ``llm_*`` totals, and the live column holds the
    current attempt. Both sides are summed over every attempt that carried a
    probe, so a task's totals accumulate across retries instead of overwriting.
    Attempts WITHOUT a probe (legacy banked rows, or the explicit
    ``{"reported": false}`` not-reported marker) are excluded from BOTH the
    provider and agent sides, so they cannot manufacture a false divergence.

    Tolerance: relative delta <= 0.1% on both the inclusive input token total
    (``provider_reported_input_tokens + cache_creation + cache_read`` vs the
    agent's inclusive input total) and output token total.

    What this proves: that the agent counts provider tokens correctly for its
    own calls. What it does NOT prove: pricing table accuracy, provider billing
    accuracy, or fleet-level billing correctness.

    Args:
        tasks: List of Task records to cross-check. Tasks without any
            provider-reported probe (NULL, all-empty, or the not-reported marker)
            are skipped (not a failure).

    Returns:
        A ``ReconciliationResult`` with ``source="provider_reported"``,
        aggregated across all tasks/attempts that had both provider-reported and
        agent-accumulated token data. When NO task carried a usable probe
        (``provider_reported_task_count == 0``), the tolerance verdict is
        unproven and therefore reports ``provider_reported_within_tolerance
        == False`` with ``provider_reported_verified == False`` — a green
        verdict from zero evidence would be exactly the false confidence this
        reconciliation exists to prevent.
    """
    total_provider_input = 0
    total_provider_output = 0
    total_provider_cache_creation = 0
    total_provider_cache_read = 0
    total_agent_input = 0
    total_agent_output = 0
    total_agent_cache_creation = 0
    total_agent_cache_read = 0
    checked_count = 0
    per_task: List[Dict[str, Any]] = []

    for task in tasks:
        p_input = 0
        p_output = 0
        p_cache_creation = 0
        p_cache_read = 0
        a_input = 0
        a_output = 0
        a_cache_creation = 0
        a_cache_read = 0
        task_checked = False

        # Banked attempts (attempt_history snapshots) that carried a probe.
        for attempt in getattr(task, "attempt_history", None) or []:
            if not isinstance(attempt, dict):
                continue
            attempt_usage = attempt.get("provider_reported_usage")
            if not _is_provider_probe(attempt_usage):
                continue
            p_input += _coerce_reconcile_int(attempt_usage.get("input_tokens"))
            p_output += _coerce_reconcile_int(attempt_usage.get("output_tokens"))
            p_cache_creation += _coerce_reconcile_int(
                attempt_usage.get("cache_creation_input_tokens")
            )
            p_cache_read += _coerce_reconcile_int(
                attempt_usage.get("cache_read_input_tokens")
            )
            a_input += _coerce_reconcile_int(attempt.get("llm_input_tokens"))
            a_output += _coerce_reconcile_int(attempt.get("llm_output_tokens"))
            a_cache_creation += _coerce_reconcile_int(
                attempt.get("llm_cache_creation_input_tokens")
            )
            a_cache_read += _coerce_reconcile_int(
                attempt.get("llm_cache_read_input_tokens")
            )
            task_checked = True

        # Current attempt (live column + api_call_summary).
        current_usage = getattr(task, "provider_reported_usage", None)
        if _is_provider_probe(current_usage):
            api_summary = task.api_call_summary or {}
            p_input += _coerce_reconcile_int(current_usage.get("input_tokens"))
            p_output += _coerce_reconcile_int(current_usage.get("output_tokens"))
            p_cache_creation += _coerce_reconcile_int(
                current_usage.get("cache_creation_input_tokens")
            )
            p_cache_read += _coerce_reconcile_int(
                current_usage.get("cache_read_input_tokens")
            )
            a_input += _coerce_reconcile_int(api_summary.get("total_input_tokens"))
            a_output += _coerce_reconcile_int(api_summary.get("total_output_tokens"))
            a_cache_creation += _coerce_reconcile_int(
                api_summary.get("total_cache_creation_input_tokens")
            )
            a_cache_read += _coerce_reconcile_int(
                api_summary.get("total_cache_read_input_tokens")
            )
            task_checked = True

        # Skip tasks with no provider-reported probe at all.
        if not task_checked:
            continue

        total_provider_input += p_input
        total_provider_output += p_output
        total_provider_cache_creation += p_cache_creation
        total_provider_cache_read += p_cache_read
        total_agent_input += a_input
        total_agent_output += a_output
        total_agent_cache_creation += a_cache_creation
        total_agent_cache_read += a_cache_read
        checked_count += 1

        # Compute per-task deltas between provider-reported and agent-accumulated
        # input/output totals. A non-zero agent total against a zero provider
        # denominator is a full divergence.
        provider_total_input = p_input + p_cache_creation + p_cache_read
        agent_total_input = a_input
        task_input_delta = (
            abs(provider_total_input - agent_total_input) / provider_total_input
            if provider_total_input > 0
            else 1.0
            if agent_total_input > 0
            else 0.0
        )
        task_output_delta = (
            abs(p_output - a_output) / p_output
            if p_output > 0
            else 1.0
            if a_output > 0
            else 0.0
        )

        per_task.append({
            "task_id": str(task.id) if hasattr(task, "id") and task.id else None,
            "provider_reported_input_tokens": p_input,
            "provider_reported_output_tokens": p_output,
            "provider_reported_cache_creation_input_tokens": p_cache_creation,
            "provider_reported_cache_read_input_tokens": p_cache_read,
            "agent_accumulated_input_tokens": a_input,
            "agent_accumulated_output_tokens": a_output,
            "agent_accumulated_cache_creation_input_tokens": a_cache_creation,
            "agent_accumulated_cache_read_input_tokens": a_cache_read,
            "provider_reported_delta": round(
                max(task_input_delta, task_output_delta), 6
            ),
            "provider_reported_input_delta": round(task_input_delta, 6),
            "provider_reported_output_delta": round(task_output_delta, 6),
        })

    provider_total_input = total_provider_input + total_provider_cache_creation + total_provider_cache_read
    agent_total_input = total_agent_input
    input_delta = (
        abs(provider_total_input - agent_total_input) / provider_total_input
        if provider_total_input > 0
        else 1.0
        if agent_total_input > 0
        else 0.0
    )
    output_delta = (
        abs(total_provider_output - total_agent_output) / total_provider_output
        if total_provider_output > 0
        else 1.0
        if total_agent_output > 0
        else 0.0
    )
    delta = max(input_delta, output_delta)
    # A zero-coverage window (checked_count == 0) yields delta 0.0 by
    # construction — no provider data means no comparison was made. Reporting
    # that as "within tolerance" would certify evidence that was never read,
    # which is exactly how a ~10% sampled run passed as clean. The verdict is
    # only green when at least one task actually carried a probe.
    within_tolerance = (
        checked_count > 0 and delta <= PROVIDER_REPORTED_TOLERANCE
    )

    return ReconciliationResult(
        source="provider_reported",
        provider_reported_input_tokens=total_provider_input,
        provider_reported_output_tokens=total_provider_output,
        provider_reported_cache_creation_input_tokens=total_provider_cache_creation,
        provider_reported_cache_read_input_tokens=total_provider_cache_read,
        agent_accumulated_input_tokens=total_agent_input,
        agent_accumulated_output_tokens=total_agent_output,
        agent_accumulated_cache_creation_input_tokens=total_agent_cache_creation,
        agent_accumulated_cache_read_input_tokens=total_agent_cache_read,
        provider_reported_delta=delta,
        provider_reported_input_delta=input_delta,
        provider_reported_output_delta=output_delta,
        provider_reported_within_tolerance=within_tolerance,
        provider_reported_verified=checked_count > 0,
        provider_reported_tolerance=PROVIDER_REPORTED_TOLERANCE,
        task_count=checked_count,
        total_task_count=len(tasks),
        provider_reported_task_count=checked_count,
        coverage_fraction=(checked_count / len(tasks)) if tasks else None,
        per_task=per_task,
    )

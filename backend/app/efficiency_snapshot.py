"""Weekly efficiency-baseline snapshot persistence.

Stores per-project efficiency axes (cost per merged PR, VM cold-start p50,
VM reuse rate, median API calls per task, median review iterations) inside
``project.design_doc["efficiency_baseline"]``.  Mirrors the storage pattern
used by ``viewport_coverage_snapshot`` / ``sr_catch_rate_trends`` so we get
the same round-trip cost (one JSONB column) and the same operator semantics.

The first snapshot doubles as the baseline (the 2026-04-05 reference point
predates this telemetry and is not recoverable). Subsequent snapshots append
to ``snapshots`` only — the baseline is immutable until an operator clears
``project.design_doc["efficiency_baseline"]`` manually.

Drift is reported as percentage delta from baseline so operators can read
each axis as ``current_value`` / ``baseline_value`` / ``drift_pct``.
"""

from __future__ import annotations

import logging
import statistics
import uuid
from datetime import datetime, timedelta
from typing import Any, Dict, Optional

from fastapi import APIRouter, Depends, Query
from sqlalchemy import func
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import load_only
from sqlalchemy.orm.attributes import flag_modified
from sqlmodel import select

from app.admin import _get_user_project_with_role
from app.auth import get_current_user
from app.database import get_session
from app.models import (
    PoolIdleCostLedger,
    PoolVm,
    PoolVmState,
    Project,
    Task,
    TaskStatus,
    _utcnow,
    get_task_total_llm_totals,
    get_task_total_vm_cost_usd,
)
from app.task_locking import project_lock
from app.vm_cost_rates import idle_cost_usd

router = APIRouter()

# ---------------------------------------------------------------------------
# Cold-start stage breakdown (per-stage p50 decomposition)
# ---------------------------------------------------------------------------
# Each entry is (source_key, dest_key, metric_label). "vm_dispatched_at" is
# resolved from Task.vm_dispatched_at; all others are ISO strings written into
# task.context["_cold_start_stages"] by agent_heartbeat and claim_task.
_COLD_START_STAGE_PAIRS: tuple[tuple[str, str, str], ...] = (
    # Queue-wait: time dispatch spends waiting for a provisioning slot to open.
    # Populated by compute.py after _acquire_provision_slot succeeds. Absent on
    # older tasks — stage value is None in that case, not an error.
    ("vm_dispatched_at", "provision_slot_acquired_at", "dispatch_to_slot_ms"),
    ("vm_dispatched_at", "beacon_a_at", "dispatch_to_creds_ms"),
    ("beacon_a_at", "beacon_b_at", "creds_to_env_ms"),
    ("beacon_b_at", "beacon_c_at", "env_to_payload_ms"),
    ("beacon_c_at", "beacon_d_at", "payload_to_systemctl_ms"),
    ("beacon_d_at", "startup_complete_at", "systemctl_to_startup_ms"),
    ("startup_complete_at", "claim_at", "startup_to_claim_ms"),
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Shared key constants (writer + reader must agree on these).
# A prior incident (fingerprint 691593a5c0a7 in admin_digest.py) was caused
# by snake_case vs human-category vocabulary mismatch — keep the strings
# centralised so the same constant flows through compute -> write -> read.
# ---------------------------------------------------------------------------
AXIS_COST_PER_MERGED_PR = "cost_per_merged_pr"
AXIS_VM_COLD_START_P50_MS = "vm_cold_start_p50_ms"
AXIS_VM_REUSE_RATE = "vm_reuse_rate"
AXIS_MEDIAN_API_CALLS_PER_TASK = "median_api_calls_per_task"
AXIS_MEDIAN_REVIEW_ITERATIONS = "median_review_iterations"
AXIS_LLM_COST_PER_MERGED_PR = "llm_cost_per_merged_pr"

ALL_AXES: tuple[str, ...] = (
    AXIS_COST_PER_MERGED_PR,
    AXIS_VM_COLD_START_P50_MS,
    AXIS_VM_REUSE_RATE,
    AXIS_MEDIAN_API_CALLS_PER_TASK,
    AXIS_MEDIAN_REVIEW_ITERATIONS,
    AXIS_LLM_COST_PER_MERGED_PR,
)

# LLM token pricing table: (provider, model) -> (input_$/token, output_$/token).
# Per-token rates = per-million-token rate / 1_000_000.
#
# Sources (retrieved 2026-05):
#   Anthropic — https://www.anthropic.com/pricing#api
#   OpenAI    — https://openai.com/api/pricing
#   Gemini    — https://ai.google.dev/gemini-api/docs/pricing
#
# Gemini tasks estimate token counts from prompt/result char lengths (4 chars ≈ 1 token)
# since the Gemini CLI outputMode='text' path has no structured usage block. The estimates
# are populated via recordLlmTokens() before any exit path in agent/src/index.ts so
# timeout and kill cases are covered. Gemini pricing entries below price those estimates.
LLM_TOKEN_PRICING: dict[tuple[str, str], tuple[float, float]] = {
    # Anthropic
    ("anthropic", "claude-opus-4-8"): (15e-6, 75e-6),
    ("anthropic", "claude-opus-4-7"): (15e-6, 75e-6),
    ("anthropic", "claude-opus-4-6"): (15e-6, 75e-6),
    ("anthropic", "claude-sonnet-4-6"): (3e-6, 15e-6),
    ("anthropic", "claude-haiku-4-5-20251001"): (0.8e-6, 4e-6),
    # OpenAI (openai.com/api/pricing, 2026-05)
    ("openai", "gpt-5.4"): (5e-6, 25e-6),
    ("openai", "gpt-5.4-mini"): (0.5e-6, 2e-6),
    ("openai", "gpt-5.3-codex"): (3e-6, 12e-6),
    # gpt-5.3-codex-spark is ChatGPT-Pro-only with no separately published API
    # rate; priced at the gpt-5.3-codex rate as the closest GA equivalent
    # (openai.com/api/pricing, 2026-05).
    ("openai", "gpt-5.3-codex-spark"): (3e-6, 12e-6),
    # gpt-5.2 pre-dates the 5.3 generation (openai.com/api/pricing, 2026-05).
    ("openai", "gpt-5.2"): (2e-6, 8e-6),
    # Gemini GA models (ai.google.dev/gemini-api/docs/pricing, 2026-05)
    ("gemini", "gemini-2.5-pro"): (1.25e-6, 10e-6),
    ("gemini", "gemini-2.5-flash"): (0.30e-6, 2.5e-6),
    ("gemini", "gemini-2.5-flash-lite"): (0.10e-6, 0.40e-6),
    # Gemini 3.x previews have no separately published rate; priced at the
    # closest GA-tier equivalent (ai.google.dev/gemini-api/docs/pricing, 2026-05).
    ("gemini", "gemini-3.1-pro-preview"): (1.25e-6, 10e-6),
    ("gemini", "gemini-3-flash-preview"): (0.30e-6, 2.5e-6),
    ("gemini", "gemini-3.1-flash-lite-preview"): (0.10e-6, 0.40e-6),
}

DESIGN_DOC_KEY = "efficiency_baseline"

# Window over which "current" axes are computed for each daily snapshot.
_AXIS_WINDOW_DAYS = 7

# Cap snapshot history length (~1 year of daily snapshots).
_SNAPSHOT_HISTORY_MAX = 365


async def _load_recent_tasks(
    session: AsyncSession,
    project_id: uuid.UUID,
    *,
    window_days: int = _AXIS_WINDOW_DAYS,
) -> list[Task]:
    """Load tasks for the project that reached a terminal state in the window.

    Includes CANCELLED alongside COMPLETED/FAILED so VM cost incurred by
    cancelled tasks (which now record ``vm_stopped_at`` on cancel) is counted
    in ``cost_per_merged_pr``. The merged-PR denominator still only counts
    COMPLETED tasks with ``pr_merged``, so cancelled tasks add cost without
    inflating the merge count.
    """
    cutoff = _utcnow() - timedelta(days=window_days)
    stmt = (
        select(Task)
        .where(
            Task.project_id == project_id,
            Task.status.in_(
                [TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.CANCELLED]
            ),
            Task.updated_at >= cutoff,
        )
        .options(
            load_only(
                Task.id,
                Task.status,
                Task.context,
                Task.vm_spot,
                Task.vm_machine_type,
                Task.vm_provisioned_at,
                Task.vm_stopped_at,
                Task.vm_cold_start_ms,
                Task.vm_warm_start,
                Task.api_call_summary,
                Task.vm_dispatched_at,
                Task.provider,
                Task.model,
            )
        )
        .limit(5000)
    )
    return list((await session.execute(stmt)).scalars().all())


def _cost_and_merges(tasks: list[Task]) -> tuple[float, int]:
    """Return (total task-attached VM cost, merged-PR count) over the window."""
    total_cost = 0.0
    merged = 0
    for t in tasks:
        total_cost += get_task_total_vm_cost_usd(t)
        if t.status == TaskStatus.COMPLETED and (t.context or {}).get("pr_merged"):
            merged += 1
    return total_cost, merged


def _compute_cost_per_merged_pr(tasks: list[Task]) -> Optional[float]:
    """Total VM cost / merged PR count over the window. None when no merges."""
    total_cost, merged = _cost_and_merges(tasks)
    if not merged:
        return None
    return round(total_cost / merged, 4)


# ---------------------------------------------------------------------------
# Warm-pool idle-VM cost accounting (direction d-5fd69fe1)
# ---------------------------------------------------------------------------
# Idle pool members still bill GCE wall-clock even though they carry no task, so
# task-attached cost (get_task_total_vm_cost_usd) understates real spend once a
# pool exists — pre-warming could make cost_per_merged_pr look BETTER while
# total spend rises. We make that honest two ways:
#   1. settle_pool_vm_idle_cost() banks elapsed idle seconds * the fleet
#      per-second rate onto PoolVm.idle_cost_banked when a member leaves IDLE
#      (the lease / reaper paths in app.warm_pool are the intended callers).
#   2. compute_pool_idle_cost() sums that banked cost AND the live (not-yet-
#      settled) accrual of currently-IDLE members, so the figure is correct even
#      before any settle call fires.
# The figure is surfaced as a clearly-labelled companion to cost_per_merged_pr
# (cost_per_merged_pr_incl_pool_idle) rather than folded into the immutable
# baseline axis, so existing drift comparisons stay apples-to-apples.


def settle_pool_vm_idle_cost(
    member: PoolVm, *, now: Optional[datetime] = None
) -> float:
    """Bank elapsed idle cost onto ``member.idle_cost_banked``; return the amount.

    Computes ``(now - idle_since) * fleet_rate`` at the fleet machine type /
    spot mode, adds it to the running ``idle_cost_banked`` total, and advances
    ``idle_since`` to ``now`` so the same span is never billed twice. A no-op
    (returns 0.0) when the member is not currently idle or no time has elapsed.
    The caller owns the transaction (commit).
    """
    if member.idle_since is None:
        return 0.0
    if now is None:
        now = _utcnow()
    elapsed = (now - member.idle_since).total_seconds()
    if elapsed <= 0:
        return 0.0
    cost = idle_cost_usd(elapsed)
    member.idle_cost_banked = (member.idle_cost_banked or 0.0) + cost
    member.idle_since = now
    return cost


async def compute_pool_idle_cost(
    session: AsyncSession, project_id: Optional[uuid.UUID] = None
) -> Dict[str, Any]:
    """Aggregate warm-pool size + idle cost (banked + live accrual).

    When ``project_id`` is given the scan is scoped to that tenant's members;
    otherwise it spans every pool member (the fleet-wide admin view).

    ``pool_size`` counts live members (PROVISIONING/IDLE/LEASED); RETIRING
    members are excluded from the size but their already-banked idle cost is
    still summed so teardown never erases measured spend. ``live_idle_cost_usd``
    is the not-yet-settled accrual of currently-IDLE members.

    Because the idle reaper HARD-DELETES a member's row on teardown, the banked
    cost of every already-reaped member is carried in the durable
    ``PoolIdleCostLedger`` (written atomically with the delete). That carry-
    forward is summed into ``settled_idle_cost_usd`` here so spend survives the
    delete — without it the metric would only ever see members that happen to
    still exist at snapshot time, undercounting by the bulk of pool idle cost.
    """
    stmt = select(PoolVm)
    if project_id is not None:
        stmt = stmt.where(PoolVm.project_id == project_id)
    members = list((await session.execute(stmt)).scalars().all())

    now = _utcnow()
    settled = 0.0
    live = 0.0
    pool_size = 0
    idle_count = 0
    for m in members:
        settled += m.idle_cost_banked or 0.0
        if m.state == PoolVmState.RETIRING:
            continue
        pool_size += 1
        if m.state == PoolVmState.IDLE and m.idle_since is not None:
            idle_count += 1
            elapsed = (now - m.idle_since).total_seconds()
            if elapsed > 0:
                live += idle_cost_usd(elapsed)

    # Carry-forward: durable idle spend of members already reaped (rows deleted).
    ledger_stmt = select(
        func.coalesce(func.sum(PoolIdleCostLedger.idle_cost_usd), 0.0)
    )
    if project_id is not None:
        ledger_stmt = ledger_stmt.where(
            PoolIdleCostLedger.project_id == project_id
        )
    retired_idle = float((await session.execute(ledger_stmt)).scalar() or 0.0)
    settled += retired_idle

    return {
        "pool_size": pool_size,
        "idle_count": idle_count,
        "settled_idle_cost_usd": round(settled, 6),
        "retired_idle_cost_usd": round(retired_idle, 6),
        "live_idle_cost_usd": round(live, 6),
        "total_idle_cost_usd": round(settled + live, 6),
    }


def _compute_llm_cost_per_merged_pr(tasks: list[Task]) -> Optional[float]:
    """Total LLM token cost / merged PR count over the window. None when no merges."""
    total_cost = 0.0
    merged = 0
    for t in tasks:
        input_tokens, output_tokens, _ = get_task_total_llm_totals(t)
        if (input_tokens > 0 or output_tokens > 0) and t.provider and t.model:
            pricing = LLM_TOKEN_PRICING.get((t.provider.lower(), t.model.lower()))
            if pricing:
                input_rate, output_rate = pricing
                total_cost += input_tokens * input_rate + output_tokens * output_rate
        if t.status == TaskStatus.COMPLETED and (t.context or {}).get("pr_merged"):
            merged += 1
    if not merged:
        return None
    return round(total_cost / merged, 6)


_UNPRICED_COMBOS_CAP = 10


def compute_llm_cost_breakdown(tasks: list[Task]) -> Dict[str, Any]:
    """Per-provider and per-model LLM cost/token rollup over the given tasks.

    Returns a structured dict with:
      - ``by_provider``: list of provider-level rollups.
      - ``by_model``: list of (provider, model) rollups with a ``priced`` flag.
      - ``totals``: aggregate totals plus priced/unpriced task counts.
      - ``unpriced_combos``: capped list of ``provider:model`` strings that
        have non-zero tokens but no entry in :data:`LLM_TOKEN_PRICING`.

    A task contributes to a bucket when it has a non-null provider, non-null
    model, and non-zero aggregate tokens across the banked retry totals plus
    the live ``api_call_summary`` snapshot. Unpriced (provider, model) combos
    are still counted in ``task_count`` and feed
    ``unpriced_coverage_fraction`` but contribute ``0.0`` cost. Cost values
    are rounded to 6 decimals and shares to 4 decimals, matching the rest of
    this module.
    """
    from collections import defaultdict

    model_buckets: Dict[tuple[str, str], Dict[str, float]] = defaultdict(
        lambda: {
            "input_tokens": 0,
            "output_tokens": 0,
            "llm_call_count": 0,
            "task_count": 0,
            "cost_usd": 0.0,
        }
    )
    provider_buckets: Dict[str, Dict[str, float]] = defaultdict(
        lambda: {
            "input_tokens": 0,
            "output_tokens": 0,
            "llm_call_count": 0,
            "task_count": 0,
            "cost_usd": 0.0,
        }
    )
    priced_combos: set[tuple[str, str]] = set()
    unpriced_combos: list[str] = []
    priced_task_count = 0
    unpriced_task_count = 0
    total_input = 0
    total_output = 0
    total_cost = 0.0
    total_task_count = 0

    for t in tasks:
        input_tokens, output_tokens, call_count = get_task_total_llm_totals(t)
        if input_tokens <= 0 and output_tokens <= 0:
            continue
        if not t.provider or not t.model:
            provider = "unattributed"
            model = "unknown"
        else:
            provider = t.provider.lower()
            model = t.model.lower()
        key = (provider, model)
        pricing = LLM_TOKEN_PRICING.get(key)
        if pricing:
            input_rate, output_rate = pricing
            cost = input_tokens * input_rate + output_tokens * output_rate
            priced_combos.add(key)
            priced_task_count += 1
        else:
            cost = 0.0
            unpriced_task_count += 1
            combo = f"{provider}:{model}"
            if (
                combo not in unpriced_combos
                and len(unpriced_combos) < _UNPRICED_COMBOS_CAP
            ):
                unpriced_combos.append(combo)

        mb = model_buckets[key]
        mb["input_tokens"] += int(input_tokens)
        mb["output_tokens"] += int(output_tokens)
        mb["llm_call_count"] += call_count
        mb["task_count"] += 1
        mb["cost_usd"] += cost

        pb = provider_buckets[provider]
        pb["input_tokens"] += int(input_tokens)
        pb["output_tokens"] += int(output_tokens)
        pb["llm_call_count"] += call_count
        pb["task_count"] += 1
        pb["cost_usd"] += cost

        total_input += int(input_tokens)
        total_output += int(output_tokens)
        total_cost += cost
        total_task_count += 1

    def _share(cost: float) -> Optional[float]:
        if total_cost <= 0:
            return None
        return round(cost / total_cost, 4)

    by_provider = [
        {
            "provider": provider,
            "total_input_tokens": int(pb["input_tokens"]),
            "total_output_tokens": int(pb["output_tokens"]),
            "total_cost_usd": round(pb["cost_usd"], 6),
            "mean_cost_per_task": round(pb["cost_usd"] / pb["task_count"], 6)
            if pb["task_count"]
            else 0.0,
            "share_of_total_cost": _share(pb["cost_usd"]),
            "task_count": int(pb["task_count"]),
            "llm_call_count": int(pb["llm_call_count"]),
        }
        for provider, pb in sorted(
            provider_buckets.items(), key=lambda kv: kv[1]["cost_usd"], reverse=True
        )
    ]
    by_model = [
        {
            "provider": provider,
            "model": model,
            "total_input_tokens": int(mb["input_tokens"]),
            "total_output_tokens": int(mb["output_tokens"]),
            "total_cost_usd": round(mb["cost_usd"], 6),
            "mean_cost_per_task": round(mb["cost_usd"] / mb["task_count"], 6)
            if mb["task_count"]
            else 0.0,
            "share_of_total_cost": _share(mb["cost_usd"]),
            "task_count": int(mb["task_count"]),
            "llm_call_count": int(mb["llm_call_count"]),
            "priced": (provider, model) in priced_combos,
        }
        for (provider, model), mb in sorted(
            model_buckets.items(), key=lambda kv: kv[1]["cost_usd"], reverse=True
        )
    ]
    priceable = priced_task_count + unpriced_task_count
    unpriced_coverage_fraction: Optional[float] = (
        round(priced_task_count / priceable, 4) if priceable else None
    )
    return {
        "by_provider": by_provider,
        "by_model": by_model,
        "totals": {
            "total_input_tokens": total_input,
            "total_output_tokens": total_output,
            "total_cost_usd": round(total_cost, 6),
            "task_count": total_task_count,
            "priced_task_count": priced_task_count,
            "unpriced_task_count": unpriced_task_count,
            "unpriced_coverage_fraction": unpriced_coverage_fraction,
        },
        "unpriced_combos": unpriced_combos,
    }


def _compute_llm_cost_pricing_coverage(
    tasks: list[Task],
) -> Dict[str, Any]:
    """Coverage diagnostic: how many token-bearing tasks have a pricing entry.

    A task is "priceable" when it has non-zero total tokens AND a non-null
    (provider, model). A priceable task is "priced" when (provider, model) is
    in LLM_TOKEN_PRICING; otherwise it is "unpriced" and silently contributes
    zero to llm_cost_per_merged_pr.
    """
    priced = 0
    unpriced = 0
    unattributed = 0
    unpriced_combos: list[str] = []
    for t in tasks:
        input_tokens, output_tokens, _ = get_task_total_llm_totals(t)
        has_tokens = (input_tokens + output_tokens) > 0
        if not has_tokens:
            continue
        if not t.provider or not t.model:
            unattributed += 1
            continue
        key = (t.provider.lower(), t.model.lower())
        if key in LLM_TOKEN_PRICING:
            priced += 1
        else:
            unpriced += 1
            combo = f"{t.provider.lower()}:{t.model.lower()}"
            if (
                combo not in unpriced_combos
                and len(unpriced_combos) < _UNPRICED_COMBOS_CAP
            ):
                unpriced_combos.append(combo)
    priceable = priced + unpriced + unattributed
    coverage_fraction: Optional[float] = (
        round(priced / priceable, 4) if priceable else None
    )
    return {
        "priced_tasks": priced,
        "unpriced_tasks": unpriced,
        "unattributed_tasks": unattributed,
        "coverage_fraction": coverage_fraction,
        "unpriced_combos": unpriced_combos,
    }


def _compute_vm_cold_start_p50_ms(tasks: list[Task]) -> Optional[float]:
    samples = [int(t.vm_cold_start_ms) for t in tasks if t.vm_cold_start_ms is not None]
    if not samples:
        return None
    return round(float(statistics.median(samples)), 2)


def _compute_vm_reuse_rate(tasks: list[Task]) -> Optional[float]:
    """warm / participating. None when no VMs participated.

    Reads ``context['_timing']['warm_start']`` — the same source the
    ``/pipeline-efficiency`` endpoint uses. The ``vm_warm_start`` column is
    only ever written ``False`` at cold-provision time; warm-claim and
    next-role telemetry land in ``context['_timing']``, so reading the column
    pinned this axis at 0%. A task participates when it has a non-empty
    ``_timing`` dict OR a recorded ``vm_provisioned_at``.
    """
    warm = 0
    total = 0
    for t in tasks:
        timing = (t.context or {}).get("_timing") or {}
        if timing or t.vm_provisioned_at is not None:
            total += 1
            if timing.get("warm_start") is True:
                warm += 1
    if not total:
        return None
    return round(warm / total, 4)


def _compute_median_api_calls_per_task(tasks: list[Task]) -> Optional[float]:
    counts: list[int] = []
    for t in tasks:
        if t.api_call_summary is None and t.llm_call_count_banked is None:
            continue
        _, _, call_count = get_task_total_llm_totals(t)
        counts.append(int(call_count))
    if not counts:
        return None
    return round(float(statistics.median(counts)), 2)


def _compute_median_review_iterations(tasks: list[Task]) -> Optional[float]:
    iters: list[int] = []
    for t in tasks:
        ctx = t.context or {}
        loop = ctx.get("review_loop")
        if isinstance(loop, dict):
            n = loop.get("iteration")
            if isinstance(n, int) and n >= 0:
                iters.append(n)
        elif ctx.get("pr_merged"):
            # Review was skipped (e.g. auto-approved small diff) — counts as 0 iterations.
            # Excluding these tasks inflates the median whenever the pipeline gets more
            # efficient by skipping review, inverting the signal.
            iters.append(0)
    if not iters:
        return None
    return round(float(statistics.median(iters)), 2)


def _parse_stage_ts(val: Any) -> Optional[datetime]:
    """Parse a stage timestamp (datetime or ISO string) to a naive datetime."""
    if isinstance(val, datetime):
        return val.replace(tzinfo=None) if val.tzinfo is not None else val
    if isinstance(val, str):
        try:
            dt = datetime.fromisoformat(val.replace("Z", "+00:00"))
            return dt.replace(tzinfo=None) if dt.tzinfo is not None else dt
        except (ValueError, TypeError):
            return None
    return None


def _compute_cold_start_breakdown_p50(
    tasks: list[Task],
) -> Optional[Dict[str, Optional[float]]]:
    """Per-stage p50 ms breakdown of the VM cold-start interval.

    Returns None when no tasks have any stage data.  Individual stage values
    are None when fewer than one sample exists for that stage.
    """
    stage_samples: Dict[str, list[float]] = {
        label: [] for _, _, label in _COLD_START_STAGE_PAIRS
    }
    for t in tasks:
        stages: Dict[str, Any] = dict((t.context or {}).get("_cold_start_stages") or {})
        if not stages:
            continue
        if t.vm_dispatched_at is not None:
            stages["vm_dispatched_at"] = t.vm_dispatched_at
        for src_key, dst_key, label in _COLD_START_STAGE_PAIRS:
            if src_key not in stages or dst_key not in stages:
                continue
            t_src = _parse_stage_ts(stages[src_key])
            t_dst = _parse_stage_ts(stages[dst_key])
            if t_src is None or t_dst is None:
                continue
            delta_ms = (t_dst - t_src).total_seconds() * 1000
            if 0 <= delta_ms < 300_000:  # sanity: cap at 5 min per stage
                stage_samples[label].append(delta_ms)
    if not any(stage_samples.values()):
        return None
    return {
        label: round(float(statistics.median(stage_samples[label])), 2)
        if stage_samples[label]
        else None
        for _, _, label in _COLD_START_STAGE_PAIRS
    }


async def compute_current_axes(
    session: AsyncSession, project_id: uuid.UUID
) -> Dict[str, Any]:
    """Compute all efficiency axes (plus cold-start breakdown) from recent terminal tasks."""
    tasks = await _load_recent_tasks(session, project_id)
    pool_metrics = await compute_pool_idle_cost(session, project_id)

    # Honest net-cost companion: fold the project's warm-pool idle cost into the
    # cost_per_merged_pr numerator so pre-warming a pool can't make the headline
    # axis look better while real spend rises. Kept SEPARATE from the immutable
    # AXIS_COST_PER_MERGED_PR baseline (drift comparisons stay apples-to-apples).
    total_vm_cost, merged = _cost_and_merges(tasks)
    cost_incl_pool_idle: Optional[float] = (
        round((total_vm_cost + pool_metrics["total_idle_cost_usd"]) / merged, 4)
        if merged
        else None
    )

    return {
        AXIS_COST_PER_MERGED_PR: _compute_cost_per_merged_pr(tasks),
        AXIS_VM_COLD_START_P50_MS: _compute_vm_cold_start_p50_ms(tasks),
        AXIS_VM_REUSE_RATE: _compute_vm_reuse_rate(tasks),
        AXIS_MEDIAN_API_CALLS_PER_TASK: _compute_median_api_calls_per_task(tasks),
        AXIS_MEDIAN_REVIEW_ITERATIONS: _compute_median_review_iterations(tasks),
        AXIS_LLM_COST_PER_MERGED_PR: _compute_llm_cost_per_merged_pr(tasks),
        "vm_cold_start_breakdown": _compute_cold_start_breakdown_p50(tasks),
        "llm_cost_pricing_coverage": _compute_llm_cost_pricing_coverage(tasks),
        "pool_idle_cost_usd": pool_metrics["total_idle_cost_usd"],
        "pool_metrics": pool_metrics,
        "cost_per_merged_pr_incl_pool_idle": cost_incl_pool_idle,
    }


async def write_snapshot(
    session: AsyncSession, project_id: uuid.UUID
) -> Dict[str, Any]:
    """Compute axes and persist them under ``design_doc["efficiency_baseline"]``.

    First-snapshot semantics: the very first call seeds ``baseline``; later
    calls only append to ``snapshots`` (capped at _SNAPSHOT_HISTORY_MAX).
    Returns the snapshot dict that was written.
    """
    # Compute axes before acquiring lock — reads only Task rows, not the project.
    axes = await compute_current_axes(session, project_id)
    captured_at = _utcnow().isoformat()
    snapshot: Dict[str, Any] = {"captured_at": captured_at, **axes}

    # Lock the project row for the read-modify-write so concurrent background
    # writers (e.g. sr_blind_spots, playbook_learning) cannot clobber each
    # other's keys with stale reads.  skip_locked=False: background callers
    # should wait rather than silently skip (no 429 to surface).
    async with project_lock(session, project_id, skip_locked=False) as project:
        if project is None:
            raise ValueError(f"Project {project_id} not found")

        doc = dict(project.design_doc or {})
        container = dict(doc.get(DESIGN_DOC_KEY) or {})

        if not container.get("baseline"):
            container["baseline"] = dict(snapshot)
        else:
            baseline = dict(container["baseline"])
            # One-time migration: baselines captured before the heartbeat-beacon fix
            # (schema_version 0) stored vm_cold_start_p50_ms from the first boot
            # beacon rather than true dispatch-to-claim latency. Clear it so the
            # next snapshot re-seeds with accurate data.
            if not baseline.get("_schema_version"):
                baseline[AXIS_VM_COLD_START_P50_MS] = None
                baseline["_schema_version"] = 1
            # One-time migration: baselines captured before review-skipped tasks
            # counted as 0 iterations (schema_version 1) excluded auto-approved
            # tasks, biasing the median upward. Clear and re-seed.
            if baseline.get("_schema_version") == 1:
                baseline[AXIS_MEDIAN_REVIEW_ITERATIONS] = None
                baseline["_schema_version"] = 2
            # One-time migration: baselines captured before the vm_dispatched_at fix
            # (schema_version 2) stored vm_cold_start_p50_ms anchored at
            # provision-confirm time, excluding GCE boot latency. Clear and re-seed
            # from the corrected dispatch->agent-ready measurement.
            if baseline.get("_schema_version") == 2:
                baseline[AXIS_VM_COLD_START_P50_MS] = None
                baseline["_schema_version"] = 3
            # One-time migration: baselines captured before LLM token pricing was
            # added (schema_version 3) may have been seeded with a VM-only cost
            # value under the llm_cost_per_merged_pr key. Clear and re-seed so
            # current-vs-baseline drift is apples-to-apples.
            if baseline.get("_schema_version") == 3:
                baseline[AXIS_LLM_COST_PER_MERGED_PR] = None
                baseline["_schema_version"] = 4
            # One-time migration: baselines captured before pricing was added for
            # gpt-5.3-codex-spark, gpt-5.2, and the Gemini 3.x preview models
            # (schema_version 4) may be silently zero for tasks on those models.
            # Clear llm_cost_per_merged_pr so it re-seeds with the now-complete
            # pricing table.
            if baseline.get("_schema_version") == 4:
                baseline[AXIS_LLM_COST_PER_MERGED_PR] = None
                baseline["_schema_version"] = 5
            # Re-seed any individual axes that were cleared (e.g. by the migration
            # above) once a valid current value is available.
            for axis in ALL_AXES:
                if baseline.get(axis) is None and snapshot.get(axis) is not None:
                    baseline[axis] = snapshot[axis]
            container["baseline"] = baseline

        snapshots = list(container.get("snapshots") or [])
        snapshots.append(snapshot)
        if len(snapshots) > _SNAPSHOT_HISTORY_MAX:
            snapshots = snapshots[-_SNAPSHOT_HISTORY_MAX:]
        container["snapshots"] = snapshots
        container["last_run_at"] = captured_at

        doc[DESIGN_DOC_KEY] = container
        project.design_doc = doc
        flag_modified(project, "design_doc")
        session.add(project)

    logger.info(
        "[EFFICIENCY_SNAPSHOT] project=%s captured_at=%s axes=%s",
        project_id,
        captured_at,
        {k: axes[k] for k in ALL_AXES},
    )
    return snapshot


async def read_baseline_with_drift(
    session: AsyncSession, project_id: uuid.UUID
) -> Dict[str, Any]:
    """Return per-axis ``{current, baseline, drift_pct}`` plus capture metadata.

    ``drift_pct`` is None when either side is missing or baseline is 0.
    ``current`` reflects the latest stored snapshot (NOT a re-compute) so
    operators see the same numbers the snapshot service published. Endpoint
    consumers that want a freshly recomputed view should call
    ``compute_current_axes`` directly.

    Also includes ``reuse_funnel``: rolling 24h warm-reuse claim funnel
    counters (see :func:`app.warm_backlog_reuse.get_reuse_funnel_snapshot`).
    These counters are in-memory and instance-local; they reset on restart and
    are not stored in the efficiency baseline snapshot.
    """
    from app.warm_backlog_reuse import get_reuse_funnel_snapshot  # noqa: PLC0415

    project = await session.get(Project, project_id)
    if project is None:
        raise ValueError(f"Project {project_id} not found")

    container = (project.design_doc or {}).get(DESIGN_DOC_KEY) or {}
    baseline = container.get("baseline") or {}
    snapshots = container.get("snapshots") or []
    latest = snapshots[-1] if snapshots else {}

    axes: Dict[str, Dict[str, Any]] = {}
    for axis in ALL_AXES:
        cur = latest.get(axis)
        base = baseline.get(axis)
        drift_pct: Optional[float]
        if (
            cur is None
            or base is None
            or not isinstance(cur, (int, float))
            or not isinstance(base, (int, float))
            or base == 0
        ):
            drift_pct = None
        else:
            drift_pct = round(((cur - base) / base) * 100.0, 2)
        history = [
            {"captured_at": snap.get("captured_at"), "value": snap.get(axis)}
            for snap in snapshots
        ]
        axes[axis] = {
            "current": cur,
            "baseline": base,
            "drift_pct": drift_pct,
            "history": history,
        }

    return {
        "axes": axes,
        "baseline_captured_at": baseline.get("captured_at"),
        "last_snapshot_at": container.get("last_run_at") or latest.get("captured_at"),
        "vm_cold_start_breakdown": latest.get("vm_cold_start_breakdown"),
        "reuse_funnel": get_reuse_funnel_snapshot(),
        # Warm-pool idle cost from the latest snapshot, plus the net-cost
        # companion that folds it into cost_per_merged_pr (see compute_current_axes).
        "pool_idle_cost_usd": latest.get("pool_idle_cost_usd"),
        "pool_metrics": latest.get("pool_metrics"),
        "cost_per_merged_pr_incl_pool_idle": latest.get(
            "cost_per_merged_pr_incl_pool_idle"
        ),
    }


# ---------------------------------------------------------------------------
# Background-service helpers
# ---------------------------------------------------------------------------

# Snapshot cadence: one snapshot per UTC day.
# Service ticks every hour and decides whether the cadence has elapsed.
SNAPSHOT_INTERVAL_SECONDS = 3600
_DAY_SECONDS = 24 * 3600


def _last_run_due(last_run_at_iso: Optional[str], now: datetime) -> bool:
    """Return True when a new snapshot is due.

    Cadence is one snapshot per UTC day. The first call is always due.
    Subsequent calls are due once at least one day has passed since the
    previous ``last_run_at`` marker.
    """
    if not last_run_at_iso:
        return True
    try:
        last_run = datetime.fromisoformat(last_run_at_iso)
    except (TypeError, ValueError):
        return True
    if last_run.tzinfo is not None:
        last_run = last_run.replace(tzinfo=None)
    return (now - last_run).total_seconds() >= _DAY_SECONDS


async def maybe_write_daily_snapshot(
    session: AsyncSession, project_id: uuid.UUID, *, now: Optional[datetime] = None
) -> Optional[Dict[str, Any]]:
    """Write a snapshot iff the daily cadence has elapsed for the project.

    Reads ``design_doc["efficiency_baseline"]["last_run_at"]`` for the
    cadence marker. The marker doubles as a multi-instance-deploy guard:
    even if leader election briefly hands off, the next instance sees the
    marker and skips. Returns the snapshot dict on write, None on skip.
    """
    if now is None:
        now = _utcnow()

    # populate_existing: a caller that left a partial Project (e.g. via
    # ``load_only(Project.id)``) in the session identity map would otherwise
    # return that cached partial here, and the ``design_doc`` access below
    # would fire a sync lazy-load in async context. See ``run_once``.
    project = await session.get(Project, project_id, populate_existing=True)
    if project is None:
        return None

    container = (project.design_doc or {}).get(DESIGN_DOC_KEY) or {}
    if not _last_run_due(container.get("last_run_at"), now):
        return None
    return await write_snapshot(session, project_id)


# ---------------------------------------------------------------------------
# HTTP endpoints (sub-router, mounted by app.routers.admin_digest)
# ---------------------------------------------------------------------------


@router.get("/projects/{project_id}/efficiency-axes")
async def get_efficiency_axes(
    project_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    current_user: dict = Depends(get_current_user),
) -> Dict[str, Any]:
    """Return per-axis current/baseline/drift/history for the project's efficiency baseline.

    Reads ``project.design_doc["efficiency_baseline"]`` (populated weekly by
    the ``_efficiency_snapshot_service``) and returns each of the five
    foundational axes — ``cost_per_merged_pr``, ``vm_cold_start_p50_ms``,
    ``vm_reuse_rate``, ``median_api_calls_per_task``,
    ``median_review_iterations`` — with the latest snapshot value, the
    immutable baseline value, percentage drift from baseline, and a
    time-series of up to 26 weekly snapshots for trend rendering.

    Also returns ``vm_cold_start_breakdown``: a per-stage p50 ms breakdown of
    the cold-start interval from the latest snapshot.  ``dispatch_to_slot_ms``
    is the queue-wait before a provisioning slot is acquired (isolates our own
    concurrency throttle from real GCE boot latency); the remaining stages
    decompose boot through agent claim.  All values are None when no tasks in
    the latest snapshot had stage data.

    Response shape::

        {
          "axes": {
            "<axis>": {
              "current": float|None,
              "baseline": float|None,
              "drift_pct": float|None,
              "history": [{"captured_at": iso8601, "value": float|None}, ...]
            },
            ...
          },
          "baseline_captured_at": iso8601|None,
          "last_snapshot_at": iso8601|None,
          "vm_cold_start_breakdown": {
            "dispatch_to_slot_ms": float|None,
            "dispatch_to_creds_ms": float|None,
            "creds_to_env_ms": float|None,
            "env_to_payload_ms": float|None,
            "payload_to_systemctl_ms": float|None,
            "systemctl_to_startup_ms": float|None,
            "startup_to_claim_ms": float|None
          } | None
        }

    ``drift_pct`` is None whenever either side is missing or baseline is 0.
    ``history`` is ordered oldest-first; before any snapshot fires it is ``[]``.
    Before the first snapshot fires, all axis values are None and both
    timestamps are None.
    """
    await _get_user_project_with_role(
        project_id, current_user["user_id"], session, min_role="viewer"
    )
    return await read_baseline_with_drift(session, project_id)


@router.get("/projects/{project_id}/llm-cost-breakdown")
async def get_llm_cost_breakdown(
    project_id: uuid.UUID,
    window_days: int = Query(default=7, ge=1, le=90),
    session: AsyncSession = Depends(get_session),
    current_user: dict = Depends(get_current_user),
) -> Dict[str, Any]:
    """Per-provider and per-model LLM cost/token breakdown over a project window.

    Read-only view over recently-terminal tasks (COMPLETED/FAILED/CANCELLED
    within ``window_days``, default 7). Reuses the snapshot pricing table
    (:data:`app.efficiency_snapshot.LLM_TOKEN_PRICING`) so cost numbers are
    consistent with ``llm_cost_per_merged_pr``.

    Response shape::

        {
          "window_days": int,
          "generated_at": iso8601,
          "by_provider": [
            {
              "provider": str,
              "total_input_tokens": int,
              "total_output_tokens": int,
              "total_cost_usd": float,
              "mean_cost_per_task": float,
              "share_of_total_cost": float|None,
              "task_count": int,
              "llm_call_count": int
            }, ...
          ],
          "by_model": [
            {
              "provider": str,
              "model": str,
              "total_input_tokens": int,
              "total_output_tokens": int,
              "total_cost_usd": float,
              "mean_cost_per_task": float,
              "share_of_total_cost": float|None,
              "task_count": int,
              "llm_call_count": int,
              "priced": bool
            }, ...
          ],
          "totals": {
            "total_input_tokens": int,
            "total_output_tokens": int,
            "total_cost_usd": float,
            "task_count": int,
            "priced_task_count": int,
            "unpriced_task_count": int,
            "unpriced_coverage_fraction": float|None
          },
          "unpriced_combos": [str, ...]
        }
    """
    await _get_user_project_with_role(
        project_id, current_user["user_id"], session, min_role="viewer"
    )
    tasks = await _load_recent_tasks(session, project_id, window_days=window_days)
    breakdown = compute_llm_cost_breakdown(tasks)
    return {
        "window_days": window_days,
        "generated_at": _utcnow().isoformat(),
        **breakdown,
    }

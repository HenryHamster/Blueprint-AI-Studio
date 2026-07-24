"""Spawn health and scope calibration helpers.

Extracted from app.research_completion to keep that module under its
line-count guardrail. Contains:

- `_assess_spawn_health`: zone/account/failure-rate gating for spawn-time degradation.
- `_maybe_cold_start_upgrade_medium`: pipeline-wide medium→large upgrade for new directions.
- `_calibrate_scope`: main scope calibration (downgrade / decompose-upgrade) from history.
- `predict_spawn_success`: in-memory P(success) prediction used to gate spawns.
- `SCOPE_FILE_BUDGETS`: per-scope file-count budgets surfaced to research agents.
"""

import logging
import os
from datetime import timedelta
from typing import Any

from sqlalchemy import func
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from app.direction_metrics import (
    _compute_acceptance_rate,
    _compute_direction_quality_score,
    _compute_momentum_factor,
    _compute_rolling_median_review_iterations,
)
from app.models import Task, TaskStatus
from app.shared_state import runtime_config as _runtime_config
from app.time_utils import _utcnow

logger = logging.getLogger(__name__)

_SCOPE_ORDER = ["small", "medium", "large"]

# Hard file-count budgets enforced by implementing agents per scope.
# Surfaced to research agents via pipeline_health so they can align finding
# scope with these limits. Values mirror SCOPE_OVERFLOW_THRESHOLDS and
# MEDIUM_ANALYSIS_PLAN_FILE_LIMIT in agent/src/constants.ts — keep in sync.
SCOPE_FILE_BUDGETS: dict[str, dict[str, int]] = {
    "small": {"total_files_max": 6},
    "medium": {"production_files_max": 10, "total_files_max": 10},
    "large": {"total_files_max": 20},
}

# Mirrors agent/src/constants.ts MEDIUM_ANALYSIS_PLAN_FILE_LIMIT. If a medium-scope
# finding lists more files than this, the pre-spawn gate auto-upgrades to large.
MEDIUM_ANALYSIS_PLAN_FILE_LIMIT: int = 10

# Maximum implementation tasks to spawn when pipeline health is degraded.
# Remaining findings are deferred to task.context['deferred_findings'] for
# the next research cycle.
_DEGRADED_SPAWN_CAP = 2

# BLOCKED is intentionally absent: a blocked task is parked on an absent
# precondition, occupies no VM/active slot, and must not count against the
# active-capacity budget (counting it would throttle spawning of genuinely
# runnable work behind work that cannot run yet).
#
# PARKED IS counted (unlike BLOCKED): a parked task is committed in-flight work
# that will deterministically reclaim a slot when its external wake fires (RESUME
# re-queues it; FINALIZE completes it). It currently holds no VM, but the budget
# must reserve for its return — omitting it would let the project over-spawn past
# the slots its parked siblings are about to take back. BLOCKED has no such
# guaranteed return (its precondition may never clear), so the cases differ.
_ACTIVE_SPAWN_HEALTH_STATUSES = (
    TaskStatus.QUEUED,
    TaskStatus.PROVISIONING,
    TaskStatus.IN_PROGRESS,
    TaskStatus.EVALUATING,
    TaskStatus.PARKED,
)


def _active_task_budget_for_design_cap(design_cap: int) -> int:
    return max(1, int(design_cap) * 2)


async def _count_project_active_tasks(session: AsyncSession, project_id: Any) -> int:
    row = (
        await session.execute(
            select(func.count())
            .select_from(Task)
            .where(
                Task.project_id == project_id,
                Task.status.in_(_ACTIVE_SPAWN_HEALTH_STATUSES),
            )
        )
    ).one()
    return int(row[0] or 0)


async def _assess_project_spawn_health(
    session: AsyncSession,
    project_id: Any,
    design_cap: int,
) -> tuple[bool, str | None]:
    active_task_count = await _count_project_active_tasks(session, project_id)
    active_task_budget = _active_task_budget_for_design_cap(design_cap)
    return _assess_spawn_health(
        active_task_count=active_task_count,
        active_task_budget=active_task_budget,
    )


def _assess_spawn_health(
    *,
    active_task_count: int | None = None,
    active_task_budget: int | None = None,
) -> tuple[bool, str | None]:
    """Check pipeline health signals for spawn-time gating.

    Returns (is_degraded, reason). When degraded, _handle_research_completion
    caps new implementation task creation at _DEGRADED_SPAWN_CAP.

    Checks (in order):
    1. Zone availability: all zones in cooldown → degraded
    2. Account availability: no accounts available for create model → degraded
    3. Active backlog: project already has > budget active tasks → degraded
    4. Rolling failure rate: >50% of recent task_results are failures → degraded
    """
    from app.compute import any_zone_available  # noqa: PLC0415
    from app.max_plan_pool import _account_by_id, _account_available_for_model  # noqa: PLC0415
    from app.shared_state import deploy_monitoring  # noqa: PLC0415

    if not any_zone_available():
        return True, "all zones in cooldown"

    if not os.getenv("ANTHROPIC_API_KEY"):
        create_model = _runtime_config.get("task_type_models", {}).get("create")
        all_accounts = list(_account_by_id.values())
        if all_accounts and not any(
            _account_available_for_model(a, create_model) for a in all_accounts
        ):
            return True, "no accounts available"

    if active_task_count is not None and active_task_budget is not None:
        if active_task_count > active_task_budget:
            return True, (
                f"active task backlog over budget "
                f"({active_task_count}/{active_task_budget} queued/in-flight)"
            )

    task_results = list(deploy_monitoring.get("task_results", []))
    # Apply a 60-minute window to avoid false positives from historical failures that
    # are no longer representative of current pipeline health. Old failures (hours ago)
    # can dominate the ring buffer even when the system has fully recovered.
    _sixty_min_ago = (_utcnow() - timedelta(minutes=60)).isoformat()
    recent_results = [r for r in task_results if r[0] >= _sixty_min_ago]
    if len(recent_results) >= 10:
        failures = sum(1 for entry in recent_results if entry[1] == "failure")
        if failures / len(recent_results) > 0.5:
            return (
                True,
                f"high failure rate ({failures}/{len(recent_results)} recent tasks failed)",
            )

    return False, None


def _maybe_cold_start_upgrade_medium(
    proposed_scope: str, direction: dict
) -> tuple[str, str | None]:
    """Upgrade medium→large(decompose) when no direction/project history exists yet.

    Mirrors the in-history upgrade path (medium failing, small healthy) but uses
    the pipeline-wide baseline rates from runtime_config so a new direction
    doesn't burn through 3+ medium-scope failures before the standard calibration
    upgrade triggers. Returns (proposed_scope, None) when not eligible.
    """
    if proposed_scope != "medium":
        return proposed_scope, None
    baselines = _runtime_config.get("pipeline_scope_baseline_rates") or {}
    medium_rate = baselines.get("medium")
    small_rate = baselines.get("small")
    threshold = 0.4
    if (
        medium_rate is None
        or small_rate is None
        or medium_rate >= threshold
        or small_rate < threshold
    ):
        return proposed_scope, None
    logger.info(
        "[RESEARCH] Cold-start upgrade scope medium→large(decompose) for direction '%s' "
        "(pipeline baseline medium %.0f%% < %.0f%%, small %.0f%% healthy)",
        direction.get("title", direction.get("id", "?")),
        medium_rate * 100,
        threshold * 100,
        small_rate * 100,
    )
    return "large", "medium"


def _poor_decomp_history(direction: dict, label: str = "") -> bool:
    """Suppression signal: weighted avg subtask_merge_rate < 0.3 across >= 3
    subtasks in last 3 cycles. Logs when True.
    """
    ws, total = 0.0, 0
    for entry in direction.get("research_history", [])[-3:]:
        for dq in entry.get("decomposition_quality") or []:
            r = dq.get("subtask_merge_rate")
            if isinstance(r, (int, float)):
                w = dq.get("subtask_count") or 1
                ws, total = ws + float(r) * w, total + w
    # Incorporate folded aggregate decomposition quality so evicted cycles contribute.
    agg_dq = (direction.get("history_aggregate") or {}).get("decomposition_quality") or {}
    agg_ws = agg_dq.get("subtask_merge_rate_weighted_sum") or 0.0
    agg_cnt = agg_dq.get("subtask_count_sum") or 0
    if isinstance(agg_ws, (int, float)) and isinstance(agg_cnt, int) and agg_cnt > 0:
        ws += float(agg_ws)
        total += agg_cnt
    if total < 3 or ws / total >= 0.3:
        return False
    logger.info(
        "[RESEARCH] Suppressing medium→large upgrade%s (avg %.0f%% < 30%% over %d subtasks)",
        f" for '{label}'" if label else "",
        (ws / total) * 100,
        total,
    )
    return True


def _merge_agg_scope_breakdown(target: dict[str, dict], agg_scope: dict) -> None:
    """Merge folded history_aggregate scope_breakdown counters into a live scope_agg.

    The aggregate stores a subset of the counters that _aggregate_scope_breakdown
    produces (created, merged, failed, failed_infra, failed_scope_overflow).
    Only those keys are merged; keys absent from the aggregate (merged_cross_module,
    failed_analysis_overflow, failure_stages) are left at their live-data values.
    """
    for scope, stats in agg_scope.items():
        bucket = target.setdefault(
            scope,
            {
                "created": 0,
                "merged": 0,
                "merged_cross_module": 0,
                "failed": 0,
                "failed_infra": 0,
                "failed_scope_overflow": 0,
                "failed_analysis_overflow": 0,
                "failure_stages": {},
            },
        )
        for key in ("created", "merged", "failed", "failed_infra", "failed_scope_overflow"):
            bucket[key] = bucket.get(key, 0) + stats.get(key, 0)


def _aggregate_scope_breakdown(history_entries: list[dict]) -> dict[str, dict]:
    """Sum scope_breakdown counters across a list of research_history entries."""
    agg: dict[str, dict] = {}
    for entry in history_entries:
        for scope, stats in (entry.get("scope_breakdown") or {}).items():
            bucket = agg.setdefault(
                scope,
                {
                    "created": 0,
                    "merged": 0,
                    "merged_cross_module": 0,
                    "failed": 0,
                    "failed_infra": 0,
                    "failed_scope_overflow": 0,
                    "failed_analysis_overflow": 0,
                    "failure_stages": {},
                },
            )
            for key in (
                "created",
                "merged",
                "merged_cross_module",
                "failed",
                "failed_infra",
                "failed_scope_overflow",
                "failed_analysis_overflow",
            ):
                bucket[key] = bucket.get(key, 0) + stats.get(key, 0)
            stage_bucket = bucket["failure_stages"]
            for stage_name, count in (stats.get("failure_stages") or {}).items():
                stage_bucket[stage_name] = stage_bucket.get(stage_name, 0) + count
    return agg


def _effective_scope_denominator(stats: dict) -> int:
    """Denominator for scope merge rate: terminal tasks minus true-infra failures.

    Counts merged + failed (terminal) rather than `created` to exclude in-flight
    QUEUED/IN_PROGRESS/EVALUATING tasks that would bias rates downward (mirrors
    strategic_spawning._window_counts). SCOPE_OVERFLOW stays in the denominator
    (scope-quality signal); only non-overflow infra fails are excluded.
    """
    infra_excl_overflow = stats.get("failed_infra", 0) - stats.get(
        "failed_scope_overflow", 0
    )
    return stats.get("merged", 0) + stats.get("failed", 0) - infra_excl_overflow


def _get_failure_coaching_suffix(direction_id: str, design_doc: dict) -> str | None:
    """Prompt suffix naming the dominant first-attempt failure category for a direction.

    Returns None when fewer than 3 rejections or no single category accounts for ≥40%.
    Called at research-task spawn time so the agent can bias findings toward mitigations.
    """
    dfa = (design_doc or {}).get("direction_first_attempt_feedback") or {}
    entry = dfa.get(str(direction_id)) or {}
    cat_counts = {
        k: v
        for k, v in entry.items()
        if k not in ("total", "last_updated") and isinstance(v, (int, float)) and v > 0
    }
    total = sum(cat_counts.values())
    if total < 3 or not cat_counts:
        return None
    top_cat, top_count = max(cat_counts.items(), key=lambda kv: kv[1])
    if top_count / total < 0.4:
        return None
    logger.info(
        "[RESEARCH] Direction %s: dominant failure category '%s' (%d/%d first-attempt rejections) — injecting coaching",
        direction_id,
        top_cat,
        int(top_count),
        int(total),
    )
    return (
        f"Note: this direction has recurring first-attempt rejections in the '{top_cat}' category "
        f"({int(top_count)}/{int(total)} recent cases). Prefer findings that address this failure mode "
        "and consider smaller scopes to reduce implementation risk."
    )


def _feedback_scope_override(
    proposed_scope: str,
    idx: int,
    direction: dict,
    project: Any,
    task_type: str | None,
    *,
    _dfa: dict | None = None,
) -> tuple[str, str | None] | None:
    """Check failure feedback and success patterns for a cold-start scope override.

    Returns (scope, calibrated_from) if an override applies, else None.
    Reads direction_first_attempt_feedback for scope_related failures and
    playbook/success-{task_type}-{scope} for confirmed success patterns.
    Only called when historical merge-rate data is insufficient.

    ``_dfa`` is optional telemetry-preferred data for the
    ``direction_first_attempt_feedback`` key (which is stripped from
    ``project.design_doc`` by ``strip_migrated_doc_keys`` at write time).
    When provided, it takes precedence over ``design_doc`` data. Callers
    with session access should pre-load it via ``get_project_telemetry``.
    """
    doc = getattr(project, "design_doc", None) or {}
    label = direction.get("title", direction.get("id", "?"))
    dir_id = str(direction.get("id") or "")
    # scope_related failure feedback: repeated scope misfits → downscope
    if idx > 0 and dir_id:
        dfa = _dfa or doc.get("direction_first_attempt_feedback") or {}
        sc_count = dfa.get(dir_id, {}).get("scope_related", 0)
        if isinstance(sc_count, (int, float)) and sc_count >= 3:
            smaller = _SCOPE_ORDER[idx - 1]
            logger.info(
                "[RESEARCH] Downgrading scope %s→%s for direction '%s' "
                "(failure feedback: %d scope_related first-attempt rejections)",
                proposed_scope,
                smaller,
                label,
                int(sc_count),
            )
            return smaller, proposed_scope
    # Success-pattern evidence: retain scope on strong global history OR
    # on direction-local history (lower threshold because direction-specific
    # data is more precise than project-wide task-type/scope counts).
    if task_type:
        sp_entry = doc.get(f"playbook/success-{task_type}-{proposed_scope}") or {}
        cnt = sp_entry.get("success_count", 0)
        if isinstance(cnt, (int, float)) and cnt >= 5:
            logger.info(
                "[RESEARCH] Retaining scope %s for direction '%s' "
                "(success-pattern: %d %s/%s confirmed successes)",
                proposed_scope,
                label,
                int(cnt),
                task_type,
                proposed_scope,
            )
            return proposed_scope, None
        # Direction-affinity: 2+ local successes for this specific direction
        # override the global 5-success threshold.
        if dir_id:
            raw_dir_succ = sp_entry.get("direction_successes")
            dir_succ = raw_dir_succ if isinstance(raw_dir_succ, dict) else {}
            dir_cnt = dir_succ.get(dir_id, 0)
            if isinstance(dir_cnt, (int, float)) and dir_cnt >= 2:
                logger.info(
                    "[RESEARCH] Retaining scope %s for direction '%s' "
                    "(direction-affinity: %d local %s/%s successes)",
                    proposed_scope,
                    label,
                    int(dir_cnt),
                    task_type,
                    proposed_scope,
                )
                return proposed_scope, None
    return None


def _calibrate_scope(
    proposed_scope: str,
    direction: dict,
    project: Any | None = None,
    task_type: str | None = None,
    *,
    _dfa: dict | None = None,
) -> tuple[str, str | None]:
    """Downgrade scope if historical merge rate at that scope is below threshold.

    Returns (final_scope, calibrated_from) where calibrated_from is the original
    scope if a downgrade occurred, else None.

    ``_dfa`` is optional telemetry-preferred data for the
    ``direction_first_attempt_feedback`` key. When provided, forwarded to
    :func:`_feedback_scope_override` so it reads the canonical telemetry
    value rather than the stripped ``design_doc`` copy.

    Uses direction's last-3 research_history (40% threshold) when it has >= 3
    data points. Otherwise falls back to project-wide aggregation across all
    directions with a softer 30% threshold to avoid over-aggressive downgrades.
    When neither source has enough data, applies a cold-start upgrade for
    medium-scope tasks based on pipeline-wide baseline rates.
    """
    try:
        idx = _SCOPE_ORDER.index(proposed_scope)
    except ValueError:
        return proposed_scope, None

    # Aggregate direction-specific scope_breakdown across last 3 history entries,
    # then merge folded aggregate so evicted cycles contribute when live data is sparse.
    aggregated = _aggregate_scope_breakdown(direction.get("research_history", [])[-3:])
    direction_data_points = sum(v["created"] for v in aggregated.values())
    if direction_data_points < 3:
        _merge_agg_scope_breakdown(
            aggregated,
            (direction.get("history_aggregate") or {}).get("scope_breakdown") or {},
        )
        direction_data_points = sum(v["created"] for v in aggregated.values())

    if direction_data_points >= 3:
        agg = aggregated
        threshold = 0.4
        source = "direction"
    elif project is not None:
        # Fallback: aggregate scope_breakdown across all directions in the project
        proj_entries = [
            entry
            for d in (project.directions or [])
            for entry in (d.get("research_history") or [])
        ]
        proj_agg = _aggregate_scope_breakdown(proj_entries)
        if sum(v["created"] for v in proj_agg.values()) < 3:
            _ov = _feedback_scope_override(
                proposed_scope, idx, direction, project, task_type,
                _dfa=_dfa,
            )
            if _ov is not None:
                return _ov
            return _maybe_cold_start_upgrade_medium(proposed_scope, direction)
        agg = proj_agg
        threshold = 0.3
        source = "project"
    else:
        return _maybe_cold_start_upgrade_medium(proposed_scope, direction)

    def _merge_rate(scope: str) -> float | None:
        stats = agg.get(scope)
        if not stats:
            return None
        effective = _effective_scope_denominator(stats)
        if effective < 3:
            return None
        return stats["merged"] / effective

    rate = _merge_rate(proposed_scope)

    # Overflow-rate escalation: even when the merge rate is acceptable,
    # proactively upgrade a scope that predictably overflows. Without this,
    # compute is spent on a first attempt that gets rescoped on retry anyway.
    proposed_stats = agg.get(proposed_scope)
    if (
        proposed_stats
        and proposed_stats.get("created", 0) >= 3
        and idx + 1 < len(_SCOPE_ORDER)
    ):
        overflow = proposed_stats.get("failed_scope_overflow", 0)
        overflow_rate = overflow / proposed_stats["created"]
        if overflow_rate >= 0.3:
            larger = _SCOPE_ORDER[idx + 1]
            logger.info(
                "[RESEARCH] Upgrading scope %s→%s for direction '%s' "
                "(%s overflow rate %.0f%% >= 30%%, %d/%d tasks overflowed)",
                proposed_scope,
                larger,
                direction.get("title", direction.get("id", "?")),
                source,
                overflow_rate * 100,
                overflow,
                proposed_stats["created"],
            )
            return larger, proposed_scope

        # Analysis-overflow dominance escalation (medium only): when the agent's
        # analysis phase consistently cannot plan within the medium file budget,
        # the research estimate is systematically too optimistic — hard-escalate
        # to large rather than relying on the soft predict_spawn_success penalty.
        if proposed_scope == "medium":
            analysis_overflow = proposed_stats.get("failed_analysis_overflow", 0)
            analysis_rate = analysis_overflow / proposed_stats["created"]
            if analysis_rate >= 0.5:
                larger = _SCOPE_ORDER[idx + 1]
                logger.info(
                    "[RESEARCH] Upgrading scope %s→%s for direction '%s' "
                    "(%s analysis_overflow rate %.0f%% >= 50%%, %d/%d tasks)",
                    proposed_scope,
                    larger,
                    direction.get("title", direction.get("id", "?")),
                    source,
                    analysis_rate * 100,
                    analysis_overflow,
                    proposed_stats["created"],
                )
                return larger, proposed_scope

    if rate is None or rate >= threshold:
        return proposed_scope, None

    # Upgrade path: medium→large(decompose) when medium is failing but small is healthy.
    # Why: downgrading medium→small forces medium-sized work into a small budget, triggering
    # scope_overflow failures. Routing through large invokes the plan agent, which decomposes
    # the work into multiple small subtasks — each succeeding at the healthy small-scope rate.
    # calibrated_from is kept as the bare proposed scope so downstream consumers
    # (direction_outcomes bookkeeping, prompt.ts, admin analytics) continue to receive
    # a valid scope name — the "(decompose)" detail is captured in logs only.
    if proposed_scope == "medium" and idx + 1 < len(_SCOPE_ORDER):
        small_rate = _merge_rate("small")
        if small_rate is not None and small_rate >= threshold:
            # Suppress upgrade if prior plan decompositions for this direction
            # produced poorly-merging subtasks — continuing to route medium→large
            # would just generate more failing decompositions. Fall through to
            # the downgrade loop so the task routes to the healthy small scope
            # rather than being stuck at a failing medium scope.
            if _poor_decomp_history(
                direction, direction.get("title", direction.get("id", "?"))
            ):
                pass  # fall through to downgrade loop below
            else:
                larger = _SCOPE_ORDER[idx + 1]
                logger.info(
                    "[RESEARCH] Upgrading scope %s→%s(decompose) for direction '%s' "
                    "(%s medium rate %.0f%% < %.0f%%, small rate %.0f%% healthy)",
                    proposed_scope,
                    larger,
                    direction.get("title", direction.get("id", "?")),
                    source,
                    rate * 100,
                    threshold * 100,
                    small_rate * 100,
                )
                return larger, proposed_scope

    # Downgrade to next smaller scope with acceptable rate
    for smaller_scope in reversed(_SCOPE_ORDER[:idx]):
        smaller_rate = _merge_rate(smaller_scope)
        if smaller_rate is None or smaller_rate >= threshold:
            logger.info(
                "[RESEARCH] Downgrading scope %s→%s for direction '%s' "
                "(%s merge rate %.0f%% < %.0f%%)",
                proposed_scope,
                smaller_scope,
                direction.get("title", direction.get("id", "?")),
                source,
                rate * 100,
                threshold * 100,
            )
            return smaller_scope, proposed_scope

    return proposed_scope, None


def predict_spawn_success(
    scope: str,
    direction: dict,
    project: Any | None = None,
    related_files: list[str] | None = None,
    complexity_score: float | None = None,
    *,
    _module_feedback: dict | None = None,
    _dfa: dict | None = None,
) -> float:
    """Predict P(success) for a spawned task using in-memory data only.

    ``_module_feedback`` and ``_dfa`` are optional telemetry-preferred data
    for the ``module_feedback`` and ``direction_first_attempt_feedback`` keys
    (both stripped from ``project.design_doc`` by ``strip_migrated_doc_keys``
    at write time). When provided, they take precedence over ``design_doc``
    data. Callers with session access should pre-load them via
    ``get_project_telemetry``.

    Combines four signals with weighted averaging:
    1. Direction's scope-specific merge rate from last 3 history entries
       (weight 2.0 — most relevant signal)
    2. Project-wide scope merge rate across all directions
       (weight 1.0 — proxy for project-level first_attempt_success_rate by task type)
    3. Direction's recent overall failure rate from last 3 history entries
       (weight 1.5 — captures transient regressions)
    4. Pipeline-wide scope merge rate from runtime_config["pipeline_scope_merge_rates"]
       (weight 0.5 — weakest fallback so new directions with sparse data don't
       collapse to a neutral 0.5 that bypasses the low_confidence_spawn gate)
    5. Average subtask_merge_rate from decomposition_quality in recent history
       (weight 1.5, large scope only — reflects whether plan decomposition
       actually produces mergeable subtasks for this direction; omitted when
       no decomposition data exists)
    6. Diagnosis state penalty (weight 1.0). Penalizes directions with active
       diagnosis indicators: needs_manual_intervention (0.1), diagnosis_in_progress
       (0.3), or remediation_attempts >= 2 with last_remediation_result='failed'
       (proportional). Omitted entirely when no diagnosis state is present so
       healthy directions are unaffected.

    7. Operator acceptance rate from research history (weight 1.5).
    8. Finding file-count complexity (weight 1.0): 3-4 files moderate penalty,
       5+ files strong penalty. Omitted for 1-2 files. Requires related_files.
       Cross-module span is not a separate signal here — the composite complexity
       score already includes a module-span factor, so double-counting is avoided.

    18. First-attempt rejection concentration (weight 1.0). Reads the direction's
        per-category first-attempt rejection tally from
        project.design_doc.direction_first_attempt_feedback[direction_id].
        Applies 0.3 when total>=10 and max-category share>=0.5, 0.5 when
        total>=5 and max-category share>=0.4, omitted otherwise.

    Returns a float in [0.0, 1.0]. Falls back to 0.5 when data is insufficient.
    No DB queries — uses only data already loaded during research_completion processing.
    """
    signals: list[float] = []
    weights: list[float] = []

    # Signal 1: direction scope-specific merge rate (same aggregation as _calibrate_scope).
    # SCOPE_OVERFLOW stays in the effective denominator — it signals the scope
    # assessment was wrong, which is what this signal should reflect.
    # Merge folded aggregate scope_breakdown unconditionally so evicted cycles contribute.
    history3 = direction.get("research_history", [])[-3:]
    scope_agg = _aggregate_scope_breakdown(history3)
    _merge_agg_scope_breakdown(
        scope_agg,
        (direction.get("history_aggregate") or {}).get("scope_breakdown") or {},
    )
    scope_stats = scope_agg.get(scope)
    if scope_stats:
        effective = _effective_scope_denominator(scope_stats)
        if effective >= 2:
            signals.append(scope_stats["merged"] / effective)
            weights.append(2.0)

    # Signal 2: project-level scope merge rate across all directions
    # (proxy for project-level first_attempt_success_rate for this task type, since
    # create/plan/refactor tasks map to small/medium/large scopes respectively)
    if project is not None:
        proj_entries = [
            entry
            for d in (project.directions or [])
            for entry in (d.get("research_history") or [])
        ]
        proj_scope_stats = _aggregate_scope_breakdown(proj_entries).get(scope)
        if proj_scope_stats:
            proj_effective = _effective_scope_denominator(proj_scope_stats)
            if proj_effective >= 5:
                signals.append(proj_scope_stats["merged"] / proj_effective)
                weights.append(1.0)

    # Signal 3: direction's recent code-attributable failure rate (last 3 history
    # entries). Infra failures (provision failures, rate limits, timeouts) are
    # uncorrelated with direction quality — strategic_completion.py excludes them
    # when assessing direction health, and spawn prediction should too.
    # When taxonomy_summary is present, weight failures by category: review_quality
    # is a pipeline issue not a direction problem (0.3x); scope_related indicates
    # systematic scope miscalibration (1.5x); code_defect stays at 1.0x.
    if history3:
        total_created = sum(e.get("tasks_created", 0) for e in history3)
        total_failed = sum(e.get("tasks_failed", 0) for e in history3)
        total_failed_infra = sum(e.get("tasks_failed_infra", 0) for e in history3)
        taxonomy_entries = [e.get("taxonomy_summary") or {} for e in history3]
        if any(taxonomy_entries):
            effective_failed: float = 0.0
            for ts in taxonomy_entries:
                effective_failed += (
                    ts.get("code_defect", 0) * 1.0
                    + ts.get("scope_related", 0) * 1.5
                    + ts.get("review_quality", 0) * 0.3
                )
        else:
            effective_failed = max(0, total_failed - total_failed_infra)
        if total_created >= 3:
            failure_rate = effective_failed / total_created
            signals.append(max(0.0, 1.0 - failure_rate))
            weights.append(1.5)

    # Signal 5: decomposition quality for large-scope spawns. Large-scope findings
    # are routed to the plan agent, which decomposes them into subtasks; the
    # historical merge rate of those subtasks is a direct indicator of whether
    # plan decomposition works for this direction. Omitted entirely when no
    # decomposition data exists so brand-new directions aren't penalized.
    # Incorporates folded aggregate decomposition quality so evicted cycles contribute.
    if scope == "large":
        decomp_ws, decomp_total = 0.0, 0
        for entry in history3:
            for dq in entry.get("decomposition_quality") or []:
                rate = dq.get("subtask_merge_rate")
                if isinstance(rate, (int, float)):
                    w = dq.get("subtask_count") or 1
                    decomp_ws += float(rate) * w
                    decomp_total += w
        agg_dq = (direction.get("history_aggregate") or {}).get("decomposition_quality") or {}
        agg_ws = agg_dq.get("subtask_merge_rate_weighted_sum") or 0.0
        agg_cnt = agg_dq.get("subtask_count_sum") or 0
        if isinstance(agg_ws, (int, float)) and isinstance(agg_cnt, int) and agg_cnt > 0:
            decomp_ws += float(agg_ws)
            decomp_total += agg_cnt
        if decomp_total > 0:
            signals.append(decomp_ws / decomp_total)
            weights.append(1.5)

    # Signal 4: pipeline-wide scope merge rate fallback. Weighted lowest so it
    # only dominates when other signals are absent — prevents brand-new
    # directions from passing the spawn_confidence_threshold via a neutral 0.5
    # default when the global rate for this scope is in fact poor.
    pipeline_rates = _runtime_config.get("pipeline_scope_merge_rates") or {}
    pipeline_rate = pipeline_rates.get(scope)
    if isinstance(pipeline_rate, (int, float)):
        signals.append(max(0.0, min(1.0, float(pipeline_rate))))
        weights.append(0.5)

    # Signal 6: diagnosis state penalty. needs_manual_intervention is the
    # strongest indicator (escalation already required), so it overrides
    # weaker indicators. Omitted entirely when no diagnosis state is present
    # so directions without diagnosis history are unaffected.
    diagnosis_score: float | None = None
    if direction.get("needs_manual_intervention"):
        diagnosis_score = 0.1
    elif direction.get("diagnosis_in_progress"):
        diagnosis_score = 0.3
    else:
        rem_attempts = int(direction.get("remediation_attempts") or 0)
        if rem_attempts >= 2 and direction.get("last_remediation_result") == "failed":
            # 2 failures → 0.4, 3 → 0.3, 4 → 0.2, 5+ → 0.1.
            diagnosis_score = max(0.1, 0.5 - 0.1 * (rem_attempts - 1))
    if diagnosis_score is not None:
        signals.append(diagnosis_score)
        weights.append(1.0)

    # Signal 18: remediation regression rate. A regression is a 'merged'
    # remediation entry followed by a later entry with the same fingerprint —
    # meaning the fix did not hold and the same failure re-surfaced. When
    # regressions are frequent relative to total merged remediations, the
    # direction's fixes systematically don't stick; penalize future spawns so
    # compute isn't reinvested in a direction that can't close its loops.
    rem_history = direction.get("remediation_history") or []
    if isinstance(rem_history, list):
        merged_fps: list[str] = []
        regressions = 0
        for entry in rem_history:
            if not isinstance(entry, dict):
                continue
            fp = entry.get("fingerprint")
            if not fp:
                continue
            if fp in merged_fps:
                regressions += 1
            if entry.get("result") == "merged":
                merged_fps.append(fp)
        merged_total = len(merged_fps)
        if merged_total >= 2:
            regression_rate = regressions / merged_total
            if regression_rate >= 0.5:
                signals.append(0.2)
                weights.append(1.5)
            elif regression_rate >= 0.25:
                signals.append(0.4)
                weights.append(1.0)

    # Signal 7: operator acceptance rate (merged / (merged + rejected)) from
    # research history. Directions where the operator consistently rejects PRs
    # are wasting compute regardless of code quality, so this feedback must
    # flow back into spawn gating. Requires 3+ terminal PRs (merged or rejected)
    # to avoid noise from a single early rejection. Weight 1.5 so a <0.5
    # acceptance rate meaningfully reduces predicted success but can still be
    # overridden by strong direction-level signals.
    history = direction.get("research_history", [])
    agg = direction.get("history_aggregate") or {}
    total_prs = (
        sum(e.get("prs_merged", 0) + e.get("prs_rejected", 0) for e in history)
        + (agg.get("total_prs_merged") or 0)
        + (agg.get("total_prs_rejected") or 0)
    )
    if total_prs >= 3:
        acceptance = _compute_acceptance_rate(direction)
        if acceptance is not None:
            signals.append(acceptance)
            weights.append(1.5)

    # Signal 8: finding file-count complexity. Findings touching more files are
    # harder to implement correctly, especially at small scope (6-file budget).
    # Graduated penalty; omitted for 1-2 files so simple findings aren't affected.
    # Cross-module span is intentionally NOT a separate signal here: the composite
    # complexity score (weight 1.5, added elsewhere) already includes a module-span
    # factor, so an explicit penalty would double-count the module dimension.
    if related_files:
        file_count = len(related_files)
        # Scope-aware thresholds: small has a 6-file budget so 5+ is already
        # over-budget; medium/large have a 10-file budget (10 production files)
        # so thresholds shift up by 2 to avoid over-penalizing findings at
        # their natural file counts.
        if scope == "small":
            strong_threshold, moderate_threshold = 5, 3
        else:
            strong_threshold, moderate_threshold = 7, 5
        if file_count >= strong_threshold:
            signals.append(0.3)
            weights.append(1.0)
        elif file_count >= moderate_threshold:
            signals.append(0.6)
            weights.append(1.0)

    # Signal 10: analysis_overflow dominance at medium scope. When 50%+ of a
    # direction's medium-scope failures come from the agent's analysis_overflow
    # gate (could not produce a valid implementation plan within the medium
    # budget), the research agent's scope estimate is systematically too
    # optimistic for this direction. Stronger signal than post-implementation
    # scope_overflow, so apply a steep 0.2 penalty with weight 1.5.
    if scope == "medium" and scope_stats:
        failed_total = scope_stats.get("failed", 0)
        analysis_overflow = scope_stats.get("failed_analysis_overflow", 0)
        if failed_total >= 2 and analysis_overflow / failed_total >= 0.5:
            signals.append(0.2)
            weights.append(1.5)

    # Signal 11: scope_trim near-miss dominance at medium scope. When medium
    # tasks consistently need post-implementation scope trimming to fit the
    # budget, the direction's medium work is borderline — apply a moderate
    # penalty so it routes to a larger scope more aggressively.
    if scope == "medium" and scope_stats:
        created = scope_stats.get("created", 0)
        trim_count = scope_stats.get("scope_trim_count", 0)
        if created >= 2 and trim_count / created >= 0.5:
            signals.append(0.4)
            weights.append(1.0)

    # Signal 13: historical expansion ratio for the proposed scope. When the
    # avg_expansion_ratio in recent scope_breakdown entries exceeds 1.3x, the
    # agent has been touching more files than related_files declared — a direct
    # SCOPE_OVERFLOW precursor. Graduated penalty; requires 2+ data points to
    # avoid reacting to a single noisy cycle.
    exp_ratios: list[float] = []
    for entry in history3:
        _stats = (entry.get("scope_breakdown") or {}).get(scope) or {}
        _r = _stats.get("avg_expansion_ratio")
        if isinstance(_r, (int, float)):
            exp_ratios.append(float(_r))
    if len(exp_ratios) >= 2:
        avg_exp = sum(exp_ratios) / len(exp_ratios)
        if avg_exp >= 1.5:
            signals.append(0.25)
            weights.append(1.0)
        elif avg_exp >= 1.3:
            signals.append(0.45)
            weights.append(1.0)

    # Signal 14: failure-stage composition. When merge-stage failures dominate
    # the non-infra failures, the scope estimate is likely too small — the
    # implementation finishes but the resulting PR can't be merged cleanly
    # (conflicts, behind base, blocking checks), a scope-miscalibration
    # signature. Review-stage dominance is weaker: it indicates the review
    # pipeline surfaced real quality issues, less correlated with scope.
    # Infra stages (provision, clone) are excluded from the denominator.
    stage_totals: dict[str, int] = {}
    for _s, _stats in scope_agg.items():
        if _s != scope:
            continue
        for _name, _cnt in (_stats.get("failure_stages") or {}).items():
            stage_totals[_name] = stage_totals.get(_name, 0) + int(_cnt or 0)
    non_infra_total = sum(
        c for n, c in stage_totals.items() if n not in ("provision", "clone")
    )
    if non_infra_total >= 3:
        merge_share = stage_totals.get("merge", 0) / non_infra_total
        review_share = stage_totals.get("review", 0) / non_infra_total
        if merge_share >= 0.5:
            signals.append(0.25)
            weights.append(1.5)
        elif review_share >= 0.5:
            signals.append(0.5)
            weights.append(1.0)

    # Signal 12: composite complexity score from scope_complexity. Combines
    # file count, historical expansion, cochange density, and cross-module
    # span into a single P(merge) estimate in [0,1]. Low scores at medium
    # scope indicate the finding is likely too complex for its budget, so
    # route it through the low-confidence gate for large-scope escalation.
    if isinstance(complexity_score, (int, float)):
        signals.append(max(0.0, min(1.0, float(complexity_score))))
        weights.append(1.5)

    # Signal 15: module review-feedback history. When a finding's related_files
    # overlap with modules that have accumulated review-issue counts in the
    # project's module_feedback profiles, the review pipeline has historically
    # caught many issues there — penalize the spawn proportionally. Uses the
    # same decay-adjusted `total` counts surfaced as module_feedback_warnings
    # via the claim endpoint (decay is applied at write time in
    # _update_module_feedback_profiles). Requires project and related_files.
    if project is not None and related_files:
        design_doc = getattr(project, "design_doc", None) or {}
        module_feedback = _module_feedback or design_doc.get("module_feedback") or {}
        if module_feedback:
            aggregate_total = 0
            for f in related_files:
                profile = module_feedback.get(f)
                if not isinstance(profile, dict):
                    continue
                t = profile.get("total", 0)
                if isinstance(t, int) and t >= 3:
                    aggregate_total += t
            if aggregate_total >= 15:
                signals.append(0.2)
                weights.append(1.5)
            elif aggregate_total >= 8:
                signals.append(0.4)
                weights.append(1.5)
            elif aggregate_total >= 3:
                signals.append(0.6)
                weights.append(1.5)

    # Signal 14: rolling median review-iteration count. Directions whose tasks
    # consistently need many review cycles before merging are producing
    # lower-quality findings that consume disproportionate compute. Graduated
    # penalty: no effect at 1-2 iterations (normal), moderate at 3-4, strong at 5+.
    median_iters = _compute_rolling_median_review_iterations(direction)
    if median_iters is not None:
        if median_iters >= 5:
            signals.append(0.2)
            weights.append(1.0)
        elif median_iters >= 3:
            signals.append(0.5)
            weights.append(1.0)

    # Signal 16: direction quality score (taxonomy health composite). Already
    # used in autonomous_research.py for direction selection; feeding it back
    # into spawn prediction makes spawn gating consistent with selection. Only
    # emitted when the last 3 cycles carry at least one taxonomy_summary so
    # directions without taxonomy data aren't pulled toward the 0.5 neutral
    # default that _compute_direction_quality_score returns.
    if any((e.get("taxonomy_summary") or {}) for e in history3):
        signals.append(_compute_direction_quality_score(direction))
        weights.append(1.0)

    # Signal 19: cross-module merge boost. When the direction has a proven
    # track record of merging cross-module work at the proposed scope, and the
    # current finding also spans 2+ top-level directories, give a mild
    # confidence boost to counter the file-count / expansion / module-span
    # penalties that otherwise dominate multi-directory findings. Requires
    # merged >= 2 at this scope to avoid noisy early-stage boosts.
    if related_files and scope_stats:
        merged_at_scope = scope_stats.get("merged", 0)
        cross_merged = scope_stats.get("merged_cross_module", 0)
        if merged_at_scope >= 2:
            top_dirs = {f.split("/", 1)[0] for f in related_files if f}
            if len(top_dirs) >= 2:
                cross_rate = cross_merged / merged_at_scope
                if cross_rate >= 0.5:
                    signals.append(0.8)
                    weights.append(1.0)

    # Signal 17: momentum factor. Hot-streak directions (last 2 cycles both
    # productive) get a confidence boost; stalled directions (last 3 cycles
    # all empty) get a penalty. Neutral momentum is omitted so it doesn't
    # pull the weighted average toward 0.5.
    momentum = _compute_momentum_factor(direction)
    if momentum >= 2.0:
        signals.append(0.8)
        weights.append(1.0)
    elif momentum <= 0.5:
        signals.append(0.2)
        weights.append(1.0)

    # Signal 18: first-attempt rejection concentration. When a direction has
    # accumulated many first-attempt rejections concentrated in a single review
    # category, it suggests a systematic prompt-quality or scope-calibration
    # issue rather than random implementation flaws. Reads the per-direction
    # tally written by playbook_learning._accumulate_direction_first_attempt.
    direction_id = direction.get("id")
    if project is not None and direction_id:
        design_doc = getattr(project, "design_doc", None) or {}
        dfa = _dfa or design_doc.get("direction_first_attempt_feedback") or {}
        entry = dfa.get(str(direction_id)) or {}
        cat_counts = [
            v
            for k, v in entry.items()
            if k not in ("total", "last_updated") and isinstance(v, (int, float))
        ]
        total = sum(cat_counts)
        if total >= 5 and cat_counts:
            concentration = max(cat_counts) / total
            if total >= 10 and concentration >= 0.5:
                signals.append(0.3)
                weights.append(1.0)
            elif concentration >= 0.4:
                signals.append(0.5)
                weights.append(1.0)

    if not signals:
        return 0.5  # insufficient data — neutral

    return min(
        1.0, max(0.0, sum(s * w for s, w in zip(signals, weights)) / sum(weights))
    )


def _compute_scope_budget_utilization(
    changed_files: list[str] | None, scope: str
) -> dict | None:
    """Summarize how close a PR's file count is to its scope budget.

    Returns None when no signal is available (no files known, or unknown scope).
    ``percentage`` is a whole number (0-100+); callers surface guidance when it
    crosses ``SCOPE_PRESSURE_WARN_THRESHOLD``.
    """
    from app.review_pipeline import _is_test_file  # noqa: PLC0415

    if not changed_files:
        return None
    budget_info = SCOPE_FILE_BUDGETS.get(scope) or {}
    budget = budget_info.get("production_files_max") or budget_info.get(
        "total_files_max"
    )
    if not budget:
        return None
    non_test_count = sum(1 for f in changed_files if not _is_test_file(f))
    return {
        "scope": scope,
        "file_count": len(changed_files),
        "non_test_file_count": non_test_count,
        "budget": budget,
        "percentage": round(non_test_count / budget * 100),
    }


def _compute_scope_overflow_warning(
    changed_files: list[str] | None, scope: str
) -> dict | None:
    """Return machine-readable scope-overflow warning details, if triggered."""
    from app.review_pipeline import _is_test_file  # noqa: PLC0415

    if not changed_files:
        return None
    budget_info = SCOPE_FILE_BUDGETS.get(scope) or {}
    threshold = budget_info.get("production_files_max") or budget_info.get(
        "total_files_max"
    )
    if not threshold:
        return None
    non_test_count = sum(1 for f in changed_files if not _is_test_file(f))
    if non_test_count <= threshold:
        return None
    return {
        "scope": scope,
        "threshold": threshold,
        "non_test_file_count": non_test_count,
        "file_count": len(changed_files),
        "changed_files": changed_files[:50],
    }


# Marker convention for the ``requires_approval`` hold reason. When a task is
# held out of dispatch (``requires_approval=True``) because of a deliberate
# SPEND/BUDGET decision — as opposed to a review-pipeline ESCALATION — the set
# site stamps ``task.context["approval_hold_reason"] = SPEND_BUDGET_APPROVAL_HOLD_REASON``.
# The recovery sweep keys off this marker to give spend/budget holds a separate,
# longer, operator-configurable window that resolves to a CLEAN NON-FAILURE
# CANCELLED terminal instead of the review-escalation 2h→FAILED disposition (a
# cost decision an operator simply has not reached is not a pipeline failure).
#
# ABSENCE of the marker MUST preserve today's review-escalation behavior exactly
# — do NOT stamp it at any review-escalation set site. Currently set in
# research_completion.py for the large-scope-budget-mismatch path below; the
# future compute_experiment spend-threshold hold MUST reuse this same constant
# so both paths share one recovery-sweep branch.
SPEND_BUDGET_APPROVAL_HOLD_REASON = "spend_budget"


def check_scope_budget_mismatch(
    scope: str,
    related_files: list[str] | None,
) -> tuple[str, str | None, str | None, bool]:
    """Pre-spawn scope-budget gate: detect obvious file-count/scope mismatches.

    Checks the declared scope against the number of related_files before
    dispatching a task, so the mismatch is caught at research time rather
    than after a task overflows during implementation.

    Returns (new_scope, scope_calibrated_from, scope_budget_warning, requires_approval).
    - scope_calibrated_from is set when the scope is upgraded.
    - scope_budget_warning is a human-readable string when a mismatch is detected.
    - requires_approval is True only for large-scope findings with too many files.
      When the caller sets ``requires_approval`` from this gate it MUST also stamp
      ``approval_hold_reason=SPEND_BUDGET_APPROVAL_HOLD_REASON`` on the task
      context so the recovery sweep treats the hold as a deliberate spend/budget
      decision (clean non-failure timeout) rather than a review escalation.
    """
    n = len(related_files) if related_files else 0
    _large_limit = (SCOPE_FILE_BUDGETS.get("large") or {}).get("total_files_max") or 20

    if scope == "small" and n > 2:
        warning = (
            f"scope_budget_mismatch: small scope with {n} related_files exceeds "
            f"small-scope practical limit (2); auto-upgraded to medium."
        )
        logger.info(
            "[RESEARCH] pre-spawn budget gate: small→medium (%d related_files > 2)", n
        )
        return "medium", "small", warning, False

    if scope == "medium" and n > MEDIUM_ANALYSIS_PLAN_FILE_LIMIT:
        warning = (
            f"scope_budget_mismatch: medium scope with {n} related_files exceeds "
            f"MEDIUM_ANALYSIS_PLAN_FILE_LIMIT ({MEDIUM_ANALYSIS_PLAN_FILE_LIMIT}); auto-upgraded to large."
        )
        logger.info(
            "[RESEARCH] pre-spawn budget gate: medium→large (%d related_files > %d)",
            n,
            MEDIUM_ANALYSIS_PLAN_FILE_LIMIT,
        )
        return "large", "medium", warning, False

    if scope == "large" and n > _large_limit:
        warning = (
            f"scope_budget_mismatch: large scope with {n} related_files exceeds "
            f"large-scope budget ({_large_limit} files); requires operator approval."
        )
        logger.info(
            "[RESEARCH] pre-spawn budget gate: large scope with %d related_files > %d "
            "— marking requires_approval=True",
            n,
            _large_limit,
        )
        return scope, None, warning, True

    return scope, None, None, False

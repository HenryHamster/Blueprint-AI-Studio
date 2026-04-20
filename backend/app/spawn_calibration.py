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

from app.direction_metrics import _compute_acceptance_rate, _compute_rolling_median_review_iterations
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

# Maximum implementation tasks to spawn when pipeline health is degraded.
# Remaining findings are deferred to task.context['deferred_findings'] for
# the next research cycle.
_DEGRADED_SPAWN_CAP = 2


def _assess_spawn_health() -> tuple[bool, str | None]:
    """Check pipeline health signals for spawn-time gating.

    Returns (is_degraded, reason). When degraded, _handle_research_completion
    caps new implementation task creation at _DEGRADED_SPAWN_CAP.

    Checks (in order):
    1. Zone availability: all zones in cooldown → degraded
    2. Account availability: no accounts available for create model → degraded
    3. Rolling failure rate: >50% of recent task_results are failures → degraded
    """
    from app.compute import any_zone_available  # noqa: PLC0415
    from app.max_plan_pool import _account_by_id, _account_available_for_model  # noqa: PLC0415
    from app.shared_state import deploy_monitoring  # noqa: PLC0415

    if not any_zone_available():
        return True, "all zones in cooldown"

    if not os.getenv("ANTHROPIC_API_KEY"):
        create_model = _runtime_config.get("task_type_models", {}).get("create")
        all_accounts = list(_account_by_id.values())
        if all_accounts and not any(_account_available_for_model(a, create_model) for a in all_accounts):
            return True, "no accounts available"

    task_results = list(deploy_monitoring.get("task_results", []))
    # Apply a 60-minute window to avoid false positives from historical failures that
    # are no longer representative of current pipeline health. Old failures (hours ago)
    # can dominate the ring buffer even when the system has fully recovered.
    _sixty_min_ago = (_utcnow() - timedelta(minutes=60)).isoformat()
    recent_results = [r for r in task_results if r[0] >= _sixty_min_ago]
    if len(recent_results) >= 10:
        failures = sum(1 for entry in recent_results if entry[1] == "failure")
        if failures / len(recent_results) > 0.5:
            return True, f"high failure rate ({failures}/{len(recent_results)} recent tasks failed)"

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
        medium_rate * 100, threshold * 100, small_rate * 100,
    )
    return "large", "medium"


def _poor_decomp_history(direction: dict, label: str = "") -> bool:
    """Suppression signal: weighted avg subtask_merge_rate < 0.3 across >= 3
    subtasks in last 3 cycles. Logs when True.
    """
    ws, total = 0.0, 0
    for entry in direction.get("research_history", [])[-3:]:
        for dq in (entry.get("decomposition_quality") or []):
            r = dq.get("subtask_merge_rate")
            if isinstance(r, (int, float)):
                w = dq.get("subtask_count") or 1
                ws, total = ws + float(r) * w, total + w
    if total < 3 or ws / total >= 0.3:
        return False
    logger.info("[RESEARCH] Suppressing medium→large upgrade%s (avg %.0f%% < 30%% over %d subtasks)",
                f" for '{label}'" if label else "", (ws / total) * 100, total)
    return True


def _aggregate_scope_breakdown(history_entries: list[dict]) -> dict[str, dict]:
    """Sum scope_breakdown counters across a list of research_history entries."""
    agg: dict[str, dict] = {}
    for entry in history_entries:
        for scope, stats in (entry.get("scope_breakdown") or {}).items():
            bucket = agg.setdefault(scope, {
                "created": 0, "merged": 0, "merged_cross_module": 0,
                "failed": 0,
                "failed_infra": 0, "failed_scope_overflow": 0,
                "failed_analysis_overflow": 0,
                "failure_stages": {},
            })
            for key in (
                "created", "merged", "merged_cross_module",
                "failed", "failed_infra",
                "failed_scope_overflow", "failed_analysis_overflow",
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
    infra_excl_overflow = stats.get("failed_infra", 0) - stats.get("failed_scope_overflow", 0)
    return stats.get("merged", 0) + stats.get("failed", 0) - infra_excl_overflow


def _calibrate_scope(
    proposed_scope: str,
    direction: dict,
    project: Any | None = None,
) -> tuple[str, str | None]:
    """Downgrade scope if historical merge rate at that scope is below threshold.

    Returns (final_scope, calibrated_from) where calibrated_from is the original
    scope if a downgrade occurred, else None.

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

    # Aggregate direction-specific scope_breakdown across last 3 history entries
    aggregated = _aggregate_scope_breakdown(direction.get("research_history", [])[-3:])
    direction_data_points = sum(v["created"] for v in aggregated.values())

    if direction_data_points >= 3:
        agg = aggregated
        threshold = 0.4
        source = "direction"
    elif project is not None:
        # Fallback: aggregate scope_breakdown across all directions in the project
        proj_entries = [
            entry for d in (project.directions or [])
            for entry in (d.get("research_history") or [])
        ]
        proj_agg = _aggregate_scope_breakdown(proj_entries)
        if sum(v["created"] for v in proj_agg.values()) < 3:
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
                proposed_scope, larger,
                direction.get("title", direction.get("id", "?")),
                source, overflow_rate * 100,
                overflow, proposed_stats["created"],
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
                    proposed_scope, larger,
                    direction.get("title", direction.get("id", "?")),
                    source, analysis_rate * 100,
                    analysis_overflow, proposed_stats["created"],
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
            if _poor_decomp_history(direction, direction.get("title", direction.get("id", "?"))):
                pass  # fall through to downgrade loop below
            else:
                larger = _SCOPE_ORDER[idx + 1]
                logger.info(
                    "[RESEARCH] Upgrading scope %s→%s(decompose) for direction '%s' "
                    "(%s medium rate %.0f%% < %.0f%%, small rate %.0f%% healthy)",
                    proposed_scope, larger,
                    direction.get("title", direction.get("id", "?")),
                    source, rate * 100, threshold * 100, small_rate * 100,
                )
                return larger, proposed_scope

    # Downgrade to next smaller scope with acceptable rate
    for smaller_scope in reversed(_SCOPE_ORDER[:idx]):
        smaller_rate = _merge_rate(smaller_scope)
        if smaller_rate is None or smaller_rate >= threshold:
            logger.info(
                "[RESEARCH] Downgrading scope %s→%s for direction '%s' "
                "(%s merge rate %.0f%% < %.0f%%)",
                proposed_scope, smaller_scope,
                direction.get("title", direction.get("id", "?")),
                source, rate * 100, threshold * 100,
            )
            return smaller_scope, proposed_scope

    return proposed_scope, None


def predict_spawn_success(
    scope: str,
    direction: dict,
    project: Any | None = None,
    related_files: list[str] | None = None,
    complexity_score: float | None = None,
) -> float:
    """Predict P(success) for a spawned task using in-memory data only.

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

    Returns a float in [0.0, 1.0]. Falls back to 0.5 when data is insufficient.
    No DB queries — uses only data already loaded during research_completion processing.
    """
    signals: list[float] = []
    weights: list[float] = []

    # Signal 1: direction scope-specific merge rate (same aggregation as _calibrate_scope).
    # SCOPE_OVERFLOW stays in the effective denominator — it signals the scope
    # assessment was wrong, which is what this signal should reflect.
    history3 = direction.get("research_history", [])[-3:]
    scope_agg = _aggregate_scope_breakdown(history3)
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
            entry for d in (project.directions or [])
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
    if scope == "large":
        decomp_rates: list[float] = []
        for entry in history3:
            for dq in (entry.get("decomposition_quality") or []):
                rate = dq.get("subtask_merge_rate")
                if isinstance(rate, (int, float)):
                    decomp_rates.append(float(rate))
        if decomp_rates:
            signals.append(sum(decomp_rates) / len(decomp_rates))
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

    if not signals:
        return 0.5  # insufficient data — neutral

    return min(1.0, max(0.0, sum(s * w for s, w in zip(signals, weights)) / sum(weights)))

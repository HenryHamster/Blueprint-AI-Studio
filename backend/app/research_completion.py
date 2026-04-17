"""Research completion handler and supporting functions.

Extracted from app.task_completion to keep that module focused on plan/security/perf
completion handlers. Contains the research findings parser, context builder,
and the large _handle_research_completion handler.
"""
import hashlib
import json
import logging
import os
import re
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm.attributes import flag_modified
from sqlmodel import select

from app.models import (
    Alert,
    AlertSeverity,
    Artifact,
    ArtifactType,
    MessageType,
    Project,
    ProjectMessage,
    Task,
    TaskHistory,
    TaskStatus,
)
from app.broadcast import broadcast_to_project
from app.cochange_decomposition import (
    auto_decompose_findings_by_cochange,
    cluster_files_by_cochange as _cluster_files_by_cochange,  # noqa: F401 — re-exported for tests
)
from app.payload_schemas import validate_internal_payload
from app.scope_complexity import _composite_complexity_score
from app.shared_state import runtime_config as _runtime_config, DEFAULT_TASK_SCOPE
from app.task_completion import (
    _extract_json_array,
    _ensure_branch,
    _validate_plan_dependencies,
    _log_file_overlaps,
    TASK_PRIORITY_MAP,
)
from app.time_utils import _utcnow

logger = logging.getLogger(__name__)

_SCOPE_ORDER = ["small", "medium", "large"]

# Hard file-count budgets enforced by implementing agents per scope.
# Surfaced to research agents via pipeline_health so they can align finding
# scope with these limits. Values mirror SCOPE_OVERFLOW_THRESHOLDS and
# MEDIUM_ANALYSIS_PLAN_FILE_LIMIT in agent/src/constants.ts — keep in sync.
SCOPE_FILE_BUDGETS: dict[str, dict[str, int]] = {
    "small": {"total_files_max": 6},
    "medium": {"production_files_max": 7, "total_files_max": 10},
    "large": {"total_files_max": 20},
}

# Maximum implementation tasks to spawn when pipeline health is degraded.
# Remaining findings are deferred to task.context['deferred_findings'] for
# the next research cycle.
_DEGRADED_SPAWN_CAP = 2

_HISTORY_AGG_DEFAULTS = {
    "total_cycles": 0,
    "total_tasks_created": 0,
    "total_prs_merged": 0,
    "total_prs_rejected": 0,
    "total_tasks_failed": 0,
    "total_tasks_cancelled": 0,
    "first_cycle_at": None,
}

_DECOMP_AGG_DEFAULTS = {
    "total_decompositions": 0,
    "subtask_merge_rate_weighted_sum": 0.0,
    "subtask_count_sum": 0,
    "cross_module_count_sum": 0,
}


def _fold_evicted_into_aggregate(prev_agg: dict | None, evicted: list[dict]) -> dict:
    """Fold evicted research_history entries into a running history_aggregate.

    Preserves basic counters plus per-scope breakdown and decomposition-quality
    signals that _calibrate_scope and predict_spawn_success read. Without this,
    directions older than 10 cycles silently lost all scope/decomposition data.
    """
    agg = prev_agg or {}
    for k, v in _HISTORY_AGG_DEFAULTS.items():
        agg.setdefault(k, v)
    sb = agg.setdefault("scope_breakdown", {})
    dq = agg.setdefault("decomposition_quality", dict(_DECOMP_AGG_DEFAULTS))
    for ev in evicted:
        agg["total_cycles"] += 1
        agg["total_tasks_created"] += ev.get("tasks_created", 0)
        agg["total_prs_merged"] += ev.get("prs_merged", 0)
        agg["total_prs_rejected"] += ev.get("prs_rejected", 0)
        agg["total_tasks_failed"] += ev.get("tasks_failed", 0)
        agg["total_tasks_cancelled"] += ev.get("tasks_cancelled", 0)
        if not agg["first_cycle_at"] and ev.get("timestamp"):
            agg["first_cycle_at"] = ev["timestamp"]
        for scope, stats in (ev.get("scope_breakdown") or {}).items():
            bucket = sb.setdefault(scope, {
                "created": 0, "merged": 0, "failed": 0, "failed_infra": 0,
                "failed_scope_overflow": 0,
            })
            bucket["created"] += stats.get("created", 0)
            bucket["merged"] += stats.get("merged", 0)
            bucket["failed"] += stats.get("failed", 0)
            bucket["failed_infra"] += stats.get("failed_infra", 0)
            bucket["failed_scope_overflow"] = bucket.get("failed_scope_overflow", 0) + stats.get("failed_scope_overflow", 0)
        for qd in (ev.get("decomposition_quality") or []):
            dq["total_decompositions"] += 1
            count = qd.get("subtask_count") or 0
            dq["subtask_count_sum"] += count
            dq["cross_module_count_sum"] += qd.get("cross_module_count") or 0
            rate = qd.get("subtask_merge_rate")
            if isinstance(rate, (int, float)) and count > 0:
                dq["subtask_merge_rate_weighted_sum"] += float(rate) * count
    return agg


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
                "created": 0, "merged": 0, "failed_infra": 0, "failed_scope_overflow": 0,
            })
            for key in ("created", "merged", "failed_infra", "failed_scope_overflow"):
                bucket[key] += stats.get(key, 0)
    return agg


def _effective_scope_denominator(stats: dict) -> int:
    """Denominator for scope merge rate: created minus true-infra failures.

    SCOPE_OVERFLOW is tracked under failed_infra for direction-health purposes
    but excluded here — it signals the scope assessment was wrong (scope-quality
    signal), so it belongs in the denominator, not subtracted from it.
    """
    infra_excl_overflow = stats.get("failed_infra", 0) - stats.get("failed_scope_overflow", 0)
    return stats["created"] - infra_excl_overflow


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
            # would just generate more failing decompositions. Leave scope at
            # medium and let predict_spawn_success gate it via confidence.
            if _poor_decomp_history(direction, direction.get("title", direction.get("id", "?"))):
                return proposed_scope, None
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
    if history3:
        total_created = sum(e.get("tasks_created", 0) for e in history3)
        total_failed = sum(e.get("tasks_failed", 0) for e in history3)
        total_failed_infra = sum(e.get("tasks_failed_infra", 0) for e in history3)
        code_failed = max(0, total_failed - total_failed_infra)
        if total_created >= 3:
            failure_rate = code_failed / total_created
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

    if not signals:
        return 0.5  # insufficient data — neutral

    return min(1.0, max(0.0, sum(s * w for s, w in zip(signals, weights)) / sum(weights)))


def _compute_momentum_factor(d: dict) -> float:
    """Return a momentum multiplier based on recent research history productivity."""
    history = d.get("research_history", [])
    if len(history) >= 2 and all(e.get("tasks_created", 0) > 0 for e in history[-2:]):
        return 2.0  # hot streak: last 2 cycles both productive
    if len(history) >= 3 and all(e.get("tasks_created", 0) == 0 for e in history[-3:]):
        return 0.5  # stalled: last 3 cycles all empty
    return 1.0  # neutral


def _compute_acceptance_rate(d: dict) -> float | None:
    """Return operator acceptance rate (merged / (merged + rejected)) with recency weighting.

    Weights the last 3 cycles 3x relative to the lifetime average, matching
    _compute_merge_rate's recency scheme. Returns None when no PRs exist (merged
    or rejected) so callers can distinguish "unknown" from a low rate.
    """
    history = d.get("research_history", [])
    agg = d.get("history_aggregate", {})
    total_merged = sum(e.get("prs_merged", 0) for e in history) + (agg.get("total_prs_merged") or 0)
    total_rejected = sum(e.get("prs_rejected", 0) for e in history) + (agg.get("total_prs_rejected") or 0)
    total_prs = total_merged + total_rejected
    if total_prs == 0:
        return None  # no data yet
    lifetime_rate = total_merged / total_prs
    # Weight last 3 cycles 3x for responsiveness to recent changes
    recent = history[-3:]
    if not recent:
        return lifetime_rate
    recent_merged = sum(e.get("prs_merged", 0) for e in recent)
    recent_rejected = sum(e.get("prs_rejected", 0) for e in recent)
    recent_prs = recent_merged + recent_rejected
    if recent_prs == 0:
        return lifetime_rate
    recent_rate = recent_merged / recent_prs
    return (recent_rate * 3 + lifetime_rate) / 4


def _compute_merge_rate(d: dict) -> float:
    """Return net productivity score for a direction, with recency weighting.

    Accounts for both merged PRs (positive signal) and rejected PRs (negative
    signal from operator close-without-merge). Weights the last 3 cycles 3x
    relative to the lifetime average for responsiveness to recent changes.
    Returns 0.5 (neutral) if never researched.
    """
    history = d.get("research_history", [])
    agg = d.get("history_aggregate", {})
    total_cycles = len(history) + (agg.get("total_cycles") or 0)
    total_merged = sum(e.get("prs_merged", 0) for e in history) + (agg.get("total_prs_merged") or 0)
    total_rejected = sum(e.get("prs_rejected", 0) for e in history) + (agg.get("total_prs_rejected") or 0)
    if total_cycles == 0:
        return 0.5  # unknown, neutral
    # Net productivity: merge contributes +1, rejection contributes -0.5
    # (rejection is penalized but not as harshly as a merge is rewarded)
    lifetime_rate = (total_merged - 0.5 * total_rejected) / total_cycles
    # Weight last 3 cycles 3x for responsiveness to recent changes
    recent = history[-3:]
    if not recent:
        return lifetime_rate
    recent_merged = sum(e.get("prs_merged", 0) for e in recent)
    recent_rejected = sum(e.get("prs_rejected", 0) for e in recent)
    recent_rate = (recent_merged - 0.5 * recent_rejected) / len(recent)
    return (recent_rate * 3 + lifetime_rate) / 4


def _update_cycle_history(project: Any, cycle_id: str, updates: Dict[str, Any]) -> None:
    """Find a cycle entry by cycle_id in project.vision and apply updates in-place."""
    vision = dict(project.vision or {})
    cycle_history = list(vision.get("_strategic_cycle_history") or [])
    for entry in cycle_history:
        if entry.get("cycle_id") == cycle_id:
            entry.update(updates)
            break
    vision["_strategic_cycle_history"] = cycle_history
    project.vision = vision
    flag_modified(project, "vision")


def _compute_saturation(history: list) -> bool:
    """Return True if last 3 cycles all produced 0 or 1 tasks."""
    if len(history) < 3:
        return False
    recent = history[-3:]
    return all(entry.get("tasks_created", 0) <= 1 for entry in recent)


def _detect_dedup_deadlock(history: list, window: int = 3) -> tuple[bool, int]:
    """Detect dedup deadlock: last `window` entries all have tasks_created==0 with dedup blocks.

    A direction is considered deadlocked when research cycles keep producing findings that are
    immediately rejected by hash or word-overlap deduplication, wasting VM compute. Detection
    requires that the last `window` entries all have tasks_created==0 AND at least 2 of those
    entries show non-zero hash_dedup or word_overlap_dedup counts.

    Returns (is_deadlocked, consecutive_empty_count).
    """
    if len(history) < window:
        return False, 0
    recent = history[-window:]
    if not all(e.get("tasks_created", 0) == 0 for e in recent):
        return False, 0
    # At least 2 of the last `window` entries must show dedup activity
    dedup_active_count = sum(
        1 for e in recent
        if (e.get("filter_breakdown", {}).get("hash_dedup", 0) or 0)
        + (e.get("filter_breakdown", {}).get("word_overlap_dedup", 0) or 0) > 0
    )
    if dedup_active_count < 2:
        return False, 0
    # Count consecutive empty cycles from the end
    consecutive = 0
    for e in reversed(history):
        if e.get("tasks_created", 0) == 0:
            consecutive += 1
        else:
            break
    return True, consecutive


def _parse_research_context(text: str) -> dict:
    """Parse a research_context string into its 3 sections and optional eviction summary."""
    result: dict = {"established": [], "failed": [], "open_questions": [], "eviction_summary": None}
    if not text:
        return result
    section_map = {
        "## Established Findings": "established",
        "## Failed Approaches": "failed",
        "## Open Questions": "open_questions",
    }
    current_key: Optional[str] = None
    in_eviction = False
    eviction_lines: list = []
    for line in text.splitlines():
        stripped = line.strip()
        if stripped == "## Eviction Summary":
            in_eviction = True
            current_key = None
        elif stripped in section_map:
            in_eviction = False
            current_key = section_map[stripped]
        elif in_eviction and stripped:
            eviction_lines.append(stripped)
        elif current_key and stripped.startswith("- "):
            result[current_key].append(stripped[2:])
    if eviction_lines:
        result["eviction_summary"] = "\n".join(eviction_lines)
    return result


def _parse_eviction_counts(summary_text: Optional[str]) -> dict:
    """Parse counts and failed titles from an eviction summary body."""
    result: dict = {"established": 0, "failed": 0, "open_questions": 0, "failed_titles": []}
    if not summary_text:
        return result
    for line in summary_text.splitlines():
        line = line.strip()
        # Check "Failed:" prefix before "evicted" keyword — titles may themselves contain "evicted"
        if line.startswith("Failed:"):
            titles_str = line[len("Failed:"):].strip()
            result["failed_titles"] = [t.strip() for t in titles_str.split(";") if t.strip()]
        elif "evicted" in line:
            m = re.search(r'(\d+) established', line)
            if m:
                result["established"] = int(m.group(1))
            m = re.search(r'(\d+) failed', line)
            if m:
                result["failed"] = int(m.group(1))
            m = re.search(r'(\d+) open question', line)
            if m:
                result["open_questions"] = int(m.group(1))
    return result


def _format_evicted_failed(entry: str, max_len: int = 60) -> str:
    """Format an evicted failed-approach entry preserving the category.

    Failed entries follow '{title} ({category}) [at=... stage=...] — {reason}'.
    Long titles push the category past a naive prefix truncation, so extract
    the category and ensure it is preserved within max_len.
    """
    m = re.match(r"^(.*?)\s*\(([^)]+)\)(?:\s*\[|\s*—|\s*$)", entry)
    if not m:
        return entry[:max_len]
    title = m.group(1).strip()
    category = m.group(2).strip()
    suffix = f" ({category})"
    budget = max_len - len(suffix)
    if budget <= 0:
        return entry[:max_len]
    if len(title) <= budget:
        return f"{title}{suffix}"
    ellipsis = "..."
    truncated = title[: max(0, budget - len(ellipsis))] + ellipsis
    return f"{truncated}{suffix}"


def _format_eviction_body(established: int, failed: int, open_questions: int, failed_titles: list) -> str:
    """Format the eviction summary body content (without the ## header line)."""
    count_parts = []
    if established > 0:
        count_parts.append(f"{established} established")
    if failed > 0:
        count_parts.append(f"{failed} failed")
    if open_questions > 0:
        plural = "s" if open_questions != 1 else ""
        count_parts.append(f"{open_questions} open question{plural}")
    lines = []
    if count_parts:
        lines.append(", ".join(count_parts) + " evicted")
    if failed_titles:
        lines.append("Failed: " + "; ".join(failed_titles))
    return "\n".join(lines)


def _build_research_context(
    prior_context: Optional[str],
    finding_titles: list,
    key_finding_descriptions: list,
    cycle_insights: list,
    history_entry: dict,
    finding_scopes: Optional[list] = None,
) -> str:
    """Build a compounding research_context document deterministically (no LLM)."""
    parsed = _parse_research_context(prior_context or "")

    # Parse prior eviction summary for accumulation
    prior_eviction = _parse_eviction_counts(parsed["eviction_summary"])

    # Append new findings to Established Findings as (proposed)
    for i, title in enumerate(finding_titles):
        desc = key_finding_descriptions[i] if i < len(key_finding_descriptions) else ""
        scope = (finding_scopes[i] if finding_scopes and i < len(finding_scopes) else None) or "small"
        scope_tag = f" [{scope}]"
        if desc:
            first_sentence = desc.split(".")[0].strip()
            entry_text = f"{title}{scope_tag} (proposed). {first_sentence}" if first_sentence else f"{title}{scope_tag} (proposed)"
        else:
            entry_text = f"{title}{scope_tag} (proposed)"
        parsed["established"].append(entry_text)

    # Add cycle_insights to Open Questions (deduplicate)
    existing_oq = set(parsed["open_questions"])
    for insight in cycle_insights:
        if insight and insight not in existing_oq:
            parsed["open_questions"].append(insight)
            existing_oq.add(insight)

    def _format_doc(sections: dict, eviction_body: Optional[str] = None) -> str:
        parts: list = []
        if eviction_body:
            parts.append("## Eviction Summary")
            parts.append(eviction_body)
            parts.append("")
        for header, key in [
            ("## Established Findings", "established"),
            ("## Failed Approaches", "failed"),
            ("## Open Questions", "open_questions"),
        ]:
            parts.append(header)
            for item in sections[key]:
                parts.append(f"- {item}")
            parts.append("")
        return "\n".join(parts).strip() + "\n"

    MAX_MAIN_CHARS = 7500
    EVICTION_MAX = 500
    TOTAL_MAX = MAX_MAIN_CHARS + EVICTION_MAX  # 8000

    doc = _format_doc(parsed)
    if len(doc) <= MAX_MAIN_CHARS:
        # No trimming needed; preserve any existing eviction summary
        if prior_eviction["established"] or prior_eviction["failed"] or prior_eviction["open_questions"]:
            eviction_body = _format_eviction_body(
                prior_eviction["established"],
                prior_eviction["failed"],
                prior_eviction["open_questions"],
                prior_eviction["failed_titles"],
            )
            return _format_doc(parsed, eviction_body[:EVICTION_MAX])[:TOTAL_MAX]
        return doc

    # Snapshot sections before trimming
    before = {k: list(v) for k, v in parsed.items() if k in ("established", "failed", "open_questions")}

    # Trim oldest entries (from top of each section) to stay under main limit
    while len(doc) > MAX_MAIN_CHARS:
        trimmed = False
        for key in sorted(
            ["established", "failed", "open_questions"],
            key=lambda k: -len(parsed[k]),
        ):
            if len(parsed[key]) > 1:
                parsed[key] = parsed[key][1:]
                trimmed = True
                break
        if not trimmed:
            break
        doc = _format_doc(parsed)

    # Compute newly evicted entries
    new_established = len(before["established"]) - len(parsed["established"])
    new_failed = len(before["failed"]) - len(parsed["failed"])
    new_oq = len(before["open_questions"]) - len(parsed["open_questions"])

    evicted_failed_titles = (
        [_format_evicted_failed(e) for e in before["failed"][:new_failed]] if new_failed > 0 else []
    )

    # Merge with prior eviction summary
    total_established = prior_eviction["established"] + new_established
    total_failed = prior_eviction["failed"] + new_failed
    total_oq = prior_eviction["open_questions"] + new_oq
    all_failed_titles = prior_eviction["failed_titles"] + evicted_failed_titles

    eviction_body = _format_eviction_body(total_established, total_failed, total_oq, all_failed_titles)
    return _format_doc(parsed, eviction_body[:EVICTION_MAX])[:TOTAL_MAX]


def _parse_research_findings(text: str) -> Optional[List[Dict[str, Any]]]:
    """Extract structured research findings JSON array from agent output text."""
    json_str = _extract_json_array(text, "title", "prompt")
    if json_str:
        try:
            findings = json.loads(json_str)
            if isinstance(findings, list):
                return findings
        except json.JSONDecodeError:
            # JSON may be truncated — try to recover complete objects
            # Find last complete top-level object using depth tracking
            depth = 0
            last_close = -1
            in_string = False
            escape_next = False
            for i, c in enumerate(json_str):
                if escape_next:
                    escape_next = False
                    continue
                if c == '\\' and in_string:
                    escape_next = True
                    continue
                if c == '"' and not escape_next:
                    in_string = not in_string
                    continue
                if in_string:
                    continue
                if c == '{':
                    depth += 1
                elif c == '}':
                    depth -= 1
                    if depth == 0:
                        last_close = i
            if last_close > 0:
                try:
                    truncated = json_str[:last_close + 1] + ']'
                    findings = json.loads(truncated)
                    if isinstance(findings, list):
                        logger.info(f"[RESEARCH] Recovered {len(findings)} findings from truncated JSON")
                        return findings
                except json.JSONDecodeError:
                    pass
    return None


def _parse_research_insights(task_id: str) -> List[Dict[str, Any]]:
    """Read structured research insights from the agent's /tmp output file."""
    insights_path = f"/tmp/research_insights_{task_id}.json"
    try:
        if not os.path.exists(insights_path):
            return []
        with open(insights_path, "r") as f:
            raw = f.read(200_000)
        data = json.loads(raw)
        if isinstance(data, list):
            return [i for i in data if isinstance(i, dict) and "summary" in i]
    except Exception as e:
        logger.warning(f"[RESEARCH] Failed to read insights file {insights_path}: {e}")
    return []


async def _handle_research_completion(task: Task, logs: str, session: AsyncSession):
    """Parse research findings and create tasks from them."""
    # Depth guard: prevent infinite research recursion
    MAX_RESEARCH_DEPTH = 3
    research_depth = (task.context or {}).get("research_depth", 0)
    parent_research_task_id = (task.context or {}).get("parent_research_task_id")
    if research_depth >= MAX_RESEARCH_DEPTH:
        task.latest_status_message = f"Research depth limit ({MAX_RESEARCH_DEPTH}) reached — not spawning further tasks"
        session.add(task)
        await session.commit()
        logger.warning(f"[RESEARCH] Depth limit reached for task {task.id} (depth={research_depth})")
        return

    findings = _parse_research_findings(logs)
    insights = _parse_research_insights(str(task.id))
    if not findings:
        task.latest_status_message = "Research completed with no actionable findings"
        task.plan_output = []
        session.add(task)
        await session.commit()
        return

    # Store all findings for admin visibility before filtering
    task.plan_output = findings
    session.add(task)
    await session.commit()

    # Separate direction proposals and notes from actionable findings — these are processed
    # separately and must not enter the hash-based deduplication loop (they never create tasks,
    # so their hashes would never be stored, allowing identical entries to accumulate).
    direction_proposals = [f for f in findings if f.get("type") == "direction_proposal"]
    direction_notes = [f for f in findings if f.get("type") == "direction_note"]
    findings = [f for f in findings if f.get("type") not in ("direction_proposal", "direction_note")]

    # Recover findings that were deferred in the previous cycle due to pipeline degradation.
    # These were already researched but couldn't be spawned; prepend them so they go through
    # the full dedup + health gate pipeline alongside this cycle's new findings.
    prior_deferred = (task.context or {}).get("prior_deferred_findings", [])
    if prior_deferred:
        # Filter out non-actionable types that would have been handled separately above
        prior_actionable = [
            f for f in prior_deferred
            if isinstance(f, dict) and f.get("type") not in ("direction_proposal", "direction_note")
        ]
        if prior_actionable:
            logger.info(
                f"[RESEARCH] Prepending {len(prior_actionable)} deferred finding(s) from previous cycle "
                f"to findings list for task {task.id}"
            )
            findings = prior_actionable + findings

    # Compute capacity-aware batch cap — no DB queries, uses in-memory state only
    from app import shared_state as _ss  # noqa: PLC0415
    from app import max_plan_pool as _pool_mod  # noqa: PLC0415
    _max_vms = _runtime_config.get("max_concurrent_vms", 8)
    _available_slots = max(0, _max_vms - _ss.tasks_active_gauge)
    _queued_count = _ss.tasks_queued_gauge
    _all_pool_accounts = _pool_mod._all_accounts()
    if _all_pool_accounts:
        _healthy_accounts = sum(
            1 for a in _all_pool_accounts
            if _pool_mod._account_available_for_model(a, "claude-opus-4-7")
        )
        _cap_by_accounts = _healthy_accounts * 3
    else:
        _healthy_accounts = -1  # sentinel: pool unconfigured, don't constrain
        _cap_by_accounts = 15
    _cap_by_slots = _available_slots * 2
    _cap_by_queue = max(3, 15 - _queued_count)
    # Floor slots and accounts at 1 so a saturated-but-recovering system (all VMs
    # busy, or all accounts temporarily rate-limited) doesn't zero out the cap and
    # permanently discard this research cycle's findings.  Queue depth already has
    # its own max(3, …) floor; consistent treatment here prevents _effective_cap=0.
    _effective_cap = min(15, max(1, _cap_by_slots), max(1, _cap_by_accounts), _cap_by_queue)
    _cap_factors = {
        "max_findings": 15,
        "vm_slots": _cap_by_slots,
        "healthy_accounts": _cap_by_accounts,
        "queue_depth": _cap_by_queue,
    }
    _limiting_factor = min(_cap_factors, key=lambda k: _cap_factors[k])
    logger.info(
        "[RESEARCH] Batch cap: %d (limited_by=%s, available_slots=%d, healthy_accounts=%s, queue_depth=%d)",
        _effective_cap, _limiting_factor, _available_slots,
        _healthy_accounts if _healthy_accounts >= 0 else "n/a",
        _queued_count,
    )
    # Cap findings to prevent runaway task creation and backlog build-up
    if len(findings) > _effective_cap:
        findings = findings[:_effective_cap]

    original_base = (task.first_payload).get("base_branch", "main")
    short_id = str(task.id).replace("-", "")[:8]
    research_branch = f"research/{short_id}"

    # Create staging branch for this research batch
    if not await _ensure_branch(task, research_branch, original_base, "RESEARCH", session):
        return

    # Validate dependencies if present
    if not _validate_plan_dependencies(findings):
        logger.warning("[RESEARCH] Findings have circular dependencies — ignoring depends_on_indices")
        for f in findings:
            f.pop("depends_on_indices", None)

    # Query active/queued tasks for word-overlap deduplication
    existing_tasks = (await session.execute(
        select(Task).where(
            Task.project_id == task.project_id,
            Task.status.in_([TaskStatus.QUEUED, TaskStatus.IN_PROGRESS, TaskStatus.EVALUATING, TaskStatus.PROVISIONING])
        )
    )).scalars().all()
    # Build (direction_id, title) pairs to enable direction-aware dedup thresholds
    existing_tasks_with_dirs = [
        ((t.context or {}).get("direction_id"), t.title.lower())
        for t in existing_tasks
    ]

    # Query recent tasks (last 90 days) for hash-based deduplication across research cycles
    ninety_days_ago = _utcnow() - timedelta(days=90)
    recent_tasks_for_dedup = (await session.execute(
        select(Task).where(
            Task.project_id == task.project_id,
            Task.created_at >= ninety_days_ago,
        )
    )).scalars().all()
    # Collect parent IDs for COMPLETED+pr_merged tasks to detect ghost merges
    # (code merged to staging branch but parent rollup PR to main failed)
    potential_ghost_parent_ids = {
        t.parent_task_id for t in recent_tasks_for_dedup
        if t.parent_task_id
        and t.status == TaskStatus.COMPLETED
        and (t.context or {}).get("pr_merged")
    }
    failed_parent_ids: set = set()
    if potential_ghost_parent_ids:
        parent_tasks_rows = (await session.execute(
            select(Task).where(Task.id.in_(potential_ghost_parent_ids))
        )).scalars().all()
        failed_parent_ids = {
            p.id for p in parent_tasks_rows
            if p.status in (TaskStatus.FAILED, TaskStatus.CANCELLED)
        }

    from app.failure_classification import INFRA_CLASSES, classify_failure  # noqa: PLC0415
    _code_error_cooldown = timedelta(hours=48)
    _dedup_now = _utcnow()

    recent_finding_hashes = {
        t.context["finding_hash"] for t in recent_tasks_for_dedup
        if t.context and t.context.get("finding_hash")
        # Exclude COMPLETED without pr_merged (code was never shipped)
        and not (t.status == TaskStatus.COMPLETED and not (t.context or {}).get("pr_merged"))
        # Exclude ghost merges: pr_merged=True but rollup parent failed (code never reached main)
        and not (
            t.status == TaskStatus.COMPLETED
            and (t.context or {}).get("pr_merged")
            and t.parent_task_id in failed_parent_ids
        )
        # Exclude transient infra failures (task never ran, should be retried)
        and not (
            t.status in (TaskStatus.FAILED, TaskStatus.CANCELLED)
            and classify_failure(t) in INFRA_CLASSES
        )
        # Exclude CODE_ERROR failures older than cooldown — codebase may have changed,
        # or improved context may allow a different implementation approach.
        # Tasks within the cooldown window still block to prevent rapid re-spin loops.
        and not (
            t.status in (TaskStatus.FAILED, TaskStatus.CANCELLED)
            and classify_failure(t) == "CODE_ERROR"
            and t.created_at < _dedup_now - _code_error_cooldown
        )
    }
    # Build a map of finding_hash -> outcome for failed/cancelled tasks for better logging
    failed_hashes = {
        t.context["finding_hash"]: t.status
        for t in recent_tasks_for_dedup
        if t.context and t.context.get("finding_hash")
        and t.status in (TaskStatus.FAILED, TaskStatus.CANCELLED)
    }
    # Build a map of finding_hash -> most-recent failed task for prior-attempt context injection.
    # When a re-spawned finding passes dedup (e.g. CODE_ERROR cooldown elapsed), inject the
    # prior failure's retry context so the new agent avoids repeating the same approach.
    prior_failure_by_hash: dict[str, Task] = {}
    for _pt in recent_tasks_for_dedup:
        if not _pt.context:
            continue
        _pfh = _pt.context.get("finding_hash")
        if not _pfh:
            continue
        if _pt.status not in (TaskStatus.FAILED, TaskStatus.CANCELLED):
            continue
        _existing = prior_failure_by_hash.get(_pfh)
        if _existing is None or (_pt.created_at or _dedup_now) > (_existing.created_at or _dedup_now):
            prior_failure_by_hash[_pfh] = _pt
    # Also suppress tasks that have an active suppress_until field (set on user-cancelled low-priority tasks)
    now_iso = _utcnow().isoformat()
    for t in recent_tasks_for_dedup:
        if t.context and t.context.get("suppress_until") and t.context["suppress_until"] > now_iso:
            fhash = t.context.get("finding_hash")
            if fhash:
                recent_finding_hashes.add(fhash)
                failed_hashes.setdefault(fhash, "suppressed")

    # Query open PR artifacts to detect file-scope conflicts with in-flight work
    open_pr_stmt = (
        select(Artifact.pr_url, Artifact.logs, Task.context)
        .join(Task, Artifact.task_id == Task.id)
        .where(
            Task.project_id == task.project_id,
            Artifact.artifact_type == ArtifactType.PULL_REQUEST,
            Artifact.pr_url.isnot(None),
            Task.status.not_in([TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.CANCELLED])
        )
        .limit(200)
    )
    open_pr_rows = (await session.execute(open_pr_stmt)).all()
    if len(open_pr_rows) >= 200:
        logger.warning(f"[RESEARCH] open PR artifact query hit 200-row cap for project {task.project_id} — file-overlap dedup may be incomplete")
    open_pr_files: set[str] = set()
    for _, _, task_ctx in open_pr_rows:
        if task_ctx:
            for f in (task_ctx.get('related_files') or []):
                open_pr_files.add(f)

    def _finding_hash(title: str, description: str) -> str:
        return hashlib.sha256((title + description).encode()).hexdigest()[:16]

    def _is_duplicate(finding_title: str, finding_dir_id: str | None, existing: list[tuple]) -> bool:
        STOPWORDS = {'the', 'a', 'an', 'to', 'for', 'and', 'or', 'in', 'of', 'is', 'it',
                     'this', 'that', 'with', 'by', 'we', 'should', 'be', 'are', 'on', 'as',
                     'feat', 'fix', 'refactor', 'add', 'update', 'improve'}
        words = set(finding_title.lower().split()) - STOPWORDS
        for task_dir_id, title in existing:
            overlap = words & (set(title.lower().split()) - STOPWORDS)
            # Cross-direction: require 6+ word overlap. Independent strategic tracks rarely
            # produce true duplicates, so a higher threshold avoids false positives.
            # If either direction is unknown (None), treat as same-direction (safer default).
            same_direction = (
                not finding_dir_id
                or not task_dir_id
                or finding_dir_id == task_dir_id
            )
            threshold = 4 if same_direction else 6
            if len(overlap) >= threshold:
                return True
        return False

    # Sort findings by priority: high → medium → low (preserve original order within each tier).
    # The orchestrator dispatches by (status, priority, created_at), so higher-priority
    # tasks naturally run first without needing artificial wave dependencies.
    priority_order = {"high": 0, "medium": 1, "low": 2}
    indexed_findings = list(enumerate(findings))
    indexed_findings.sort(key=lambda x: priority_order.get(x[1].get("priority", "medium"), 1))

    # Deduplicate findings against recent tasks
    hash_dedup_count = 0
    word_overlap_count = 0
    file_overlap_count = 0
    type_filtered_count = 0
    empty_prompt_count = 0
    deduplicated: list[tuple[int, dict, str]] = []  # (orig_i, finding, fhash)
    for orig_i, finding in indexed_findings:
        title = finding.get("title", f"Research finding #{orig_i+1}")
        description = finding.get("description", finding.get("prompt", ""))
        fhash = _finding_hash(title, description)
        if fhash in recent_finding_hashes:
            outcome = failed_hashes.get(fhash, "prior")
            logger.info(f"[RESEARCH] Skipping finding '{title}' — hash {fhash} matched {outcome} task")
            hash_dedup_count += 1
            continue
        finding_dir_id = finding.get("direction_id") or (task.context or {}).get("direction_id")
        if _is_duplicate(title, finding_dir_id, existing_tasks_with_dirs):
            logger.warning(f"[RESEARCH] Skipping duplicate finding '{title}' — matches existing task")
            word_overlap_count += 1
            continue
        # Skip if all related_files for this finding are already covered by open PRs
        finding_files = set(finding.get('related_files', []))
        if finding_files and finding_files.issubset(open_pr_files):
            logger.info(f"[RESEARCH] Skipping finding '{title}' — all related files already have open PRs")
            file_overlap_count += 1
            continue
        deduplicated.append((orig_i, finding, fhash))

    # Assess pipeline health once before spawning implementation tasks.
    # Metadata findings (design_doc_observation, direction_update) are processed
    # regardless; only VM-consuming implementation tasks are capped.
    _pipeline_degraded, _health_reason = _assess_spawn_health()
    _impl_spawned = 0
    _deferred_findings: list[dict] = []

    # Create all child tasks — no wave dependencies, priority ordering handles dispatch order
    child_tasks = []
    finding_indices = []  # Maps child_tasks index → findings array index

    # Pre-load project for scope calibration (SQLAlchemy identity map — no extra query if already loaded)
    _scope_cal_project = await session.get(Project, task.project_id)
    _directions_by_id: dict = {}
    if _scope_cal_project:
        for _d in (_scope_cal_project.directions or []):
            if _d.get("id"):
                _directions_by_id[_d["id"]] = _d
    # Also index by parent_direction_id in task context as fallback
    _parent_direction_id = (task.context or {}).get("direction_id")

    _cochange_index_for_decomp = (
        (_scope_cal_project.design_doc or {}).get("cochange_index")
        if _scope_cal_project
        else None
    )
    deduplicated = auto_decompose_findings_by_cochange(
        deduplicated,
        _cochange_index_for_decomp,
        task.context,
        _finding_hash,
    )

    for orig_i, finding, fhash in deduplicated:
        scope = finding.get("scope", DEFAULT_TASK_SCOPE)
        risk = finding.get("risk", "low")
        title = finding.get("title", f"Research finding #{orig_i+1}")
        prompt = finding.get("prompt", finding.get("description", ""))

        # Determine task type early — some findings don't spawn tasks
        finding_type = finding.get("type", "improvement")

        if finding_type == "design_doc_observation":
            # Append to design doc proposals for Phase 1 to review
            obs_project = await session.get(Project, task.project_id)
            if obs_project:
                doc = dict(obs_project.design_doc or {})
                proposals = doc.get("proposed_changes", [])
                proposals.append({
                    "id": f"pc-{uuid.uuid4().hex[:8]}",
                    "observation": finding.get("description", "")[:500],
                    "section": finding.get("section", "general"),
                    "proposed_by_task": str(task.id),
                    "proposed_at": _utcnow().isoformat(),
                    "status": "pending",
                })
                doc["proposed_changes"] = proposals[-30:]  # cap at 30 pending
                obs_project.design_doc = doc
                flag_modified(obs_project, "design_doc")
                session.add(obs_project)
            type_filtered_count += 1
            continue  # Don't create a task

        if finding_type == "direction_update":
            # Process as a direction modification proposal, not a task
            target_id = finding.get("direction_id")
            if not target_id:
                type_filtered_count += 1
                continue  # direction_update requires a target direction_id
            dir_project = await session.get(Project, task.project_id)
            if not dir_project:
                type_filtered_count += 1
                continue
            directions = list(dir_project.directions or [])
            for d in directions:
                if d.get("id") == target_id:
                    d["pending_update"] = {
                        "suggested_text": finding.get("suggested_text", d.get("text")),
                        "suggested_priority": finding.get("suggested_priority", d.get("priority")),
                        "suggested_paths": finding.get("suggested_paths", d.get("paths", [])),
                        "rationale": finding.get("description", "")[:300],
                        "proposed_at": _utcnow().isoformat(),
                        "proposed_by": str(task.id),
                    }
                    break
            dir_project.directions = directions
            flag_modified(dir_project, "directions")
            session.add(dir_project)
            type_filtered_count += 1
            continue  # Don't create a task for direction_update findings

        if not prompt:
            empty_prompt_count += 1
            continue

        # Skip findings already directly dispatched by the research agent (scope=small+risk=low).
        # These were sent via /api/agent/spawn before research completed; no task needed here.
        if finding.get("already_spawned"):
            continue

        # Spawn-time health gate: when pipeline is degraded, cap implementation task creation
        # to avoid queuing tasks that will fail immediately or stall indefinitely.
        # Metadata findings (already handled above) and already_spawned bypass this check.
        if _pipeline_degraded and _impl_spawned >= _DEGRADED_SPAWN_CAP:
            _deferred_findings.append(finding)
            continue

        # Apply scope calibration from direction's historical merge rates (skip if already calibrated)
        scope_calibrated_from: str | None = finding.get("scope_calibrated_from")
        if not (scope_calibrated_from or (task.context or {}).get("scope_calibrated_from")):
            _dir_id = finding.get("direction_id") or _parent_direction_id
            _direction = _directions_by_id.get(str(_dir_id)) if _dir_id else None
            if _direction:
                scope, scope_calibrated_from = _calibrate_scope(scope, _direction, _scope_cal_project)

        # Pre-spawn file-count heuristic: medium-scope findings with 5+ related_files
        # will almost certainly overflow the 10-file threshold in standard.ts once
        # tests and discovered imports are added. Upgrade to large to route through
        # the plan agent for decomposition into smaller subtasks.
        if scope == "medium":
            _heuristic_files = finding.get("related_files") or []
            if len(_heuristic_files) >= 5:
                logger.info(
                    "[RESEARCH] Upgrading scope medium→large for '%s' "
                    "(file-count heuristic: %d related_files >= 5)",
                    title, len(_heuristic_files),
                )
                scope = "large"
                scope_calibrated_from = "medium"
            elif len({f.split("/", 1)[0] for f in _heuristic_files if "/" in f}) >= 2:
                # Cross-module heuristic: related_files span 2+ top-level directories
                # (e.g. backend + agent + admin-ui). These face language boundaries,
                # separate build/test systems, and independent CI — upgrade to large
                # for plan decomposition into per-module subtasks.
                _modules = sorted({f.split("/", 1)[0] for f in _heuristic_files if "/" in f})
                logger.info(
                    "[RESEARCH] Upgrading scope medium→large for '%s' "
                    "(cross-module heuristic: related_files span %d modules: %s)",
                    title, len(_modules), ",".join(_modules),
                )
                scope = "large"
                scope_calibrated_from = "medium:cross_module"
            elif _heuristic_files:
                # Predictive expansion-ratio check: direction's historical avg_expansion_ratio
                # (actual files touched / expected related_files) multiplied by this finding's
                # related_files predicts how many files the task will actually touch. If that
                # exceeds the medium overflow threshold (10 in SCOPE_OVERFLOW_THRESHOLDS), route
                # to large for plan decomposition before burning 5-10 minutes on a task that
                # will almost certainly scope-overflow during implementation.
                _exp_dir_id = finding.get("direction_id") or _parent_direction_id
                _exp_direction = _directions_by_id.get(str(_exp_dir_id)) if _exp_dir_id else None
                if _exp_direction:
                    _ratios = [
                        (entry.get("scope_breakdown") or {}).get("medium", {}).get("avg_expansion_ratio")
                        for entry in (_exp_direction.get("research_history") or [])[-3:]
                    ]
                    _ratios = [float(r) for r in _ratios if isinstance(r, (int, float))]
                    if _ratios:
                        _avg_ratio = sum(_ratios) / len(_ratios)
                        _predicted_files = len(_heuristic_files) * _avg_ratio
                        if _predicted_files > 10:
                            logger.info(
                                "[RESEARCH] Upgrading scope medium→large for '%s' "
                                "(expansion-ratio prediction: %d related_files × %.2f avg_ratio "
                                "= %.1f predicted files > 10 threshold)",
                                title, len(_heuristic_files), _avg_ratio, _predicted_files,
                            )
                            scope = "large"
                            scope_calibrated_from = "medium"
            # Composite complexity score: combines file count, historical expansion,
            # cochange cluster density, and cross-module span into a single merge-
            # probability estimate. Upgrades when score indicates low P(merge) even if
            # individual binary checks above let the task through.
            if scope == "medium":
                _cc_dir = _directions_by_id.get(str(finding.get("direction_id") or _parent_direction_id)) if (finding.get("direction_id") or _parent_direction_id) else None
                _cc_score, _cc_signals = _composite_complexity_score(
                    finding.get("related_files") or [], _cc_dir, _cochange_index_for_decomp,
                )
                finding["_complexity_score"] = round(_cc_score, 3)
                finding["_complexity_signals"] = _cc_signals
                if _cc_score < 0.5:
                    logger.info(
                        "[RESEARCH] Upgrading scope medium→large for '%s' "
                        "(composite complexity %.2f < 0.50, signals=%s)",
                        title, _cc_score, _cc_signals,
                    )
                    scope = "large"
                    scope_calibrated_from = "medium"
        elif scope == "small":
            _heuristic_files = finding.get("related_files") or []
            if len(_heuristic_files) >= 3:
                logger.info(
                    "[RESEARCH] Upgrading scope small→medium for '%s' "
                    "(file-count heuristic: %d related_files >= 3)",
                    title, len(_heuristic_files),
                )
                scope = "medium"
                scope_calibrated_from = "small"

        if finding_type == "research":
            task_type = "research"  # deeper investigation before implementation
        elif finding_type in ("security_audit", "qa", "deps", "docs", "migrate", "perf"):
            task_type = finding_type  # spawn specialized agent
        elif finding_type == "refactor":
            task_type = "refactor"  # code quality improvement, no behavior change
        elif scope == "large":
            task_type = "plan"     # large scope needs decomposition
        else:
            task_type = "create"   # small/medium scope is single-PR work
        # Pre-spawn approval removed: the review pipeline handles quality gates.
        # Only final merge to main requires human approval (critical files gate).
        needs_approval = False

        direction_id = finding.get("direction_id")
        # Apply per-type model/effort overrides — take the higher of finding vs config
        _model_rank = {"claude-opus-4-7": 4, "claude-opus-4-6": 3, "claude-sonnet-4-6": 2, "claude-haiku-4-5-20251001": 1}
        _effort_rank = {"max": 4, "high": 3, "medium": 2, "low": 1}
        finding_model = finding.get('model')
        config_model = _runtime_config.get("task_type_models", {}).get(task_type)
        if finding_model and config_model:
            finding_model = finding_model if _model_rank.get(finding_model, 0) >= _model_rank.get(config_model, 0) else config_model
        elif config_model:
            finding_model = config_model
        finding_effort = finding.get('effort')
        config_effort = _runtime_config.get("task_type_efforts", {}).get(task_type)
        if finding_effort and config_effort:
            finding_effort = finding_effort if _effort_rank.get(finding_effort, 0) >= _effort_rank.get(config_effort, 0) else config_effort
        elif config_effort:
            finding_effort = config_effort

        related_files = finding.get("related_files", [])
        full_prompt = prompt + ("\n\nKey files to review or modify:\n" + "\n".join(f"- {f}" for f in related_files)) if related_files else prompt
        type_suffix = _runtime_config.get("agent_prompt_suffixes", {}).get(task_type)

        # For sub-research tasks, enrich prompt with parent context to avoid duplication
        if task_type == "research":
            sibling_titles = [
                f.get('title', '') for _, f, _ in deduplicated
                if f.get('title') and f != finding
            ][:5]
            if sibling_titles:
                parent_summary = (
                    '\n\nContext from parent research task: '
                    f'This sub-research is part of a broader investigation (task {str(task.id)[:8]}) '
                    f'that also produced these parallel findings:\n' +
                    '\n'.join(f'- {t}' for t in sibling_titles) +
                    '\nFocus your investigation on the specific area described above without duplicating these parallel efforts.'
                )
                full_prompt = full_prompt + parent_summary

        # Predict spawn success probability using in-memory direction + project data
        _confidence_threshold = _runtime_config.get("spawn_confidence_threshold", 0.25)
        _pred_dir_id = finding.get("direction_id") or _parent_direction_id
        _pred_direction = _directions_by_id.get(str(_pred_dir_id)) if _pred_dir_id else {}
        _pred_p = predict_spawn_success(scope, _pred_direction or {}, _scope_cal_project)
        _low_confidence = _pred_p < _confidence_threshold
        if _low_confidence:
            logger.info(
                "[RESEARCH] Low-confidence spawn '%s' (scope=%s, type=%s): P(success)=%.2f < threshold=%.2f",
                title, scope, task_type, _pred_p, _confidence_threshold,
            )
        if _low_confidence and scope == "medium" and not _poor_decomp_history(_pred_direction or {}):
            logger.info("[RESEARCH] Auto-escalating scope medium→large for '%s' (low-confidence spawn: P=%.2f < %.2f)",
                        title, _pred_p, _confidence_threshold)
            scope, scope_calibrated_from, task_type = "large", "medium", "plan"

        finding_priority = TASK_PRIORITY_MAP.get(finding.get("priority", "medium"), 50)
        # Inherit parent direction_id if the finding doesn't specify one
        parent_direction_id = (task.context or {}).get("direction_id")

        # If this finding matches a prior failed task (e.g. re-spawn after CODE_ERROR cooldown),
        # inject its failure context using the _previous_* naming convention so the agent's
        # buildRetryContext renders prior-attempt guidance.
        prior_retry_ctx: dict = {}
        _retry_prompt_suffix = ""
        _prior = prior_failure_by_hash.get(fhash)
        if _prior is not None:
            _prior_ctx = _prior.context or {}
            prior_retry_ctx["_attempt_number"] = 2
            if _prior.latest_status_message:
                prior_retry_ctx["_previous_error"] = _prior.latest_status_message[:5000]
            failure_class_val: str | None = None
            try:
                failure_class_val = classify_failure(_prior)
                prior_retry_ctx["_previous_failure_class"] = failure_class_val
            except Exception:
                pass
            # Enriched failure diagnosis to mirror task_lifecycle._requeue_parent_task,
            # so research-spawned retries get the same buildRetryContext rendering as
            # inline retries (failure stage labels, recurring warnings, error summary).
            try:
                from app.anomaly_detection import _error_fingerprint, _is_recurring_failure_class  # noqa: PLC0415
                from app.task_lifecycle import _derive_failure_stage  # noqa: PLC0415
                from app import shared_state as _ss  # noqa: PLC0415
                error_msg = _prior.latest_status_message or ""
                prior_retry_ctx["_previous_error_fingerprint"] = _error_fingerprint(error_msg)
                if failure_class_val:
                    stage = _derive_failure_stage(failure_class_val, error_msg)
                    if stage:
                        prior_retry_ctx["_previous_failure_stage"] = stage
                    if failure_class_val == "CODE_ERROR":
                        prior_retry_ctx["_previous_error_summary"] = error_msg[:500]
                    proj_anomalies = _ss.persistent_anomalies.get(str(_prior.project_id), {})
                    if _is_recurring_failure_class(proj_anomalies, failure_class_val):
                        prior_retry_ctx["_previous_is_recurring"] = True
            except Exception:
                logger.exception("[RESEARCH_COMPLETION] Failed to enrich prior_retry_ctx for finding %s", fhash)
            if _prior_ctx.get("_analysis_plan"):
                prior_retry_ctx["_previous_analysis_plan"] = _prior_ctx["_analysis_plan"]
            if _prior_ctx.get("_pr_changed_files"):
                prior_retry_ctx["_previous_changed_files"] = _prior_ctx["_pr_changed_files"]
            if _prior_ctx.get("_pr_diff_summary"):
                prior_retry_ctx["_previous_diff_summary"] = _prior_ctx["_pr_diff_summary"]
            if _prior_ctx.get("_pr_url"):
                prior_retry_ctx["_previous_pr_url"] = _prior_ctx["_pr_url"]
            if _prior_ctx.get("_test_failure_summary"):
                prior_retry_ctx["_previous_test_failure_summary"] = _prior_ctx["_test_failure_summary"]
            if _prior_ctx.get("_self_review_outcome"):
                prior_retry_ctx["_previous_self_review_outcome"] = _prior_ctx["_self_review_outcome"]
            if _prior_ctx.get("_file_change_details"):
                prior_retry_ctx["_previous_file_change_details"] = _prior_ctx["_file_change_details"]
            # Generate failure-class-specific retry guidance, mirroring
            # task_lifecycle._requeue_parent_task so re-spawned findings get the
            # same tailored prompt_suffix as directly requeued retries.
            try:
                from app.task_lifecycle import _build_retry_prompt_suffix  # noqa: PLC0415
                _retry_prompt_suffix, _scope_override = _build_retry_prompt_suffix(
                    failure_class_val,
                    _prior.latest_status_message,
                    prior_retry_ctx["_attempt_number"],
                    scope,
                    prior_retry_ctx,
                )
                if _scope_override:
                    scope = _scope_override
                    prior_retry_ctx["_scope_reduced"] = True
            except Exception:
                logger.exception("[RESEARCH_COMPLETION] Failed to build retry prompt_suffix for finding %s", fhash)
        _combined_suffix = _retry_prompt_suffix
        if type_suffix:
            _combined_suffix = (_combined_suffix + ("\n" if _combined_suffix else "") + type_suffix)
        finding_payload = validate_internal_payload({
            "type": task_type,
            "prompt": full_prompt,
            "base_branch": research_branch,
            **(({'model': finding_model} if finding_model else {})),
            **(({'effort': finding_effort} if finding_effort else {})),
            **({"prompt_suffix": _combined_suffix} if _combined_suffix else {}),
        }, f"research_completion:finding:{task_type}")
        new_task = Task(
            project_id=task.project_id,
            title=title,
            description=finding.get("description", prompt[:200]),
            parent_task_id=task.id,
            payloads=[finding_payload],
            context={
                "research_task_id": str(task.id),
                "parent_research_task_id": str(task.id),
                "finding_index": orig_i,
                "scope": scope,
                "risk": risk,
                "research_branch": research_branch,
                "original_base": original_base,
                "research_depth": research_depth + 1,
                "related_files": related_files,
                "finding_hash": fhash,
                **({"direction_id": direction_id} if direction_id else ({"direction_id": parent_direction_id} if parent_direction_id else {})),
                **({"parent_direction_id": parent_direction_id} if parent_direction_id else {}),
                **({"scope_calibrated_from": scope_calibrated_from} if scope_calibrated_from else {}),
                **({"complexity_score": finding["_complexity_score"], "complexity_signals": finding.get("_complexity_signals", {})} if "_complexity_score" in finding else {}),
                **({"low_confidence_spawn": True, "low_confidence_spawn_rate": round(_pred_p, 3)} if _low_confidence else {}),
                **prior_retry_ctx,
            },
            vm_instance_id=f"agent-vm-{uuid.uuid4().hex}",
            status=TaskStatus.QUEUED,
            created_by="ORCHESTRATOR",
            requires_approval=needs_approval,
            depends_on=[],
            priority=finding_priority,
        )
        session.add(new_task)
        child_tasks.append(new_task)
        finding_indices.append(orig_i)
        _impl_spawned += 1

    if _deferred_findings:
        ctx = dict(task.context or {})
        ctx["deferred_findings"] = _deferred_findings
        task.context = ctx
        flag_modified(task, "context")
        session.add(task)
        logger.warning(
            f"[RESEARCH] Pipeline degraded ({_health_reason}) — deferred {len(_deferred_findings)} "
            f"of {len(deduplicated)} findings; spawning {_impl_spawned} tasks. "
            f"Deferred findings stored in task.context['deferred_findings'] for next cycle."
        )

    await session.commit()
    for ct in child_tasks:
        await session.refresh(ct)

    # Resolve explicit depends_on_indices to actual UUIDs (agent-specified inter-finding deps only)
    finding_to_child = {fi: ci for ci, fi in enumerate(finding_indices)}
    for ci, ct in enumerate(child_tasks):
        fi = finding_indices[ci]
        dep_indices = findings[fi].get("depends_on_indices", [])
        explicit_deps = [
            str(child_tasks[finding_to_child[j]].id)
            for j in dep_indices
            if j in finding_to_child
        ]
        if explicit_deps:
            ct.depends_on = explicit_deps
            session.add(ct)

    # Log file overlap warnings for observability and surface them to agents
    overlaps = _log_file_overlaps(findings, "RESEARCH")
    for (i, j, shared_files) in overlaps:
        for fi in (i, j):
            ci = finding_to_child.get(fi)
            if ci is not None and ci < len(child_tasks):
                ctx = dict(child_tasks[ci].context or {})
                warnings = ctx.get('file_overlap_warnings', [])
                other_fi = j if fi == i else i
                other_title = findings[other_fi].get('title', f'#{other_fi}')
                warnings.append({'shared_files': sorted(shared_files), 'with_task': other_title})
                ctx['file_overlap_warnings'] = warnings
                child_tasks[ci].context = ctx
                session.add(child_tasks[ci])

    await session.commit()

    # Update last_researched_at for directions that produced findings.
    # Also update the current direction if all findings were deduped — prevents
    # the system from immediately re-researching and hitting the same duplicates.
    direction_ids_with_findings = set()
    for ct in child_tasks:
        did = (ct.context or {}).get("direction_id")
        if did:
            direction_ids_with_findings.add(did)
    # If all findings were deduped OR no findings at all, still put the current
    # direction on cooldown and record a cycle entry. This closes a blind spot
    # where research tasks that completed with zero actionable findings (agent
    # subtask failure, malformed output, etc.) left no trace in research_history
    # and _check_direction_health could not see the stalled cycle.
    current_direction_id = (task.context or {}).get("direction_id") or (task.first_payload).get("current_direction_id")
    skipped_duplicates = hash_dedup_count + word_overlap_count + file_overlap_count
    all_deduped = len(child_tasks) == 0 and skipped_duplicates > 0
    # Build filter breakdown string (computed here so it's available for summary artifact below)
    _filter_parts = []
    if hash_dedup_count:
        _filter_parts.append(f"{hash_dedup_count} hash-dedup")
    if word_overlap_count:
        _filter_parts.append(f"{word_overlap_count} word-overlap")
    if file_overlap_count:
        _filter_parts.append(f"{file_overlap_count} file-overlap")
    if type_filtered_count:
        _filter_parts.append(f"{type_filtered_count} type-filtered")
    if empty_prompt_count:
        _filter_parts.append(f"{empty_prompt_count} empty-prompt")
    _filter_str = f" ({', '.join(_filter_parts)} skipped)" if _filter_parts else ""
    no_findings_cycle = len(child_tasks) == 0 and skipped_duplicates == 0
    if (all_deduped or no_findings_cycle) and current_direction_id:
        direction_ids_with_findings.add(current_direction_id)
    if direction_ids_with_findings:
        project = await session.get(Project, task.project_id)
        if project:
            directions = list(project.directions or [])
            now_iso = _utcnow().isoformat()
            for d in directions:
                if d.get("id") in direction_ids_with_findings:
                    d["last_researched_at"] = now_iso
            # Append to research_history for each direction that produced findings
            finding_titles_by_dir: dict = {}
            finding_descriptions_by_dir: dict = {}
            finding_scopes_by_dir: dict = {}
            for orig_i, finding, fhash in deduplicated:
                did = finding.get("direction_id")
                if did:
                    finding_titles_by_dir.setdefault(did, []).append(
                        finding.get("title", f"Research finding #{orig_i+1}")
                    )
                    finding_descriptions_by_dir.setdefault(did, []).append(
                        (finding.get("description", finding.get("prompt", "")))[:200]
                    )
                    finding_scopes_by_dir.setdefault(did, []).append(
                        finding.get("scope", DEFAULT_TASK_SCOPE)
                    )
            for d in directions:
                did = d.get("id")
                if did in direction_ids_with_findings:
                    history = d.get("research_history", [])
                    dir_child_tasks = [ct for ct in child_tasks if (ct.context or {}).get("direction_id") == did]
                    # Compact history entry — just counters and a short summary.
                    # Full details available via task API using task_id.
                    titles = finding_titles_by_dir.get(did, [])[:3]
                    descriptions = finding_descriptions_by_dir.get(did, [])[:3]
                    entry = {
                        "timestamp": now_iso,
                        "task_id": str(task.id),
                        "tasks_created": len(dir_child_tasks),
                        "summary": "; ".join(t[:60] for t in titles) if titles else "no findings",
                        "finding_titles": titles,
                        "key_finding_descriptions": descriptions,
                        "filter_breakdown": {
                            "hash_dedup": hash_dedup_count,
                            "word_overlap_dedup": word_overlap_count,
                            "file_overlap_dedup": file_overlap_count,
                            "type_filtered": type_filtered_count,
                            "empty_prompt": empty_prompt_count,
                        },
                    }
                    if no_findings_cycle and not titles:
                        entry["no_findings"] = True
                    # Keep last 10 history entries; aggregate evicted entries to preserve lifetime stats
                    new_history = history + [entry]
                    if len(new_history) > 10:
                        d["history_aggregate"] = _fold_evicted_into_aggregate(
                            d.get("history_aggregate"), new_history[:-10]
                        )
                    d["research_history"] = new_history[-10:]
                    d["possibly_saturated"] = _compute_saturation(d["research_history"])

                    # Deadlock detection: 3+ consecutive 0-task cycles driven by dedup
                    _deadlocked, _consec = _detect_dedup_deadlock(d["research_history"])
                    if _deadlocked and d.get("status") == "active":
                        _blocked_titles = [
                            f.get("title", "")[:60]
                            for f in (task.plan_output or [])
                            if f.get("type") not in (
                                "direction_proposal", "direction_note",
                                "design_doc_observation", "direction_update",
                            )
                            and f.get("title")
                        ][:3]
                        # Guard: only create alert if no undismissed alert exists for this direction
                        _existing_deadlock_alert = (await session.execute(
                            select(Alert).where(
                                Alert.project_id == task.project_id,
                                Alert.title == "Dedup deadlock detected",
                                Alert.dismissed_at.is_(None),
                                Alert.message.contains(did),
                            )
                        )).scalars().first()
                        if not _existing_deadlock_alert:
                            _deadlock_alert = Alert(
                                project_id=task.project_id,
                                severity=AlertSeverity.WARNING,
                                title="Dedup deadlock detected",
                                message=(
                                    f"Direction '{d.get('text', did)[:80]}' (id: {did}) has "
                                    f"{_consec} consecutive research cycles with 0 tasks created "
                                    f"due to deduplication. "
                                    + (
                                        f"Top blocked findings: {'; '.join(_blocked_titles)}."
                                        if _blocked_titles else ""
                                    )
                                ),
                                task_id=task.id,
                            )
                            session.add(_deadlock_alert)
                            _deadlock_msg = ProjectMessage(
                                project_id=task.project_id,
                                message_type=MessageType.BUG_REPORT,
                                from_type="agent",
                                from_task_id=task.id,
                                content=(
                                    f"Dedup deadlock detected for direction "
                                    f"'{d.get('text', did)[:80]}' (id: {did}): "
                                    f"{_consec} consecutive research cycles produced 0 tasks "
                                    f"because findings keep matching existing hashes or "
                                    f"word-overlapping active tasks. "
                                    + (
                                        f"Blocked: {'; '.join(_blocked_titles)}. "
                                        if _blocked_titles else ""
                                    )
                                    + "Consider resetting the direction or waiting for "
                                    "blocked tasks to complete."
                                ),
                                context={
                                    "source": "dedup_deadlock",
                                    "direction_id": did,
                                    "consecutive_empty_cycles": _consec,
                                },
                            )
                            session.add(_deadlock_msg)
                            logger.warning(
                                f"[RESEARCH] Dedup deadlock: direction {did}, "
                                f"{_consec} consecutive empty cycles (task {task.id})"
                            )

                    d["research_context"] = _build_research_context(
                        prior_context=d.get("research_context"),
                        finding_titles=finding_titles_by_dir.get(did, [])[:5],
                        key_finding_descriptions=finding_descriptions_by_dir.get(did, [])[:5],
                        finding_scopes=finding_scopes_by_dir.get(did, [])[:5],
                        cycle_insights=[i["summary"][:200] for i in insights[:3]],
                        history_entry=entry,
                    )

                    # Auto-complete direction if saturated with sufficient merged PRs
                    if (
                        d.get("status") == "active"
                        and d.get("possibly_saturated")
                        and entry.get("tasks_created", 0) > 0
                    ):
                        history = d.get("research_history", [])
                        agg = d.get("history_aggregate", {})
                        total_merged = (
                            sum(e.get("prs_merged", 0) for e in history)
                            + (agg.get("total_prs_merged") or 0)
                        )
                        if total_merged >= 3:
                            total_cycles = len(history) + (agg.get("total_cycles") or 0)
                            d["status"] = "completed"
                            d["completed_at"] = _utcnow().isoformat()
                            d["auto_completed"] = True
                            d["auto_completed_reason"] = (
                                f"Auto-completed: direction saturated with {total_merged} "
                                f"total PRs merged across {total_cycles} cycles"
                            )
                            alert = Alert(
                                project_id=task.project_id,
                                severity=AlertSeverity.INFO,
                                title="Direction auto-completed",
                                message=(
                                    f"Direction '{d.get('text', did)[:80]}' was auto-completed: "
                                    f"saturated with {total_merged} PRs merged across {total_cycles} cycles."
                                ),
                            )
                            session.add(alert)
                            try:
                                from app.notification_dispatch import post_direction_milestone_message  # noqa: PLC0415
                                _ac_reason = (
                                    f"Saturated with {total_merged} PRs merged across {total_cycles} cycles"
                                )
                                await post_direction_milestone_message(
                                    session, task.project_id, d.get("text", did),
                                    "auto-completed", _ac_reason, task.id,
                                )
                            except Exception as _e:
                                logger.warning(f"[NOTIFY] Failed to post auto-completed message: {_e}")

            project.directions = directions
            flag_modified(project, "directions")
            session.add(project)

            # If this is a sub-research task (depth > 0), also annotate the parent direction's
            # most recent research_history entry with a note about sub-research findings.
            if research_depth > 0 and parent_research_task_id and direction_ids_with_findings:
                parent_project = await session.get(Project, task.project_id)
                if parent_project and parent_project.directions:
                    parent_dirs = list(parent_project.directions)
                    for d in parent_dirs:
                        did = d.get("id")
                        if did not in direction_ids_with_findings:
                            continue
                        history = d.get("research_history", [])
                        if history:
                            last_entry = history[-1]
                            sub_notes = last_entry.get("sub_research_notes", [])
                            sub_notes.append({
                                "sub_task_id": str(task.id),
                                "depth": research_depth,
                                "tasks_created": len(child_tasks),
                                "finding_titles": [f.get("title", "") for _, f, _ in deduplicated[:3]],
                                "insights": [i["summary"][:150] for i in insights[:2]],  # up to 2 insights, capped
                            })
                            last_entry["sub_research_notes"] = sub_notes[-5:]  # keep last 5
                            history[-1] = last_entry
                            d["research_history"] = history
                    parent_project.directions = parent_dirs
                    flag_modified(parent_project, "directions")
                    session.add(parent_project)

    # Process direction notes — append to the target direction's notes list
    if direction_notes:
        # Deferred import of strategic caps — shared with main.py
        from app.strategic_constants import STRATEGIC_NOTES_HISTORY_MAX, STRATEGIC_RATIONALE_MAX  # noqa: PLC0415
        note_project = await session.get(Project, task.project_id)
        if note_project:
            note_directions = list(note_project.directions or [])
            changed = False
            for note_finding in direction_notes:
                target_id = note_finding.get("direction_id")
                raw_note = note_finding.get("description") or note_finding.get("prompt") or ""
                if len(raw_note) > STRATEGIC_RATIONALE_MAX:
                    logger.warning(f"[RESEARCH] Task {task.id}: truncating direction note {len(raw_note)}→{STRATEGIC_RATIONALE_MAX} chars for direction {target_id}")
                note_text = raw_note[:STRATEGIC_RATIONALE_MAX]
                if not note_text or not target_id:
                    continue
                for d in note_directions:
                    if d.get("id") == target_id:
                        notes = d.get("notes", [])
                        notes.append({
                            "text": note_text,
                            "added_at": _utcnow().isoformat(),
                            "added_by_task": str(task.id),
                        })
                        if len(notes) > STRATEGIC_NOTES_HISTORY_MAX:
                            logger.warning(f"[RESEARCH] Task {task.id}: dropping {len(notes) - STRATEGIC_NOTES_HISTORY_MAX} oldest notes on direction {target_id}")
                        d["notes"] = notes[-STRATEGIC_NOTES_HISTORY_MAX:]
                        changed = True
                        break
            if changed:
                note_project.directions = note_directions
                flag_modified(note_project, "directions")
                session.add(note_project)

    # Process direction proposals — add to Project.directions with status='proposed'
    proposals_created = 0
    if direction_proposals:
        # Deferred import to avoid circular dependency
        from app.strategic_completion import _is_direction_duplicate  # noqa: PLC0415
        proposal_project = await session.get(Project, task.project_id)
        if proposal_project:
            proj_directions = list(proposal_project.directions or [])
            rejected_proposals = [d for d in proj_directions if d.get("status") == "rejected"]
            pending_count = sum(1 for d in proj_directions if d.get("status") == "proposed")
            max_proposals = 10
            now_iso = _utcnow().isoformat()
            for proposal in direction_proposals[:5]:  # max 5 per research call
                if pending_count >= max_proposals:
                    logger.info(f"[RESEARCH] Skipping direction proposal — max {max_proposals} pending proposals reached")
                    break
                proposal_text = (proposal.get("prompt") or proposal.get("description") or "")[:500]
                if not proposal_text.strip():
                    continue
                if _is_direction_duplicate(proposal_text, proj_directions, rejected_proposals):
                    logger.info(f"[RESEARCH] Skipping direction proposal — overlaps with existing direction")
                    continue
                new_direction = {
                    "id": f"d-{uuid.uuid4().hex[:8]}",
                    "text": proposal_text,
                    "priority": proposal.get("priority", "medium") if proposal.get("priority") in ("high", "medium", "low") else "medium",
                    "paths": [],
                    "added_at": now_iso,
                    "status": "proposed",
                    "proposed_by": str(task.id),
                    "proposal_rationale": (proposal.get("description") or "")[:500],
                    "proposed_at": now_iso,
                }
                proj_directions.append(new_direction)
                pending_count += 1
                proposals_created += 1
            if proposals_created:
                proposal_project.directions = proj_directions
                flag_modified(proposal_project, "directions")
                session.add(proposal_project)
                logger.info(f"[RESEARCH] Created {proposals_created} direction proposals from task {task.id}")

    # Store a human-readable research summary artifact
    if child_tasks:
        direction_texts = []
        payload0 = task.first_payload
        if payload0.get('directions'):
            direction_texts = [payload0['directions'][0].get('text', 'general improvement')]
        summary_lines = [
            f'# Research Summary',
            f'**Direction**: {direction_texts[0][:200] if direction_texts else "general"}',
            f'**Tasks Created**: {len(child_tasks)}{_filter_str}',
            f'',
            f'## Findings:',
        ]
        for ct in child_tasks[:10]:
            p = ct.first_payload
            summary_lines.append(f'- [{p.get("type", "?")}] {ct.title}: {(ct.description or "")[:150]}')
        summary = '\n'.join(summary_lines)

        summary_artifact = Artifact(
            task_id=task.id,
            artifact_type=ArtifactType.INVESTIGATION_REPORT,
            logs=summary,
        )
        session.add(summary_artifact)

    direction_id = (task.context or {}).get("direction_id", "?")
    status_msg = (
        f"Research created {len(child_tasks)} tasks from {len(findings)} findings"
        + _filter_str
        + (f", {proposals_created} direction proposals pending review" if proposals_created else "")
    )

    if not child_tasks:
        task.status = TaskStatus.COMPLETED
        task.latest_status_message = status_msg + " — auto-completed (no actionable tasks)"
        task.context = {**(task.context or {}), "research_branch": research_branch, "original_base": original_base}
        session.add(task)

        _note_parts = []
        if skipped_duplicates:
            _note_parts.append(f"{skipped_duplicates} duplicate(s) skipped"
                               + (f" ({hash_dedup_count} hash-match, {word_overlap_count} word-overlap,"
                                  f" {file_overlap_count} file-overlap)"
                                  if hash_dedup_count or word_overlap_count or file_overlap_count else ""))
        if type_filtered_count:
            _note_parts.append(f"{type_filtered_count} type-filtered (design_doc_observation/direction_update)")
        if empty_prompt_count:
            _note_parts.append(f"{empty_prompt_count} had no prompt")
        _note_filter = ("; ".join(_note_parts) + ".") if _note_parts else "not actionable."
        note = (
            f"Research cycle {task.id} found {len(findings)} findings but created 0 tasks: "
            + _note_filter
        )
        project = await session.get(Project, task.project_id)
        if project and str(direction_id) != "?" and direction_id != "crosscutting":
            directions = list(project.directions or [])
            for d in directions:
                if d.get("id") == direction_id:
                    d.setdefault("notes", [])
                    d["notes"] = (d["notes"] or [])[-4:] + [{
                        "t": datetime.now(timezone.utc).replace(tzinfo=None).isoformat(),
                        "msg": note,
                    }]
                    break
            project.directions = directions
            flag_modified(project, "directions")
            session.add(project)
        elif project:
            msg = ProjectMessage(
                project_id=task.project_id,
                message_type=MessageType.NOTE,
                from_type="agent",
                from_task_id=task.id,
                content=note,
                context={"source": "research_zero_children", "direction_id": str(direction_id)},
            )
            session.add(msg)

        await session.commit()
        logger.info(f"[RESEARCH] Task {task.id}: 0 children created, auto-completed (direction: {str(direction_id)[:8]})")
    else:
        task.status = TaskStatus.EVALUATING
        task.latest_status_message = status_msg
        task.context = {**(task.context or {}), "research_branch": research_branch, "original_base": original_base}
        session.add(task)
        await session.commit()

    logger.info(
        f"[RESEARCH] Task {task.id}: {len(findings)} raw findings, "
        f"{skipped_duplicates} deduped, {len(child_tasks)} tasks created "
        f"(direction: {str(direction_id)[:8]})"
    )
    await broadcast_to_project(str(task.project_id), "autonomous_activity",
                               {"research_task_id": str(task.id), "tasks_created": len(child_tasks)})


async def _record_research_task_failure(task: Task, session: AsyncSession) -> None:
    """Record a FAILED research task in the direction's research_history.

    Closes a blind spot where research tasks that never reached _handle_research_completion
    (VM crash, provision failure, agent timeout, etc.) left no trace on the direction,
    so _check_direction_health could not observe the stalled cycle.

    Appends a minimal history entry tagged research_task_failed=True to the direction
    referenced in task.context.direction_id (falling back to the payload field).
    """
    from app.failure_classification import classify_failure  # noqa: PLC0415

    direction_id = (task.context or {}).get("direction_id") or task.first_payload.get("current_direction_id")
    if not direction_id:
        return
    project = await session.get(Project, task.project_id)
    if not project:
        return
    directions = list(project.directions or [])
    target = next((d for d in directions if d.get("id") == direction_id), None)
    if not target:
        return
    now_iso = _utcnow().isoformat()
    failure_category = classify_failure(task, task.latest_status_message or "")
    entry = {
        "timestamp": now_iso,
        "task_id": str(task.id),
        "tasks_created": 0,
        "tasks_failed": 0,
        "tasks_completed": 0,
        "prs_merged": 0,
        "summary": f"research task failed: {failure_category}",
        "finding_titles": [],
        "key_finding_descriptions": [],
        "research_task_failed": True,
        "failure_category": failure_category,
    }
    history = target.get("research_history", [])
    new_history = history + [entry]
    # Share the existing eviction/aggregate rule: keep last 10, aggregate older entries
    if len(new_history) > 10:
        target["history_aggregate"] = _fold_evicted_into_aggregate(
            target.get("history_aggregate"), new_history[:-10]
        )
    target["research_history"] = new_history[-10:]
    target["last_researched_at"] = now_iso
    project.directions = directions
    flag_modified(project, "directions")
    session.add(project)
    logger.warning(
        f"[RESEARCH] Recorded failed research task {task.id} on direction "
        f"{direction_id} ({failure_category})"
    )

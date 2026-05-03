"""Research completion handler and supporting functions.

Extracted from app.task_completion to keep that module focused on plan/security/perf
completion handlers. Contains the research findings parser, context builder,
and the large _handle_research_completion handler.
"""
import asyncio
import json
import logging
import os
import random
import uuid
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm.attributes import flag_modified
from sqlmodel import select

from app.models import (
    Artifact,
    ArtifactType,
    Project,
    Task,
    TaskStatus,
)
from app.cochange_decomposition import (
    auto_decompose_findings_by_cochange,
    cluster_files_by_cochange as _cluster_files_by_cochange,  # noqa: F401 — re-exported for tests
)
from app.direction_metrics import (  # noqa: F401 — re-exported for callers/tests
    _HISTORY_AGG_DEFAULTS,
    _DECOMP_AGG_DEFAULTS,
    _fold_evicted_into_aggregate,
    _compute_momentum_factor,
    _compute_acceptance_rate,
    _compute_productivity_score,
    _update_cycle_history,
)
from app.failure_classification import classify_failure
from app.finding_deduplication import (
    build_dedup_state,
    deduplicate_findings,
    finding_hash as _finding_hash,  # noqa: F401 — re-exported for callers/tests
    is_duplicate as _is_duplicate,  # noqa: F401 — re-exported for callers/tests
)
from app.payload_schemas import validate_internal_payload
from app.playbook_learning import RECENT_SUCCESSES_TASK_TYPES, summarize_recent_successes
from app.research_context import (  # noqa: F401 — re-exported for callers/tests
    _parse_research_context,
    _parse_eviction_counts,
    _format_evicted_failed,
    _format_eviction_body,
    _build_research_context,
)
from app.research_direction_state import (  # noqa: F401 — re-exported for callers/tests
    _compute_saturation,
    _detect_dedup_deadlock,
    apply_post_spawn_state,
)
from app.scope_complexity import _composite_complexity_score
from app.shared_state import runtime_config as _runtime_config, DEFAULT_TASK_SCOPE, scope_to_effort
from app.spawn_calibration import (  # noqa: F401 — re-exported for tests/callers
    _SCOPE_ORDER,
    SCOPE_FILE_BUDGETS,
    MEDIUM_ANALYSIS_PLAN_FILE_LIMIT,
    _DEGRADED_SPAWN_CAP,
    _assess_spawn_health,
    _assess_project_spawn_health,
    _maybe_cold_start_upgrade_medium,
    _calibrate_scope,
    _poor_decomp_history,
    predict_spawn_success,
    check_scope_budget_mismatch,
)
from app.task_completion import (
    _extract_json_array,
    _ensure_branch,
    _validate_plan_dependencies,
    _log_file_overlaps,
    TASK_PRIORITY_MAP,
)
from app.time_utils import _utcnow

logger = logging.getLogger(__name__)

# Task types whose effort is driven by the calibrated scope rather than the
# type-level task_type_efforts config default. These are the "implementation"
# types where cost should scale with code-change size; specialized types like
# research/security_audit/qa keep their type-level effort regardless of scope.
_SCOPE_AWARE_EFFORT_TYPES = frozenset({"create", "fix", "refactor", "plan"})

# Minimum medium-scope exploration fraction. Prevents the adaptive formula from
# collapsing to 0 for a direction with no successful medium merges, which would
# permanently shut down medium exploration and prevent data collection.
MEDIUM_EXPLORATION_FLOOR = 0.05


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


async def _parse_research_insights(task_id: str) -> List[Dict[str, Any]]:
    """Read structured research insights from the agent's /tmp output file."""
    insights_path = f"/tmp/research_insights_{task_id}.json"

    def _read_sync() -> List[Dict[str, Any]]:
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

    return await asyncio.to_thread(_read_sync)


def _avg_medium_expansion_ratio(
    directions: List[Dict[str, Any]],
) -> Optional[float]:
    """Aggregate medium-scope expansion ratios across ``directions``'s last 3
    research_history entries. Prefers max(mean_merged, mean_failed) so the
    failed-task overflow signal biases conservatively. Returns None when no
    directions contribute any ratio data.
    """
    _merged: list[float] = []
    _failed: list[float] = []
    for _d in directions:
        for entry in (_d.get("research_history") or [])[-3:]:
            _m = (entry.get("scope_breakdown") or {}).get("medium", {})
            _r = _m.get("avg_expansion_ratio")
            if isinstance(_r, (int, float)):
                _merged.append(float(_r))
            _rf = _m.get("avg_expansion_ratio_failed")
            if isinstance(_rf, (int, float)):
                _failed.append(float(_rf))
    _candidates = []
    if _merged:
        _candidates.append(sum(_merged) / len(_merged))
    if _failed:
        _candidates.append(sum(_failed) / len(_failed))
    return max(_candidates) if _candidates else None


def _compute_prompt_telemetry(
    full_prompt: str,
    related_files: List[str],
    scope: str,
) -> Dict[str, Any]:
    """Lightweight prompt characteristics for outcome correlation.

    Stored in task.context['prompt_telemetry'] at spawn time and aggregated by
    direction_outcomes to surface patterns like "prompts mentioning tests merge
    at higher rates than those that don't".
    """
    import re  # noqa: PLC0415
    text = full_prompt or ""
    budget = SCOPE_FILE_BUDGETS.get(scope, {}).get("total_files_max") or 0
    n_related = len(related_files or [])
    return {
        "char_count": len(text),
        "related_file_count": n_related,
        "mentions_tests": bool(re.search(r"\btest", text, re.IGNORECASE)),
        "mentions_symbols": bool(re.search(r"\b[a-z][a-zA-Z0-9_]*[a-z][A-Z]\w*\b|\b[A-Z][a-zA-Z0-9_]*[a-z]\w*\b\(|\b[a-z_][a-z0-9_]+\(", text)),
        "files_to_budget_ratio": round(n_related / budget, 3) if budget > 0 else None,
    }


def _outcome_complexity_threshold(
    direction: Optional[Dict[str, Any]],
) -> Optional[float]:
    """Derive the escalation threshold for medium-scope findings from the
    direction's recent avg_complexity_merged / avg_complexity_failed observations
    (last 3 research_history entries, 'medium' scope_breakdown).

    Returns midpoint(merged, failed) when both exist, avg_failed when only failed
    exists, or None when no data is available.
    """
    if not direction:
        return None
    merged_vals: list[float] = []
    failed_vals: list[float] = []
    for entry in (direction.get("research_history") or [])[-3:]:
        med = (entry.get("scope_breakdown") or {}).get("medium", {}) or {}
        m = med.get("avg_complexity_merged")
        f = med.get("avg_complexity_failed")
        if isinstance(m, (int, float)):
            merged_vals.append(float(m))
        if isinstance(f, (int, float)):
            failed_vals.append(float(f))
    avg_merged = sum(merged_vals) / len(merged_vals) if merged_vals else None
    avg_failed = sum(failed_vals) / len(failed_vals) if failed_vals else None
    if avg_merged is not None and avg_failed is not None:
        return (avg_merged + avg_failed) / 2.0
    if avg_failed is not None:
        return avg_failed
    return None


def _cochange_decomposition_suppressed(
    direction: Optional[Dict[str, Any]],
    min_sub_findings: int = 3,
    merge_rate_threshold: float = 0.4,
) -> bool:
    """Return True when cochange auto-decomposition should be skipped for this
    direction because historical sub-finding merge rate is poor.

    Aggregates ``cochange_decomposition`` entries across the direction's
    ``research_history``. When cumulative sub_findings_total >= min_sub_findings
    and merge rate < merge_rate_threshold, callers should skip auto-splitting
    to avoid fragmenting findings that empirically don't benefit from it.
    """
    if not direction:
        return False
    total = 0
    merged = 0
    for entry in (direction.get("research_history") or []):
        cd = entry.get("cochange_decomposition") or {}
        total += cd.get("sub_findings_total", 0) or 0
        merged += cd.get("sub_findings_merged", 0) or 0
    if total < min_sub_findings:
        return False
    return (merged / total) < merge_rate_threshold


def _calibrate_finding_scope(
    finding: Dict[str, Any],
    scope: str,
    direction: Optional[Dict[str, Any]],
    cochange_index: Optional[Dict[str, Any]],
    all_directions: Optional[List[Dict[str, Any]]] = None,
) -> tuple[str, Optional[str]]:
    """Apply pre-spawn scope-upgrade heuristics (file count, cross-module span,
    expansion-ratio prediction, composite complexity).

    Returns (new_scope, scope_calibrated_from). scope_calibrated_from is None
    when no heuristic fired, so callers can preserve any prior calibration tag.
    Mutates ``finding`` to record ``_complexity_score`` / ``_complexity_signals``
    when the composite-complexity check runs.
    """
    title = finding.get("title", "")
    scope_calibrated_from: Optional[str] = None

    if scope == "small":
        _heuristic_files = finding.get("related_files") or []
        if len(_heuristic_files) >= 4:
            logger.info(
                "[RESEARCH] Upgrading scope small→medium for '%s' "
                "(file-count heuristic: %d related_files >= 4)",
                title, len(_heuristic_files),
            )
            scope = "medium"
            scope_calibrated_from = "small"

    if scope == "medium":
        _heuristic_files = finding.get("related_files") or []
        if len(_heuristic_files) >= 8:
            logger.info(
                "[RESEARCH] Upgrading scope medium→large for '%s' "
                "(file-count heuristic: %d related_files >= 8)",
                title, len(_heuristic_files),
            )
            scope = "large"
            scope_calibrated_from = "medium"
        elif len({f.split("/", 1)[0] for f in _heuristic_files if "/" in f}) >= 3:
            # Cross-module heuristic: related_files span 3+ top-level directories
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
            # Predictive expansion-ratio check: historical avg_expansion_ratio
            # (actual files touched / expected related_files) multiplied by this finding's
            # related_files predicts how many files the task will actually touch. If that
            # exceeds the medium overflow threshold (10 in SCOPE_OVERFLOW_THRESHOLDS), route
            # to large for plan decomposition before burning 5-10 minutes on a task that
            # will almost certainly scope-overflow during implementation.
            _avg_ratio = (
                _avg_medium_expansion_ratio([direction]) if direction else None
            )
            _ratio_source = "direction"
            if _avg_ratio is None and all_directions:
                _avg_ratio = _avg_medium_expansion_ratio(all_directions)
                _ratio_source = "project"
            if _avg_ratio is None:
                _pipeline_ratio = (
                    _runtime_config.get("pipeline_scope_expansion_ratios") or {}
                ).get("medium")
                if isinstance(_pipeline_ratio, (int, float)):
                    _avg_ratio = float(_pipeline_ratio)
                    _ratio_source = "pipeline"
            if _avg_ratio is None:
                # Conservative prior for brand-new projects with no expansion-ratio
                # data anywhere — still applies the safety net rather than skipping.
                _avg_ratio = 2.0
                _ratio_source = "default"
            _predicted_files = len(_heuristic_files) * _avg_ratio
            if _predicted_files > 10:
                logger.info(
                    "[RESEARCH] Upgrading scope medium→large for '%s' "
                    "(expansion-ratio prediction [%s]: %d related_files × %.2f avg_ratio "
                    "= %.1f predicted files > 10 threshold)",
                    title, _ratio_source, len(_heuristic_files), _avg_ratio, _predicted_files,
                )
                scope = "large"
                scope_calibrated_from = "medium"
        # Composite complexity score: combines file count, historical expansion,
        # cochange cluster density, and cross-module span into a single merge-
        # probability estimate. Upgrades when score indicates low P(merge) even if
        # individual binary checks above let the task through.
        if scope == "medium":
            # Unconditional escalation: if pipeline-wide medium merge rate has
            # collapsed to at-or-below the exploration floor, medium scope is
            # proven unviable system-wide. Skip the composite-complexity check
            # entirely and route to large for plan decomposition.
            _pipeline_medium_rate = (
                _runtime_config.get("pipeline_scope_merge_rates") or {}
            ).get("medium")
            if (
                isinstance(_pipeline_medium_rate, (int, float))
                and float(_pipeline_medium_rate) <= MEDIUM_EXPLORATION_FLOOR
            ):
                logger.info(
                    "[RESEARCH] Upgrading scope medium→large for '%s' "
                    "(pipeline medium merge rate %.3f <= exploration floor %.3f — "
                    "unconditional escalation)",
                    title, float(_pipeline_medium_rate), MEDIUM_EXPLORATION_FLOOR,
                )
                return "large", "medium:pipeline_floor"
        if scope == "medium":
            _cc_score, _cc_signals = _composite_complexity_score(
                finding.get("related_files") or [], direction, cochange_index,
            )
            finding["_complexity_score"] = round(_cc_score, 3)
            finding["_complexity_signals"] = _cc_signals
            # Adaptive escalation threshold. Base preference order:
            #   1. Direction outcome-derived midpoint of avg_complexity_merged /
            #      avg_complexity_failed (last 3 cycles) — empirically calibrated.
            #   2. Fixed 0.45 baseline when no outcome data exists yet.
            # The pipeline-wide medium merge-rate tightening is layered on top as
            # a secondary adjustment so poor pipeline-wide performance still
            # raises the bar during degradation regardless of the base source.
            _outcome_base = _outcome_complexity_threshold(direction)
            _cc_threshold = _outcome_base if _outcome_base is not None else 0.45
            _pipeline_medium = (
                _runtime_config.get("pipeline_scope_merge_rates") or {}
            ).get("medium")
            if isinstance(_pipeline_medium, (int, float)) and _pipeline_medium < 0.4:
                _cc_threshold = min(
                    0.75, _cc_threshold + (0.4 - float(_pipeline_medium)) * 0.5
                )
            if _cc_score < _cc_threshold:
                logger.info(
                    "[RESEARCH] Upgrading scope medium→large for '%s' "
                    "(composite complexity %.2f < %.2f, pipeline_medium=%s, signals=%s)",
                    title, _cc_score, _cc_threshold,
                    f"{_pipeline_medium:.2f}" if isinstance(_pipeline_medium, (int, float)) else "n/a",
                    _cc_signals,
                )
                scope = "large"
                scope_calibrated_from = "medium"
    return scope, scope_calibrated_from


def _build_preventive_context(
    finding: Dict[str, Any],
    directions_by_id: Dict[str, Dict[str, Any]],
    project_anomalies: Optional[Dict[str, Dict[str, Any]]],
) -> Optional[Dict[str, Any]]:
    """Produce a concise preventive warning when a finding's related_files
    overlap with recently-diagnosed failure hotspots or active anomaly patterns.

    Returns None when no overlap exists, so callers can skip injection.
    """
    related_files = [f for f in (finding.get("related_files") or []) if isinstance(f, str)]
    if not related_files:
        return None
    related_set = set(related_files)
    cutoff = _utcnow() - timedelta(days=14)

    hotspot: Optional[Dict[str, Any]] = None
    for d in directions_by_id.values():
        affected = [a for a in (d.get("diagnosed_affected_files") or []) if isinstance(a, str)]
        overlap = sorted(related_set.intersection(affected))
        if not overlap:
            continue
        completed_at = d.get("diagnosis_completed_at")
        if not isinstance(completed_at, str):
            continue
        try:
            ts = datetime.fromisoformat(completed_at.replace("Z", "+00:00"))
            if ts.tzinfo is not None:
                ts = ts.replace(tzinfo=None)
        except ValueError:
            continue
        if ts < cutoff:
            continue
        hotspot = {
            "overlap_files": overlap,
            "summary": (d.get("diagnosis_summary") or "").strip()[:220],
            "fingerprint": d.get("last_remediation_fingerprint"),
        }
        break

    anomaly_match: Optional[Dict[str, Any]] = None
    if project_anomalies:
        modules = {f.split("/", 1)[0].lower() for f in related_files if "/" in f}
        for key, info in project_anomalies.items():
            details = (info.get("latest_details") or "").lower()
            if not details:
                continue
            if any(m and m in details for m in modules):
                anomaly_match = {"key": key, "details": info.get("latest_details") or ""}
                break

    if not hotspot and not anomaly_match:
        return None

    parts: list[str] = []
    if hotspot:
        files_str = ", ".join(hotspot["overlap_files"][:3])
        fp_str = f" (fingerprint {hotspot['fingerprint']})" if hotspot["fingerprint"] else ""
        root = hotspot["summary"] or "see direction diagnosis"
        parts.append(
            f"Files {files_str} were recently involved in a recurring failure{fp_str}. "
            f"Root cause: {root}."
        )
    if anomaly_match:
        parts.append(
            f"Active cross-direction anomaly {anomaly_match['key']}: "
            f"{anomaly_match['details'][:140]}"
        )
    parts.append("Ensure your changes don't reintroduce this pattern.")
    warning = " ".join(parts)[:500]
    return {
        "warning": warning,
        "fingerprint": hotspot["fingerprint"] if hotspot else None,
        "overlap_files": hotspot["overlap_files"] if hotspot else [],
        "anomaly_key": anomaly_match["key"] if anomaly_match else None,
    }


async def _handle_research_completion(task: Task, logs: str, session: AsyncSession):
    """Parse research findings and create tasks from them."""
    # Increment _research_spawn_count on completion (variable name is legacy;
    # see autonomous_research.py:837 — semantic is "completed research cycles
    # since last strategic refresh"). The strategic_research_frequency gate
    # uses this to decide when to fire a strategic_review. Incrementing here
    # rather than at spawn time prevents the cascade observed on 2026-05-03
    # where one strategic_review spawned 4 research tasks AT ONCE and
    # immediately tripped the modulo-6 gate for the next strategic_review.
    from sqlalchemy.orm.attributes import flag_modified
    project_obj = await session.get(Project, task.project_id)
    if project_obj is not None:
        new_count = ((project_obj.vision or {}).get("_research_spawn_count") or 0) + 1
        project_obj.vision = {**(project_obj.vision or {}), "_research_spawn_count": new_count}
        flag_modified(project_obj, "vision")
        session.add(project_obj)

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
    insights = await _parse_research_insights(str(task.id))
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
        # Discard findings older than DEFERRED_FINDING_MAX_AGE to prevent stale
        # entries from blocking fresh findings (priority inversion) and to bound
        # the deferred list during extended pipeline degradation.
        _stale_cutoff = _utcnow() - timedelta(hours=48)
        _fresh_actionable: list[dict] = []
        _stale_discarded: list[dict] = []
        for f in prior_actionable:
            _stamp = f.get("originally_researched_at")
            _stamp_dt = None
            if isinstance(_stamp, str):
                try:
                    from datetime import datetime as _dt  # noqa: PLC0415
                    _stamp_dt = _dt.fromisoformat(_stamp.replace("Z", "+00:00"))
                    if _stamp_dt.tzinfo is not None:
                        _stamp_dt = _stamp_dt.replace(tzinfo=None)
                except ValueError:
                    _stamp_dt = None
            if _stamp_dt is not None and _stamp_dt < _stale_cutoff:
                _stale_discarded.append(f)
            else:
                _fresh_actionable.append(f)
        if _stale_discarded:
            logger.warning(
                f"[RESEARCH] Discarding {len(_stale_discarded)} stale deferred finding(s) "
                f"(older than 48h) from task {task.id}: "
                f"titles={[f.get('title') or f.get('description', '')[:60] for f in _stale_discarded]}"
            )
        prior_actionable = _fresh_actionable
        # Decay: discard findings that have been deferred repeatedly for outcome-blind
        # reasons (ultra_low_confidence / degradation_gate). Retrying them unchanged
        # wastes a findings slot each cycle. batch_cap deferrals are pure capacity
        # constraints and are not decayed.
        _decayed: list[dict] = []
        _kept: list[dict] = []
        for f in prior_actionable:
            _count = int(f.get("deferred_count", 0) or 0)
            _reason = f.get("deferred_reason")
            if _reason == "ultra_low_confidence" and _count >= 4:
                _decayed.append(f)
            elif _reason == "degradation_gate" and _count >= 3:
                _decayed.append(f)
            else:
                _kept.append(f)
        if _decayed:
            logger.warning(
                f"[RESEARCH] Discarding {len(_decayed)} repeatedly-deferred finding(s) "
                f"from task {task.id}: "
                f"titles={[f.get('title') or f.get('description', '')[:60] for f in _decayed]}"
            )
        prior_actionable = _kept
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
    # Select the top _effective_cap findings by priority so high-priority
    # findings survive capacity-limited cycles regardless of the agent's
    # output ordering. We preserve agent order among survivors and remap
    # each surviving finding's depends_on_indices to the compacted positions
    # so _validate_plan_dependencies doesn't see spurious self-cycles and
    # UUID resolution wires deps to the correct child tasks.
    _BATCH_PRIORITY_ORDER = {"high": 0, "medium": 1, "low": 2}

    # Cap findings to prevent runaway task creation and backlog build-up.
    # Excess findings are deferred to the next cycle (via task.context['deferred_findings'])
    # rather than discarded, so capacity-constrained cycles don't permanently lose work.
    batch_cap_applied_count = max(0, len(findings) - _effective_cap)
    _batch_cap_deferred: list[dict] = []
    if len(findings) > _effective_cap:
        _ranked = sorted(
            enumerate(findings),
            key=lambda pair: _BATCH_PRIORITY_ORDER.get(pair[1].get("priority", "medium"), 1),
        )
        _kept_orig_indices = sorted(i for i, _ in _ranked[:_effective_cap])
        _kept_set = set(_kept_orig_indices)
        _batch_cap_deferred = [f for i, f in enumerate(findings) if i not in _kept_set]
        _old_to_new = {old: new for new, old in enumerate(_kept_orig_indices)}
        findings = [findings[i] for i in _kept_orig_indices]
        for _f in findings:
            _deps = _f.get("depends_on_indices")
            if _deps:
                _f["depends_on_indices"] = [_old_to_new[d] for d in _deps if d in _old_to_new]
        logger.info(
            "[RESEARCH] Batch cap (%d) exceeded — deferring %d lower-priority finding(s) to next cycle",
            _effective_cap, len(_batch_cap_deferred),
        )

    original_base = (task.first_payload).get("base_branch", "main")
    short_id = str(task.id).replace("-", "")[:8]
    research_branch = f"research/{short_id}"

    # Create staging branch for this research batch.
    # Round 4 W1 (audit C2): _ensure_branch now returns Task | None.
    # On success, returns the freshly-locked Task (caller mutates under
    # the new lock). On failure or peer-mutation defer, returns None.
    relocked = await _ensure_branch(task, research_branch, original_base, "RESEARCH", session)
    if relocked is None:
        return
    task = relocked

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

    recent_finding_hashes, failed_hashes, prior_failure_by_hash = build_dedup_state(
        recent_tasks_for_dedup, failed_parent_ids,
    )

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

    # Sort findings by priority (high → medium → low) and apply the hash,
    # word-overlap, and file-overlap dedup stages. The orchestrator dispatches
    # by (status, priority, created_at), so higher-priority tasks naturally
    # run first without needing artificial wave dependencies.
    _dedup_outcome = deduplicate_findings(
        findings,
        task.context,
        recent_finding_hashes,
        failed_hashes,
        existing_tasks_with_dirs,
        open_pr_files,
    )
    deduplicated = _dedup_outcome.deduplicated
    hash_dedup_count = _dedup_outcome.hash_dedup_count
    word_overlap_count = _dedup_outcome.word_overlap_count
    file_overlap_count = _dedup_outcome.file_overlap_count
    filtered_findings = _dedup_outcome.filtered
    type_filtered_count = 0
    empty_prompt_count = 0

    # Assess pipeline health once before spawning implementation tasks.
    # Metadata findings (design_doc_observation, direction_update) are processed
    # regardless; only VM-consuming implementation tasks are capped.
    _pipeline_degraded, _health_reason = await _assess_project_spawn_health(
        session, task.project_id, _effective_cap,
    )
    _impl_spawned = 0
    _deferred_findings: list[dict] = []
    _low_conf_skipped_findings: list[dict] = []
    _scope_skip_counts: dict[str, int] = {}

    # Create all child tasks — no wave dependencies, priority ordering handles dispatch order
    child_tasks = []
    finding_indices = []  # Maps child_tasks index → findings array index
    # Parallel flag: True when the child came from an auto-decomposed sub-finding.
    # Used to chain consecutive siblings of the same parent finding via depends_on
    # (so they execute serially, each agent seeing prior siblings' actually-merged
    # work in its base branch instead of partial scaffolding from in-flight peers).
    _auto_decomposed_flags: list[bool] = []

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
    _suppressed_decomp_dirs = {
        str(_d.get("id"))
        for _d in _directions_by_id.values()
        if _cochange_decomposition_suppressed(_d)
    }
    if _suppressed_decomp_dirs:
        _to_decomp: list = []
        _skip_decomp: list = []
        for _item in deduplicated:
            _fid = str(_item[1].get("direction_id") or _parent_direction_id or "")
            if _fid in _suppressed_decomp_dirs:
                _skip_decomp.append(_item)
            else:
                _to_decomp.append(_item)
        if _skip_decomp:
            logger.info(
                "[RESEARCH] Suppressing cochange decomposition for %d finding(s) "
                "in %d direction(s) with poor historical sub-finding merge rate",
                len(_skip_decomp), len(_suppressed_decomp_dirs),
            )
        deduplicated = auto_decompose_findings_by_cochange(
            _to_decomp,
            _cochange_index_for_decomp,
            task.context,
            _finding_hash,
        ) + _skip_decomp
    else:
        deduplicated = auto_decompose_findings_by_cochange(
            deduplicated,
            _cochange_index_for_decomp,
            task.context,
            _finding_hash,
        )
    # Track auto-decomposition outcomes per direction so direction_outcomes
    # can correlate decomposition success with merge rates (parallels
    # plan-parent decomposition_quality tracking).
    _decomp_stats_by_dir: dict[str, dict] = {}
    for _oi, _f, _ in deduplicated:
        if not _f.get("auto_decomposed"):
            continue
        _did = str(_f.get("direction_id") or _parent_direction_id or "")
        if not _did:
            continue
        _s = _decomp_stats_by_dir.setdefault(
            _did, {"sub_findings_produced": 0, "_orig_ids": set()}
        )
        _s["sub_findings_produced"] += 1
        _s["_orig_ids"].add(_oi)
    if _decomp_stats_by_dir:
        _ctx = dict(task.context or {})
        _ctx["cochange_decomposition_stats"] = {
            _did: {
                "medium_findings_split": len(_s["_orig_ids"]),
                "sub_findings_produced": _s["sub_findings_produced"],
            }
            for _did, _s in _decomp_stats_by_dir.items()
        }
        task.context = _ctx
        flag_modified(task, "context")

    for orig_i, finding, fhash in deduplicated:
        scope = finding.get("scope", DEFAULT_TASK_SCOPE)
        risk = finding.get("risk", "low")
        title = finding.get("title", f"Research finding #{orig_i+1}")
        prompt = finding.get("prompt", finding.get("description", ""))

        # Pre-spawn scope-budget gate: catch obvious file-count/scope mismatch before
        # an agent is dispatched. Runs before history-based calibration so all
        # subsequent steps work on the corrected scope.
        _sbg_files = finding.get("related_files") or []
        scope, _sbg_cal_from, _scope_budget_warning, _budget_requires_approval = (
            check_scope_budget_mismatch(scope, _sbg_files)
        )

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
        scope_calibrated_from: str | None = finding.get("scope_calibrated_from") or _sbg_cal_from
        _history_calibration_fired = False
        if not (scope_calibrated_from or (task.context or {}).get("scope_calibrated_from")):
            _dir_id = finding.get("direction_id") or _parent_direction_id
            _direction = _directions_by_id.get(str(_dir_id)) if _dir_id else None
            if _direction:
                # Determine preliminary task_type from finding_type so success-pattern
                # evidence in _feedback_scope_override can be used during calibration.
                # The scope-dependent branch (plan/create) is resolved using the
                # pre-calibration scope, which is the proposed scope at this point.
                if finding_type == "research":
                    _prelim_task_type: str | None = "research"
                elif finding_type in ("security_audit", "qa", "deps", "docs", "migrate", "perf"):
                    _prelim_task_type = finding_type
                elif finding_type == "refactor":
                    _prelim_task_type = "refactor"
                elif scope == "large":
                    _prelim_task_type = "plan"
                else:
                    _prelim_task_type = "create"
                scope, scope_calibrated_from = _calibrate_scope(scope, _direction, _scope_cal_project, _prelim_task_type)
                _history_calibration_fired = scope_calibrated_from is not None

        # Pre-spawn scope-upgrade heuristics (file count, cross-module span,
        # expansion-ratio prediction, composite complexity). Extracted into
        # _calibrate_finding_scope so the heuristics are independently testable.
        # Skip when _calibrate_scope just fired: historical merge-rate data is
        # a stronger signal than file-count heuristics, so a freshly-applied
        # downgrade (e.g. medium→small) should not be reversed by the small→medium
        # heuristic that fires on related_files >= 4.
        # Also skip heuristics for auto-decomposed findings AT SMALL SCOPE: the
        # decomposition is a deliberate small-scope decision that must not be
        # reversed by the small→medium related_files>=4 heuristic (decomposed
        # clusters often span 3-5 files by design). Medium-scope decomposed
        # sub-findings still need the medium→large safety gates (file count,
        # cross-module span, expansion ratio, composite complexity).
        _skip_auto_decomp = finding.get("auto_decomposed") and scope == "small"
        if not _history_calibration_fired and not _skip_auto_decomp:
            _heur_dir_id = finding.get("direction_id") or _parent_direction_id
            _heur_direction = _directions_by_id.get(str(_heur_dir_id)) if _heur_dir_id else None
            scope, _heur_calibrated_from = _calibrate_finding_scope(
                finding, scope, _heur_direction, _cochange_index_for_decomp,
                all_directions=list(_directions_by_id.values()),
            )
            if _heur_calibrated_from is not None:
                scope_calibrated_from = _heur_calibrated_from

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

        # Apply failure-feedback mitigation: downscope when rejection patterns are high
        # for this direction, and bias toward smaller scopes with proven success patterns.
        _mit_dir_id = finding.get("direction_id") or _parent_direction_id
        _mit_direction = _directions_by_id.get(str(_mit_dir_id)) if _mit_dir_id else None
        if _mit_direction and _scope_cal_project:
            from app.strategic_spawning import _build_failure_feedback_mitigation  # noqa: PLC0415
            scope, _, _ = _build_failure_feedback_mitigation(
                _mit_direction, scope, task_type, _scope_cal_project.design_doc
            )

        # Pre-spawn approval removed: the review pipeline handles quality gates.
        # Only final merge to main requires human approval (critical files gate).
        # Exception: large-scope findings that far exceed the large-scope file budget
        # require operator confirmation before dispatching (scope_budget_mismatch gate).
        needs_approval = _budget_requires_approval

        direction_id = finding.get("direction_id")
        # Ignore finding.model: it's a legacy anthropic-only hint that
        # silently anchored children to a provider regardless of the
        # operator's category_routing. Model resolution is the
        # orchestrator's job (via provider_preference + effort matrix).
        # Operator-level pins via runtime_config.task_type_models still
        # flow through.
        finding_model = _runtime_config.get("task_type_models", {}).get(task_type)
        _effort_rank = {"max": 4, "high": 3, "medium": 2, "low": 1}
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
        _pred_p = predict_spawn_success(
            scope, _pred_direction or {}, _scope_cal_project, related_files=related_files,
            complexity_score=finding.get("_complexity_score"),
        )
        _low_confidence = _pred_p < _confidence_threshold
        if _low_confidence:
            logger.info(
                "[RESEARCH] Low-confidence spawn '%s' (scope=%s, type=%s): P(success)=%.2f < threshold=%.2f",
                title, scope, task_type, _pred_p, _confidence_threshold,
            )
        # Hard skip gate: ultra-low-confidence medium findings are dropped
        # entirely rather than spawned, escalated, or explored. Runs BEFORE
        # the exploration lottery so exploration cannot override the gate.
        # Exception: data-starved directions (no medium scope_breakdown in the
        # last 3 cycles) fall through to the exploration lottery, which has
        # its own data-starved bypass. Otherwise weak pipeline-wide fallback
        # signals create a death spiral where medium is never attempted.
        _hard_skip_threshold = _runtime_config.get("medium_hard_skip_threshold", 0.10)
        _hard_skip_data_starved = False
        if scope == "medium":
            _hard_skip_data_starved = True
            for _entry in ((_pred_direction or {}).get("research_history") or [])[-3:]:
                if (_entry.get("scope_breakdown") or {}).get("medium"):
                    _hard_skip_data_starved = False
                    break
        if (
            scope == "medium"
            and not _hard_skip_data_starved
            and isinstance(_hard_skip_threshold, (int, float))
            and _pred_p < float(_hard_skip_threshold)
        ):
            logger.info(
                "[RESEARCH] Hard-skipping ultra-low-confidence finding '%s' "
                "(scope=%s): P(success)=%.2f < hard_skip=%.2f",
                title, scope, _pred_p, float(_hard_skip_threshold),
            )
            _low_conf_skipped_findings.append(finding)
            _scope_skip_counts[scope] = _scope_skip_counts.get(scope, 0) + 1
            continue

        _medium_exploration = False
        if _low_confidence and scope == "medium" and not _poor_decomp_history(_pred_direction or {}):
            # Exploration fraction: let a configurable share of medium findings bypass the
            # confidence gate so the pipeline can accumulate real medium-scope conversion
            # data. Without this, medium rates stay low because no medium tasks ever run,
            # which keeps P(success) below threshold in a self-reinforcing loop.
            _explore_frac = _runtime_config.get("medium_exploration_fraction", 0.2)
            _explore_frac_base = _explore_frac
            # Adapt the exploration fraction from this direction's observed
            # medium-scope exploration outcomes. Directions where exploration
            # consistently merges get a boosted fraction so they accumulate
            # medium data faster; directions where exploration consistently
            # fails get a reduced fraction so they waste fewer attempts.
            _exp_created = 0
            _exp_merged = 0
            for _entry in ((_pred_direction or {}).get("research_history") or [])[-5:]:
                _med = (_entry.get("scope_breakdown") or {}).get("medium") or {}
                _exp_created += int(_med.get("exploration_created") or 0)
                _exp_merged += int(_med.get("exploration_merged") or 0)
            if _exp_created > 0 and isinstance(_explore_frac, (int, float)):
                _exp_rate = _exp_merged / _exp_created
                _base = float(_explore_frac)
                if _exp_rate >= 0.5:
                    _explore_frac = min(1.0, _base * 2.0)
                else:
                    _explore_frac = _base * (_exp_rate / 0.5)
                logger.info(
                    "[RESEARCH] Adaptive exploration fraction for direction %s: rate=%.2f (%d/%d), frac=%.3f",
                    _pred_dir_id, _exp_rate, _exp_merged, _exp_created, float(_explore_frac),
                )
            elif _exp_created == 0 and isinstance(_explore_frac, (int, float)):
                # No direction-level exploration history: use pipeline-wide medium
                # merge rate as a prior so new directions don't burn attempts at the
                # optimistic fixed default when the pipeline is underperforming.
                # Exception: if the direction has ZERO medium-scope outcomes in its
                # recent research_history (no medium key in scope_breakdown across
                # the last 3 cycles), it is data-starved — applying the pipeline
                # discount creates a self-reinforcing loop where medium is never
                # attempted, no direction-specific data is collected, and the
                # fraction stays suppressed forever. Use the base fraction so the
                # direction gets a fair chance to collect its own medium data.
                _has_medium_history = False
                for _entry in ((_pred_direction or {}).get("research_history") or [])[-3:]:
                    if (_entry.get("scope_breakdown") or {}).get("medium"):
                        _has_medium_history = True
                        break
                _pipeline_rates = _runtime_config.get("pipeline_scope_merge_rates") or {}
                _pipeline_medium = _pipeline_rates.get("medium")
                if (
                    _has_medium_history
                    and isinstance(_pipeline_medium, (int, float))
                    and _pipeline_medium < 0.5
                ):
                    _base = float(_explore_frac)
                    _explore_frac = _base * (float(_pipeline_medium) / 0.5)
                    logger.info(
                        "[RESEARCH] Pipeline-prior exploration fraction for direction %s: "
                        "pipeline_medium=%.2f, frac=%.3f",
                        _pred_dir_id, float(_pipeline_medium), float(_explore_frac),
                    )
                elif not _has_medium_history:
                    logger.info(
                        "[RESEARCH] Data-starved direction %s: no medium history, "
                        "using base exploration fraction=%.3f (pipeline prior skipped)",
                        _pred_dir_id, float(_explore_frac),
                    )
            # Suppress exploration when medium→large escalations in this direction
            # have also been failing: if both direct medium execution and the
            # escalation fallback merge poorly, spawning more medium findings just
            # feeds a losing pipeline regardless of exploration-merge stats.
            _esc_attempted = 0
            _esc_merged = 0
            for _entry in ((_pred_direction or {}).get("research_history") or [])[-3:]:
                _esc = (_entry.get("escalation_outcomes") or {}).get("medium_to_large") or {}
                _esc_attempted += int(_esc.get("attempted") or 0)
                _esc_merged += int(_esc.get("merged") or 0)
            if (
                _esc_attempted >= 3
                and isinstance(_explore_frac, (int, float))
                and (_esc_merged / _esc_attempted) < 0.30
            ):
                _explore_frac = float(_explore_frac) * 0.5
                logger.info(
                    "[RESEARCH] Suppressing exploration for direction %s: medium→large "
                    "escalation rate=%.2f (%d/%d) < 0.30, frac=%.3f",
                    _pred_dir_id, _esc_merged / _esc_attempted, _esc_merged,
                    _esc_attempted, float(_explore_frac),
                )
            # Per-finding modulation: structurally complex findings (low
            # composite complexity score) are more likely to overflow at medium
            # scope, so halve their exploration fraction to bias the lottery
            # away from them without affecting simpler findings in the same
            # direction. Applied after direction-level adaptations but before
            # the floor so the floor can still rescue over-suppressed fractions.
            _cs = finding.get("_complexity_score")
            if (
                isinstance(_cs, (int, float))
                and float(_cs) < 0.35
                and isinstance(_explore_frac, (int, float))
            ):
                _explore_frac = float(_explore_frac) * 0.5
                logger.info(
                    "[RESEARCH] Complexity-modulated exploration for '%s': "
                    "complexity_score=%.2f < 0.35, frac=%.3f",
                    title, float(_cs), float(_explore_frac),
                )
            # Enforce a minimum exploration fraction so directions with zero
            # observed exploration merges still occasionally attempt medium scope —
            # otherwise the fraction locks at 0 and no new data is ever collected.
            # Skip the floor when the operator has explicitly disabled exploration
            # by setting the base fraction to 0.
            # Once a direction has enough exploration data to make an informed
            # decision (3+ attempts with 0 merges), skip the floor — the data
            # collection purpose is fulfilled and continued exploration is waste.
            _exploration_proven_unviable = _exp_created >= 3 and _exp_merged == 0
            # Pipeline-wide evidence of unviability: if the pipeline medium merge
            # rate is already at or below the floor, new directions shouldn't
            # independently re-discover that medium is unviable — treat the
            # pipeline signal as sufficient proof and skip the floor.
            _pipeline_rates_cfg = _runtime_config.get("pipeline_scope_merge_rates") or {}
            _pipeline_medium_cfg = _pipeline_rates_cfg.get("medium")
            _pipeline_proven_unviable = (
                isinstance(_pipeline_medium_cfg, (int, float))
                and float(_pipeline_medium_cfg) <= MEDIUM_EXPLORATION_FLOOR
            )
            if (
                isinstance(_explore_frac, (int, float))
                and isinstance(_explore_frac_base, (int, float))
                and float(_explore_frac_base) > 0
                and 0 <= float(_explore_frac) < MEDIUM_EXPLORATION_FLOOR
                and not _exploration_proven_unviable
                and not _pipeline_proven_unviable
            ):
                _explore_frac = MEDIUM_EXPLORATION_FLOOR
            if isinstance(_explore_frac, (int, float)) and _explore_frac > 0 and random.random() < float(_explore_frac):
                _medium_exploration = True
                logger.info("[RESEARCH] Exploration bypass keeping medium scope for '%s' (P=%.2f, explore_frac=%.2f)",
                            title, _pred_p, float(_explore_frac))
            else:
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
            # Tighten analysis plan file limit for medium-scope retries whose
            # prior attempt tripped a scope gate or touched ≥80% of the medium
            # file threshold. The agent reads _analysis_plan_file_limit and
            # overrides MEDIUM_ANALYSIS_PLAN_FILE_LIMIT (default 7) so the
            # retry plans a narrower change set.
            if scope == "medium":
                _prior_scope_gate = _prior_ctx.get("scope_gate_triggered")
                _prior_changed = _prior_ctx.get("_pr_changed_files") or []
                _prior_file_count = len(_prior_changed) if isinstance(_prior_changed, list) else 0
                _tight_triggers = {"scope_trim", "analysis_overflow", "post_review_overflow", "small_overflow"}
                if _prior_scope_gate in _tight_triggers or _prior_file_count >= 8:
                    prior_retry_ctx["_analysis_plan_file_limit"] = 5
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
        # Scope-aware effort override for implementation task types: the scope
        # (calibrated through history/heuristic/low-confidence/retry) drives the
        # baseline effort — small→low, medium→medium, large→high — replacing
        # the coarse type-level config default. Explicit finding_effort still
        # wins when it ranks higher.
        if task_type in _SCOPE_AWARE_EFFORT_TYPES:
            _scope_effort = scope_to_effort(scope)
            _finding_explicit = finding.get('effort')
            if _finding_explicit and _effort_rank.get(_finding_explicit, 0) > _effort_rank.get(_scope_effort, 0):
                finding_effort = _finding_explicit
            else:
                finding_effort = _scope_effort
            # Complexity-aware boost: complex tasks (low P(merge) complexity
            # score) get elevated effort so the agent has deeper inference to
            # navigate the complexity without requiring a scope upgrade.
            _cscore = finding.get("_complexity_score")
            if _cscore is not None and _cscore < 0.4 and _effort_rank.get(finding_effort, 0) < _effort_rank["high"]:
                finding_effort = "high"
        # Predicted file-budget utilization for medium-scope tasks: gives the
        # implementing agent early visibility into budget pressure before it
        # commits to a broad approach. Complements the review-pipeline's runtime
        # pressure system (which fires at 70%/90% after changes are made).
        _predicted_utilization: Optional[float] = None
        _file_budget_warning: Optional[str] = None
        if scope == "medium" and related_files:
            _util_ratio = (
                _avg_medium_expansion_ratio([_pred_direction]) if _pred_direction else None
            )
            if _util_ratio is None:
                _util_ratio = _avg_medium_expansion_ratio(list(_directions_by_id.values()))
            if _util_ratio is None:
                _util_ratio = 2.0
            _medium_budget = SCOPE_FILE_BUDGETS.get("medium", {}).get("total_files_max") or 10
            _predicted_utilization = round(
                len(related_files) * _util_ratio / _medium_budget, 3
            )
            if _predicted_utilization > 0.7:
                _file_budget_warning = (
                    f"Predicted file-budget utilization is {_predicted_utilization:.0%} of the "
                    f"medium-scope limit ({_medium_budget} files). Plan conservatively: prioritize "
                    f"the most critical files from related_files and avoid incidental edits that "
                    f"would push the change past the budget."
                )
        from app import shared_state as _ss_pc  # noqa: PLC0415
        _project_anomalies = _ss_pc.persistent_anomalies.get(str(task.project_id))
        _preventive = _build_preventive_context(
            finding, _directions_by_id, _project_anomalies,
        )

        # Surface 'what has compounded here' from prior playbook/success-* entries.
        # Additive to the existing scope-bias and design_doc playbook injection.
        if task_type in RECENT_SUCCESSES_TASK_TYPES and _scope_cal_project is not None:
            _recent = summarize_recent_successes(
                _scope_cal_project.design_doc or {},
                task_type,
                _pred_dir_id,
                scope,
            )
            if _recent:
                full_prompt = full_prompt + "\n\n" + _recent

        finding_payload = validate_internal_payload({
            "type": task_type,
            "prompt": full_prompt,
            "base_branch": research_branch,
            **(({'model': finding_model} if finding_model else {})),
            **(({'effort': finding_effort} if finding_effort else {})),
            **({"prompt_suffix": _combined_suffix} if _combined_suffix else {}),
        }, f"research_completion:finding:{task_type}")
        # Audit W1: previous_error_fingerprint is a column. Pull it out of
        # prior_retry_ctx so it lands on the column, not in JSONB context.
        _prev_fp_col = prior_retry_ctx.pop("_previous_error_fingerprint", None)
        new_task = Task(
            project_id=task.project_id,
            title=title,
            description=finding.get("description", prompt[:200]),
            parent_task_id=task.id,
            payloads=[finding_payload],
            previous_error_fingerprint=_prev_fp_col,
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
                **({"medium_exploration": True} if _medium_exploration else {}),
                **({"auto_decomposed": True} if finding.get("auto_decomposed") else {}),
                **({"_predicted_file_utilization": _predicted_utilization} if _predicted_utilization is not None else {}),
                **({"_file_budget_warning": _file_budget_warning} if _file_budget_warning else {}),
                **({"scope_budget_warning": _scope_budget_warning} if _scope_budget_warning else {}),
                **({"preventive_context": _preventive, "has_preventive_context": True} if _preventive else {}),
                "prompt_telemetry": _compute_prompt_telemetry(full_prompt, related_files, scope),
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
        _auto_decomposed_flags.append(bool(finding.get("auto_decomposed")))
        _impl_spawned += 1

    _now_iso = _utcnow().isoformat()

    def _stamp_deferral(f: dict, reason: str) -> dict:
        stamped = dict(f)
        stamped["deferred_count"] = int(stamped.get("deferred_count", 0)) + 1
        stamped["deferred_reason"] = reason
        stamped.setdefault("originally_researched_at", _now_iso)
        if not stamped.get("finding_hash"):
            stamped["finding_hash"] = _finding_hash(
                stamped.get("title", ""), stamped.get("description", "")
            )
        return stamped

    _all_deferred = (
        [_stamp_deferral(f, "degradation_gate") for f in _deferred_findings]
        + [_stamp_deferral(f, "batch_cap") for f in _batch_cap_deferred]
        + [
            {**_stamp_deferral(f, "ultra_low_confidence"), "skip_reason": "ultra_low_confidence"}
            for f in _low_conf_skipped_findings
        ]
    )
    if _all_deferred or _scope_skip_counts:
        ctx = dict(task.context or {})
        if _all_deferred:
            ctx["deferred_findings"] = _all_deferred
        if _scope_skip_counts:
            ctx["skipped_low_confidence_by_scope"] = _scope_skip_counts
        task.context = ctx
        flag_modified(task, "context")
        session.add(task)
        if _deferred_findings and _batch_cap_deferred:
            _reason = f"degradation gate ({_health_reason}) + batch cap"
        elif _deferred_findings:
            _reason = f"degradation gate ({_health_reason})"
        else:
            _reason = "batch cap"
        logger.warning(
            f"[RESEARCH] Deferred {len(_all_deferred)} findings ({_reason}); "
            f"spawning {_impl_spawned} tasks. "
            f"Stored in task.context['deferred_findings'] for next cycle."
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

    # Chain auto-decomposed siblings: when a finding is split into N sub-tasks
    # by cochange/path clustering, run them serially rather than in parallel.
    # Each sibling's base branch otherwise picks up partial sibling-merged
    # state, which confuses agents (see dcd6e840: cluster 3's agent saw
    # cluster 1's already-merged scaffolding in task_completion.py and edited
    # the wrong file). Sequential execution lets each agent work against the
    # actually-merged state of prior siblings.
    for ci in range(1, len(child_tasks)):
        if not (_auto_decomposed_flags[ci] and _auto_decomposed_flags[ci - 1]):
            continue
        if finding_indices[ci] != finding_indices[ci - 1]:
            continue
        prev_id = str(child_tasks[ci - 1].id)
        existing = list(child_tasks[ci].depends_on or [])
        if prev_id not in existing:
            existing.append(prev_id)
            child_tasks[ci].depends_on = existing
            session.add(child_tasks[ci])

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

    await apply_post_spawn_state(
        session=session,
        task=task,
        findings=findings,
        child_tasks=child_tasks,
        deduplicated=deduplicated,
        insights=insights,
        direction_notes=direction_notes,
        direction_proposals=direction_proposals,
        hash_dedup_count=hash_dedup_count,
        word_overlap_count=word_overlap_count,
        file_overlap_count=file_overlap_count,
        type_filtered_count=type_filtered_count,
        empty_prompt_count=empty_prompt_count,
        batch_cap_applied_count=batch_cap_applied_count,
        research_branch=research_branch,
        original_base=original_base,
        research_depth=research_depth,
        parent_research_task_id=parent_research_task_id,
        filtered_findings=filtered_findings,
    )

    # Persist per-cycle deferred-finding counts on the direction's latest
    # research_history entry so the next research agent can see how many of
    # its prior findings failed to spawn and why. Without this, the agent
    # cannot tell whether to produce fewer, higher-confidence findings.
    _total_deferred = len(_all_deferred)
    if _total_deferred > 0:
        _denom = _impl_spawned + _total_deferred
        _deferred_stats = {
            "total": _total_deferred,
            "by_reason": {
                "degradation_gate": len(_deferred_findings),
                "batch_cap": len(_batch_cap_deferred),
                "ultra_low_confidence": len(_low_conf_skipped_findings),
            },
            "spawn_ratio": (_impl_spawned / _denom) if _denom > 0 else None,
        }
        _proj = await session.get(Project, task.project_id)
        if _proj and _proj.directions:
            _dirs = list(_proj.directions)
            _changed = False
            for _d in _dirs:
                _hist = _d.get("research_history") or []
                if _hist and _hist[-1].get("task_id") == str(task.id):
                    _hist[-1]["deferred_stats"] = _deferred_stats
                    _d["research_history"] = _hist
                    _changed = True
            if _changed:
                _proj.directions = _dirs
                flag_modified(_proj, "directions")
                session.add(_proj)
                await session.commit()

    # Mark per-direction cycle productivity so direction_lifecycle can pick
    # an adaptive cooldown: productive cycles (>=1 task spawned) get a shorter
    # cooldown, unproductive cycles keep the standard cooldown.
    tasks_by_dir: dict[str, int] = {}
    for ct in child_tasks:
        did = (ct.context or {}).get("direction_id")
        if did:
            tasks_by_dir[did] = tasks_by_dir.get(did, 0) + 1
    current_direction_id = (task.context or {}).get("direction_id") or (task.first_payload).get("current_direction_id")
    touched_dir_ids = set(tasks_by_dir.keys())
    if current_direction_id:
        touched_dir_ids.add(current_direction_id)
    if touched_dir_ids:
        project = await session.get(Project, task.project_id)
        if project:
            directions = list(project.directions or [])
            changed = False
            for d in directions:
                did = d.get("id")
                if did in touched_dir_ids:
                    productive = tasks_by_dir.get(did, 0) > 0
                    d["last_cycle_productive"] = productive
                    if productive:
                        d["consecutive_unproductive_cycles"] = 0
                    else:
                        d["consecutive_unproductive_cycles"] = int(
                            d.get("consecutive_unproductive_cycles", 0) or 0
                        ) + 1
                    changed = True
            if changed:
                project.directions = directions
                flag_modified(project, "directions")
                session.add(project)
                await session.commit()

    # Credit playbook entries shown to this research agent when research succeeded.
    # followed_count tracks how many times each entry was seen by a successful research agent,
    # giving a denominator for per-entry success rates used by the archival ranker.
    _shown_pb_keys = list((task.context or {}).get("_shown_playbook_keys") or [])
    if _shown_pb_keys and _impl_spawned > 0:
        _pb_proj = await session.get(Project, task.project_id)
        if _pb_proj and _pb_proj.design_doc:
            _pb_doc = dict(_pb_proj.design_doc)
            _pb_updated = False
            for _pb_key in _shown_pb_keys:
                _pb_full_key = f"playbook/{_pb_key}"
                _pb_entry = _pb_doc.get(_pb_full_key)
                if isinstance(_pb_entry, dict):
                    _pb_entry = dict(_pb_entry)
                    _pb_entry["followed_count"] = _pb_entry.get("followed_count", 0) + 1
                    _pb_doc[_pb_full_key] = _pb_entry
                    _pb_updated = True
            if _pb_updated:
                _pb_proj.design_doc = _pb_doc
                flag_modified(_pb_proj, "design_doc")
                session.add(_pb_proj)
                await session.commit()


async def _record_research_task_failure(task: Task, session: AsyncSession) -> None:
    """Record a FAILED research task in the direction's research_history.

    Closes a blind spot where research tasks that never reached _handle_research_completion
    (VM crash, provision failure, agent timeout, etc.) left no trace on the direction,
    so _check_direction_health could not observe the stalled cycle.

    Appends a minimal history entry tagged research_task_failed=True to the direction
    referenced in task.context.direction_id (falling back to the payload field).
    """
    from app.failure_classification import INFRA_CLASSES, classify_failure  # noqa: PLC0415

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
    # Infra failures are transient — use the shorter productive cooldown so the
    # direction is retried promptly instead of waiting out the full unproductive window.
    productive = failure_category in INFRA_CLASSES
    target["last_cycle_productive"] = productive
    if productive:
        target["consecutive_unproductive_cycles"] = 0
    else:
        target["consecutive_unproductive_cycles"] = int(
            target.get("consecutive_unproductive_cycles", 0) or 0
        ) + 1
    project.directions = directions
    flag_modified(project, "directions")
    session.add(project)
    logger.warning(
        f"[RESEARCH] Recorded failed research task {task.id} on direction "
        f"{direction_id} ({failure_category})"
    )

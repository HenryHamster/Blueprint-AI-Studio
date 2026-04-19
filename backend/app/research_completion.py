"""Research completion handler and supporting functions.

Extracted from app.task_completion to keep that module focused on plan/security/perf
completion handlers. Contains the research findings parser, context builder,
and the large _handle_research_completion handler.
"""
import json
import logging
import os
import random
import uuid
from datetime import timedelta
from typing import Any, Dict, List, Optional

from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm.attributes import flag_modified
from sqlmodel import select

from app.models import (
    Artifact,
    ArtifactType,
    Project,
    Task,
    TaskHistory,
    TaskStatus,
)
from app.cochange_decomposition import (
    auto_decompose_findings_by_cochange,
    cluster_files_by_cochange as _cluster_files_by_cochange,  # noqa: F401 — re-exported for tests
)
from app.failure_classification import classify_failure
from app.finding_deduplication import (
    build_dedup_state,
    deduplicate_findings,
    finding_hash as _finding_hash,  # noqa: F401 — re-exported for callers/tests
    is_duplicate as _is_duplicate,  # noqa: F401 — re-exported for callers/tests
)
from app.payload_schemas import validate_internal_payload
from app.scope_complexity import _composite_complexity_score
from app.shared_state import runtime_config as _runtime_config, DEFAULT_TASK_SCOPE, scope_to_effort
from app.spawn_calibration import (  # noqa: F401 — re-exported for tests/callers
    _SCOPE_ORDER,
    SCOPE_FILE_BUDGETS,
    _DEGRADED_SPAWN_CAP,
    _assess_spawn_health,
    _maybe_cold_start_upgrade_medium,
    _calibrate_scope,
    _poor_decomp_history,
    predict_spawn_success,
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

from app.direction_metrics import (  # noqa: F401 — re-exported for callers/tests
    _HISTORY_AGG_DEFAULTS,
    _DECOMP_AGG_DEFAULTS,
    _fold_evicted_into_aggregate,
    _compute_momentum_factor,
    _compute_acceptance_rate,
    _compute_productivity_score,
    _update_cycle_history,
)


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


def _calibrate_finding_scope(
    finding: Dict[str, Any],
    scope: str,
    direction: Optional[Dict[str, Any]],
    cochange_index: Optional[Dict[str, Any]],
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
            if direction:
                _recent = [
                    (entry.get("scope_breakdown") or {}).get("medium", {})
                    for entry in (direction.get("research_history") or [])[-3:]
                ]
                _merged_ratios = [float(m.get("avg_expansion_ratio")) for m in _recent
                                  if isinstance(m.get("avg_expansion_ratio"), (int, float))]
                _failed_ratios = [float(m.get("avg_expansion_ratio_failed")) for m in _recent
                                  if isinstance(m.get("avg_expansion_ratio_failed"), (int, float))]
                # Prefer the max of merged/failed means — failed ratios reflect the
                # overflow scenario we're trying to prevent, so they bias conservatively.
                _candidates = []
                if _merged_ratios:
                    _candidates.append(sum(_merged_ratios) / len(_merged_ratios))
                if _failed_ratios:
                    _candidates.append(sum(_failed_ratios) / len(_failed_ratios))
                if _candidates:
                    _avg_ratio = max(_candidates)
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
            _cc_score, _cc_signals = _composite_complexity_score(
                finding.get("related_files") or [], direction, cochange_index,
            )
            finding["_complexity_score"] = round(_cc_score, 3)
            finding["_complexity_signals"] = _cc_signals
            if _cc_score < 0.45:
                logger.info(
                    "[RESEARCH] Upgrading scope medium→large for '%s' "
                    "(composite complexity %.2f < 0.45, signals=%s)",
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

    return scope, scope_calibrated_from


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
        _history_calibration_fired = False
        if not (scope_calibrated_from or (task.context or {}).get("scope_calibrated_from")):
            _dir_id = finding.get("direction_id") or _parent_direction_id
            _direction = _directions_by_id.get(str(_dir_id)) if _dir_id else None
            if _direction:
                scope, scope_calibrated_from = _calibrate_scope(scope, _direction, _scope_cal_project)
                _history_calibration_fired = scope_calibrated_from is not None

        # Pre-spawn scope-upgrade heuristics (file count, cross-module span,
        # expansion-ratio prediction, composite complexity). Extracted into
        # _calibrate_finding_scope so the heuristics are independently testable.
        # Skip when _calibrate_scope just fired: historical merge-rate data is
        # a stronger signal than file-count heuristics, so a freshly-applied
        # downgrade (e.g. medium→small) should not be reversed by the small→medium
        # heuristic that fires on related_files >= 3.
        if not _history_calibration_fired:
            _heur_dir_id = finding.get("direction_id") or _parent_direction_id
            _heur_direction = _directions_by_id.get(str(_heur_dir_id)) if _heur_dir_id else None
            scope, _heur_calibrated_from = _calibrate_finding_scope(
                finding, scope, _heur_direction, _cochange_index_for_decomp,
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
        _pred_p = predict_spawn_success(
            scope, _pred_direction or {}, _scope_cal_project, related_files=related_files
        )
        _low_confidence = _pred_p < _confidence_threshold
        if _low_confidence:
            logger.info(
                "[RESEARCH] Low-confidence spawn '%s' (scope=%s, type=%s): P(success)=%.2f < threshold=%.2f",
                title, scope, task_type, _pred_p, _confidence_threshold,
            )
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
                _pipeline_rates = _runtime_config.get("pipeline_scope_merge_rates") or {}
                _pipeline_medium = _pipeline_rates.get("medium")
                if isinstance(_pipeline_medium, (int, float)) and _pipeline_medium < 0.5:
                    _base = float(_explore_frac)
                    _explore_frac = _base * (float(_pipeline_medium) / 0.5)
                    logger.info(
                        "[RESEARCH] Pipeline-prior exploration fraction for direction %s: "
                        "pipeline_medium=%.2f, frac=%.3f",
                        _pred_dir_id, float(_pipeline_medium), float(_explore_frac),
                    )
            # Enforce a minimum exploration fraction so directions with zero
            # observed exploration merges still occasionally attempt medium scope —
            # otherwise the fraction locks at 0 and no new data is ever collected.
            # Skip the floor when the operator has explicitly disabled exploration
            # by setting the base fraction to 0.
            if (
                isinstance(_explore_frac, (int, float))
                and isinstance(_explore_frac_base, (int, float))
                and float(_explore_frac_base) > 0
                and 0 <= float(_explore_frac) < MEDIUM_EXPLORATION_FLOOR
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
                **({"medium_exploration": True} if _medium_exploration else {}),
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
    )
    if _all_deferred:
        ctx = dict(task.context or {})
        ctx["deferred_findings"] = _all_deferred
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

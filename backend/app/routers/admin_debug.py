"""Admin diagnostics / debug endpoints.

Project-level and global diagnostic tools. These endpoints are read-only
snapshots of pipeline state used by the admin UI to debug why autonomy is
(or isn't) spawning, inspect strategic cycle state, surface stuck tasks,
and reset circuit breakers.
"""

import os
import uuid
import asyncio
import logging
from typing import Dict, List, TypedDict
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func, or_
from sqlalchemy.orm import load_only
from sqlalchemy.orm.attributes import flag_modified
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from app.database import get_session, resolve_dialect_name
from app.models import (
    Task,
    TaskStatus,
    Project,
    DeploymentState,
    DeploymentStatus,
    SystemMaturity,
    _utcnow,
)
from app.auth import get_current_user, require_admin
from app.knowledge_query import (
    get_knowledge_document as kq_get_knowledge_document,
    list_knowledge_documents as kq_list_knowledge_documents,
    serialize_knowledge_document,
)
from app.broadcast import broadcast_to_project
from app.admin import _get_user_project, _get_user_project_with_role
from app.direction_refractory import get_direction_refractory_info
from app.autonomous_research import project_parallelism_cap
from app.reaper import _heartbeat_stale_cutoff
from app.task_capabilities import STRATEGIC_TASK_TYPES
from app.payload_schemas import _MAX_PAYLOAD_BYTES
from app.task_evaluators import (
    ADVERSARIAL_GATE_VERDICT_KEY,
    ADVERSARIAL_PARSE_OK,
    build_adversarial_challenge_manifest,
    spawn_forward_quality_grader,
)
from app.finding_deduplication import finding_hash as _finding_hash
from app.findings_index import (
    FINALIZED_RESEARCH_FINDINGS_KEY,
    finalized_research_findings_from_state,
)

# ---------------------------------------------------------------------------
# Canary spawn-gate probe
# ---------------------------------------------------------------------------

_CANARY_PROBE_TITLE = "CANARY adversarial gate health probe"
_CANARY_PROBE_DESC = (
    "Synthetic probe for spawn-gate health verification. "
    "Must not spawn implementation work."
)
_CANARY_PROBE_HASH: str = _finding_hash(_CANARY_PROBE_TITLE, _CANARY_PROBE_DESC)


def classify_canary_spawn_gate_result(gate_record: dict | None) -> dict:
    """Classify a resolved adversarial gate record as real_verdict or fail_closed.

    Returns a dict with ``status`` ("pending" | "real_verdict" | "fail_closed"),
    ``parse_outcome`` (the raw parse string from the gate record, or None), and
    ``explicit_count`` (number of findings with an explicit verdict).
    """
    if gate_record is None:
        return {"status": "pending", "parse_outcome": None, "explicit_count": 0}
    parse_outcome = gate_record.get("parse_outcome")
    finding_verdicts: dict = gate_record.get("finding_verdicts") or {}
    explicit_count = sum(
        1
        for v in finding_verdicts.values()
        if isinstance(v, dict) and v.get("explicit")
    )
    if parse_outcome == ADVERSARIAL_PARSE_OK and explicit_count > 0:
        return {
            "status": "real_verdict",
            "parse_outcome": parse_outcome,
            "explicit_count": explicit_count,
        }
    return {
        "status": "fail_closed",
        "parse_outcome": parse_outcome,
        "explicit_count": explicit_count,
    }


logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/api/admin", tags=["admin-debug"], dependencies=[Depends(get_current_user)]
)


# ---------------------------------------------------------------------------
# Diagnostics TypedDicts and helpers
# ---------------------------------------------------------------------------


class _StuckTaskInfo(TypedDict):
    task_id: str
    title: str
    status: TaskStatus
    stuck_reason: str
    stuck_for_seconds: int
    attempt_count: int
    failure_class: str | None
    unresolvable_deps: List[str]
    remediation_hint: str


class _CircuitBreakerInfo(TypedDict):
    state: str
    failure_count: int
    infra_failure_count: int
    open_reason: str | None
    cooldown_seconds: int
    cooldown_remaining_seconds: int | None
    opened_at: str | None


class _ProjectSummary(TypedDict):
    project_id: str
    stuck_count: int
    queued_count: int
    in_progress_count: int
    circuit_breaker_state: str
    needs_manual_intervention_count: int


class _DirectionRemediationInfo(TypedDict):
    direction_id: str
    direction_text: str
    status: str
    auto_paused: bool
    paused_reason: str | None
    paused_at: str | None
    auto_pause_count: int
    remediation_attempts: int
    last_remediation_result: str | None
    last_remediation_at: str | None
    error_fingerprint: str | None
    remediation_monitor_active: bool
    remediation_monitor_started_at: str | None
    remediation_regression_signal: bool
    remediation_regression_count: int
    needs_manual_intervention: bool


def _build_direction_remediation_state(
    project: Project,
) -> List[_DirectionRemediationInfo]:
    """Surface per-direction diagnosis/remediation state for operators.

    Returns entries for directions that have notable state: active remediation
    monitoring, any prior remediation attempt, an active auto-pause, or a
    manual-intervention flag. Directions with no such state are omitted so the
    operator view stays focused on what actually needs attention.
    """
    entries: List[_DirectionRemediationInfo] = []
    for d in project.directions or []:
        attempts = int(d.get("remediation_attempts", 0) or 0)
        monitor_started = d.get("remediation_monitor_started_at")
        needs_intervention = bool(d.get("needs_manual_intervention"))
        auto_paused = bool(d.get("auto_paused"))
        if not (attempts or monitor_started or needs_intervention or auto_paused):
            continue
        entries.append(
            {
                "direction_id": d.get("id", ""),
                "direction_text": d.get("text", ""),
                "status": d.get("status", "active"),
                "auto_paused": auto_paused,
                "paused_reason": d.get("paused_reason"),
                "paused_at": d.get("paused_at"),
                "auto_pause_count": int(d.get("auto_pause_count", 0) or 0),
                "remediation_attempts": attempts,
                "last_remediation_result": d.get("last_remediation_result"),
                "last_remediation_at": d.get("last_remediation_at"),
                "error_fingerprint": d.get("error_fingerprint"),
                "remediation_monitor_active": bool(monitor_started),
                "remediation_monitor_started_at": monitor_started,
                "remediation_regression_signal": bool(
                    d.get("remediation_regression_signal")
                ),
                "remediation_regression_count": int(
                    d.get("remediation_regression_count", 0) or 0
                ),
                "needs_manual_intervention": needs_intervention,
            }
        )
    return entries


def _build_direction_portfolio_summary(project: Project) -> List[dict]:
    """Surface per-direction generalized health for the diagnostics endpoint.

    Returns entries for active/paused directions that have non-code evaluation
    successes in their recent research_history — i.e. directions whose positive
    signal would be invisible to PR-only metrics. Only included when the
    generalized_successes field exceeds prs_merged, so pure code-change
    directions produce no extra noise in the response.

    Limited to the 3 most recent cycles per direction for a compact summary.
    """
    results: list = []
    for d in project.directions or []:
        if d.get("status") not in ("active", "paused"):
            continue
        history = d.get("research_history") or []
        if not history:
            continue
        recent = history[-3:]
        gen_successes = sum(
            h.get("generalized_successes", h.get("prs_merged", 0)) for h in recent
        )
        prs_merged = sum(h.get("prs_merged", 0) for h in recent)
        if gen_successes <= prs_merged:
            # No non-code contribution beyond PR merges — skip to keep compact.
            continue
        results.append(
            {
                "direction_id": d.get("id", ""),
                "direction_text": (d.get("text") or "")[:100],
                "status": d.get("status", "active"),
                "recent_cycles": len(recent),
                "prs_merged_recent": prs_merged,
                "generalized_successes_recent": gen_successes,
                "non_code_successes_recent": gen_successes - prs_merged,
            }
        )
    return results


def _classify_stuck_failure(task: Task) -> str | None:
    """Classify stuck tasks using the canonical classifier.

    Stuck tasks have not actually failed yet, so misleading classifications are
    suppressed. CODE_ERROR (the default fallback) and AGENT_TIMEOUT (which
    classify_failure returns for empty/whitespace messages — meaningful for an
    actually-failed task with a crashed agent, but not for one still in flight
    that just hasn't emitted a status yet) both return None so the UI shows
    'unclassified' instead.
    """
    from sqlalchemy import inspect as _sa_inspect
    from sqlalchemy.exc import NoInspectionAvailable

    from app.failure_classification import classify_failure

    # classify_failure reads the heavy JSONB columns (context, payloads,
    # api_call_summary). QUEUED rows arrive from the health-overview scan via
    # load_only() with those columns deferred — a first access would sync-
    # lazy-load under the async session → MissingGreenlet → the whole health
    # overview 500s (prod 2026-06-04..07, every digest health tick during a
    # rate-limit pause). Deliberately NOT loading them for a potentially
    # thousands-deep QUEUED backlog (OOM history on this endpoint); a task
    # that is merely stuck-in-queue reports unclassified instead. Transient
    # instances have no identity key, and mocks/non-ORM stand-ins are not
    # inspectable — both skip the guard since plain attribute access on them
    # never emits IO.
    try:
        state = _sa_inspect(task)
    except NoInspectionAvailable:
        state = None
    if (
        state is not None
        and state.key is not None
        and (
            {"context", "payloads", "api_call_summary", "latest_status_message"}
            & state.unloaded
        )
    ):
        return None
    if not (task.latest_status_message or "").strip():
        return None
    result = classify_failure(task)
    return result if result != "CODE_ERROR" else None


def _fmt_duration(seconds: float) -> str:
    """Format seconds into a compact human-readable string."""
    s = int(seconds)
    if s < 60:
        return f"{s}s"
    if s < 3600:
        return f"{s // 60}m{s % 60:02d}s" if s % 60 else f"{s // 60}m"
    h, rem = divmod(s, 3600)
    m = rem // 60
    return f"{h}h{m:02d}m" if m else f"{h}h"


async def _build_stuck_tasks(
    tasks: List[Task],
    now: datetime,
    session: AsyncSession,
) -> List[_StuckTaskInfo]:
    """Detect stuck tasks and build the diagnostics list."""
    from app.compute import get_zone_health

    stuck: List[_StuckTaskInfo] = []
    # Align with reaper's pre-claim threshold (6 min) so diagnostics don't flag a
    # PROVISIONING task before the reaper would act on it. IN_PROGRESS heartbeat
    # threshold is computed per-task via reaper._heartbeat_stale_cutoff to match
    # the reaper's scope-aware logic (small=300, medium=420, large=900, plus
    # effort-based floors).
    provision_timeout_secs = 6 * 60
    long_queued_secs = 30 * 60
    stuck_deps_secs = 10 * 60
    evaluating_timeout_secs = 2 * 3600  # 2 hours

    # Parent age alone is not a liveness signal. Research/rollup parents stay
    # EVALUATING while their pipeline children run, and some of those children
    # can legitimately be BLOCKED waiting for an external prerequisite. A child
    # held for operator approval is not progress-capable, however, and must not
    # hide a stale ancestor indefinitely.
    evaluating_parent_ids = {
        task.id for task in tasks if task.status == TaskStatus.EVALUATING
    }
    parents_with_active_children: set[uuid.UUID] = set()
    if evaluating_parent_ids:
        evaluating_parent_ids_by_text = {
            str(parent_id): parent_id for parent_id in evaluating_parent_ids
        }
        original_task_ids = tuple(evaluating_parent_ids_by_text)
        if resolve_dialect_name(session) == "sqlite":
            payload_original_id = func.json_extract(
                Task.payloads, "$[0].original_task_id"
            )
            context_original_id = func.json_extract(
                Task.context, "$.review_loop.original_task_id"
            )
        else:
            payload_original_id = Task.payloads[0]["original_task_id"].as_string()
            context_original_id = Task.context["review_loop"][
                "original_task_id"
            ].as_string()

        active_child_rows = await session.execute(
            select(
                Task.id,
                Task.parent_task_id,
                Task.requires_approval,
                payload_original_id,
                context_original_id,
            ).where(
                Task.project_id.in_({task.project_id for task in tasks}),
                Task.status.in_(
                    [
                        TaskStatus.QUEUED,
                        TaskStatus.PROVISIONING,
                        TaskStatus.IN_PROGRESS,
                        TaskStatus.EVALUATING,
                        TaskStatus.BLOCKED,
                    ]
                ),
                Task.archived.is_(False),
                or_(
                    Task.parent_task_id.in_(evaluating_parent_ids),
                    payload_original_id.in_(original_task_ids),
                    context_original_id.in_(original_task_ids),
                ),
            )
        )
        for (
            child_id,
            parent_id,
            requires_approval,
            payload_original,
            context_original,
        ) in (
            active_child_rows.all()
        ):
            if requires_approval:
                continue
            if parent_id in evaluating_parent_ids:
                parents_with_active_children.add(parent_id)

            chain_parent_id = evaluating_parent_ids_by_text.get(
                str(payload_original or context_original or "")
            )
            if chain_parent_id is not None and child_id != chain_parent_id:
                parents_with_active_children.add(chain_parent_id)

    # Pre-fetch all dependency statuses and titles in a single batch query to avoid N+1
    from app.time_utils import _to_naive_utc

    all_dep_uuids: set[uuid.UUID] = set()
    for task in tasks:
        if task.status == TaskStatus.QUEUED:
            # task.created_at is TIMESTAMPTZ since 0061; now is naive _utcnow().
            created_age = (now - _to_naive_utc(task.created_at)).total_seconds()
            if (task.depends_on or []) and created_age > stuck_deps_secs:
                for d in task.depends_on:
                    try:
                        all_dep_uuids.add(uuid.UUID(d))
                    except (ValueError, AttributeError):
                        pass

    dep_status_map: Dict[str, TaskStatus] = {}
    dep_title_map: Dict[str, str] = {}
    if all_dep_uuids:
        dep_result = await session.execute(
            select(Task.id, Task.status, Task.title).where(Task.id.in_(all_dep_uuids))
        )
        for row in dep_result.all():
            dep_status_map[str(row.id)] = row.status
            dep_title_map[str(row.id)] = row.title or str(row.id)[:8]

    # Count running/queued tasks for capacity hints
    running_count = sum(1 for t in tasks if t.status == TaskStatus.IN_PROGRESS)
    queued_count = sum(1 for t in tasks if t.status == TaskStatus.QUEUED)

    # Fetch zone health once for provision_timeout hints
    try:
        zone_health = await asyncio.to_thread(get_zone_health)
    except Exception:
        zone_health = {}

    for task in tasks:
        stuck_reason = None
        stuck_for_seconds = 0
        unresolvable_deps: List[str] = []
        remediation_hint = ""

        if (
            task.status == TaskStatus.PROVISIONING
            and task.last_heartbeat is None
            and task.vm_provisioned_at is not None
        ):
            # vm_provisioned_at is the orchestrator-side VM lifecycle clock (set
            # at dispatch and refreshed when GCE confirms running). last_heartbeat
            # is the agent-side clock and is None until claim. Using
            # vm_provisioned_at here matches the reaper's pre-claim threshold.
            age = (now - task.vm_provisioned_at).total_seconds()
            if age > provision_timeout_secs:
                stuck_reason = "provision_timeout"
                stuck_for_seconds = int(age - provision_timeout_secs)
                ctx = task.context or {}
                zone = ctx.get("provision_zone")
                if zone:
                    zh = zone_health.get(zone, {})
                    if zh.get("cooldown_active"):
                        remediation_hint = f"VM timed out in {zone} — zone in cooldown, reaper will retry"
                    else:
                        remediation_hint = f"VM timed out in {zone} — zone available, reaper will retry"
                else:
                    remediation_hint = (
                        "VM provision timed out — reaper will detect and retry"
                    )

        elif task.status == TaskStatus.IN_PROGRESS and task.last_heartbeat is not None:
            hb_cutoff = _heartbeat_stale_cutoff(task, now)
            last_hb = _to_naive_utc(task.last_heartbeat)
            if last_hb and last_hb < hb_cutoff:
                heartbeat_age = (now - last_hb).total_seconds()
                threshold_secs = (now - hb_cutoff).total_seconds()
                stuck_reason = "no_heartbeat"
                stuck_for_seconds = int(heartbeat_age - threshold_secs)
                remediation_hint = f"No heartbeat for {_fmt_duration(heartbeat_age)} — VM may have crashed, reaper will clean up"

        elif task.status == TaskStatus.QUEUED:
            created_age = (now - _to_naive_utc(task.created_at)).total_seconds()
            dep_ids = task.depends_on or []

            if dep_ids and created_age > stuck_deps_secs:
                # Check whether all deps are terminal-failure using pre-fetched map
                valid_dep_uuids = []
                for d in dep_ids:
                    try:
                        valid_dep_uuids.append(uuid.UUID(d))
                    except (ValueError, AttributeError):
                        pass
                if valid_dep_uuids:
                    known_rows = [
                        (str(uid), dep_status_map[str(uid)])
                        for uid in valid_dep_uuids
                        if str(uid) in dep_status_map
                    ]
                    failed_dep_ids = [
                        dep_id
                        for dep_id, status in known_rows
                        if status in (TaskStatus.FAILED, TaskStatus.CANCELLED)
                    ]
                    if failed_dep_ids and len(failed_dep_ids) == len(known_rows):
                        stuck_reason = "stuck_deps"
                        stuck_for_seconds = int(created_age - stuck_deps_secs)
                        unresolvable_deps = failed_dep_ids
                        dep_titles = [
                            dep_title_map.get(d, d[:8]) for d in failed_dep_ids[:2]
                        ]
                        suffix = (
                            f" +{len(failed_dep_ids) - 2} more"
                            if len(failed_dep_ids) > 2
                            else ""
                        )
                        remediation_hint = f"Blocked by {len(failed_dep_ids)} failed task(s): {', '.join(dep_titles)}{suffix}"

            elif not dep_ids and created_age > long_queued_secs:
                stuck_reason = "long_queued"
                stuck_for_seconds = int(created_age - long_queued_secs)
                exhausted = [
                    z for z, d in zone_health.items() if d.get("cooldown_active")
                ]
                if exhausted:
                    remediation_hint = f"All zones exhausted — {running_count} running, {queued_count}Q waiting"
                else:
                    remediation_hint = f"No available slots — {running_count} running, {queued_count}Q waiting"

        elif task.status == TaskStatus.EVALUATING:
            if task.id in parents_with_active_children:
                continue
            # Skip CI-waiting tasks — they have their own timeout
            ctx = task.context or {}
            if not ctx.get("ci_waiting") and not ctx.get("review_loop", {}).get(
                "ci_waiting"
            ):
                # Find when the task entered EVALUATING from status_log
                evaluating_since = None
                for entry in reversed(task.status_log or []):
                    msg = entry.get("msg", "")
                    if "→ EVALUATING" in msg:
                        try:
                            evaluating_since = datetime.fromisoformat(entry["t"])
                        except (KeyError, ValueError, TypeError):
                            pass
                        break
                if evaluating_since is None:
                    evaluating_since = task.started_at or task.created_at
                eval_age = (now - evaluating_since).total_seconds()
                if eval_age > evaluating_timeout_secs:
                    stuck_reason = "evaluation_timeout"
                    stuck_for_seconds = int(eval_age - evaluating_timeout_secs)
                    remediation_hint = f"Evaluating for {_fmt_duration(eval_age)} — may need manual review"

        if stuck_reason:
            stuck.append(
                {
                    "task_id": str(task.id),
                    "title": task.title,
                    "status": task.status,
                    "stuck_reason": stuck_reason,
                    "stuck_for_seconds": stuck_for_seconds,
                    "attempt_count": task.attempt_count,
                    "failure_class": _classify_stuck_failure(task),
                    "unresolvable_deps": unresolvable_deps,
                    "remediation_hint": remediation_hint,
                }
            )

    return stuck


def _format_circuit_breaker(cb: dict) -> _CircuitBreakerInfo:
    opened_at = cb.get("opened_at")
    cooldown_remaining = None
    if opened_at and cb.get("state") == "OPEN":
        elapsed = (_utcnow() - opened_at).total_seconds()
        cooldown_remaining = max(0, int(cb.get("cooldown_seconds", 1800) - elapsed))
    return {
        "state": cb.get("state", "CLOSED"),
        "failure_count": cb.get("failure_count", 0),
        "infra_failure_count": cb.get("infra_failure_count", 0),
        "open_reason": cb.get("open_reason"),
        "cooldown_seconds": cb.get("cooldown_seconds", 1800),
        "cooldown_remaining_seconds": cooldown_remaining,
        "opened_at": opened_at.isoformat() if opened_at else None,
    }


def _build_problem_model_trim_verdict(payload: dict) -> dict | None:
    """Summarize how read-time packing treated the problem_model in a strategic payload.

    Operators already see when a strategic cycle was *skipped* because the assembled
    payload exceeded the hard cap (``payload_bloat_skip``). They do NOT see when a cycle
    that actually RAN shipped a summarized / floored / dropped problem_model because the
    byte-budget packer (``pack_under_budget``) trimmed it to fit. This derives that
    verdict from the assembled task payload's ``_packer_meta`` plus the agent-view markers
    the packer leaves on the problem_model itself (``_agent_view_floor_applied`` /
    ``_gap_memory_trimmed``), so the verdict is visible without inspecting raw payload JSON.

    ``status`` is one of: ``full`` (shipped untrimmed), ``summarized`` (byte-budget
    summarize ran), ``floor`` (summarize degraded it to the read-time floor), ``dropped``
    (omitted entirely). Returns None when the payload carries no packer metadata.
    """
    meta = payload.get("_packer_meta")
    if not isinstance(meta, dict):
        return None
    pm = payload.get("problem_model")
    pm = pm if isinstance(pm, dict) else {}

    dropped_entry = next(
        (
            d
            for d in (meta.get("dropped") or [])
            if isinstance(d, dict) and d.get("key") == "problem_model"
        ),
        None,
    )
    summarized = any(
        isinstance(t, dict) and t.get("key") == "problem_model"
        for t in (meta.get("trimmed") or [])
    )
    floor = pm.get("_agent_view_floor_applied")
    floor = floor if isinstance(floor, dict) else None

    if dropped_entry is not None:
        status = "dropped"
    elif floor:
        status = "floor"
    elif summarized:
        status = "summarized"
    else:
        status = "full"

    gap_trimmed = pm.get("_gap_memory_trimmed")
    gap_trimmed_count = len(gap_trimmed) if isinstance(gap_trimmed, list) else 0

    verdict: dict = {
        "status": status,
        "budget_total": meta.get("budget_total"),
        "budget_used": meta.get("budget_used"),
        "gap_memory_trimmed_count": gap_trimmed_count,
    }

    def _floor_count(value: object) -> int:
        if isinstance(value, int) and not isinstance(value, bool):
            return value
        if isinstance(value, list):
            return len(value)
        return 0

    if floor:
        verdict["floor"] = {
            "floor_bytes": floor.get("floor_bytes"),
            "stripped_gap_fields": floor.get("stripped_gap_fields") or [],
            "dropped_gaps": int(floor.get("dropped_gaps", 0) or 0),
            "summarized_capabilities_count": _floor_count(
                floor.get("summarized_capabilities")
            ),
            "summarized_friction_count": _floor_count(floor.get("summarized_friction")),
            "still_over_floor": bool(floor.get("still_over_floor")),
        }
    if dropped_entry is not None and dropped_entry.get("reason"):
        verdict["dropped_reason"] = dropped_entry.get("reason")
    return verdict


# ---------------------------------------------------------------------------
# Strategic cycle history helper (shared by autonomy-debug + strategic-debug)
# ---------------------------------------------------------------------------


async def _get_cycle_history_for_debug(
    session: AsyncSession, project_id: uuid.UUID
) -> list[dict]:
    """Return all cycles for a project from the StrategicCycle table in legacy
    dict shape, newest-first.

    Replaces dead reads of vision._strategic_cycle_history (retired migration 0047).
    """
    from app import strategic_cycle as _sc

    rows = await _sc.all_for_project(session, project_id)
    return [_sc._to_legacy_dict(c) for c in rows]


# ---------------------------------------------------------------------------
# GET /api/admin/projects/{project_id}/autonomy-debug
# ---------------------------------------------------------------------------


@router.get("/projects/{project_id}/autonomy-debug")
async def get_autonomy_debug(
    project_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    current_user: dict = Depends(get_current_user),
):
    """Return a detailed snapshot of why autonomy is or isn't spawning for a project."""
    from app.shared_state import runtime_config as _runtime_config, get_circuit_breaker
    from app.max_plan_pool import _max_plan_pools, _account_available_for_model
    from app.autonomous_spawn_budget import (
        build_autonomous_spawn_budget_guard,
        compute_autonomous_spawn_budget_snapshot,
    )
    import app.shared_state as _shared_state_mod

    project = await _get_user_project(project_id, current_user["user_id"], session)

    now = _utcnow()
    blocking_guards = []

    # Guard 0: no available Max Plan accounts
    user_pool = _max_plan_pools.get(project.user_id, [])
    research_model = _runtime_config.get("task_type_models", {}).get("research")
    has_available = any(
        _account_available_for_model(a, research_model) for a in user_pool
    ) or bool(os.getenv("ANTHROPIC_API_KEY"))
    if not has_available:
        blocking_guards.append(
            {
                "guard": "no_available_accounts",
                "reason": f"No Max Plan accounts available for research model ({research_model})",
                "pool_size": len(user_pool),
            }
        )

    # Guard 1: autonomous_disabled
    if not project.autonomous_enabled:
        blocking_guards.append(
            {
                "guard": "autonomous_disabled",
                "reason": "project.autonomous_enabled is False",
            }
        )

    if project.paused:
        blocking_guards.append(
            {
                "guard": "project_paused",
                "reason": "project.paused is True",
            }
        )

    # Guard 2: circuit_breaker_open
    raw_breaker = get_circuit_breaker(str(project_id))
    breaker_state = raw_breaker["state"]
    cooldown_ends_at = None
    if raw_breaker.get("opened_at"):
        cooldown_end = raw_breaker["opened_at"] + timedelta(
            seconds=raw_breaker["cooldown_seconds"]
        )
        cooldown_ends_at = cooldown_end.isoformat()
        if breaker_state == "OPEN" and now < cooldown_end:
            blocking_guards.append(
                {
                    "guard": "circuit_breaker_open",
                    "reason": "Circuit breaker is OPEN and cooldown has not elapsed",
                    "opened_at": raw_breaker["opened_at"].isoformat(),
                    "cooldown_ends_at": cooldown_ends_at,
                    "failure_count": raw_breaker["failure_count"],
                }
            )

    circuit_breaker_info = {
        "state": breaker_state,
        "failure_count": raw_breaker["failure_count"],
        "success_count": raw_breaker["success_count"],
        "opened_at": raw_breaker["opened_at"].isoformat()
        if raw_breaker.get("opened_at")
        else None,
        "cooldown_seconds": raw_breaker["cooldown_seconds"],
        "cooldown_ends_at": cooldown_ends_at,
    }

    # Mirror `_spawn_research_task_inner`'s two separate concerns:
    #   - busy_direction_ids (direction eligibility): include EVALUATING because
    #     the direction is still "owned" by a research cycle whose children are
    #     merging — re-spawning on it would be wasteful.
    #   - active_research_count (parallel-research cap): exclude EVALUATING
    #     because those tasks aren't consuming VM slots and shouldn't count
    #     against max_parallel (see autonomous_research.py:366, "EVALUATING
    #     parents are just waiting on children — they don't block new research").
    #
    # Conflating them (the previous single-query approach) made this endpoint
    # report `research_task_active` blocks that the spawn path would not
    # actually hit, which was misleading the autonomy-debug UI.
    busy_tasks_result = await session.execute(
        select(Task)
        .options(load_only(Task.id, Task.payloads, Task.status, raiseload=True))
        .where(
            Task.project_id == project_id,
            Task.status.in_(
                [
                    TaskStatus.QUEUED,
                    TaskStatus.PROVISIONING,
                    TaskStatus.IN_PROGRESS,
                    TaskStatus.EVALUATING,
                    # BLOCKED research parents still hold their direction (see
                    # _spawn_research_task_inner): a lane parked on an unresolved
                    # upstream prerequisite is wanted-but-not-runnable, so the
                    # direction is structurally occupied and not eligible.
                    TaskStatus.BLOCKED,
                ]
            ),
        )
        .limit(1000)
    )
    busy_direction_ids: list[str] = []
    blocked_direction_ids: set[str] = set()
    active_research_count = 0
    for t in busy_tasks_result.scalars().all():
        payload = t.first_payload
        if payload.get("type") != "research":
            continue
        did = payload.get("current_direction_id")
        if did:
            busy_direction_ids.append(did)
            if t.status == TaskStatus.BLOCKED:
                blocked_direction_ids.add(did)
        # Only the VM-consuming statuses count against the parallel cap.
        # BLOCKED (like EVALUATING) does not consume a VM slot.
        if t.status in (
            TaskStatus.QUEUED,
            TaskStatus.PROVISIONING,
            TaskStatus.IN_PROGRESS,
        ):
            active_research_count += 1

    # Guard 3: no_eligible_directions — a direction is eligible only if it's
    # active, out of refractory, AND not already held by another research task.
    cooldown_minutes = _runtime_config.get("autonomous_direction_cooldown_minutes", 240)
    directions_info = []
    for d in project.directions or []:
        if d.get("status") != "active":
            continue
        refractory_info = get_direction_refractory_info(d, now=now)
        last_researched_at = d.get("last_researched_at")
        remaining_minutes = refractory_info["refractory_remaining_minutes"]
        in_cooldown = remaining_minutes is not None
        seconds_until_eligible = (
            max(0, int(remaining_minutes * 60)) if in_cooldown else None
        )
        is_busy = d.get("id") in busy_direction_ids
        is_eligible = not in_cooldown and not is_busy
        entry = {
            "id": d.get("id"),
            "text": d.get("text", ""),
            "status": d.get("status", "active"),
            "last_researched_at": last_researched_at,
            "cooldown_minutes": cooldown_minutes,
            "refractory_reason": refractory_info["refractory_reason"],
            "is_eligible": is_eligible,
            "seconds_until_eligible": seconds_until_eligible,
        }
        if is_busy:
            if d.get("id") in blocked_direction_ids:
                entry["busy_reason"] = (
                    "direction held by a BLOCKED research task parked on an "
                    "unresolved upstream prerequisite"
                )
            else:
                entry["busy_reason"] = (
                    "direction has active research task (QUEUED/PROVISIONING/IN_PROGRESS/EVALUATING)"
                )
        directions_info.append(entry)

    eligible_directions = [d for d in directions_info if d["is_eligible"]]
    if not eligible_directions:
        min_wait = min(
            (
                d["seconds_until_eligible"]
                for d in directions_info
                if d["seconds_until_eligible"] is not None
            ),
            default=None,
        )
        busy_count = sum(1 for d in directions_info if d.get("busy_reason"))
        if not directions_info:
            reason = "No active directions exist"
        elif busy_count == len(directions_info):
            reason = f"All {len(directions_info)} active directions are held by running research"
        elif busy_count > 0:
            reason = f"All remaining directions in cooldown; {busy_count} held by running research"
        else:
            reason = "All active directions are still in cooldown"
        blocking_guards.append(
            {
                "guard": "no_eligible_directions",
                "reason": reason,
                "time_until_eligible_seconds": min_wait,
                "busy_direction_count": busy_count,
                "total_active_directions": len(directions_info),
            }
        )

    # Guard 4: research_task_active — parallel-research cap (now matches spawn logic)
    max_parallel = project_parallelism_cap(
        project, "max_parallel_research", "max_parallel_research_per_project", 6
    )
    if active_research_count >= max_parallel:
        blocking_guards.append(
            {
                "guard": "research_task_active",
                "reason": f"Active research ({active_research_count}) >= max_parallel ({max_parallel})",
                "active_research_count": active_research_count,
                "max_parallel": max_parallel,
            }
        )

    # Guard 5: rate_limit_exceeded
    one_hour_ago = now - timedelta(hours=1)
    dialect_name = resolve_dialect_name(session)
    if dialect_name == "sqlite":
        type_is_research = func.json_extract(Task.payloads, "$[0].type") == "research"
    else:
        type_is_research = Task.payloads[0]["type"].as_string() == "research"
    tasks_this_hour = (
        await session.execute(
            select(func.count()).where(
                Task.project_id == project_id,
                Task.created_by == "ORCHESTRATOR",
                Task.created_at >= one_hour_ago,
                type_is_research,
            )
        )
    ).scalar() or 0
    max_per_hour = project_parallelism_cap(
        project, "max_autonomous_tasks_per_hour", "max_autonomous_tasks_per_hour", 5
    )
    if tasks_this_hour >= max_per_hour:
        blocking_guards.append(
            {
                "guard": "rate_limit_exceeded",
                "reason": f"tasks_this_hour ({tasks_this_hour}) >= max_per_hour ({max_per_hour})",
                "tasks_this_hour": tasks_this_hour,
                "max_per_hour": max_per_hour,
            }
        )

    budget_snapshot = None
    try:
        budget_snapshot = await compute_autonomous_spawn_budget_snapshot(
            session,
            runtime_config=_runtime_config,
            now=now,
        )
    except Exception:  # noqa: BLE001
        logger.warning(
            "[AUTONOMY-DEBUG] spawn budget snapshot failed open for project %s",
            project_id,
            exc_info=True,
        )
    if budget_snapshot is not None:
        budget_guard = build_autonomous_spawn_budget_guard(
            snapshot=budget_snapshot,
            spawn_category="initiative",
            enforce_initiative=budget_snapshot.mode == "enforce",
        )
        if budget_guard is not None:
            blocking_guards.append(budget_guard)

    # Guard 6a: pipeline_paused — admin-paused or auto-paused deployments halt dispatch,
    # which means _check_idle_research silently skips spawning.
    deploy_state = await session.get(DeploymentState, 1)
    if deploy_state and deploy_state.status == DeploymentStatus.PAUSED:
        blocking_guards.append(
            {
                "guard": "pipeline_paused",
                "reason": f"Pipeline is PAUSED ({deploy_state.pause_reason or 'no reason recorded'})",
                "pause_reason": deploy_state.pause_reason,
                "paused_at": deploy_state.paused_at.isoformat()
                if deploy_state.paused_at
                else None,
            }
        )

    # Guard 6b: vm_slots_full — _check_idle_research only spawns when VMs are available.
    # This is the most common silent-return path when max_concurrent_vms is small.
    vm_consuming = (
        await session.execute(
            select(func.count())
            .select_from(Task)
            .where(
                Task.status.in_([TaskStatus.PROVISIONING, TaskStatus.IN_PROGRESS]),
            )
        )
    ).scalar() or 0
    max_concurrent_vms = _runtime_config.get("max_concurrent_vms", 5)
    if vm_consuming >= max_concurrent_vms:
        blocking_guards.append(
            {
                "guard": "vm_slots_full",
                "reason": f"VM slots full ({vm_consuming}/{max_concurrent_vms}) — waiting for a task to finish",
                "vm_consuming": vm_consuming,
                "max_concurrent_vms": max_concurrent_vms,
            }
        )

    # Guard 6: waiting_for_interval — interval gate hasn't elapsed since last_idle_check
    interval_minutes = _runtime_config.get("autonomous_research_interval_minutes", 30)
    last_idle_check = _shared_state_mod.last_idle_check
    if last_idle_check:
        interval_elapsed = (
            now - last_idle_check
        ).total_seconds() >= interval_minutes * 60
        next_check_at = (
            last_idle_check + timedelta(minutes=interval_minutes)
        ).isoformat()
    else:
        interval_elapsed = True
        next_check_at = now.isoformat()

    if not interval_elapsed:
        secs_until_check = max(
            0,
            int(
                (
                    last_idle_check + timedelta(minutes=interval_minutes) - now
                ).total_seconds()
            ),
        )
        blocking_guards.append(
            {
                "guard": "waiting_for_interval",
                "reason": f"Research interval ({interval_minutes}m) has not elapsed since last check",
                "next_check_at": next_check_at,
                "seconds_until_check": secs_until_check,
            }
        )

    return {
        "spawn_blocked": len(blocking_guards) > 0,
        "blocking_guards": blocking_guards,
        "circuit_breaker": circuit_breaker_info,
        "rate_limit": {
            "tasks_this_hour": tasks_this_hour,
            "max_per_hour": max_per_hour,
        },
        "global_rate_limit": budget_snapshot.as_log_fields()
        if budget_snapshot is not None
        else None,
        "directions": directions_info,
        "next_check_at": next_check_at,
        "strategic_cycle_history": await _get_cycle_history_for_debug(
            session, project_id
        ),
    }


@router.get("/projects/{project_id}/strategic-debug")
async def get_strategic_debug(
    project_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    current_user: dict = Depends(get_current_user),
):
    """Return at-a-glance status for the project's strategic / architecture cycle.

    Strategic cycles are triggered every N research spawns (strategic_research_frequency),
    and every Mth strategic cycle also runs an architecture review (architecture_review_frequency).
    This endpoint surfaces: current phase, next-trigger countdown, recent cycle outcomes,
    and any blockers (failure threshold, missing purpose) so the user doesn't have to
    dig into the Strategic tab to tell what's happening.
    """
    from app.shared_state import runtime_config as _runtime_config

    project = await _get_user_project(project_id, current_user["user_id"], session)
    vision = project.vision or {}
    now = _utcnow()
    blocking_guards: list[dict] = []

    # Active strategic task (design_doc_update or strategic_review) — mirrors
    # _has_active_strategic_task but without the FOR UPDATE lock (read-only view).
    active_stmt = (
        select(Task)
        .where(
            Task.project_id == project_id,
            Task.status.in_(
                [TaskStatus.QUEUED, TaskStatus.PROVISIONING, TaskStatus.IN_PROGRESS]
            ),
            Task.payloads[0]["type"].astext.in_(sorted(STRATEGIC_TASK_TYPES)),
        )
        .limit(1)
    )
    active_strategic_task: Task | None = None
    active_phase: str | None = None
    result = (await session.execute(active_stmt)).scalars().first()
    if result is not None:
        active_strategic_task = result
        active_phase = (result.first_payload).get("type")

    # Last read-time packing verdict for the problem_model: derived from the most
    # recent strategic task's assembled payload (any status, so the verdict survives
    # after the cycle completes). Surfaces whether the strategist actually saw the
    # problem_model in full or a summarized / floored / dropped view — the success-case
    # complement to the payload_bloat_skip (cap-exceeded skip) signal below.
    latest_strategic = (
        (
            await session.execute(
                select(Task)
                .where(
                    Task.project_id == project_id,
                    Task.payloads[0]["type"].astext.in_(sorted(STRATEGIC_TASK_TYPES)),
                )
                .order_by(Task.created_at.desc())
                .limit(1)
            )
        )
        .scalars()
        .first()
    )
    problem_model_trim: dict | None = None
    if latest_strategic is not None:
        problem_model_trim = _build_problem_model_trim_verdict(
            latest_strategic.first_payload
        )
        if problem_model_trim is not None:
            problem_model_trim["task_id"] = str(latest_strategic.id)
            problem_model_trim["task_status"] = latest_strategic.status
            problem_model_trim["assembled_at"] = (
                latest_strategic.created_at.isoformat()
                if latest_strategic.created_at
                else None
            )

    # Trigger countdowns
    strategic_frequency = vision.get(
        "_strategic_frequency_override"
    ) or _runtime_config.get("strategic_research_frequency", 6)
    arch_frequency = _runtime_config.get("architecture_review_frequency", 3)
    research_spawn_count = vision.get("_research_spawn_count", 0) or 0
    strategic_cycle_count = vision.get("_strategic_cycle_count", 0) or 0

    if strategic_frequency > 0:
        remainder = research_spawn_count % strategic_frequency
        # When count > 0 and remainder == 0, strategic is due to spawn this cycle
        # (autonomous_research.py:365 triggers on the same condition). Reporting
        # 0 lets the UI show "ready now" instead of jumping back to `frequency`,
        # which looked like the counter overflowing/resetting.
        if research_spawn_count == 0:
            next_strategic_in = strategic_frequency
        else:
            next_strategic_in = (strategic_frequency - remainder) % strategic_frequency
    else:
        next_strategic_in = None

    # Will the *next* strategic cycle also do architecture?
    next_cycle_number = strategic_cycle_count + 1
    next_is_architecture = (
        arch_frequency > 0 and next_cycle_number % arch_frequency == 0
    )

    # No-delta skips — cycles short-circuited by the pre-context gate
    # (strategic_cycle_gating.py). Surfaced so skipped cycles are visible and
    # countable rather than reading as the strategist silently stopping.
    strategic_no_delta_skip_count = vision.get("_strategic_no_delta_skip_count", 0) or 0
    strategic_no_delta_skips = vision.get("_strategic_no_delta_skips") or []

    # Strategic failure state (auto-decays after 48h)
    strategic_failures = vision.get("_strategic_failures", 0) or 0
    failures_at_iso = vision.get("_strategic_failures_at")
    failures_decay_at_iso: str | None = None
    if failures_at_iso:
        try:
            last_failure = datetime.fromisoformat(
                failures_at_iso.replace("Z", "+00:00")
            )
            if last_failure.tzinfo is not None:
                last_failure = last_failure.astimezone(timezone.utc).replace(
                    tzinfo=None
                )
            decay_deadline = last_failure + timedelta(hours=48)
            failures_decay_at_iso = decay_deadline.isoformat()
            if strategic_failures >= 3 and now < decay_deadline:
                blocking_guards.append(
                    {
                        "guard": "strategic_failures_high",
                        "reason": f"{strategic_failures} consecutive strategic failures — auto-recovery in "
                        f"{int((decay_deadline - now).total_seconds() / 3600)}h",
                        "failures": strategic_failures,
                        "decay_at": failures_decay_at_iso,
                    }
                )
        except (ValueError, TypeError):
            pass

    # Payload-bloat skip — autonomous_research.py skips the strategic spawn when the
    # assembled payload (design_doc + problem_model + conversation_thread) exceeds the
    # 256 KB cap, writing this marker. The skip does NOT increment _strategic_failures,
    # so without surfacing it here the panel would compute a healthy Idle/Checking phase
    # while strategic refresh is actually frozen. Treat it as a blocking guard.
    payload_bloat_skip = vision.get("_strategic_payload_bloat_skip") or None
    if payload_bloat_skip:
        contributors = payload_bloat_skip.get("top_contributors") or []
        top_names = ", ".join(
            str(c.get("key")) for c in contributors[:3] if c.get("key")
        )
        size_kb = round((payload_bloat_skip.get("size") or 0) / 1024)
        max_kb = _MAX_PAYLOAD_BYTES // 1024
        blocking_guards.append(
            {
                "guard": "payload_bloat_skip",
                "reason": (
                    f"Strategic paused: assembled payload ({size_kb} KB) exceeds the "
                    f"{max_kb} KB cap"
                    + (f" — compact {top_names}" if top_names else "")
                ),
                "size": payload_bloat_skip.get("size"),
                "skipped_at": payload_bloat_skip.get("skipped_at"),
                "top_contributors": contributors,
            }
        )

    # Purpose bootstrapping — without purpose, strategic can only attempt once
    # and won't auto-advance if it fails (strategic_failures gates the retry loop).
    # Resolves problem_model.intent.purpose first (vision.purpose is the
    # deprecated legacy fallback) via the same helper the force-trigger uses,
    # so the UI warning and actual trigger behavior cannot disagree.
    from app.problem_model import project_has_purpose

    has_purpose = project_has_purpose(project)

    # Recent cycle history (last 3, most recent first) from StrategicCycle table.
    # vision._strategic_cycle_history was retired in migration 0047.
    from app import strategic_cycle as _sc

    recent_cycles = await _sc.recent_for_agent_context(session, project_id, limit=3)
    last_cycle = recent_cycles[0] if recent_cycles else None

    # Determine phase
    # 2 = Running (task active), 3 = Waiting (blocked), 1 = Checking (near trigger,
    # no blockers), 0 = Idle (not near trigger, no activity).
    phase: int
    if active_strategic_task is not None:
        phase = 2
    elif blocking_guards:
        phase = 3
    elif next_strategic_in is not None and next_strategic_in <= 1:
        # One or zero spawns away from triggering a cycle — effectively "checking"
        phase = 1
    else:
        phase = 0

    # Architecture (design doc) summary: last update, revision, system count,
    # and how many strategic cycles until the next architecture review fires.
    design_doc = project.design_doc or {}
    dd_meta = design_doc.get("_meta") or {}
    system_keys = [
        k for k in design_doc.keys() if isinstance(k, str) and k.startswith("systems/")
    ]
    capability_keys = [
        k
        for k in design_doc.keys()
        if isinstance(k, str) and k.startswith("capabilities/")
    ]
    if arch_frequency > 0:
        arch_remainder = strategic_cycle_count % arch_frequency
        if strategic_cycle_count == 0:
            next_arch_in_cycles = arch_frequency
        else:
            next_arch_in_cycles = (arch_frequency - arch_remainder) % arch_frequency
    else:
        next_arch_in_cycles = None
    architecture_info = {
        "revision": dd_meta.get("revision", 0),
        "last_updated": dd_meta.get("last_updated"),
        "updated_by_task": dd_meta.get("updated_by_task"),
        "system_count": len(system_keys),
        "capability_count": len(capability_keys),
        "next_arch_in_cycles": next_arch_in_cycles,
    }

    return {
        "phase": phase,
        "blocking_guards": blocking_guards,
        "payload_bloat_skip": payload_bloat_skip,
        "problem_model_trim": problem_model_trim,
        "has_purpose": has_purpose,
        "research_spawn_count": research_spawn_count,
        "strategic_cycle_count": strategic_cycle_count,
        "strategic_frequency": strategic_frequency,
        "architecture_review_frequency": arch_frequency,
        "next_strategic_in": next_strategic_in,
        "next_is_architecture": next_is_architecture,
        "strategic_failures": strategic_failures,
        "strategic_failures_at": failures_at_iso,
        "strategic_failures_decay_at": failures_decay_at_iso,
        "strategic_no_delta_skip_count": strategic_no_delta_skip_count,
        "strategic_no_delta_skips": strategic_no_delta_skips,
        "architecture": architecture_info,
        "active_task": {
            "id": str(active_strategic_task.id),
            "phase": active_phase,
            "status": active_strategic_task.status,
            "started_at": active_strategic_task.started_at.isoformat()
            if active_strategic_task.started_at
            else None,
        }
        if active_strategic_task
        else None,
        "last_cycle": last_cycle,
        "recent_cycles": recent_cycles,
    }


@router.post("/projects/{project_id}/circuit-breaker/reset")
async def reset_circuit_breaker(
    project_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    current_user: dict = Depends(get_current_user),
):
    """Manually reset a project's circuit breaker to CLOSED state."""
    await _get_user_project_with_role(
        project_id, current_user["user_id"], session, "member"
    )
    import app.shared_state as _shared_state
    from app.orchestrator import _persist_circuit_breakers

    key = str(project_id)
    breaker = _shared_state.circuit_breakers.get(key)
    if not breaker:
        raise HTTPException(
            status_code=404, detail="No circuit breaker state found for this project"
        )

    base_cooldown = breaker.get("base_cooldown", 1800)
    breaker["state"] = "CLOSED"
    breaker["failure_count"] = 0
    breaker["cooldown_seconds"] = base_cooldown
    breaker["opened_at"] = None
    breaker["half_open_task_id"] = None

    await _persist_circuit_breakers()

    # Also clear strategic cycle state so the cycle can recover
    project = await session.get(Project, project_id)
    if project and project.vision:
        vision_keys_to_clear = {"_strategic_failures", "_pending_strategic_review"}
        if vision_keys_to_clear & project.vision.keys():
            project.vision = {**project.vision}
            for vkey in vision_keys_to_clear:
                project.vision.pop(vkey, None)
            flag_modified(project, "vision")
            session.add(project)
            await session.commit()
            logger.info(
                f"[ADMIN] Cleared strategic cycle state (_strategic_failures, _pending_strategic_review) for project {project_id}"
            )

    user_email = current_user.get("email", current_user.get("sub", "unknown"))
    logger.info(
        f"[ADMIN] Circuit breaker manually reset for project {project_id} by {user_email}"
    )

    await broadcast_to_project(
        key, "autonomy_updated", {"project_id": key, "circuit_breaker_reset": True}
    )

    return {
        "project_id": key,
        "state": breaker["state"],
        "failure_count": breaker["failure_count"],
        "cooldown_seconds": breaker["cooldown_seconds"],
        "opened_at": None,
        "half_open_task_id": None,
    }


@router.post("/anomaly-throttle/restore")
async def restore_anomaly_throttle(
    current_user: dict = Depends(require_admin),
):
    """Manually clear the active anomaly auto-throttle and restore the stashed
    pre-adjustment max_concurrent_vms and failure-gate values.

    This is an operator early-restore: the throttle otherwise auto-restores
    once the triggering anomalies clear (or after the 6h forced-restoration
    safeguard). The restore path honours the same deploy_health edge cases the
    automatic restoration guards rather than blindly overwriting live capacity.
    """
    from app.anomaly_detection import manual_restore_anomaly_adjustments

    result = manual_restore_anomaly_adjustments()
    if not result.get("restored"):
        raise HTTPException(
            status_code=404, detail="No active anomaly throttle to restore"
        )

    user_email = current_user.get("email", current_user.get("sub", "unknown"))
    logger.info(
        f"[ADMIN] Anomaly auto-throttle manually restored by {user_email}: "
        f"max_concurrent_vms={result.get('max_concurrent_vms')}, "
        f"failure_gate={result.get('failure_gate_threshold')}, "
        f"cleared_triggers={result.get('cleared_trigger_anomalies')}"
    )
    return result


# ---------------------------------------------------------------------------
# GET /api/admin/projects/{project_id}/diagnostics
# ---------------------------------------------------------------------------


_MAX_ACTIVE_TASKS_DEFAULT = 500
_MAX_ACTIVE_TASKS_CEILING = 5000


@router.get("/projects/{project_id}/diagnostics")
async def get_project_diagnostics(
    project_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    current_user: dict = Depends(get_current_user),
    max_active_tasks: int = Query(
        _MAX_ACTIVE_TASKS_DEFAULT,
        ge=1,
        le=_MAX_ACTIVE_TASKS_CEILING,
    ),
):
    """Return a structured health snapshot for a single project's pipeline.

    ``max_active_tasks`` caps the number of active tasks fetched (default 500,
    bounded to [1, 5000]). Tasks are ordered oldest-first so a truncated subset
    still surfaces the genuinely stuck tasks. When the result is truncated the
    response includes ``active_tasks_truncated: true``.
    """
    from app.shared_state import get_circuit_breaker
    from app.compute import get_zone_health_summary

    project = await _get_user_project(project_id, current_user["user_id"], session)

    now = _utcnow()

    active_statuses = [
        TaskStatus.QUEUED,
        TaskStatus.PROVISIONING,
        TaskStatus.IN_PROGRESS,
        TaskStatus.EVALUATING,
    ]
    active_result = await session.execute(
        select(Task)
        .where(
            Task.project_id == project_id,
            Task.status.in_(active_statuses),
            Task.archived.is_(False),
        )
        # Oldest-first so a truncated subset keeps the genuinely stuck tasks.
        .order_by(Task.created_at.asc())
        .limit(max_active_tasks + 1)
    )
    active_tasks_raw = active_result.scalars().all()
    active_tasks_truncated = len(active_tasks_raw) > max_active_tasks
    active_tasks = active_tasks_raw[:max_active_tasks]

    stuck_tasks = await _build_stuck_tasks(active_tasks, now, session)

    queued_count = sum(1 for t in active_tasks if t.status == TaskStatus.QUEUED)
    in_progress_count = sum(
        1 for t in active_tasks if t.status == TaskStatus.IN_PROGRESS
    )

    cb_raw = get_circuit_breaker(str(project_id))
    circuit_breaker = _format_circuit_breaker(cb_raw)

    try:
        zone_health = await asyncio.to_thread(get_zone_health_summary)
    except Exception:
        zone_health = {"available": 0, "exhausted": []}

    direction_remediation = _build_direction_remediation_state(project)
    direction_portfolio = _build_direction_portfolio_summary(project)

    response: dict = {
        "project_id": str(project_id),
        "stuck_tasks": stuck_tasks,
        "circuit_breaker": circuit_breaker,
        "queued_count": queued_count,
        "in_progress_count": in_progress_count,
        "zone_health": zone_health,
        "direction_remediation": direction_remediation,
        "needs_manual_intervention_count": sum(
            1 for e in direction_remediation if e["needs_manual_intervention"]
        ),
        "active_tasks_truncated": active_tasks_truncated,
        "active_tasks_cap": max_active_tasks,
    }
    # direction_portfolio_summary is ADDITIVE — only present when non-code work
    # exists so the response stays compact for code-only portfolios.
    if direction_portfolio:
        response["direction_portfolio_summary"] = direction_portfolio
    return response


# ---------------------------------------------------------------------------
# GET /api/admin/diagnostics  (aggregated across all projects)
# ---------------------------------------------------------------------------

DIAGNOSTICS_TOP_N = 20


@router.get("/diagnostics")
async def get_global_diagnostics(
    limit: int = Query(default=50, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    session: AsyncSession = Depends(get_session),
    current_user: dict = Depends(require_admin),
):
    """Aggregate pipeline diagnostics across all projects.

    Uses limit/offset pagination over active tasks (default limit=50, max=500).
    Returns stuck_tasks and direction_remediation as top-N summaries with total counts.
    """
    from app.shared_state import get_circuit_breaker, circuit_breakers
    from app.compute import get_zone_health_summary

    now = _utcnow()

    active_statuses = [
        TaskStatus.QUEUED,
        TaskStatus.PROVISIONING,
        TaskStatus.IN_PROGRESS,
        TaskStatus.EVALUATING,
    ]

    # Count total active tasks for pagination warnings without loading full rows.
    count_result = await session.execute(
        select(func.count())
        .select_from(Task)
        .where(
            Task.status.in_(active_statuses),
            Task.archived.is_(False),
        )
    )
    total_active_count = count_result.scalar() or 0

    # Global per-status counts over the FULL active set (independent of the
    # paginated window below). queued_count / in_progress_count are consumed as
    # global totals by scripts/ops.py and scripts/audit_tasks_prs.py, so they must
    # not be derived from the page (which is oldest-first and would drop fresh
    # QUEUED tasks). Cheap GROUP BY, no row payloads loaded.
    status_count_result = await session.execute(
        select(Task.status, func.count())
        .where(
            Task.status.in_(active_statuses),
            Task.archived.is_(False),
        )
        .group_by(Task.status)
    )
    status_counts: Dict[TaskStatus, int] = {
        row[0]: row[1] for row in status_count_result.all()
    }
    total_queued = status_counts.get(TaskStatus.QUEUED, 0)
    total_in_progress = status_counts.get(TaskStatus.IN_PROGRESS, 0)

    active_result = await session.execute(
        select(Task)
        .options(
            load_only(
                Task.id,
                Task.project_id,
                Task.status,
                Task.title,
                Task.created_at,
                Task.updated_at,
                Task.vm_instance_id,
                Task.last_heartbeat,
                Task.vm_provisioned_at,
                Task.started_at,
                Task.attempt_count,
                Task.latest_status_message,
                Task.context,
                Task.status_log,
                Task.depends_on,
                # payloads is read by classify_failure → _classify_stuck_failure;
                # omitting it here triggers MissingGreenlet on lazy-load.
                Task.payloads,
            raiseload=True,)
        )
        .where(
            Task.status.in_(active_statuses),
            Task.archived.is_(False),
        )
        .order_by(Task.created_at.asc())
        .limit(limit)
        .offset(offset)
    )
    active_tasks = list(active_result.scalars().all())

    # Build warnings when the scan window doesn't cover all active tasks.
    warnings: List[str] = []
    tasks_beyond_window = total_active_count - (offset + len(active_tasks))
    if tasks_beyond_window > 0:
        warnings.append(
            f"Showing {len(active_tasks)} of {total_active_count} active tasks "
            f"(offset={offset}, limit={limit}); {tasks_beyond_window} tasks not scanned — "
            "use offset to paginate."
        )
    if limit >= 400:
        warnings.append(
            f"limit={limit} is near the maximum (500); response may be large."
        )

    # Group tasks by project
    by_project: Dict[str, List[Task]] = {}
    for task in active_tasks:
        pid = str(task.project_id)
        by_project.setdefault(pid, []).append(task)

    # Fetch project remediation state once so the aggregate can surface
    # needs_manual_intervention directions even for projects with no active tasks.
    # Skip projects with no directions — they can never contribute remediation entries.
    # Narrow the scan to relevant projects before the .limit(500) cap so stale
    # rows don't crowd out active ones. Project has no updated_at/activity
    # timestamp, so archived-state is the available relevance signal: archived
    # projects are inactive and never contribute live remediation entries.
    projects_result = await session.execute(
        select(Project)
        .where(
            func.coalesce(func.jsonb_array_length(Project.directions), 0) > 0,
            Project.not_archived(),
        )
        .options(load_only(Project.id, Project.directions, raiseload=True))
        .limit(500)
    )
    remediation_by_project: Dict[str, List[_DirectionRemediationInfo]] = {}
    for proj in projects_result.scalars().all():
        entries = _build_direction_remediation_state(proj)
        if entries:
            remediation_by_project[str(proj.id)] = entries

    all_stuck: List[_StuckTaskInfo] = []
    project_summaries: List[_ProjectSummary] = []
    all_remediation: List[_DirectionRemediationInfo] = []

    project_ids = set(by_project.keys()) | set(remediation_by_project.keys())
    for pid in project_ids:
        tasks = by_project.get(pid, [])
        stuck = await _build_stuck_tasks(tasks, now, session) if tasks else []
        # Per-project queued/in_progress are window-scoped (reflect only the
        # tasks on this page); the global queued_count / in_progress_count in the
        # response come from the GROUP BY above.
        queued = sum(1 for t in tasks if t.status == TaskStatus.QUEUED)
        in_prog = sum(1 for t in tasks if t.status == TaskStatus.IN_PROGRESS)
        all_stuck.extend(stuck)
        remediation = remediation_by_project.get(pid, [])
        all_remediation.extend(remediation)
        manual_count = sum(1 for e in remediation if e["needs_manual_intervention"])

        cb_raw = get_circuit_breaker(pid)
        project_summaries.append(
            {
                "project_id": pid,
                "stuck_count": len(stuck),
                "queued_count": queued,
                "in_progress_count": in_prog,
                "circuit_breaker_state": cb_raw.get("state", "CLOSED"),
                "needs_manual_intervention_count": manual_count,
            }
        )

    # All known circuit breakers (includes projects with no active tasks)
    all_circuit_breakers = {
        pid: _format_circuit_breaker(cb) for pid, cb in circuit_breakers.items()
    }

    try:
        zone_health = await asyncio.to_thread(get_zone_health_summary)
    except Exception:
        zone_health = {"available": 0, "exhausted": []}

    # Return top-N for stuck_tasks and direction_remediation to bound response size.
    # Callers that need the full list should page through all offsets and aggregate.
    stuck_by_urgency = sorted(
        all_stuck, key=lambda x: x["stuck_for_seconds"], reverse=True
    )
    remediation_by_priority = sorted(
        all_remediation,
        key=lambda x: (not x["needs_manual_intervention"], x.get("direction_text", "")),
    )

    return {
        "stuck_tasks": stuck_by_urgency[:DIAGNOSTICS_TOP_N],
        # total_stuck_count reflects the scanned window only. Stuck detection is a
        # per-task derived property (heartbeat/timing/JSONB) with no cheap GROUP BY
        # equivalent, so computing it globally would require the full active-set
        # scan this endpoint is designed to bound. Stuck tasks are old by
        # definition and the window is ordered created_at ASC (oldest first), so
        # the default window captures them; widen via limit/offset for an exhaustive count.
        "total_stuck_count": len(all_stuck),
        "queued_count": total_queued,
        "in_progress_count": total_in_progress,
        "total_active_tasks_scanned": len(active_tasks),
        "projects": project_summaries,
        "circuit_breakers": all_circuit_breakers,
        "zone_health": zone_health,
        "direction_remediation": remediation_by_priority[:DIAGNOSTICS_TOP_N],
        "total_direction_remediation_count": len(all_remediation),
        "needs_manual_intervention_count": sum(
            1 for e in all_remediation if e["needs_manual_intervention"]
        ),
        # NOTE: semantics changed with pagination. truncated now means "this page
        # does not cover all active tasks" (tasks_beyond_window > 0), not the prior
        # ">5000 active tasks exist" sentinel. The global endpoint has no frontend
        # consumer reading this field; see warnings for the human-readable detail.
        "truncated": tasks_beyond_window > 0,
        "warnings": warnings,
    }


# ---------------------------------------------------------------------------
# GET /api/admin/projects/{project_id}/system-maturity
# ---------------------------------------------------------------------------


@router.get("/projects/{project_id}/system-maturity")
async def get_project_system_maturity(
    project_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    current_user: dict = Depends(get_current_user),
):
    """Return per-system maturity rows for a project, ordered by score ascending."""
    await _get_user_project(project_id, current_user["user_id"], session)
    result = await session.execute(
        select(SystemMaturity)
        .where(SystemMaturity.project_id == project_id)
        .order_by(SystemMaturity.score.asc())
    )
    rows = result.scalars().all()
    return [
        {
            "system_id": r.system_id,
            "score": r.score,
            "recommended_mode": r.recommended_mode,
            "top_issues": r.top_issues or [],
            "last_audited_at": r.last_audited_at.isoformat(),
        }
        for r in rows
    ]


# GET /api/admin/tasks/{task_id}/fix-loop-history
# ---------------------------------------------------------------------------


@router.get("/tasks/{task_id}/fix-loop-history")
async def get_task_fix_loop_history(
    task_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    current_user: dict = Depends(get_current_user),
):
    """Return the fix-loop telemetry persisted on a task.

    Per-loop attempt arrays + a small summary block (attempts count and
    last_exit_reason for lint/test/check). Auth follows the surrounding
    project drilldown pattern: load the task, then verify the caller has
    access to its project.
    """
    from app.fix_loop_telemetry import summarize_fix_loop_history

    task = (
        (await session.execute(select(Task).where(Task.id == task_id)))
        .scalars()
        .first()
    )
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")

    await _get_user_project(task.project_id, current_user["user_id"], session)

    history = task.fix_loop_history or {}
    return {
        "task_id": str(task.id),
        "fix_loop_history": history,
        "summary": summarize_fix_loop_history(history),
    }


# ---------------------------------------------------------------------------
# GET /api/admin/projects/{project_id}/recent-findings
# ---------------------------------------------------------------------------


@router.get("/projects/{project_id}/recent-findings")
async def get_recent_findings(
    project_id: uuid.UUID,
    limit: int = 50,
    session: AsyncSession = Depends(get_session),
    current_user: dict = Depends(get_current_user),
):
    """Return finalized research findings and their actionability distribution."""
    await _get_user_project(project_id, current_user["user_id"], session)
    cutoff = _utcnow() - timedelta(days=7)
    result = await session.execute(
        select(Task)
        .options(load_only(Task.id, Task.plan_output, Task.created_at, Task.context, raiseload=True))
        .where(
            Task.project_id == project_id,
            Task.status == TaskStatus.COMPLETED,
            Task.created_at >= cutoff,
            Task.plan_output.isnot(None),
            Task.context[FINALIZED_RESEARCH_FINDINGS_KEY].as_boolean().is_(True),
            Task.payloads[0]["type"].as_string() == "research",
        )
        .order_by(Task.created_at.desc())
        .limit(100)
    )
    tasks = result.scalars().all()

    findings_out: list[dict] = []
    grade_counts: dict[str, int] = {"HIGH": 0, "MEDIUM": 0, "LOW": 0}
    for t in tasks:
        findings = finalized_research_findings_from_state(
            t.plan_output,
            t.context,
            status=TaskStatus.COMPLETED,
            task_type="research",
        )
        for f in findings:
            if not f.get("title"):
                continue
            grade = f.get("actionability_grade", "MEDIUM")
            if grade not in grade_counts:
                grade = "MEDIUM"
            grade_counts[grade] = grade_counts.get(grade, 0) + 1
            findings_out.append(
                {
                    "task_id": str(t.id),
                    "title": f.get("title", ""),
                    "type": f.get("type", ""),
                    "scope": f.get("scope", ""),
                    "priority": f.get("priority", "medium"),
                    "actionability_grade": grade,
                    "direction_id": f.get("direction_id"),
                    "researched_at": t.created_at.isoformat() if t.created_at else None,
                }
            )
            if len(findings_out) >= limit:
                break
        if len(findings_out) >= limit:
            break

    total = sum(grade_counts.values())
    grade_pct = {
        g: round(c / total * 100, 1) if total > 0 else 0.0
        for g, c in grade_counts.items()
    }
    return {
        "findings": findings_out,
        "grade_distribution": grade_counts,
        "grade_distribution_pct": grade_pct,
        "total": total,
    }


# ---------------------------------------------------------------------------
# Knowledge-document store: list / search / get-by-id
#
# Durable, queryable home for web_research output (output_kind='knowledge').
# Project-scoped (router-level get_current_user + _get_user_project membership
# check), paginated, newest-first. Search matches title/summary/body/topic via
# ILIKE (trigram-GIN-backed on Postgres for title/body) plus a tag-list scan.
# Response shapes are stable for the frontend; the list shape omits the
# (potentially large) markdown body, which only the get-by-id endpoint returns.
# ---------------------------------------------------------------------------


@router.get("/projects/{project_id}/knowledge-documents")
async def list_knowledge_documents(
    project_id: uuid.UUID,
    q: str | None = Query(default=None, description="Full-text search term"),
    topic: str | None = Query(default=None, description="Exact topic filter"),
    tag: str | None = Query(default=None, description="Exact tag filter"),
    direction: str | None = Query(
        default=None, description="Exact direction-id filter"
    ),
    source_kind: str | None = Query(
        default=None, description="Exact source-kind filter (design / web_research)"
    ),
    origin_state: str | None = Query(
        default=None, description="Exact origin provenance state filter"
    ),
    challenge_state: str | None = Query(
        default=None, description="Exact origin challenge state filter"
    ),
    lifecycle_status: str | None = Query(
        default=None,
        description="Exact lifecycle-status filter (pending / consumed / dismissed)",
    ),
    finding_hash: str | None = Query(
        default=None, description="Exact originating research finding hash filter"
    ),
    sweep_id: str | None = Query(
        default=None, description="Exact sweep-id filter (compute_experiment sweeps)"
    ),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    session: AsyncSession = Depends(get_session),
    current_user: dict = Depends(get_current_user),
):
    """List / search a project's knowledge documents, newest-first, paginated.

    ``q`` matches title/summary/body/topic (ILIKE, trigram-backed on Postgres)
    and the tag list. ``topic`` / ``tag`` / ``direction`` / ``source_kind``
    apply exact filters (``source_kind`` scopes to a single producing task kind,
    e.g. ``design`` vs ``web_research``). The list shape omits the markdown body
    — use the get-by-id endpoint for the full document.

    Search/serialization is shared with the agent-facing endpoints via
    ``app.knowledge_query`` so behavior stays identical across surfaces.
    """
    await _get_user_project(project_id, current_user["user_id"], session)
    return await kq_list_knowledge_documents(
        session,
        project_id,
        q=q,
        topic=topic,
        tag=tag,
        direction=direction,
        source_kind=source_kind,
        origin_state=origin_state,
        challenge_state=challenge_state,
        lifecycle_status=lifecycle_status,
        finding_hash=finding_hash,
        sweep_id=sweep_id,
        limit=limit,
        offset=offset,
    )


@router.get("/projects/{project_id}/knowledge-documents/{document_id}")
async def get_knowledge_document(
    project_id: uuid.UUID,
    document_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    current_user: dict = Depends(get_current_user),
):
    """Return a single knowledge document (including its markdown body).

    Project-scoped: a document from another project (or a missing id) is a 404
    even for an authenticated user, after the membership check on the path
    project.
    """
    await _get_user_project(project_id, current_user["user_id"], session)
    doc = await kq_get_knowledge_document(session, project_id, document_id)
    if doc is None:
        raise HTTPException(status_code=404, detail="Knowledge document not found")
    return serialize_knowledge_document(doc, include_body=True)


# ---------------------------------------------------------------------------
# GET /api/admin/operator-inbox-liveness
# ---------------------------------------------------------------------------


@router.get("/operator-inbox-liveness")
async def get_operator_inbox_liveness(
    force_run: bool = Query(default=False),
    session: AsyncSession = Depends(get_session),
    current_user: dict = Depends(require_admin),
):
    """Return a snapshot of the operator-inbox liveness signal.

    Always returns the process-local observability ring (``total_ticks``,
    ``total_forward_leaked``, ``total_badge_invisible``,
    ``total_info_alert_noise``, ``recent_ticks``).

    When ``?force_run=true`` is passed, also triggers a live DB scan via
    ``run_operator_inbox_liveness_check`` (bypasses the cadence gate) and
    returns the per-project results under ``run_results``.

    Adds durable observation history (``durable_observations``) and open
    persistent-leak episodes (``open_episodes``) from the durable table.
    """
    from app.operator_inbox_liveness import (
        compute_undelivered_strategist_replies,
        get_operator_inbox_liveness_observability,
        run_operator_inbox_liveness_check,
    )
    from app.models import (
        OperatorInboxLivenessObservation,
        ProjectMessage,
        _utcnow as _ts,
    )

    snapshot = get_operator_inbox_liveness_observability()

    if force_run:
        run_results = await run_operator_inbox_liveness_check(
            session, _ts(), _force=True
        )
        snapshot = get_operator_inbox_liveness_observability()
        snapshot["run_results"] = [r._asdict() for r in (run_results or [])]

    # ── Undelivered strategist-reply aggregate ───────────────────────────────
    # Compute fleet-wide aggregates and per-project breakdown.  Error-isolated
    # inside compute_undelivered_strategist_replies; returned dict is always safe.
    try:
        undelivered_data = await compute_undelivered_strategist_replies(session)
        snapshot["undelivered_strategist_replies"] = undelivered_data
    except Exception:  # noqa: BLE001
        snapshot["undelivered_strategist_replies"] = {
            "total_count": 0,
            "total_projects_affected": 0,
            "oldest_age_seconds": None,
            "by_project": [],
        }

    # ── Undelivered-reply detail rows (force-run only) ───────────────────────
    if force_run:
        try:
            staleness_cutoff = _ts() - timedelta(minutes=15)
            from app.message_audience import operator_visible_message_audience_filter
            from sqlalchemy import or_ as _or

            detail_rows = (
                await session.execute(
                    select(
                        ProjectMessage.id,
                        ProjectMessage.project_id,
                        ProjectMessage.created_at,
                        ProjectMessage.reply_to_message_id,
                        ProjectMessage.context["_resident_reply_covered_message_ids"].as_string(),
                    )
                    .where(
                        ProjectMessage.from_type != "user",
                        ProjectMessage.delivered_at.is_(None),
                        ProjectMessage.created_at < staleness_cutoff,
                        _or(
                            ProjectMessage.reply_to_message_id.isnot(None),
                            ProjectMessage.context["_resident_reply_covered_message_ids"].as_string().isnot(None),
                        ),
                        operator_visible_message_audience_filter(ProjectMessage.context),
                    )
                    .order_by(ProjectMessage.created_at.asc())
                    .limit(200)
                )
            ).all()
            now = _ts()
            snapshot["undelivered_replies_detail"] = [
                {
                    "id": str(r.id),
                    "project_id": str(r.project_id),
                    "created_at": r.created_at.isoformat() if r.created_at else None,
                    "age_seconds": int((now - r.created_at).total_seconds())
                    if r.created_at
                    else None,
                    "reply_to_message_id": str(r.reply_to_message_id)
                    if r.reply_to_message_id
                    else None,
                    "covered_message_ids": (
                        r[4] if r[4] else None
                    ),
                }
                for r in detail_rows
            ]
        except Exception:  # noqa: BLE001
            snapshot["undelivered_replies_detail"] = []

    # ── Durable observation history (most recent N rows across all projects) ─
    try:
        durable_rows = (
            await session.execute(
                select(OperatorInboxLivenessObservation)
                .order_by(OperatorInboxLivenessObservation.created_at.desc())
                .limit(100)
            )
        ).scalars().all()
        snapshot["durable_observations"] = [
            {
                "id": str(r.id),
                "project_id": r.project_id,
                "status": r.status,
                "forward_leaked": r.forward_leaked,
                "badge_invisible": r.badge_invisible,
                "info_alert_noise": r.info_alert_noise,
                "detail": r.detail,
                "tick_number": r.tick_number,
                "created_at": r.created_at.isoformat() if r.created_at else None,
            }
            for r in durable_rows
        ]
    except Exception:  # noqa: BLE001
        snapshot["durable_observations"] = []

    # ── Open persistent-leak escalation episodes ───────────────────────────
    try:
        dedupe_prefix = "operator_inbox_leak:"
        open_episodes = (
            await session.execute(
                select(ProjectMessage)
                .where(
                    ProjectMessage.status == "open",
                    ProjectMessage.context["dedupe_key"].as_string().like(
                        f"{dedupe_prefix}%"
                    ),
                )
                .order_by(ProjectMessage.created_at.desc())
                .limit(50)
            )
        ).scalars().all()
        snapshot["open_episodes"] = [
            {
                "id": str(m.id),
                "project_id": str(m.project_id),
                "content": m.content[:500] if m.content else "",
                "context": m.context or {},
                "created_at": m.created_at.isoformat() if m.created_at else None,
            }
            for m in open_episodes
        ]
    except Exception:  # noqa: BLE001
        snapshot["open_episodes"] = []

    return snapshot


# ---------------------------------------------------------------------------
# Adversarial spawn-gate canary endpoints
# ---------------------------------------------------------------------------


@router.post("/projects/{project_id}/canary/adversarial-challenge")
async def spawn_canary_adversarial_challenge(
    project_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    current_user: dict = Depends(get_current_user),
):
    """Spawn a canary adversarial-challenge task to verify the spawn gate is healthy.

    Creates a synthetic source task and probe finding, then calls the real
    adversarial challenger path (same as high-stakes research findings use).
    The canary source task never spawns implementation work; its sole purpose is
    to carry the challenger's verdict so operators can confirm the gate returns
    real verdicts rather than silently fail-closing.

    Returns ``{canary_task_id, grader_task_id}``.
    """
    # Member+ floor, matching the sibling reset_circuit_breaker: this commits a
    # synthetic Task and spawns a REAL agent task that executes on a VM (LLM
    # spend + compute), which membership alone does not authorize.
    await _get_user_project_with_role(
        project_id, current_user["user_id"], session, "member"
    )

    probe = {
        "title": _CANARY_PROBE_TITLE,
        "description": _CANARY_PROBE_DESC,
        "risk": "high",
        "scope": "large",
        "hash": _CANARY_PROBE_HASH,
    }
    manifest_items, manifest_json = build_adversarial_challenge_manifest([probe])
    if not manifest_items:
        raise HTTPException(status_code=500, detail="Failed to build canary manifest")

    # The payload type "canary_spawn_gate_probe" has no registered capability, so
    # task_lifecycle._resolve_quality_grader_completion will write the verdict and
    # leave the source COMPLETED without dispatching to any spawn handler — ensuring
    # the canary never produces implementation tasks.
    source = Task(
        project_id=project_id,
        title="[CANARY] adversarial spawn-gate health probe",
        description=(
            "On-demand canary to verify the adversarial spawn gate returns real verdicts "
            "from the currently-serving agent image."
        ),
        payloads=[
            {
                "type": "canary_spawn_gate_probe",
                "base_branch": "main",
                "scope": "small",
            }
        ],
        context={"_is_canary": True},
        plan_output=[probe],
        vm_instance_id=f"agent-vm-{uuid.uuid4().hex}",
        status=TaskStatus.COMPLETED,
        created_by="ORCHESTRATOR",
        priority=5,
    )
    session.add(source)
    await session.commit()
    await session.refresh(source)

    grader = await spawn_forward_quality_grader(
        source,
        manifest_json,
        session,
        adversarial_mode=True,
        finding_hashes=[_CANARY_PROBE_HASH],
    )
    if grader is None:
        raise HTTPException(
            status_code=409,
            detail="Canary grader already in flight or verdict already recorded",
        )

    return {
        "canary_task_id": str(source.id),
        "grader_task_id": str(grader.id),
    }


@router.get("/projects/{project_id}/canary/adversarial-challenge/{canary_task_id}")
async def get_canary_adversarial_challenge_result(
    project_id: uuid.UUID,
    canary_task_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    current_user: dict = Depends(get_current_user),
):
    """Return the classification of a previously spawned canary challenge.

    ``status`` is one of:
    - ``"pending"`` — the grader has not yet returned a verdict
    - ``"real_verdict"`` — the grader returned at least one explicit pass/fail verdict
    - ``"fail_closed"`` — the grader returned no parseable verdicts (gate is broken)

    Also includes ``parse_outcome`` (the raw parse string) and ``explicit_count``
    (number of findings with an explicit verdict).
    """
    await _get_user_project(project_id, current_user["user_id"], session)

    canary = (
        (
            await session.execute(
                select(Task).where(
                    Task.id == canary_task_id, Task.project_id == project_id
                )
            )
        )
        .scalars()
        .first()
    )
    if canary is None or not (canary.context or {}).get("_is_canary"):
        raise HTTPException(status_code=404, detail="Canary task not found")

    gate_record = (canary.context or {}).get(ADVERSARIAL_GATE_VERDICT_KEY)
    result = classify_canary_spawn_gate_result(gate_record)
    result["canary_task_id"] = str(canary_task_id)
    grader_task_id = (canary.context or {}).get("awaiting_adversarial_grader")
    if isinstance(grader_task_id, str):
        result["grader_task_id"] = grader_task_id
    elif gate_record:
        result["grader_task_id"] = gate_record.get("grader_task_id")
    return result

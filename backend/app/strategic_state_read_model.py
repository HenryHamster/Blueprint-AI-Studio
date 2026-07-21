"""Pure read-model helpers for the strategic-state endpoint.

Formatting utilities, task-focus builders, and strategic summary projections
extracted from routers/admin_project_strategic_session.py.
"""

import uuid
from typing import Dict, Any
from sqlalchemy import func, case, and_
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import load_only
from sqlmodel import select

from app.models import (
    CYCLE_META_INTEGRITY_CONFLICT_COUNTS,
    Project,
    ProjectMessage,
    StrategicCycle,
    Task,
    TaskStatus,
)
from app import strategic_cycle
from app.shared_state import runtime_config
from app.strategic_constants import STATUS_LATEST_MESSAGE_MAX
# [RESIDENT-REMOVAL] removed with resident subsystem
# from app.strategic_resident_session import (
#     RESIDENT_SESSION_REASON_CYCLE_IN_FLIGHT,
#     RESIDENT_SESSION_REASON_RUNNER_OFFLINE,
#     RESIDENT_SESSION_REASON_START_FAILED_PREFIX,
#     STRATEGIC_SESSION_ENABLED_SETTING,
#     read_resident_session_blocked_reason,
#     resident_session_snapshot,
# )
from app.task_movement import build_task_movement_explanation

TASK_FOCUS_BUCKET_LIMIT = 8

_LIVE_STRATEGIC_TASK_STATUSES = frozenset(
    {
        TaskStatus.QUEUED,
        TaskStatus.PROVISIONING,
        TaskStatus.IN_PROGRESS,
        TaskStatus.EVALUATING,
        TaskStatus.BLOCKED,
        TaskStatus.PARKED,
    }
)


def _enum_value(value: Any) -> str:
    return value.value if hasattr(value, "value") else str(value)


def _compact_text(value: Any, *, max_chars: int = 360) -> str | None:
    if value is None:
        return None
    text = " ".join(str(value).strip().split())
    if not text:
        return None
    if len(text) <= max_chars:
        return text
    return f"{text[: max_chars - 1]}…"


def _dedupe_workbench_refs(refs: Any, *, limit: int = 12) -> list[Dict[str, Any]]:
    if not isinstance(refs, list):
        return []
    seen: set[tuple[str, str]] = set()
    deduped: list[Dict[str, Any]] = []
    for ref in refs:
        if not isinstance(ref, dict):
            continue
        kind = _compact_text(ref.get("kind"), max_chars=40)
        ref_id = _compact_text(ref.get("id"), max_chars=160)
        if not kind or not ref_id:
            continue
        key = (kind, ref_id)
        if key in seen:
            continue
        seen.add(key)
        item: Dict[str, Any] = {"kind": kind, "id": ref_id}
        label = _compact_text(ref.get("label"), max_chars=180)
        if label:
            item["label"] = label
        deduped.append(item)
        if len(deduped) >= limit:
            break
    return deduped


def _message_selected_refs(message: ProjectMessage) -> list[Dict[str, Any]]:
    context = message.context if isinstance(message.context, dict) else {}
    strategic_session = context.get("strategic_session")
    if not isinstance(strategic_session, dict):
        return []
    return _dedupe_workbench_refs(strategic_session.get("selected_refs"))


def _collect_selected_refs_for_resolution(
    *ref_groups: Any,
    limit: int = 48,
) -> list[Dict[str, Any]]:
    selected_refs: list[Dict[str, Any]] = []
    for refs in ref_groups:
        if isinstance(refs, ProjectMessage):
            selected_refs.extend(_message_selected_refs(refs))
        elif isinstance(refs, list):
            selected_refs.extend(_dedupe_workbench_refs(refs, limit=limit))
    return _dedupe_workbench_refs(selected_refs, limit=limit)


def _is_live_strategic_task(task: Task | None) -> bool:
    return bool(task is not None and task.status in _LIVE_STRATEGIC_TASK_STATUSES)


def _task_summary(task: Task | None) -> Dict[str, Any] | None:
    if task is None:
        return None
    payload = task.first_payload or {}
    return {
        "id": str(task.id),
        "status": _enum_value(task.status),
        "title": task.title,
        "task_type": payload.get("type"),
        # Enforce the declared STATUS_LATEST_MESSAGE_MAX cap on the primary
        # status message this projection surfaces (the sibling _task_focus_item
        # already compacts the same field; this exposure was previously
        # unbounded).
        "latest_status_message": _compact_text(
            task.latest_status_message, max_chars=STATUS_LATEST_MESSAGE_MAX
        ),
        "started_at": task.started_at.isoformat() if task.started_at else None,
        "created_at": task.created_at.isoformat() if task.created_at else None,
    }


def _task_focus_reasons(task: Task) -> list[str]:
    reasons: list[str] = []
    status = task.status
    if task.requires_approval and status not in (
        TaskStatus.COMPLETED,
        TaskStatus.FAILED,
        TaskStatus.CANCELLED,
    ):
        reasons.append("approval")
    if status == TaskStatus.BLOCKED:
        reasons.append("blocked")
    elif status == TaskStatus.PARKED:
        reasons.append("parked")
    elif status == TaskStatus.FAILED:
        reasons.append("failed")
    elif status == TaskStatus.EVALUATING:
        reasons.append("evaluating")
    elif status in (TaskStatus.IN_PROGRESS, TaskStatus.PROVISIONING):
        reasons.append("active")
    elif status == TaskStatus.QUEUED:
        reasons.append("queued")
    return reasons


def _task_focus_item(
    task: Task,
    *,
    bucket: str,
    task_type: str | None = None,
    direction_id: str | None = None,
) -> Dict[str, Any]:
    status = _enum_value(task.status)
    detail = (
        task.blocked_reason
        or task.park_reason
        or task.latest_status_message
    )
    movement_explanation = build_task_movement_explanation(task)
    return {
        "id": str(task.id),
        "bucket": bucket,
        "status": status,
        "title": _compact_text(task.title, max_chars=180),
        "task_type": _compact_text(task_type, max_chars=80),
        "latest_status_message": _compact_text(task.latest_status_message, max_chars=180),
        "detail": _compact_text(detail, max_chars=220),
        "movement_explanation": _compact_text(movement_explanation, max_chars=220),
        "requires_approval": bool(task.requires_approval),
        "reasons": _task_focus_reasons(task),
        "direction_id": direction_id,
        "parent_task_id": str(task.parent_task_id) if task.parent_task_id else None,
        "blocker_kind": _compact_text(task.blocker_kind, max_chars=80),
        "blocked_reason": _compact_text(task.blocked_reason, max_chars=180),
        "blocked_at": task.blocked_at.isoformat() if task.blocked_at else None,
        "recheck_after": task.recheck_after.isoformat() if task.recheck_after else None,
        "park_reason": _compact_text(task.park_reason, max_chars=180),
        "parked_at": task.parked_at.isoformat() if task.parked_at else None,
        "created_at": task.created_at.isoformat() if task.created_at else None,
        "started_at": task.started_at.isoformat() if task.started_at else None,
        "completed_at": task.completed_at.isoformat() if task.completed_at else None,
    }


async def _task_focus_bucket(
    session: AsyncSession,
    project_id: uuid.UUID,
    *,
    bucket: str,
    filters: list[Any],
    order_by: list[Any],
    limit: int = TASK_FOCUS_BUCKET_LIMIT,
    total: int | None = None,
) -> Dict[str, Any]:
    base_filters = [
        Task.project_id == project_id,
        Task.archived.is_(False),
        *filters,
    ]
    direction_id_expr = func.coalesce(
        Task.context["direction_id"].as_string(),
        Task.context["current_direction_id"].as_string(),
        Task.payloads[0]["direction_id"].as_string(),
        Task.payloads[0]["current_direction_id"].as_string(),
        Task.payloads[0]["source_direction_id"].as_string(),
        Task.payloads[0]["target_direction_id"].as_string(),
    ).label("direction_id")
    task_type_expr = Task.payloads[0]["type"].as_string().label("task_type")
    if total is None:
        count_result = await session.execute(
            select(func.count()).select_from(Task).where(*base_filters)
        )
        total = count_result.scalar() or 0
    total = int(total)
    if total == 0:
        return {"total": 0, "limit": limit, "has_more": False, "items": []}
    rows_result = await session.execute(
        select(Task, task_type_expr, direction_id_expr)
        .where(*base_filters)
        .options(
            load_only(
                Task.id,
                Task.status,
                Task.title,
                Task.latest_status_message,
                Task.requires_approval,
                Task.parent_task_id,
                Task.created_at,
                Task.started_at,
                Task.completed_at,
                Task.depends_on,
                Task.blocker_kind,
                Task.blocked_reason,
                Task.blocked_at,
                Task.recheck_after,
                Task.park_reason,
                Task.parked_at,
                raiseload=True,
            )
        )
        .order_by(*order_by)
        .limit(limit)
    )
    rows = rows_result.all()
    return {
        "total": total,
        "limit": limit,
        "has_more": total > limit,
        "items": [
            _task_focus_item(
                task,
                bucket=bucket,
                task_type=task_type,
                direction_id=direction_id,
            )
            for task, task_type, direction_id in rows
        ],
    }


async def _build_task_focus(
    session: AsyncSession,
    project_id: uuid.UUID,
) -> Dict[str, Any]:
    nonterminal = (
        TaskStatus.QUEUED,
        TaskStatus.PROVISIONING,
        TaskStatus.IN_PROGRESS,
        TaskStatus.EVALUATING,
        TaskStatus.BLOCKED,
        TaskStatus.PARKED,
    )
    # Single query replaces five individual COUNTs (10 → 6 round-trips total).
    counts_row = (await session.execute(
        select(
            func.count(case((
                and_(Task.requires_approval.is_(True), Task.status.in_(nonterminal)),
                1,
            ))).label("needs_judgment"),
            func.count(case((
                Task.status.in_([
                    TaskStatus.PROVISIONING,
                    TaskStatus.IN_PROGRESS,
                    TaskStatus.EVALUATING,
                ]),
                1,
            ))).label("active"),
            func.count(case((
                Task.status.in_([TaskStatus.FAILED, TaskStatus.BLOCKED, TaskStatus.PARKED]),
                1,
            ))).label("friction"),
            func.count(case((
                and_(Task.status == TaskStatus.QUEUED, Task.requires_approval.is_(False)),
                1,
            ))).label("queued"),
            func.count(case((
                Task.status == TaskStatus.COMPLETED,
                1,
            ))).label("recent_completed"),
        ).select_from(Task).where(
            Task.project_id == project_id,
            Task.archived.is_(False),
        )
    )).one()

    needs_judgment_result = await _task_focus_bucket(
        session,
        project_id,
        bucket="needs_judgment",
        filters=[
            Task.requires_approval.is_(True),
            Task.status.in_(nonterminal),
        ],
        order_by=[Task.created_at.asc(), Task.id.asc()],
        total=int(counts_row.needs_judgment or 0),
    )
    active_result = await _task_focus_bucket(
        session,
        project_id,
        bucket="active",
        filters=[
            Task.status.in_(
                [
                    TaskStatus.PROVISIONING,
                    TaskStatus.IN_PROGRESS,
                    TaskStatus.EVALUATING,
                ]
            )
        ],
        order_by=[Task.started_at.desc().nullslast(), Task.created_at.desc(), Task.id.desc()],
        total=int(counts_row.active or 0),
    )
    friction_result = await _task_focus_bucket(
        session,
        project_id,
        bucket="friction",
        filters=[
            Task.status.in_(
                [TaskStatus.FAILED, TaskStatus.BLOCKED, TaskStatus.PARKED]
            )
        ],
        order_by=[Task.created_at.desc(), Task.id.desc()],
        total=int(counts_row.friction or 0),
    )
    queued_result = await _task_focus_bucket(
        session,
        project_id,
        bucket="queued",
        filters=[Task.status == TaskStatus.QUEUED, Task.requires_approval.is_(False)],
        order_by=[Task.created_at.asc(), Task.id.asc()],
        total=int(counts_row.queued or 0),
    )
    recent_completed_result = await _task_focus_bucket(
        session,
        project_id,
        bucket="recent_completed",
        filters=[Task.status == TaskStatus.COMPLETED],
        order_by=[Task.completed_at.desc().nullslast(), Task.created_at.desc(), Task.id.desc()],
        total=int(counts_row.recent_completed or 0),
    )
    buckets = {
        "needs_judgment": needs_judgment_result,
        "active": active_result,
        "friction": friction_result,
        "queued": queued_result,
        "recent_completed": recent_completed_result,
    }
    return {
        "version": 1,
        "bucket_limit": TASK_FOCUS_BUCKET_LIMIT,
        "buckets": buckets,
        "has_more": any(bucket["has_more"] for bucket in buckets.values()),
    }


def _cycle_summary(
    cycle: StrategicCycle | None,
    *,
    active_task: Task | None = None,
) -> Dict[str, Any] | None:
    if cycle is None:
        return None
    metadata = cycle.cycle_metadata or {}
    has_active_task = _is_live_strategic_task(active_task)
    return {
        "id": cycle.id,
        "outcome": cycle.outcome,
        "has_active_task": bool(has_active_task),
        "stale": cycle.outcome in strategic_cycle.ACTIVE_OUTCOMES
        and not has_active_task,
        "phase1_task_id": str(cycle.phase1_task_id) if cycle.phase1_task_id else None,
        "phase2_task_id": str(cycle.phase2_task_id) if cycle.phase2_task_id else None,
        "phase1_started_at": (
            cycle.phase1_started_at.isoformat() if cycle.phase1_started_at else None
        ),
        "phase1_completed_at": (
            cycle.phase1_completed_at.isoformat()
            if cycle.phase1_completed_at
            else None
        ),
        "phase2_started_at": (
            cycle.phase2_started_at.isoformat() if cycle.phase2_started_at else None
        ),
        "phase2_completed_at": (
            cycle.phase2_completed_at.isoformat()
            if cycle.phase2_completed_at
            else None
        ),
        "summary_text": _compact_text(cycle.summary_text, max_chars=300),
        "failure_reason": _compact_text(cycle.failure_reason, max_chars=240),
        "directions_created": cycle.directions_created,
        "directions_retired": cycle.directions_retired,
        "tasks_spawned": cycle.tasks_spawned,
        "vision_updated": cycle.vision_updated,
        "problem_model_changed": bool(metadata.get("problem_model_changed")),
        "running_update_changed": bool(metadata.get("running_update_changed")),
        "integrity_conflict_counts": metadata.get(CYCLE_META_INTEGRITY_CONFLICT_COUNTS),
    }


def _direction_summary(directions: Any) -> Dict[str, Any]:
    rows = directions if isinstance(directions, list) else []
    active = [
        d
        for d in rows
        if isinstance(d, dict) and (d.get("status") or "active") == "active"
    ]
    paused = [
        d
        for d in rows
        if isinstance(d, dict) and (d.get("status") or "active") == "paused"
    ]
    priority_rank = {"high": 0, "medium": 1, "low": 2}
    primary = None
    if active:
        primary = sorted(
            active,
            key=lambda d: (
                priority_rank.get(str(d.get("priority") or "medium"), 1),
                str(d.get("id") or ""),
            ),
        )[0]
    return {
        "total_count": len([d for d in rows if isinstance(d, dict)]),
        "active_count": len(active),
        "paused_count": len(paused),
        "primary": {
            "id": str(primary.get("id") or ""),
            "text": _compact_text(primary.get("text"), max_chars=220),
            "priority": primary.get("priority") or "medium",
            "status": primary.get("status") or "active",
            "mode": primary.get("recommended_mode") or primary.get("mode"),
        }
        if primary
        else None,
    }


def _problem_model_summary(model: Any) -> Dict[str, Any]:
    data = model if isinstance(model, dict) else {}
    intent = data.get("intent") if isinstance(data.get("intent"), dict) else {}
    gaps = data.get("gaps") if isinstance(data.get("gaps"), list) else []
    capabilities = (
        data.get("capabilities") if isinstance(data.get("capabilities"), list) else []
    )
    pending = (
        data.get("pending_user_confirmation")
        if isinstance(data.get("pending_user_confirmation"), list)
        else []
    )
    active_gaps = [
        g
        for g in gaps
        if isinstance(g, dict) and str(g.get("status") or "active") == "active"
    ]
    priority_rank = {"high": 0, "medium": 1, "low": 2}
    top_gap = None
    if active_gaps:
        top_gap = sorted(
            active_gaps,
            key=lambda g: (
                priority_rank.get(str(g.get("priority") or "medium"), 1),
                str(g.get("id") or ""),
            ),
        )[0]
    return {
        "revision": data.get("revision"),
        "last_updated": data.get("last_updated"),
        "current_focus": _compact_text(intent.get("current_focus"), max_chars=260),
        "purpose": _compact_text(intent.get("purpose"), max_chars=260),
        "active_gap_count": len(active_gaps),
        "capability_count": len([c for c in capabilities if isinstance(c, dict)]),
        "pending_confirmation_count": len([p for p in pending if isinstance(p, dict)]),
        "top_gap": {
            "id": str(top_gap.get("id") or ""),
            "name": _compact_text(top_gap.get("name"), max_chars=160),
            "priority": top_gap.get("priority") or "medium",
            "status": top_gap.get("status") or "active",
        }
        if top_gap
        else None,
    }


def _running_update_summary(project: Project) -> Dict[str, Any]:
    body = project.running_update or ""
    return {
        "excerpt": _compact_text(body, max_chars=420),
        "char_count": len(body),
        "acknowledged_at": (
            project.running_update_acknowledged_at.isoformat()
            if project.running_update_acknowledged_at
            else None
        ),
    }


def _strategic_refresh_summary(vision: Dict[str, Any] | None) -> Dict[str, Any]:
    vision = vision or {}
    marker = vision.get("_strategic_refresh")
    pending = None
    if isinstance(marker, dict):
        pending = {
            "requested_at": _compact_text(marker.get("requested_at"), max_chars=80),
            "reason": _compact_text(marker.get("reason"), max_chars=80),
            "origin": _compact_text(marker.get("origin"), max_chars=40),
            "count": int(marker.get("count") or 0)
            if isinstance(marker.get("count"), (int, float, str))
            and str(marker.get("count")).strip().isdigit()
            else 0,
            "operator_count": int(marker.get("operator_count") or 0)
            if isinstance(marker.get("operator_count"), (int, float, str))
            and str(marker.get("operator_count")).strip().isdigit()
            else 0,
            "excerpt": _compact_text(marker.get("excerpt"), max_chars=280),
        }
    return {
        "pending": pending,
        "last_fired_at": _compact_text(
            vision.get("_strategic_refresh_last_fired_at"), max_chars=80
        ),
        "last_fired_origin": _compact_text(
            vision.get("_strategic_refresh_last_fired_origin"), max_chars=40
        ),
    }


def _resident_session_summary(project: Project) -> Dict[str, Any] | None:
    """Bounded projection of the resident strategic-session state blob.

    Reads the session record stored under ``Project.vision`` (no queries) via the
    authoritative ``resident_session_snapshot`` helper so the operator UI polling
    the strategic-state read model can tell whether a resident session is active
    and its status. Returns ``None`` when no session record exists.
    """
    return resident_session_snapshot(project.vision)  # noqa: F821


def _resident_session_available() -> bool:
    """Whether resident strategic sessions are currently enabled.

    Mirrors the ``strategic_session_enabled`` runtime-config gate enforced by the
    resident-session start endpoint. Surfaced on the read model so the operator
    UI can hide the Start affordance when the feature is off, instead of letting
    a click 403 at the start endpoint.
    """
    return bool(runtime_config.get(STRATEGIC_SESSION_ENABLED_SETTING, False))  # noqa: F821


def _resident_session_has_begun_streaming(snapshot: Dict[str, Any] | None) -> bool:
    """Whether a live resident session's agent has begun (vs still starting).

    Derived purely from the vision snapshot (no queries). The agent has begun
    when either progress has been reported (``last_progress_at`` — advanced by
    the resident heartbeat on an applied strategic op) OR a post-start operator
    interaction landed. The start seeds ``last_operator_interaction_at`` equal to
    ``started_at``, so a *later* (differing) interaction timestamp means the
    operator<->agent loop is live. Absent both, the session is still bringing up
    (task pinning / agent not yet streaming) — the ``starting`` state.
    """
    if not isinstance(snapshot, dict):
        return False
    if snapshot.get("last_progress_at"):
        return True
    started = snapshot.get("started_at")
    last_op = snapshot.get("last_operator_interaction_at")
    return bool(last_op and started and last_op != started)


def _resident_selected_runner_offline(
    snapshot: Dict[str, Any] | None,
    runner_options: list[Dict[str, Any]] | None,
) -> bool:
    """True iff a live session is pinned to a device that is now offline/absent.

    Reads the pinned ``runner_id`` off the session snapshot and checks it against
    the live runner-picker options: offline iff the device is missing from the set
    (de-enrolled) OR present but not ``online``. Returns False when the session is
    unpinned (no ``runner_id``) — an older cloud-fallback session — so only a
    device-pinned session can flip to the offline block. This is the mid-session
    half of block-on-offline; Workday does NOT auto-migrate, so the UI prompts the
    operator to restart and pick another device (operator decision (c)).
    """
    if not isinstance(snapshot, dict):
        return False
    runner_id = snapshot.get("runner_id")
    if not isinstance(runner_id, str) or not runner_id.strip():
        return False
    runner_id = runner_id.strip()
    options = runner_options if isinstance(runner_options, list) else []
    match = next(
        (
            option
            for option in options
            if isinstance(option, dict) and str(option.get("id")) == runner_id
        ),
        None,
    )
    return match is None or not bool(match.get("online"))


def _resident_session_availability(
    project: Project,
    *,
    eligible_live_runner_count: int | None = None,
    runner_options: list[Dict[str, Any]] | None = None,
) -> Dict[str, Any]:
    """Legible Workday resident-session state + the reason it isn't running.

    Turns these signals — the ``strategic_session_enabled`` flag, the live
    session blob (+ its pinned ``runner_id``), the recorded ``no_runner`` /
    ``start_failed`` reason, the eligible-live-runner count, and the live
    runner-picker ``runner_options`` — into a single state the operator UI renders
    without re-deriving backend rules:

    * ``off``      — feature flag off (reason ``sessions_disabled``).
    * ``starting`` — a session blob exists (created + task pinning) but the agent
      has not begun streaming yet (no progress, no post-start operator
      interaction) — the transient bring-up window.
    * ``active``   — a resident session is live and the agent has begun
      (runner-attached / replying).
    * ``error``    — the last auto-start failed (reason ``start_failed: <msg>``).
    * ``blocked``  — enabled but can't run: either the pinned device went offline
      mid-session (reason ``runner_offline`` — the UI prompts a restart to pick
      another device, NO auto-migration), or no live local runner exists
      (reason ``no_runner``), whether recorded by the last auto-start or inferred
      from a zero eligible-runner count.
    * ``ready``    — enabled, a live runner is attached, nothing started yet
      (entering Workday auto-starts, so this is transient).

    Block-on-offline takes precedence for a live session: a session pinned to a
    now-offline device reports ``blocked``/``runner_offline`` even though the blob
    is still ``active`` (the operator must restart to re-pick). Otherwise a
    recorded ``start_failed`` always wins — a real failure is never masked by a
    runner count. A recorded ``no_runner`` is only authoritative while the live
    count is still 0/unknown: a POSITIVE ``eligible_live_runner_count`` OVERRIDES a
    stale ``no_runner`` (recorded when the operator entered Workday with no runner,
    and never cleared — no mode transition re-runs the auto-start hook when a
    runner later attaches) and reports ``ready`` instead of a phantom "blocked: no
    runner" with a runner plainly attached.
    """
    enabled = bool(runtime_config.get(STRATEGIC_SESSION_ENABLED_SETTING, False))  # noqa: F821
    snapshot = resident_session_snapshot(project.vision)  # noqa: F821
    active = bool(
        isinstance(snapshot, dict)
        and snapshot.get("status") == "active"
        and not snapshot.get("stop_requested")
    )
    recorded_reason = read_resident_session_blocked_reason(project.vision)  # noqa: F821
    recorded_start_failed = bool(
        recorded_reason
        and recorded_reason.startswith(RESIDENT_SESSION_REASON_START_FAILED_PREFIX)  # noqa: F821
    )
    has_live_runner = (
        eligible_live_runner_count is not None and eligible_live_runner_count > 0
    )

    reason: str | None = None
    if not enabled:
        state = "off"
        reason = "sessions_disabled"
    elif recorded_start_failed:
        # A recorded ``start_failed`` is authoritative and never masked by a live
        # runner count or a transient active blob — the last auto-start genuinely errored.
        reason = recorded_reason
        state = "error"
    elif active:
        # Block-on-offline: a live session pinned to a device that is now offline
        # (or de-enrolled) flips to ``blocked``/``runner_offline`` — the operator
        # restarts to pick another device (no auto-migration). Otherwise
        # distinguish the transient ``starting`` bring-up window (task pinning,
        # agent not yet streaming) from a genuinely ``active`` session.
        if _resident_selected_runner_offline(snapshot, runner_options):
            state = "blocked"
            reason = RESIDENT_SESSION_REASON_RUNNER_OFFLINE  # noqa: F821
        else:
            state = (
                "active"
                if _resident_session_has_begun_streaming(snapshot)
                else "starting"
            )
    elif recorded_reason == RESIDENT_SESSION_REASON_CYCLE_IN_FLIGHT:  # noqa: F821
        # A batch strategic cycle is in flight — not an error, just a transient
        # wait. The completion handler re-tries the auto-start when the blocking
        # cycle finishes, so this resolves on its own. Slot before the
        # has_live_runner check so a live runner does not hide the conflict.
        reason = recorded_reason
        state = "blocked"
    elif has_live_runner:
        # A live runner is plainly attached. A positive count OVERRIDES a stale
        # recorded ``no_runner`` — it is only authoritative while the live count
        # is still 0. Nothing has started yet, so this is the transient ``ready``
        # state (entering Workday auto-starts).
        state = "ready"
    elif recorded_reason:
        # Enabled, no live runner attached, and a (non-``start_failed``) reason —
        # ``no_runner`` — recorded: still authoritative while the live count is 0
        # or unknown.
        reason = recorded_reason
        state = "blocked"
    elif eligible_live_runner_count is not None and eligible_live_runner_count <= 0:
        state = "blocked"
        reason = "no_runner"
    else:
        state = "ready"
    return {"available": enabled, "state": state, "reason": reason}


# Concise, operator-facing recovery hints keyed by conflict class. Kept short
# so both the admin workspace panel and the agent section renderer can surface
# the same actionable text without re-deriving it per surface.
_RECOVERY_HINTS: dict[str, str] = {
    "orphaned_gap": "Assign an active direction to this gap or record a deferral.",
    "ungrounded_no_gap_id": "Link this direction to an open gap or retire it.",
    "ungrounded_gap_not_active": "Reground this direction to an active gap — its current gap is closed.",
    "stale_active_moves": "Reconcile the gap's active_moves — the listed directions are no longer active.",
    "criterion_inactive": "Remap to an active criterion — this key is retracted or superseded.",
    "criterion_dangling": "Remap to a criterion in the open version — this key is unknown there.",
}


def compute_model_integrity_conflicts(
    problem_model: Any,
    directions: Any,
    *,
    criterion_links: Any = None,
    active_criterion_keys: Any = None,
    inactive_criterion_keys: Any = None,
) -> dict | None:
    """Compute integrity conflict classes from the live problem model.

    Returns None when the model is clean (no conflicts). When conflicts exist,
    returns a dict with four lists — orphaned_gaps, ungrounded_directions,
    stale_active_moves, criterion_link_conflicts — each entry carrying a
    concise ``recovery_hint``, plus aggregate counts.

    Gap integrity (the first three classes) is derived purely from
    ``problem_model`` and ``directions``. Criterion-mapping integrity is
    optional: pass ``criterion_links`` (a list of ``{"id","text","status",
    "criterion_keys"}`` dicts sourced from Direction rows) together with the
    open version's ``active_criterion_keys`` / ``inactive_criterion_keys``
    frozensets. When those are omitted, criterion conflicts are simply not
    computed and the returned shape stays backward compatible.

    Pure and side-effect-free; operates only on the data handed in.
    """
    pm = problem_model if isinstance(problem_model, dict) else {}
    dirs = directions if isinstance(directions, list) else []

    gaps: list[Any] = pm.get("gaps") if isinstance(pm.get("gaps"), list) else []

    active_dirs = [
        d for d in dirs
        if isinstance(d, dict) and (d.get("status") or "active") == "active"
    ]
    active_dir_ids: set[str] = {str(d["id"]) for d in active_dirs if d.get("id")}
    referenced_gap_ids: set[str] = {
        str(d["gap_id"]) for d in active_dirs if d.get("gap_id")
    }
    active_gap_ids: set[str] = {
        str(g["id"])
        for g in gaps
        if isinstance(g, dict) and g.get("id") and g.get("status") == "active"
    }

    # 1. Open gaps with no active direction linked and no explicit deferral.
    orphaned_gaps: list[dict] = []
    for g in gaps:
        if not isinstance(g, dict) or g.get("status") not in ("active", "blocked"):
            continue
        gap_id = str(g.get("id") or "")
        if not gap_id or g.get("deferral"):
            continue
        if gap_id not in referenced_gap_ids:
            orphaned_gaps.append({
                "id": gap_id,
                "name": str(g.get("name") or "")[:80],
                "status": g.get("status") or "active",
                "recovery_hint": _RECOVERY_HINTS["orphaned_gap"],
            })

    # 2. Active directions whose gap_id is absent or points to a non-active gap.
    ungrounded_directions: list[dict] = []
    for d in active_dirs:
        gap_id = d.get("gap_id")
        if not gap_id or str(gap_id) not in active_gap_ids:
            reason = "no_gap_id" if not gap_id else "gap_not_active"
            ungrounded_directions.append({
                "id": str(d.get("id") or ""),
                "text": str(d.get("text") or "")[:120],
                "gap_id": str(gap_id) if gap_id else None,
                "reason": reason,
                "recovery_hint": _RECOVERY_HINTS[
                    "ungrounded_no_gap_id" if reason == "no_gap_id"
                    else "ungrounded_gap_not_active"
                ],
            })

    # 3. Gaps whose active_moves list cites direction IDs that are no longer active.
    stale_active_moves: list[dict] = []
    for g in gaps:
        if not isinstance(g, dict) or g.get("status") not in ("active", "blocked"):
            continue
        gap_id = str(g.get("id") or "")
        active_moves = g.get("active_moves")
        if not isinstance(active_moves, list) or not active_moves:
            continue
        stale_ids = [
            m for m in active_moves
            if isinstance(m, str) and m not in active_dir_ids
        ]
        if stale_ids:
            stale_active_moves.append({
                "gap_id": gap_id,
                "gap_name": str(g.get("name") or "")[:80],
                "stale_direction_ids": stale_ids[:8],
                "recovery_hint": _RECOVERY_HINTS["stale_active_moves"],
            })

    # 4. Active directions whose criterion_keys point at inactive or unknown
    #    (dangling / wrong-version) acceptance criteria for the open version.
    criterion_link_conflicts = _compute_criterion_link_conflicts(
        criterion_links,
        active_criterion_keys,
        inactive_criterion_keys,
    )

    if (
        not orphaned_gaps
        and not ungrounded_directions
        and not stale_active_moves
        and not criterion_link_conflicts
    ):
        return None

    return {
        "orphaned_gaps": orphaned_gaps,
        "ungrounded_directions": ungrounded_directions,
        "stale_active_moves": stale_active_moves,
        "criterion_link_conflicts": criterion_link_conflicts,
        "counts": {
            "orphaned_gaps": len(orphaned_gaps),
            "ungrounded_directions": len(ungrounded_directions),
            "stale_active_moves": len(stale_active_moves),
            "criterion_link_conflicts": len(criterion_link_conflicts),
            "total": (
                len(orphaned_gaps)
                + len(ungrounded_directions)
                + len(stale_active_moves)
                + len(criterion_link_conflicts)
            ),
        },
    }


async def _integrity_conflict_trend(
    session: AsyncSession,
    project_id,
    *,
    limit: int = 10,
) -> dict[str, Any]:
    """Return durable integrity-conflict trend data for the strategic-state endpoint.

    Delegates to :func:`strategic_cycle.recent_integrity_conflict_trend` and
    wraps the result for the HTTP response body.  Fails open — an empty trend
    dict is returned when the query raises so a DB hiccup doesn't take down
    the entire strategic-state endpoint.
    """
    try:
        return await strategic_cycle.recent_integrity_conflict_trend(
            session, project_id, limit=limit
        )
    except Exception:
        import logging  # noqa: PLC0415, RUF100
        logging.getLogger(__name__).exception(
            "[STRATEGIC_STATE] integrity_conflict_trend query failed for project %s",
            project_id,
        )
        return {
            "stamped_cycles": 0,
            "consecutive_clean_cycles": 0,
            "last_counts": None,
        }


def _compute_criterion_link_conflicts(
    criterion_links: Any,
    active_criterion_keys: Any,
    inactive_criterion_keys: Any,
) -> list[dict]:
    """Classify each active direction's criterion_keys against the open version.

    Returns one conflict entry per (direction, offending key) pair for keys that
    are ``inactive`` (retracted/superseded) or ``dangling`` (absent from the
    open version — which also covers keys carried over from a different
    version). Valid keys and inputs that are absent/malformed yield an empty
    list, so the caller can pass this through unconditionally.
    """
    if not isinstance(criterion_links, list) or active_criterion_keys is None:
        return []

    from app.direction_referential_guard import classify_criterion_key

    active_keys = frozenset(active_criterion_keys or ())
    inactive_keys = frozenset(inactive_criterion_keys or ())

    conflicts: list[dict] = []
    for link in criterion_links:
        if not isinstance(link, dict):
            continue
        if (link.get("status") or "active") != "active":
            continue
        keys = link.get("criterion_keys")
        if not isinstance(keys, list):
            continue
        direction_id = str(link.get("id") or "")
        direction_text = str(link.get("text") or "")[:120]
        for raw_key in keys:
            key = str(raw_key or "")
            if not key:
                continue
            cls = classify_criterion_key(key, active_keys, inactive_keys)
            if cls == "valid":
                continue
            conflicts.append({
                "direction_id": direction_id,
                "direction_text": direction_text,
                "criterion_key": key,
                "classification": cls,
                "recovery_hint": _RECOVERY_HINTS[
                    "criterion_inactive" if cls == "inactive"
                    else "criterion_dangling"
                ],
            })
    return conflicts

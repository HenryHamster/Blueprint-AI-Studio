"""Admin project read/write endpoints for post-onboarding project config.

This router exposes the operator-facing project surface after onboarding:
list/view endpoints, owner-gated PATCH for project config, and a re-detect
endpoint that refreshes repo-derived defaults while preserving explicit
operator overrides for branch naming, cleanup prefixes, Git identity, and
self-improvement flags.
"""

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select, delete
from sqlalchemy import update
from typing import Dict, Any, List
import uuid
from datetime import timedelta
from sqlalchemy import func
from sqlalchemy.orm import load_only
from sqlalchemy.orm.attributes import flag_modified

from app.database import get_session, resolve_dialect_name
from app.shared_state import limiter
from app.models import (
    LocalRunner,
    ProjectMessage,
    StrategicCycle,
    Task,
    TaskStatus,
    Project,
    ProjectStatus,
    ProjectMember,
    _utcnow,
    notify_tasks_terminated,
)
from app.auth import get_current_user
from app.admin import _get_user_project, _get_user_project_with_role, _user_project_ids
from app import strategic_cycle
from app.task_locking import lock_project_or_429
from app.build_config_lint import build_config_lint_warnings
from app.ci_resolution import (
    compute_merge_verification_verdict,
    merge_gate_runtime_block,
)
from app.capability_proof import enrich_problem_model_capability_proofs
from app.message_convergence import LINKED_STATUS, OPEN_STATUS
from app.operator_attention import build_operator_attention
from app.project_message_projection import message_preview_dict
from app.problem_model import compute_model_size_signal, ProblemModel
from app.capability_trial_verdict import get_trial_verdicts
from app.onboarding_proof import build_onboarding_proof_block
from app.routers.admin_runners import _RUNNER_LIVENESS_SECONDS, _runner_health
from app.task_capabilities import STRATEGIC_TASK_TYPES

router = APIRouter(
    prefix="/api/admin",
    tags=["admin-projects"],
    dependencies=[Depends(get_current_user)],
)


def _redact_build_config(build_config: dict, role: str) -> dict:
    """Return build_config with env_vars omitted for non-owner callers.

    Owners see the full config because they are responsible for Git identity
    and environment overrides; members get a redacted copy.
    """
    if role == "owner":
        return build_config
    result = dict(build_config)
    result.pop("env_vars", None)
    return result


def _build_capability_proof_summary(proof_records: Dict[str, Any]) -> Dict[str, Any]:
    """Convert capability_proof_records into a summary per capability.

    For each capability, returns: proof_count, latest proof_kind, latest proved_at,
    latest commit_sha (from the most recent proof entry in the list).
    """
    summary = {}
    if not proof_records:
        return summary
    for cap_name, entries in proof_records.items():
        if not isinstance(entries, list) or not entries:
            continue
        latest = entries[-1]  # Most recent entry is last
        summary[cap_name] = {
            "proof_count": len(entries),
            "latest_proof_kind": latest.get("proof_kind"),
            "latest_proved_at": latest.get("proved_at"),
            "latest_commit_sha": latest.get("commit_sha"),
        }
    return summary


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

_LOCAL_STRATEGIC_RUNNING_STATUSES = frozenset(
    {
        TaskStatus.IN_PROGRESS,
        TaskStatus.EVALUATING,
        TaskStatus.BLOCKED,
        TaskStatus.PARKED,
    }
)

_CORE_STRATEGIC_CYCLE_TASK_TYPES = frozenset(
    task_type
    for task_type in ("design_doc_update", "strategic_review")
    if task_type in STRATEGIC_TASK_TYPES
)


def _is_live_strategic_task(task: Task | None) -> bool:
    return bool(task is not None and task.status in _LIVE_STRATEGIC_TASK_STATUSES)


def _message_preview(message: ProjectMessage) -> Dict[str, Any]:
    return message_preview_dict(message)


def _task_summary(task: Task | None) -> Dict[str, Any] | None:
    if task is None:
        return None
    payload = task.first_payload or {}
    return {
        "id": str(task.id),
        "status": _enum_value(task.status),
        "title": task.title,
        "task_type": payload.get("type"),
        "latest_status_message": task.latest_status_message,
        "started_at": task.started_at.isoformat() if task.started_at else None,
        "created_at": task.created_at.isoformat() if task.created_at else None,
    }


def _string_or_none(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    stripped = value.strip()
    return stripped or None


def _runner_target_id(task: Task | None) -> str | None:
    if task is None:
        return None
    context = task.context or {}
    runner_target_id = _string_or_none(context.get("runner_target_id"))
    if runner_target_id:
        return runner_target_id
    vm_instance_id = _string_or_none(task.vm_instance_id)
    if vm_instance_id and vm_instance_id.startswith("runner:"):
        return _string_or_none(vm_instance_id.removeprefix("runner:"))
    return None


def _task_runtime_summary(task: Task | None) -> Dict[str, Any] | None:
    if task is None:
        return None
    context = task.context or {}
    return {
        "provider": task.provider or _string_or_none(context.get("llm_provider")),
        "model": task.model or _string_or_none(context.get("llm_model")),
        "account_id": (
            task.account_id
            or task.llm_account_id
            or _string_or_none(context.get("llm_account_id"))
            or _string_or_none(context.get("max_plan_account_id"))
        ),
        "vm_instance_id": task.vm_instance_id,
        "runner_target_id": _runner_target_id(task),
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
    }


def _runner_handles_any_strategic_work(runner: LocalRunner) -> bool:
    task_types = (runner.capabilities or {}).get("task_types", "all")
    if task_types == "all":
        return True
    if not isinstance(task_types, list):
        return False
    return any(
        isinstance(task_type, str) and task_type in STRATEGIC_TASK_TYPES
        for task_type in task_types
    )


def _runner_handles_task_types(
    runner: LocalRunner,
    required_task_types: frozenset[str],
) -> bool:
    task_types = (runner.capabilities or {}).get("task_types", "all")
    if task_types == "all":
        return True
    if not isinstance(task_types, list):
        return False
    handled = {task_type for task_type in task_types if isinstance(task_type, str)}
    return required_task_types.issubset(handled)


def _runner_is_dispatch_eligible(
    runner: LocalRunner,
    health: Dict[str, Any],
    required_task_types: frozenset[str],
) -> bool:
    if not _runner_handles_task_types(runner, required_task_types):
        return False
    if not health.get("is_live"):
        return False
    if health.get("agent_available") is False:
        return False
    available_slots = health.get("available_slots")
    return available_slots is None or int(available_slots) > 0


def _active_strategic_task_types(task: Task | None) -> frozenset[str]:
    if task is None:
        return _CORE_STRATEGIC_CYCLE_TASK_TYPES
    payload = task.first_payload or {}
    task_type = payload.get("type")
    if isinstance(task_type, str) and task_type in STRATEGIC_TASK_TYPES:
        return frozenset({task_type})
    return _CORE_STRATEGIC_CYCLE_TASK_TYPES


async def _strategic_runtime_summary(
    session: AsyncSession,
    project_id: uuid.UUID,
    *,
    active_task: Task | None,
) -> Dict[str, Any]:
    """Project the existing local-runner state relevant to Strategic work."""

    runner_rows = (
        await session.execute(
            select(LocalRunner)
            .where(
                LocalRunner.project_id == project_id,
                LocalRunner.status != "deleted",
            )
            .order_by(
                LocalRunner.last_seen_at.desc(),
                LocalRunner.created_at.desc(),
            )
        )
    ).scalars().all()
    readiness_task_types = _active_strategic_task_types(active_task)
    strategic_runners = [
        runner for runner in runner_rows if _runner_handles_any_strategic_work(runner)
    ]
    readiness_capable_runners = [
        runner
        for runner in strategic_runners
        if _runner_handles_task_types(runner, readiness_task_types)
    ]
    health_by_runner = {
        str(runner.id): _runner_health(runner) for runner in strategic_runners
    }

    live_runner_count = sum(
        1 for health in health_by_runner.values() if health.get("is_live")
    )
    readiness_health = [
        health_by_runner[str(runner.id)] for runner in readiness_capable_runners
    ]
    readiness_live_runner_count = sum(
        1 for health in readiness_health if health.get("is_live")
    )
    stale_runner_count = max(0, len(strategic_runners) - live_runner_count)
    agent_unavailable_count = sum(
        1
        for health in readiness_health
        if health.get("is_live") and health.get("agent_available") is False
    )
    busy_runner_count = sum(
        1
        for health in readiness_health
        if health.get("is_live")
        and health.get("agent_available") is not False
        and health.get("available_slots") == 0
    )
    eligible_live_count = sum(
        1
        for runner in readiness_capable_runners
        if _runner_is_dispatch_eligible(
            runner,
            health_by_runner[str(runner.id)],
            readiness_task_types,
        )
    )

    active_runner_id = _runner_target_id(active_task)
    active_task_pinned = active_runner_id is not None
    active_task_runtime = _task_runtime_summary(active_task)
    active_runner_health = (
        health_by_runner.get(active_runner_id) if active_runner_id else None
    )
    active_runner = next(
        (
            runner
            for runner in strategic_runners
            if active_runner_id and str(runner.id) == active_runner_id
        ),
        None,
    )

    fallback_reason = None
    if active_task_pinned:
        if active_runner is None:
            mode = "pending_local"
            fallback_reason = "runner_unavailable"
        elif not _runner_handles_task_types(active_runner, readiness_task_types):
            mode = "pending_local"
            fallback_reason = "capability_mismatch"
        elif not active_runner_health or not active_runner_health.get("is_live"):
            mode = "pending_local"
            fallback_reason = "runner_offline"
        elif active_runner_health.get("agent_available") is False:
            mode = "pending_local"
            fallback_reason = "agent_unavailable"
        elif (
            active_task is not None
            and active_task.status not in _LOCAL_STRATEGIC_RUNNING_STATUSES
        ):
            mode = "pending_local"
            fallback_reason = "runner_claim_pending"
        else:
            mode = "local"
    elif active_task is not None and active_task.vm_instance_id:
        mode = "cloud"
        fallback_reason = "cloud_dispatch"
    elif eligible_live_count > 0:
        mode = "local_ready"
    elif not strategic_runners:
        mode = "cloud"
        fallback_reason = "no_runner"
    elif not readiness_capable_runners:
        mode = "pending_local"
        fallback_reason = "capability_mismatch"
    elif readiness_live_runner_count == 0:
        mode = "pending_local"
        fallback_reason = "runner_offline"
    elif agent_unavailable_count > 0:
        mode = "pending_local"
        fallback_reason = "agent_unavailable"
    elif busy_runner_count > 0:
        mode = "pending_local"
        fallback_reason = "at_capacity"
    else:
        mode = "pending_local"
        fallback_reason = "runner_unavailable"

    return {
        "local_runner": {
            "mode": mode,
            "strategic_runner_count": len(strategic_runners),
            "live_runner_count": live_runner_count,
            "eligible_live_count": eligible_live_count,
            "stale_runner_count": stale_runner_count,
            "busy_runner_count": busy_runner_count,
            "agent_unavailable_count": agent_unavailable_count,
            "active_runner_id": active_runner_id,
            "active_task_pinned": active_task_pinned,
            "active_task_runtime": active_task_runtime,
            "fallback_reason": fallback_reason,
            "liveness_window_seconds": _RUNNER_LIVENESS_SECONDS,
        }
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


def _isoformat_source_time(value: Any) -> str | None:
    if value is None:
        return None
    if hasattr(value, "isoformat"):
        return value.isoformat()
    text = _compact_text(value, max_chars=80)
    return text


def _context_source(kind: str, count: int) -> Dict[str, Any] | None:
    if count <= 0:
        return None
    return {"kind": kind, "count": int(count)}


def _strategic_context_package_summary(
    project: Project,
    *,
    active_cycle: Dict[str, Any] | None,
    active_task: Task | None,
    latest_cycle: StrategicCycle | None,
    running_update: Dict[str, Any],
    problem_model: Dict[str, Any],
    directions: Dict[str, Any],
    message_counts: Dict[str, int],
    task_counts: Dict[str, int],
    attention: Dict[str, Any] | None,
) -> Dict[str, Any]:
    """Describe the current Strategic context without embedding large source docs."""

    attention_counts = (
        attention.get("counts", {}) if isinstance(attention, dict) else {}
    )
    model_count = (
        int(problem_model.get("active_gap_count") or 0)
        + int(problem_model.get("capability_count") or 0)
        + int(problem_model.get("pending_confirmation_count") or 0)
    )
    attention_count = int(attention_counts.get("needs_operator") or 0) + int(
        attention_counts.get("watch") or 0
    )
    sources = [
        source
        for source in [
            _context_source(
                "running_update",
                1 if running_update.get("char_count") else 0,
            ),
            _context_source("problem_model", model_count),
            _context_source("directions", int(directions.get("active_count") or 0)),
            _context_source(
                "messages",
                int(message_counts.get("open") or 0)
                + int(message_counts.get("linked") or 0),
            ),
            _context_source("tasks", int(task_counts.get("total") or 0)),
            _context_source("attention", attention_count),
            _context_source("design", 1 if project.design_doc else 0),
        ]
        if source is not None
    ]
    source_item_count = sum(int(source["count"]) for source in sources)
    title = (
        problem_model.get("current_focus")
        or problem_model.get("purpose")
        or ((directions.get("primary") or {}).get("text") if directions else None)
        or project.name
    )
    active_stale = bool(active_cycle and active_cycle.get("stale"))
    status = (
        "stale"
        if active_stale
        else "building"
        if active_task is not None
        or bool((project.vision or {}).get("_strategic_refresh"))
        else "ready"
    )
    latest_time = (
        _isoformat_source_time(active_task.updated_at if active_task else None)
        or _isoformat_source_time(active_task.started_at if active_task else None)
        or _isoformat_source_time(latest_cycle.completed_at if latest_cycle else None)
        or _isoformat_source_time(problem_model.get("last_updated"))
        or _isoformat_source_time(project.created_at)
    )
    drilldowns = [
        {
            "id": "running_update",
            "label": "Update",
            "target": {"surface": "project", "section": "running-update"},
        },
        {
            "id": "problem_model",
            "label": "Model",
            "target": {"surface": "project", "section": "problem-model"},
        },
        {
            "id": "directions",
            "label": "Directions",
            "target": {"surface": "project", "section": "directions"},
        },
        {
            "id": "pipeline",
            "label": "Pipeline",
            "target": {"surface": "pipeline"},
        },
        {
            "id": "runtime",
            "label": "Runtime",
            "target": {"surface": "runtime", "section": "health"},
        },
    ]
    if project.design_doc:
        drilldowns.append(
            {
                "id": "design",
                "label": "Design",
                "target": {"surface": "project", "section": "design-doc"},
            }
        )

    return {
        "id": f"strategic-context:{project.id}",
        "title": _compact_text(title, max_chars=100) or "Strategic context",
        "status": status,
        "updated_at": latest_time,
        "token_count": None,
        "item_count": source_item_count,
        "sources": sources,
        "canvas": {
            "id": f"strategic-canvas:{project.id}",
            "title": "Strategic context map",
            "kind": "strategic_context",
            "node_count": None,
        },
        "drilldowns": drilldowns,
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


@router.get("/projects")
@limiter.limit("30/minute")
async def get_all_projects(
    request: Request,
    limit: int = Query(50, ge=1, le=100),
    offset: int = Query(0, ge=0),
    archived: bool = Query(False),
    session: AsyncSession = Depends(get_session),
    current_user: dict = Depends(get_current_user),
):
    """List the caller's projects with lightweight columns and redacted config.

    By default archived projects are EXCLUDED so they disappear from the active
    dashboard. Pass ``archived=true`` to fetch only the archived set (the UI's
    restore view). The ``archived`` flag is returned per item so the frontend
    can render archive state.
    """
    # Select only lightweight columns — large JSONB (directions, vision, design_doc,
    # problem_model, self_reflection_log, running_update) are excluded here and
    # available via GET /api/admin/projects/{project_id}.

    # Build dialect-safe directions_count expression
    dialect_name = resolve_dialect_name(session)
    if dialect_name == "sqlite":
        directions_count_expr = func.coalesce(
            func.json_array_length(Project.directions), 0
        )
    else:
        directions_count_expr = func.coalesce(
            func.jsonb_array_length(Project.directions), 0
        )

    stmt = (
        select(
            Project.id,
            Project.name,
            Project.created_at,
            Project.status,
            Project.user_id,
            Project.github_owner,
            Project.github_repo,
            Project.is_self_repo,
            Project.autonomous_enabled,
            Project.paused,
            Project.archived,
            Project.build_config,
            Project.merge_gate_runtime,
            Project.sr_catch_rate_targets,
            Project.running_update_acknowledged_at,
            directions_count_expr.label("directions_count"),
            ProjectMember.role.label("my_role"),
        )
        .join(ProjectMember, ProjectMember.project_id == Project.id)
        .where(
            ProjectMember.user_id == current_user["user_id"],
            Project.archived.is_(archived),
        )
        .order_by(Project.created_at.desc())
        .limit(limit + 1)
        .offset(offset)
    )
    rows = (await session.execute(stmt)).all()
    has_more = len(rows) > limit
    page_rows = rows[:limit]

    # Single grouped query for task-status counts across all user-accessible projects.
    # Scoped to caller membership via _user_project_ids — no cross-project leakage.
    project_ids_for_page = [row.id for row in page_rows]
    task_status_counts: Dict[str, Any] = {}
    if project_ids_for_page:
        counts_result = await session.execute(
            select(Task.project_id, Task.status, func.count())
            .where(Task.project_id.in_(project_ids_for_page))
            .where(Task.archived.is_(False))
            .group_by(Task.project_id, Task.status)
        )
        for proj_id, status, count in counts_result.all():
            proj_key = str(proj_id)
            if proj_key not in task_status_counts:
                task_status_counts[proj_key] = {
                    "queued": 0,
                    "running": 0,
                    "evaluating": 0,
                    "blocked": 0,
                    "failed": 0,
                    "completed": 0,
                    "total": 0,
                }
            bucket = task_status_counts[proj_key]
            n = int(count or 0)
            bucket["total"] += n
            if status == TaskStatus.QUEUED:
                bucket["queued"] += n
            elif status in (TaskStatus.IN_PROGRESS, TaskStatus.PROVISIONING):
                bucket["running"] += n
            elif status == TaskStatus.EVALUATING:
                bucket["evaluating"] += n
            elif status == TaskStatus.BLOCKED:
                bucket["blocked"] += n
            elif status == TaskStatus.FAILED:
                bucket["failed"] += n
            elif status == TaskStatus.COMPLETED:
                bucket["completed"] += n

    _empty_counts = {
        "queued": 0,
        "running": 0,
        "evaluating": 0,
        "blocked": 0,
        "failed": 0,
        "completed": 0,
        "total": 0,
    }
    return {
        "items": [
            {
                "id": str(row.id),
                "name": row.name,
                "created_at": row.created_at.isoformat() if row.created_at else None,
                "status": row.status,
                "user_id": row.user_id,
                "github_owner": row.github_owner,
                "github_repo": row.github_repo,
                "is_self_repo": row.is_self_repo,
                "autonomous_enabled": row.autonomous_enabled,
                "paused": row.paused,
                "archived": row.archived,
                "build_config": _redact_build_config(
                    row.build_config or {}, row.my_role
                ),
                "build_config_warnings": build_config_lint_warnings(row.build_config),
                "merge_verification_verdict": compute_merge_verification_verdict(row),
                "merge_gate_runtime": merge_gate_runtime_block(row),
                "sr_catch_rate_targets": row.sr_catch_rate_targets or {},
                "running_update_acknowledged_at": (
                    row.running_update_acknowledged_at.isoformat()
                    if row.running_update_acknowledged_at
                    else None
                ),
                "directions_count": row.directions_count,
                "my_role": row.my_role,
                "task_status_counts": task_status_counts.get(
                    str(row.id), _empty_counts
                ),
                "onboarding_proof": build_onboarding_proof_block(row),
            }
            for row in page_rows
        ],
        "has_more": has_more,
    }


@router.get("/projects/{project_id}")
@limiter.limit("30/minute")
async def get_project(
    request: Request,
    project_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    current_user: dict = Depends(get_current_user),
):
    """Return the full project record including all JSONB detail columns."""
    query = (
        select(Project, ProjectMember.role)
        .join(ProjectMember, ProjectMember.project_id == Project.id)
        .where(
            Project.id == project_id,
            ProjectMember.user_id == current_user["user_id"],
        )
    )
    row = (await session.execute(query)).one_or_none()
    if row is None:
        raise HTTPException(status_code=404, detail="Project not found")
    project, role = row
    problem_model_dict = project.problem_model or {}
    proof_problem_model = enrich_problem_model_capability_proofs(
        problem_model_dict,
        getattr(project, "capability_proof_records", None),
    )
    size_health = None
    if problem_model_dict:
        try:
            pm = ProblemModel(**problem_model_dict)
            size_health = compute_model_size_signal(pm)
        except Exception:
            pass
    return {
        "id": str(project.id),
        "name": project.name,
        "user_id": project.user_id,
        "status": project.status,
        "github_owner": project.github_owner,
        "github_repo": project.github_repo,
        "created_at": project.created_at.isoformat() if project.created_at else None,
        "directions": project.directions,
        "vision": project.vision or {},
        "design_doc": project.design_doc or {},
        "problem_model": proof_problem_model,
        "problem_model_size_health": size_health,
        "running_update": project.running_update,
        "running_update_acknowledged_at": (
            project.running_update_acknowledged_at.isoformat()
            if project.running_update_acknowledged_at
            else None
        ),
        "self_reflection_log": project.self_reflection_log or [],
        "build_config": _redact_build_config(project.build_config or {}, role),
        "build_config_warnings": build_config_lint_warnings(project.build_config),
        "merge_verification_verdict": compute_merge_verification_verdict(project),
        "merge_gate_runtime": merge_gate_runtime_block(project),
        "autonomous_enabled": project.autonomous_enabled,
        "paused": project.paused,
        "is_self_repo": project.is_self_repo,
        "sr_catch_rate_targets": project.sr_catch_rate_targets or {},
        "approval_gate_config": project.approval_gate_config or {},
        "capability_proof_summary": _build_capability_proof_summary(
            project.capability_proof_records or {}
        ),
        "capability_adoption_verdicts": get_trial_verdicts(project),
        "my_role": role,
        "onboarding_proof": build_onboarding_proof_block(project),
    }


@router.get("/projects/{project_id}/strategic-state")
@limiter.limit("30/minute")
async def get_project_strategic_state(
    request: Request,
    project_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    current_user: dict = Depends(get_current_user),
):
    """Return a compact, persistent Strategic workbench read model.

    This intentionally projects existing durable state instead of creating a new
    chat/session subsystem. The chat surface can reload from this endpoint after
    a browser refresh, while deeper editing still lives in the Project workspace.
    """
    project = await _get_user_project_with_role(
        project_id, current_user["user_id"], session, "member"
    )

    active_cycle = (
        await session.execute(
            select(StrategicCycle)
            .where(
                StrategicCycle.project_id == project.id,
                StrategicCycle.outcome.in_(list(strategic_cycle.ACTIVE_OUTCOMES)),
            )
            .order_by(StrategicCycle.phase1_started_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()

    latest_cycle = (
        await session.execute(
            select(StrategicCycle)
            .where(
                StrategicCycle.project_id == project.id,
                ~StrategicCycle.outcome.in_(list(strategic_cycle.ACTIVE_OUTCOMES)),
            )
            .order_by(StrategicCycle.phase1_started_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()

    active_task = None
    active_phase = None
    if active_cycle is not None:
        task_id = None
        if active_cycle.outcome == strategic_cycle.OUTCOME_PHASE2_RUNNING:
            task_id = active_cycle.phase2_task_id
            active_phase = "strategic_review"
        elif active_cycle.phase1_task_id:
            task_id = active_cycle.phase1_task_id
            active_phase = "design_doc_update"
        if task_id is not None:
            candidate_task = await session.get(Task, task_id)
            if _is_live_strategic_task(candidate_task):
                active_task = candidate_task
            else:
                active_phase = None

    task_status_rows = (
        await session.execute(
            select(Task.status, func.count())
            .where(Task.project_id == project.id, Task.archived.is_(False))
            .group_by(Task.status)
        )
    ).all()
    task_counts = {
        "queued": 0,
        "running": 0,
        "evaluating": 0,
        "blocked": 0,
        "failed": 0,
        "completed": 0,
        "total": 0,
    }
    for status, count in task_status_rows:
        n = int(count or 0)
        task_counts["total"] += n
        if status == TaskStatus.QUEUED:
            task_counts["queued"] += n
        elif status in (TaskStatus.IN_PROGRESS, TaskStatus.PROVISIONING):
            task_counts["running"] += n
        elif status == TaskStatus.EVALUATING:
            task_counts["evaluating"] += n
        elif status == TaskStatus.BLOCKED:
            task_counts["blocked"] += n
        elif status == TaskStatus.FAILED:
            task_counts["failed"] += n
        elif status == TaskStatus.COMPLETED:
            task_counts["completed"] += n

    cost_total = (
        await session.execute(
            select(func.coalesce(func.sum(Task.estimated_cost_usd), 0.0)).where(
                Task.project_id == project.id,
                Task.archived.is_(False),
            )
        )
    ).scalar()

    message_rows = (
        await session.execute(
            select(ProjectMessage)
            .where(
                ProjectMessage.project_id == project.id,
                ProjectMessage.status.in_([OPEN_STATUS, LINKED_STATUS]),
            )
            .order_by(ProjectMessage.created_at.desc())
            .limit(12)
        )
    ).scalars().all()
    message_count_rows = (
        await session.execute(
            select(ProjectMessage.status, func.count())
            .where(
                ProjectMessage.project_id == project.id,
                ProjectMessage.status.in_([OPEN_STATUS, LINKED_STATUS]),
            )
            .group_by(ProjectMessage.status)
        )
    ).all()
    message_counts = {"open": 0, "linked": 0}
    for status, count in message_count_rows:
        if status in message_counts:
            message_counts[status] = int(count or 0)

    attention = await build_operator_attention(
        session,
        project,
        current_user["user_id"],
    )
    active_cycle_summary = _cycle_summary(active_cycle, active_task=active_task)
    latest_cycle_summary = _cycle_summary(latest_cycle)
    running_update_summary = _running_update_summary(project)
    problem_model_summary = _problem_model_summary(project.problem_model or {})
    directions_summary = _direction_summary(project.directions or [])
    context_package = _strategic_context_package_summary(
        project,
        active_cycle=active_cycle_summary,
        active_task=active_task,
        latest_cycle=latest_cycle,
        running_update=running_update_summary,
        problem_model=problem_model_summary,
        directions=directions_summary,
        message_counts=message_counts,
        task_counts=task_counts,
        attention=attention,
    )
    runtime_summary = await _strategic_runtime_summary(
        session,
        project.id,
        active_task=active_task,
    )

    return {
        "project_id": str(project.id),
        "strategic": {
            "active_cycle": active_cycle_summary,
            "active_task": _task_summary(active_task),
            "active_phase": active_phase,
            "latest_cycle": latest_cycle_summary,
            "refresh": _strategic_refresh_summary(project.vision or {}),
        },
        "running_update": running_update_summary,
        "problem_model": problem_model_summary,
        "directions": directions_summary,
        "messages": {
            "counts": message_counts,
            "items": [_message_preview(message) for message in message_rows],
        },
        "tasks": {
            "counts": task_counts,
            "cumulative_estimated_cost_usd": float(cost_total or 0.0),
        },
        "attention": attention,
        "context_package": context_package,
        "runtime": runtime_summary,
        "design": {
            "has_design_doc": bool(project.design_doc),
            "revision": ((project.design_doc or {}).get("_meta") or {}).get("revision")
            if isinstance(project.design_doc, dict)
            else None,
        },
    }


class ProjectUpdate(BaseModel):
    name: str | None = None
    # Project lifecycle status. Mainly for correcting a wrongly-terminal status
    # (e.g. reactivating a project that was auto-terminalized). Setting CANCELLED
    # here is rejected — cancellation must go through the dedicated /cancel
    # endpoint, which also tears down running VMs.
    status: ProjectStatus | None = None
    directions: List[Dict[str, Any]] | None = None
    vision: Dict[str, Any] | None = None
    # Optimistic concurrency: supply the _meta.revision value the client
    # loaded.  Server rejects with 409 if another writer has advanced it.
    vision_base_revision: int | None = None
    design_doc: Dict[str, Any] | None = None
    # Optimistic concurrency: supply the _meta.revision value the client
    # loaded.  Server rejects with 409 if another writer has advanced it.
    design_doc_base_revision: int | None = None
    build_config: Dict[str, Any] | None = None
    autonomous_enabled: bool | None = None
    # Per-category SR catch-rate targets, e.g. {'security': 0.80}.
    # Absent key = no target; empty dict {} clears all targets.
    sr_catch_rate_targets: Dict[str, Any] | None = None
    # User-editable subset of problem_model — intent, constraints, context.
    # Gaps / capabilities / friction are agent-owned and not writable here.
    problem_model_patch: Dict[str, Any] | None = None
    # Optimistic concurrency: supply the problem_model revision the client
    # loaded.  Server rejects with 409 if another writer has advanced it.
    problem_model_base_revision: int | None = None
    # Per-project approval-gate file lists (infra_files / critical_files).
    # Partial update: only the keys supplied are replaced. Takes effect for
    # agents immediately via the existing claim-response seam.
    approval_gate_config: Dict[str, Any] | None = None


_OWNER_ONLY_UPDATE_FIELDS = frozenset(
    {
        "build_config",
        "autonomous_enabled",
        "design_doc",
        "vision",
        "approval_gate_config",
        "status",
    }
)


@router.patch("/projects/{project_id}")
@limiter.limit("30/minute")
async def update_project(
    request: Request,
    project_id: uuid.UUID,
    payload: ProjectUpdate,
    session: AsyncSession = Depends(get_session),
    current_user: dict = Depends(get_current_user),
):
    """Apply operator updates while preserving project-owned branch metadata.

    Owner-only fields are gated separately. Build-config updates merge onto the
    stored config so branch namespaces, task prefixes, staging prefixes, stale
    prefixes, protected branches, and enable_self_improvement survive normal
    save cycles unless the caller explicitly replaces them.
    """
    project = await _get_user_project_with_role(
        project_id, current_user["user_id"], session, "member"
    )

    # Fetch the caller's actual role so we can gate owner-only fields.
    caller_member = (
        await session.execute(
            select(ProjectMember).where(
                ProjectMember.project_id == project_id,
                ProjectMember.user_id == current_user["user_id"],
            )
        )
    ).scalar_one_or_none()
    caller_role = caller_member.role if caller_member else "member"

    if caller_role != "owner" and any(
        getattr(payload, f) is not None for f in _OWNER_ONLY_UPDATE_FIELDS
    ):
        raise HTTPException(
            status_code=403,
            detail="Only project owners may change build_config, autonomous_enabled, design_doc, or vision.",
        )

    # Lock the project row whenever directions, design_doc, or vision are
    # being overwritten so concurrent agent writers cannot silently clobber.
    needs_lock = (
        payload.directions is not None
        or payload.design_doc is not None
        or payload.vision is not None
        or payload.approval_gate_config is not None
        or payload.problem_model_patch is not None
    )
    if needs_lock:
        project = await lock_project_or_429(session, project.id)
        attrs = [
            a
            for a, p in (
                ("directions", payload.directions),
                ("design_doc", payload.design_doc),
                ("vision", payload.vision),
                ("approval_gate_config", payload.approval_gate_config),
                ("problem_model", payload.problem_model_patch),
            )
            if p is not None
        ]
        await session.refresh(project, attribute_names=attrs)

    if payload.name is not None:
        project.name = payload.name
    if payload.status is not None:
        # Cancellation must go through POST /api/projects/{id}/cancel, which also
        # terminates running VMs; a bare status flip here would strand live VMs.
        if payload.status == ProjectStatus.CANCELLED:
            raise HTTPException(
                status_code=400,
                detail="Use the /cancel endpoint to cancel a project (it also tears down VMs).",
            )
        project.status = payload.status
    if payload.autonomous_enabled is not None:
        project.autonomous_enabled = payload.autonomous_enabled
    if payload.directions is not None:
        # Auto-generate ids for any directions missing one
        normalized = []
        for d in payload.directions:
            entry = dict(d)
            if not entry.get("id"):
                entry["id"] = f"d-{uuid.uuid4().hex[:8]}"
            if entry.get("priority") not in ("high", "medium", "low"):
                raise HTTPException(
                    status_code=400, detail=f"Invalid priority: {entry.get('priority')}"
                )
            if not isinstance(entry.get("text"), str) or not entry["text"].strip():
                raise HTTPException(
                    status_code=400, detail="Each direction must have non-empty text"
                )
            normalized.append(entry)
        project.directions = normalized
        flag_modified(project, "directions")
    if payload.vision is not None:
        stored_vision = project.vision or {}
        stored_rev = stored_vision.get("revision", 0) or 0
        if (
            payload.vision_base_revision is not None
            and payload.vision_base_revision != stored_rev
        ):
            raise HTTPException(
                status_code=409,
                detail=(
                    f"vision was modified by another writer "
                    f"(base_revision={payload.vision_base_revision}, current={stored_rev}). "
                    "Reload and retry."
                ),
            )
        vision = dict(payload.vision)
        vision["revision"] = stored_rev + 1
        vision["last_updated"] = _utcnow().isoformat()
        project.vision = vision
        flag_modified(project, "vision")
    if payload.design_doc is not None:
        stored_meta = (project.design_doc or {}).get("_meta", {}) or {}
        stored_rev = stored_meta.get("revision", 0) or 0
        if (
            payload.design_doc_base_revision is not None
            and payload.design_doc_base_revision != stored_rev
        ):
            raise HTTPException(
                status_code=409,
                detail=(
                    f"design_doc was modified by another writer "
                    f"(base_revision={payload.design_doc_base_revision}, current={stored_rev}). "
                    "Reload and retry."
                ),
            )
        doc = dict(payload.design_doc)
        # Remove the divergent top-level "revision" key; canonical counter is _meta.revision
        doc.pop("revision", None)
        # Preserve system/operator-owned underscore-prefixed keys that the
        # design-doc editor never surfaces.  The agent list/get endpoint filters
        # out ``_``-prefixed keys (admin_agent_ops.update_design_doc) and blocks
        # writing them, so a UI round-trip carries only the public keys — a
        # wholesale replace from the editor would otherwise silently DROP durable
        # config such as ``_auto_remediation`` / ``_issue_implementation`` /
        # ``_auto_review`` / ``_file_to_system`` (this is exactly how an operator's
        # enabled auto-remediation setting disappears after a later doc edit).
        # An incoming payload that DOES include a given underscore key still wins
        # (explicit operator intent).  ``_meta`` is rebuilt below, so skip it here.
        for _sys_key, _sys_val in (project.design_doc or {}).items():
            if (
                isinstance(_sys_key, str)
                and _sys_key.startswith("_")
                and _sys_key != "_meta"
                and _sys_key not in doc
            ):
                doc[_sys_key] = _sys_val
        meta = dict(stored_meta)
        meta["revision"] = stored_rev + 1
        meta["last_updated"] = _utcnow().isoformat()
        meta["updated_by"] = "user"
        doc["_meta"] = meta
        project.design_doc = doc
        flag_modified(project, "design_doc")
    if payload.build_config is not None:
        from pydantic import ValidationError

        from app.schemas import BuildConfig as BuildConfigSchema

        try:
            existing_bc = project.build_config or {}
            incoming = dict(payload.build_config)
            # Preserve operator-owned branch naming on round-trips.
            for _field in (
                "task_branch_namespace",
                "task_branch_prefix",
                "pr_title_prefix",
            ):
                if not incoming.get(_field) and existing_bc.get(_field):
                    incoming[_field] = existing_bc[_field]
            # Preserve per-project branch config using key-absence semantics:
            # an explicit empty list [] means "disable" and must not be lost.
            for _field in (
                "staging_branch_prefixes",
                "stale_branch_prefixes",
                "protected_branches",
                "enable_self_improvement",
                "requires_ci_checks",
            ):
                if _field not in incoming and _field in existing_bc:
                    incoming[_field] = existing_bc[_field]
            merged = {**existing_bc, **incoming}
            validated = BuildConfigSchema(**merged) if merged else BuildConfigSchema()
            project.build_config = validated.model_dump(exclude_none=True)
        except ValidationError as e:
            raise HTTPException(
                status_code=400, detail="Invalid build_config structure"
            ) from e
        flag_modified(project, "build_config")

    if payload.sr_catch_rate_targets is not None:
        # Validate: values must be floats in [0, 1]
        for cat, val in payload.sr_catch_rate_targets.items():
            if not isinstance(val, (int, float)) or not (0.0 <= float(val) <= 1.0):
                raise HTTPException(
                    status_code=400,
                    detail=f"sr_catch_rate_targets[{cat!r}] must be a float in [0, 1]",
                )
        project.sr_catch_rate_targets = {
            k: float(v) for k, v in payload.sr_catch_rate_targets.items()
        }
        flag_modified(project, "sr_catch_rate_targets")

    if payload.approval_gate_config is not None:
        # Reuse the dedicated config router's keys + path validation so both
        # write paths enforce identical semantics. Partial update: only the
        # supplied keys are replaced; others are preserved.
        from app.routers.admin_project_config import (
            _APPROVAL_GATE_KEYS,
            _validate_approval_gate_paths,
        )

        unknown = set(payload.approval_gate_config.keys()) - set(_APPROVAL_GATE_KEYS)
        if unknown:
            raise HTTPException(
                status_code=422,
                detail=f"Unknown approval_gate_config fields: {sorted(unknown)}",
            )
        cleaned = {
            key: _validate_approval_gate_paths(key, payload.approval_gate_config[key])
            for key in _APPROVAL_GATE_KEYS
            if key in payload.approval_gate_config
        }
        config = dict(project.approval_gate_config or {})
        config.update(cleaned)
        project.approval_gate_config = config
        flag_modified(project, "approval_gate_config")

    if payload.problem_model_patch is not None:
        # User-editable subset only — intent / context / constraints. Agent-
        # owned fields (gaps, capabilities, friction, archived_gaps,
        # pending_user_confirmation, revision_log) pass through unchanged.
        # Bump revision, stamp last_updated, mark last_updated_by="user"
        # so the agent can distinguish user edits during its next cycle.
        from pydantic import ValidationError as _PmValidationError

        from app.problem_model import apply_user_intent_patch, parse_problem_model

        # Stale-write guard runs first so a concurrent writer is reported as a
        # 409 before we attempt the merge. The editable-subset validation and
        # the operator-provenance re-stamp both live in apply_user_intent_patch,
        # so onboarding and this editor save share one correct path (the inline
        # shallow merge used to drop intent.source, silently un-protecting an
        # operator-edited purpose against the next strategic cycle).
        current = parse_problem_model(project.problem_model)
        if (
            payload.problem_model_base_revision is not None
            and payload.problem_model_base_revision != current.revision
        ):
            raise HTTPException(
                status_code=409,
                detail=(
                    f"problem_model was modified by another writer "
                    f"(base_revision={payload.problem_model_base_revision}, current={current.revision}). "
                    "Reload and retry."
                ),
            )
        try:
            validated = apply_user_intent_patch(
                project.problem_model,
                payload.problem_model_patch,
                _utcnow().isoformat(),
            )
        except ValueError as ve:
            raise HTTPException(status_code=400, detail=str(ve)) from None
        except _PmValidationError as ve:
            raise HTTPException(
                status_code=400, detail=f"Invalid problem_model patch: {ve}"
            ) from None
        project.problem_model = validated.model_dump()
        flag_modified(project, "problem_model")

    session.add(project)
    await session.commit()
    await session.refresh(project)
    # Preserve the historical response shape (top-level Project fields) and
    # add a single advisory key — operators read it from the same payload
    # they already have. Warnings are computed from the validated, committed
    # build_config so the value reflects what was actually persisted.
    response = project.model_dump()
    response["build_config_warnings"] = build_config_lint_warnings(project.build_config)
    response["merge_verification_verdict"] = compute_merge_verification_verdict(project)
    response["merge_gate_runtime"] = merge_gate_runtime_block(project)
    return response


@router.post("/projects/{project_id}/detect-build-config")
@limiter.limit("10/minute")
async def detect_project_build_config(
    request: Request,
    project_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    current_user: dict = Depends(get_current_user),
):
    """Re-run repo detection and preserve operator-owned overrides.

    This refreshes repo-derived commands/metadata from GitHub, then re-applies
    stored Git identity, branch namespace/prefixes, stale/protected branch
    lists, and enable_self_improvement before validating the final config.
    """
    project = await _get_user_project_with_role(
        project_id, current_user["user_id"], session, "owner"
    )
    if not project.github_owner or not project.github_repo:
        raise HTTPException(
            status_code=400, detail="Project has no GitHub repository configured"
        )

    from pydantic import ValidationError

    from app.build_config_detection import apply_operator_build_config_overrides
    from app.github import get_repo_installation_token
    from app.routers.admin_onboard import _detect_build_config
    from app.schemas import BuildConfig as BuildConfigSchema

    token = await get_repo_installation_token(project.github_owner, project.github_repo)
    if not token:
        raise HTTPException(
            status_code=502,
            detail="No GitHub installation token available for this repo",
        )

    detected = await _detect_build_config(
        token,
        project.github_owner,
        project.github_repo,
        # Preserve the legacy default-branch behavior for projects that still
        # have a NULL integration_branch column: omitting ref lets GitHub read
        # the repo's actual default branch instead of forcing synthetic "main".
        ref=project.integration_branch,
    )

    detected = apply_operator_build_config_overrides(
        detected, project.build_config or {}
    )

    try:
        validated = BuildConfigSchema(**detected) if detected else BuildConfigSchema()
        project.build_config = validated.model_dump(exclude_none=True)
    except ValidationError:
        # Detection produced a shape the schema doesn't accept — store raw so the user can fix it in the UI
        project.build_config = detected
    flag_modified(project, "build_config")
    await session.commit()
    await session.refresh(project)

    return {
        "build_config": project.build_config or {},
        "build_config_warnings": build_config_lint_warnings(project.build_config),
        "merge_verification_verdict": compute_merge_verification_verdict(project),
        "merge_gate_runtime": merge_gate_runtime_block(project),
    }


@router.post("/projects/{project_id}/running-update/acknowledge")
@limiter.limit("30/minute")
async def acknowledge_running_update(
    request: Request,
    project_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    current_user: dict = Depends(get_current_user),
):
    """Acknowledge the project's running_update — stamps the read-marker
    timestamp. The document itself persists; the strategic agent refines
    it on the next cycle using the acknowledgement timestamp to frame
    what's new since the user last caught up."""
    project = await _get_user_project_with_role(
        project_id, current_user["user_id"], session, "member"
    )
    project.running_update_acknowledged_at = _utcnow()
    session.add(project)
    await session.commit()
    return {
        "status": "acknowledged",
        "acknowledged_at": project.running_update_acknowledged_at.isoformat(),
    }


class RunningUpdateEdit(BaseModel):
    """Operator edit to the running_update body.

    ``body`` is the new full document. ``base`` is the document text the
    operator started editing from; when supplied it is compared against the
    current stored document under the row lock to detect a concurrent writer
    (agent cycle / routine digest) and reject the edit with a 409 so the
    operator can reload and retry — the same optimistic-concurrency discipline
    vision/design_doc use, adapted to a plain TEXT column with no embedded
    revision counter.
    """

    body: str
    base: str | None = None


@router.put("/projects/{project_id}/running-update")
@limiter.limit("30/minute")
async def edit_running_update(
    request: Request,
    project_id: uuid.UUID,
    payload: RunningUpdateEdit,
    session: AsyncSession = Depends(get_session),
    current_user: dict = Depends(get_current_user),
):
    """Operator edit of the project's running_update body.

    Mirrors the operator-editable treatment vision and problem_model already
    get: the operator can correct a stale claim, add context, or flag a
    concern in the living status narrative. The write takes the project row
    lock and refreshes the field so a concurrent agent write is observed; when
    a ``base`` is supplied and differs from the current document a 409 is
    raised. A mid-cycle edit survives the next strategic cycle via the existing
    operator-wins reconciliation in ``running_update_merge``."""
    project = await _get_user_project_with_role(
        project_id, current_user["user_id"], session, "member"
    )
    project = await lock_project_or_429(session, project.id)
    await session.refresh(project, attribute_names=["running_update"])
    if payload.base is not None and (project.running_update or "") != payload.base:
        raise HTTPException(
            status_code=409,
            detail=(
                "running_update was modified by another writer since you began "
                "editing. Reload and retry."
            ),
        )
    project.running_update = payload.body
    session.add(project)
    await session.commit()
    return {
        "status": "updated",
        "running_update": project.running_update,
    }


class _DismissPendingBody(BaseModel):
    note: str | None = Field(None, max_length=5000)
    """Optional operator note to record in the revision log alongside the dismissal."""


@router.post("/projects/{project_id}/pending-confirm/{pending_id}/dismiss")
@limiter.limit("30/minute")
async def dismiss_pending_confirmation(
    request: Request,
    project_id: uuid.UUID,
    pending_id: str,
    payload: _DismissPendingBody = _DismissPendingBody(),
    session: AsyncSession = Depends(get_session),
    current_user: dict = Depends(get_current_user),
):
    """Dismiss a strategist pending_user_confirmation item in-place.

    The strategist posts pending_user_confirmation items for external blockers
    (credentials needed, access needed, major decisions). Once the operator
    takes action they can dismiss the item here rather than waiting for the
    next autonomous strategic cycle to clear it.

    An optional ``note`` in the request body is recorded in the revision log
    so the strategist can see the operator's response on its next cycle.
    """
    from app.problem_model import append_revision_log, parse_problem_model
    from app.time_utils import _utcnow

    project = await _get_user_project_with_role(
        project_id, current_user["user_id"], session, "member"
    )
    current_model = parse_problem_model(project.problem_model)
    matched = next(
        (p for p in current_model.pending_user_confirmation if p.id == pending_id),
        None,
    )
    if matched is None:
        raise HTTPException(
            status_code=404, detail=f"Pending confirmation '{pending_id}' not found"
        )
    current_model.pending_user_confirmation = [
        p for p in current_model.pending_user_confirmation if p.id != pending_id
    ]
    current_model.revision += 1
    current_model.last_updated = _utcnow().isoformat()
    current_model.last_updated_by = "user"
    if payload.note:
        changes: dict = {
            "dismissed_proposal": matched.proposal,
            "dismissal_note": payload.note,
        }
        append_revision_log(current_model, f"operator dismissed {matched.type}", changes)
    project.problem_model = current_model.model_dump()
    flag_modified(project, "problem_model")
    session.add(project)
    await session.commit()
    return {"status": "dismissed", "pending_id": pending_id}


class _AcceptPendingBody(BaseModel):
    answer: str | None = Field(None, max_length=5000)
    """For major_decision: the operator's answer text to record."""


@router.post("/projects/{project_id}/pending-confirm/{pending_id}/accept")
@limiter.limit("30/minute")
async def accept_pending_confirmation(
    request: Request,
    project_id: uuid.UUID,
    pending_id: str,
    payload: _AcceptPendingBody,
    session: AsyncSession = Depends(get_session),
    current_user: dict = Depends(get_current_user),
):
    """Accept a pending_user_confirmation item with a type-specific action.

    - intent_change: applies the proposed text as the new intent.purpose and clears the item.
    - major_decision: records the operator's answer in the revision log and clears the item.
    - credentials_needed / access_needed: signals provided and clears the item.
    """
    from app.problem_model import (
        apply_user_intent_patch,
        append_revision_log,
        parse_problem_model,
    )
    from app.time_utils import _utcnow

    project = await _get_user_project_with_role(
        project_id, current_user["user_id"], session, "member"
    )
    current_model = parse_problem_model(project.problem_model)

    matched = next(
        (p for p in current_model.pending_user_confirmation if p.id == pending_id),
        None,
    )
    if matched is None:
        raise HTTPException(
            status_code=404, detail=f"Pending confirmation '{pending_id}' not found"
        )

    now = _utcnow().isoformat()

    if matched.type == "intent_change":
        # Apply the proposed text as the new operator-set intent.purpose.
        current_model = apply_user_intent_patch(
            project.problem_model,
            {"intent": {"purpose": matched.proposal}},
            now,
        )
        # Remove the pending item after applying the intent change.
        current_model.pending_user_confirmation = [
            p for p in current_model.pending_user_confirmation if p.id != pending_id
        ]
        if payload.answer:
            changes: dict = {
                "pending_accepted": {"id": pending_id, "type": matched.type},
                "accepted_proposal": matched.proposal,
                "operator_note": payload.answer,
            }
            append_revision_log(current_model, "operator accepted intent_change", changes)
    else:
        # For major_decision, credentials_needed, access_needed:
        # remove the item and record a revision log entry with any answer.
        current_model.pending_user_confirmation = [
            p for p in current_model.pending_user_confirmation if p.id != pending_id
        ]
        current_model.revision += 1
        current_model.last_updated = now
        current_model.last_updated_by = "user"
        changes: dict = {"pending_accepted": {"id": pending_id, "type": matched.type}}
        if payload.answer:
            changes["decision_answer"] = payload.answer
            changes["decision_question"] = matched.proposal
        append_revision_log(current_model, f"operator accepted {matched.type}", changes)

    project.problem_model = current_model.model_dump()
    flag_modified(project, "problem_model")
    session.add(project)
    await session.commit()
    return {"status": "accepted", "pending_id": pending_id, "type": matched.type}


@router.delete("/projects/{project_id}")
@limiter.limit("10/minute")
async def delete_project_admin(
    request: Request,
    project_id: uuid.UUID,
    force: bool = Query(False),
    session: AsyncSession = Depends(get_session),
    current_user: dict = Depends(get_current_user),
):
    """Delete a project and cascade to tasks/artifacts/history.
    Refuses if active tasks exist unless force=true."""
    # Phase C2 (L-C3): role check first (404/403 before lock contention surfaces
    # as 429), then acquire a Project row lock with skip_locked. Closes the
    # heartbeat-arrives-mid-delete race where a concurrent agent status POST
    # could mutate task rows between our cancel and FK-cascade delete.
    await _get_user_project_with_role(
        project_id, current_user["user_id"], session, "owner"
    )
    project = await lock_project_or_429(session, project_id)

    ACTIVE_STATUSES = [
        TaskStatus.QUEUED,
        TaskStatus.PROVISIONING,
        TaskStatus.IN_PROGRESS,
        TaskStatus.EVALUATING,
        # PARKED is live work waiting on an external wake — count it as active so
        # a non-force delete is refused while one exists, and a force delete
        # cancels it (its wake registration cascade-deletes with the task).
        TaskStatus.PARKED,
    ]
    # Bounded load: used for the existence check and per-task VM cleanup.
    # The bulk UPDATE below covers all matching rows regardless of this cap.
    active_tasks = (
        (
            await session.execute(
                select(Task)
                .where(Task.project_id == project_id, Task.status.in_(ACTIVE_STATUSES))
                .limit(1000)
            )
        )
        .scalars()
        .all()
    )

    if active_tasks and not force:
        raise HTTPException(
            status_code=409,
            detail=f"Project has {len(active_tasks)} active task(s). Cancel them first, or pass ?force=true.",
        )

    cancelled_count = 0
    if active_tasks and force:
        # Force: bulk-cancel all active tasks (covers rows beyond the 1000-row
        # load window), then delete VMs for the loaded subset.
        from app.vm_lifecycle import _ensure_vm_deleted

        # Fetch all affected task IDs before the bulk UPDATE so we can fire
        # terminal callbacks after commit — the bulk UPDATE bypasses the ORM
        # event listener that normally populates the JWT deny-list.
        all_cancelled_ids = (
            (
                await session.execute(
                    select(Task.id).where(
                        Task.project_id == project_id,
                        Task.status.in_(ACTIVE_STATUSES),
                    )
                )
            )
            .scalars()
            .all()
        )

        result = await session.execute(
            update(Task)
            .where(Task.project_id == project_id, Task.status.in_(ACTIVE_STATUSES))
            .values(
                status=TaskStatus.CANCELLED,
                latest_status_message="Project force-deleted",
            )
            .execution_options(synchronize_session=False)
        )
        cancelled_count = result.rowcount
        for task in active_tasks:
            await _ensure_vm_deleted(task, reason="project_force_delete")
        # Emit cross-instance NOTIFY before commit so sibling instances see the
        # revocation only after the bulk UPDATE is durable.  The post-commit
        # notify_tasks_terminated call still fires for in-process callback fanout.
        from app.jwt_revocation import emit_bulk_task_terminated_notify

        await emit_bulk_task_terminated_notify(session, all_cancelled_ids)
        await session.commit()
        notify_tasks_terminated(all_cancelled_ids)

    # Migration 0274 adds ON DELETE CASCADE to every remaining project-child FK
    # (messageslastseen, messagesnoiseprefs, messagessavedviews,
    # anomalyfingerprintstate, task, computejob, poolvm, poolidlecostledger,
    # poolidlespan, knowledgedocument, migrationreservation,
    # taskwakeregistration). The earlier 0058 migration already covered alert,
    # projectmember, projectmessage, strategiccycle, residualremediationcase,
    # and systemmaturity. Tables created after 0058 with explicit
    # ForeignKey(ondelete="CASCADE") in their model (direction, releaseversion,
    # qualificationverdict, auditfindingresolution, improvementlane,
    # evergreenbacklogitem) were correct from the start. With all FKs
    # cascading at the DB level, the project delete below is sufficient.
    await session.delete(project)
    await session.commit()

    # Evict from GitHub token cache
    from app.routers.agent import _github_token_cache

    _github_token_cache.pop(str(project_id), None)

    return {
        "message": f"Project {project_id} deleted",
        "active_tasks_cancelled": cancelled_count,
    }


@router.post("/projects/{project_id}/reset")
@limiter.limit("10/minute")
async def reset_project_admin(
    request: Request,
    project_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    current_user: dict = Depends(get_current_user),
):
    """Reset a wedged project to a clean, runnable state without deleting it.

    Owner-only. Safely cancels and tears down any active tasks (reusing the
    same machinery as force-delete), then deletes all of the project's task
    and alert rows and clears transient runtime state (circuit breakers,
    in-memory strategic failure counts, persistent anomalies). Durable
    configuration — directions, vision, design doc, problem model, build
    config, members — is preserved by construction (never touched). The
    project is left in DEPLOYING so the orchestrator treats it as runnable.
    """
    # Role check first (404/403 before lock contention surfaces as 429), then
    # acquire the Project row lock — same ordering as delete to close the
    # heartbeat-arrives-mid-reset race.
    await _get_user_project_with_role(
        project_id, current_user["user_id"], session, "owner"
    )
    project = await lock_project_or_429(session, project_id)

    ACTIVE_STATUSES = [
        TaskStatus.QUEUED,
        TaskStatus.PROVISIONING,
        TaskStatus.IN_PROGRESS,
        TaskStatus.EVALUATING,
        # PARKED is live work waiting on an external wake — cancel it on reset so
        # the wake worker can't later wake a task into a reset project.
        TaskStatus.PARKED,
    ]
    # Bounded load for per-task VM cleanup; the bulk UPDATE below covers all
    # matching rows regardless of this cap.
    active_tasks = (
        (
            await session.execute(
                select(Task)
                .where(Task.project_id == project_id, Task.status.in_(ACTIVE_STATUSES))
                .limit(1000)
            )
        )
        .scalars()
        .all()
    )

    cancelled_count = 0
    if active_tasks:
        # Cancel + tear down active tasks before deleting their rows, reusing
        # the same safe cancellation/VM-teardown path as force-delete.
        from app.vm_lifecycle import _ensure_vm_deleted

        all_cancelled_ids = (
            (
                await session.execute(
                    select(Task.id).where(
                        Task.project_id == project_id,
                        Task.status.in_(ACTIVE_STATUSES),
                    )
                )
            )
            .scalars()
            .all()
        )

        result = await session.execute(
            update(Task)
            .where(Task.project_id == project_id, Task.status.in_(ACTIVE_STATUSES))
            .values(
                status=TaskStatus.CANCELLED,
                latest_status_message="Project reset to clean",
            )
            .execution_options(synchronize_session=False)
        )
        cancelled_count = result.rowcount
        for task in active_tasks:
            await _ensure_vm_deleted(task, reason="project_reset")
        from app.jwt_revocation import emit_bulk_task_terminated_notify

        await emit_bulk_task_terminated_notify(session, all_cancelled_ids)
        await session.commit()
        notify_tasks_terminated(all_cancelled_ids)

    # Wipe all task history + alerts for the project, then return it to a
    # clean runnable state. Durable config columns are never touched.
    from app.models import Alert

    tasks_deleted = (
        await session.execute(delete(Task).where(Task.project_id == project_id))
    ).rowcount
    await session.execute(delete(Alert).where(Alert.project_id == project_id))
    project.status = ProjectStatus.DEPLOYING
    session.add(project)
    await session.commit()

    # Clear transient in-memory runtime/recovery state through one named seam.
    from app.anomaly_state import reset_project_state

    reset_project_state(str(project_id))

    # Evict from GitHub token cache so the next cycle re-fetches cleanly.
    from app.routers.agent import _github_token_cache

    _github_token_cache.pop(str(project_id), None)

    return {
        "message": f"Project {project_id} reset to clean",
        "active_tasks_cancelled": cancelled_count,
        "tasks_deleted": tasks_deleted,
    }


@router.post("/projects/{project_id}/archive")
@limiter.limit("10/minute")
async def archive_project_admin(
    request: Request,
    project_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    current_user: dict = Depends(get_current_user),
):
    """Archive a project: hide it from the active list and halt all its work.

    Owner-only and idempotent. Archiving sets ``archived=True`` so the project
    disappears from the default project list, the orchestrator stops dispatching
    its queued tasks, and the autonomous scheduler stops generating new work.
    All project data is retained so it can be unarchived back to normal
    operation later.
    """
    # Role check first (404/403 before lock contention surfaces as 429), then
    # acquire the Project row lock — same ordering as delete/reset.
    await _get_user_project_with_role(
        project_id, current_user["user_id"], session, "owner"
    )
    project = await lock_project_or_429(session, project_id)
    project.archived = True
    session.add(project)
    await session.commit()
    return {"message": f"Project {project_id} archived", "archived": True}


@router.post("/projects/{project_id}/unarchive")
@limiter.limit("10/minute")
async def unarchive_project_admin(
    request: Request,
    project_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    current_user: dict = Depends(get_current_user),
):
    """Unarchive a project: restore it to the active list and normal eligibility.

    Owner-only and idempotent. Clears ``archived`` so the project reappears in
    the default list and the orchestrator/autonomous scheduler resume treating
    it as eligible for work.
    """
    await _get_user_project_with_role(
        project_id, current_user["user_id"], session, "owner"
    )
    project = await lock_project_or_429(session, project_id)
    project.archived = False
    session.add(project)
    await session.commit()
    return {"message": f"Project {project_id} unarchived", "archived": False}


@router.post("/autonomous/pause-all")
@limiter.limit("10/minute")
async def pause_all_autonomous(
    request: Request,
    session: AsyncSession = Depends(get_session),
    current_user: dict = Depends(get_current_user),
):
    """Disable autonomous mode on all projects at once."""
    projects = (
        (
            await session.execute(
                select(Project).where(
                    Project.id.in_(_user_project_ids(current_user["user_id"]))
                )
            )
        )
        .scalars()
        .all()
    )
    for p in projects:
        p.autonomous_enabled = False
    await session.commit()
    return {"paused": len(projects)}


@router.get("/projects/{project_id}/directions/task-counts")
@limiter.limit("30/minute")
async def get_direction_task_counts(
    request: Request,
    project_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    current_user: dict = Depends(get_current_user),
):
    await _get_user_project(project_id, current_user["user_id"], session)

    result = await session.execute(
        select(Task)
        .where(
            Task.project_id == project_id,
            Task.status.not_in([TaskStatus.CANCELLED, TaskStatus.FAILED]),
        )
        .options(
            load_only(
                Task.id,
                Task.status,
                Task.payloads,
                Task.context,
                Task.title,
                Task.latest_status_message,
                raiseload=True,
            )
        )
        .limit(5000)
    )
    tasks = result.scalars().all()

    counts: Dict[str, int] = {}
    counts_by_type: Dict[str, Dict[str, int]] = {}
    active_tasks: Dict[str, list[dict]] = {}
    for task in tasks:
        payload = task.first_payload
        # Primary: context.direction_id (matches admin_digest.py behavior).
        # Fallback: payload.current_direction_id, then payload.directions[].id
        # (for older tasks without context).
        dir_ids: list[str] = []
        ctx_did = (task.context or {}).get("direction_id")
        if ctx_did:
            dir_ids.append(ctx_did)
        else:
            current_did = payload.get("current_direction_id")
            if current_did:
                dir_ids.append(current_did)
            else:
                for direction in payload.get("directions", []):
                    d_id = direction.get("id")
                    if d_id:
                        dir_ids.append(d_id)
        if not dir_ids:
            continue
        task_type = payload.get("type") or "unknown"
        for d_id in dir_ids:
            counts[d_id] = counts.get(d_id, 0) + 1
            counts_by_type.setdefault(d_id, {})
            counts_by_type[d_id][task_type] = counts_by_type[d_id].get(task_type, 0) + 1
            active_tasks.setdefault(d_id, []).append(
                {
                    "id": str(task.id),
                    "status": task.status.value
                    if hasattr(task.status, "value")
                    else str(task.status),
                    "title": task.title or "",
                    "latest_status_message": (task.latest_status_message or "")[:200],
                }
            )

    return {
        "counts": counts,
        "counts_by_type": counts_by_type,
        "active_tasks": active_tasks,
    }


@router.patch("/projects/{project_id}/autonomous")
@limiter.limit("30/minute")
async def toggle_autonomous(
    request: Request,
    project_id: uuid.UUID,
    payload: Dict[str, Any],
    session: AsyncSession = Depends(get_session),
    current_user: dict = Depends(get_current_user),
):
    project = await _get_user_project_with_role(
        project_id, current_user["user_id"], session, "owner"
    )

    if "enabled" not in payload:
        raise HTTPException(status_code=400, detail="'enabled' field required")

    project.autonomous_enabled = bool(payload["enabled"])
    # Reset failure counter when re-enabling
    if project.autonomous_enabled:
        from app.shared_state import autonomous_failure_counts

        autonomous_failure_counts.pop(str(project_id), None)
    session.add(project)
    await session.commit()
    return {"autonomous_enabled": project.autonomous_enabled}


@router.patch("/projects/{project_id}/pause")
@limiter.limit("30/minute")
async def set_project_paused(
    request: Request,
    project_id: uuid.UUID,
    payload: Dict[str, Any],
    session: AsyncSession = Depends(get_session),
    current_user: dict = Depends(get_current_user),
):
    """Owner-only pause/resume switch for a project.

    Pause is a first-class persisted state, separate from the
    ``autonomous_enabled`` toggle and from terminal lifecycle ``status``.
    While paused, the orchestrator selects no new QUEUED work for the project
    on any dispatch path (cold or warm) and the autonomous spawn entry points
    skip it; in-flight PROVISIONING/IN_PROGRESS tasks drain. Resuming restores
    normal dispatch with no re-configuration. ``autonomous_enabled`` is left
    untouched so the two toggles compose independently.
    """
    project = await _get_user_project_with_role(
        project_id, current_user["user_id"], session, "owner"
    )

    if "paused" not in payload:
        raise HTTPException(status_code=400, detail="'paused' field required")

    project.paused = bool(payload["paused"])
    session.add(project)
    await session.commit()
    return {"paused": project.paused}


@router.get("/task-types")
@limiter.limit("30/minute")
async def get_task_types(
    request: Request,
    current_user: dict = Depends(get_current_user),
):
    """Return the enriched task-type registry as an operator catalog.

    Project-agnostic and static (the registry lives in code), so this performs
    no DB query — it serializes ``task_type_catalog()`` directly. It is the
    single backend source of truth the admin-ui catalog and task launcher read
    so the frontend capability list cannot drift from the backend registry.
    Gated by the router-level ``Depends(get_current_user)`` like its siblings.
    """
    from app.task_capabilities import task_type_catalog

    return {"task_types": task_type_catalog()}


@router.get("/task-types/cost-adherence")
@limiter.limit("30/minute")
async def get_task_type_cost_adherence(
    request: Request,
    days: int = Query(14, ge=1, le=90),
    session: AsyncSession = Depends(get_session),
    current_user: dict = Depends(get_current_user),
):
    """Per-task-type actual VM cost vs the declared registry ceiling.

    DB-backed sibling of ``/task-types`` (which is intentionally DB-free): the
    catalog endpoint stays fast and static while the actual-cost comparison —
    which requires scanning recent terminal tasks — lives here. Returns, per
    registered task type, the average and p95 actual VM cost (including banked
    retry spend) over the last ``days`` days, a sample count, and an
    ``over_ceiling`` flag that fires only once a type has enough samples and its
    average exceeds its declared ceiling. Read-only and additive.

    The query explicitly loads every column the VM-cost accessor reads so the
    synchronous compute path never lazy-loads off the async session
    (MissingGreenlet). Scoped to the caller's projects.
    """
    from app.digest_metrics import compute_task_type_cost_adherence
    from app.models import ComputeJob
    from app.models import get_task_total_vm_cost_usd_or_none  # noqa: F401  (accessor used in compute fn)
    from app.task_capabilities import TASK_CAPABILITY_BY_VALUE

    window_start = _utcnow() - timedelta(days=days)
    user_pids = _user_project_ids(current_user["user_id"])

    result = await session.execute(
        select(Task)
        .where(
            Task.project_id.in_(user_pids),
            Task.created_at >= window_start,
            Task.status.in_(
                [TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.CANCELLED]
            ),
        )
        .options(
            load_only(
                Task.id,
                Task.status,
                Task.created_at,
                Task.payloads,
                Task.context,
                Task.vm_provisioned_at,
                Task.vm_stopped_at,
                Task.vm_spot,
                Task.vm_machine_type,
                Task.vm_runtime_cost_banked_db,
                raiseload=True,
            )
        )
        .limit(10000)
    )
    tasks = result.scalars().all()

    # Off-VM task types (e.g. compute_experiment) hold only a lightweight
    # monitoring VM; their real spend lives on the linked compute jobs' durable
    # ``final_cost_usd`` ledger. Aggregate that spend per task so the compute
    # function can measure those types honestly instead of from Task VM fields.
    # Lightweight second query (sum grouped by task_id, scoped to user projects);
    # keeps the compute transform DB-free.
    compute_cost_result = await session.execute(
        select(
            ComputeJob.task_id,
            func.coalesce(func.sum(ComputeJob.final_cost_usd), 0.0).label("spend"),
        )
        .where(
            ComputeJob.project_id.in_(user_pids),
            ComputeJob.task_id.isnot(None),
            ComputeJob.final_cost_usd.isnot(None),
        )
        .group_by(ComputeJob.task_id)
    )
    compute_job_cost_by_task = {
        str(task_id): float(spend)
        for task_id, spend in compute_cost_result.all()
        if task_id is not None
    }

    ceilings = {
        value: cap.cost_ceiling_usd for value, cap in TASK_CAPABILITY_BY_VALUE.items()
    }
    rows = compute_task_type_cost_adherence(
        tasks,
        window_start=window_start,
        ceilings=ceilings,
        compute_job_cost_by_task=compute_job_cost_by_task,
    )
    return {"window_days": days, "task_types": rows}


@router.get("/autonomous-status")
@limiter.limit("30/minute")
async def get_autonomous_status(
    request: Request,
    session: AsyncSession = Depends(get_session),
    current_user: dict = Depends(get_current_user),
):
    from app.shared_state import autonomous_failure_counts, runtime_config
    from app.autonomous_research import project_parallelism_cap

    projects = (
        (
            await session.execute(
                select(Project)
                .where(Project.id.in_(_user_project_ids(current_user["user_id"])))
                .order_by(Project.created_at.desc())
            )
        )
        .scalars()
        .all()
    )

    one_hour_ago = _utcnow() - timedelta(hours=1)

    # Count ALL orchestrator tasks per project in the last hour (mirrors the per-project
    # rate guard in autonomous_research.py which counts all ORCHESTRATOR-created tasks).
    user_pids = _user_project_ids(current_user["user_id"])
    orch_count_rows = (
        await session.execute(
            select(Task.project_id, func.count().label("cnt"))
            .where(
                Task.project_id.in_(user_pids),
                Task.created_by == "ORCHESTRATOR",
                Task.created_at >= one_hour_ago,
            )
            .group_by(Task.project_id)
        )
    ).all()
    tasks_per_project: dict = {row.project_id: row.cnt for row in orch_count_rows}

    # Batch query: most recent autonomous task per project (30-day lookback to
    # avoid a full-table scan as task history grows; sufficient for rate display)
    last_research_subq = (
        select(
            Task.project_id,
            func.max(Task.created_at).label("max_created_at"),
        )
        .where(
            Task.created_by == "ORCHESTRATOR",
            Task.created_at >= _utcnow() - timedelta(days=30),
        )
        .group_by(Task.project_id)
        .subquery()
    )
    last_research_rows = (
        await session.execute(
            select(last_research_subq.c.project_id, last_research_subq.c.max_created_at)
        )
    ).all()
    last_research_map = {
        row.project_id: row.max_created_at for row in last_research_rows
    }

    fleet_default = runtime_config["max_autonomous_tasks_per_hour"]
    result = []
    for project in projects:
        project_id_str = str(project.id)
        effective_cap = project_parallelism_cap(
            project,
            "max_autonomous_tasks_per_hour",
            "max_autonomous_tasks_per_hour",
            12,
        )
        bc = getattr(project, "build_config", None) or {}
        override_val = bc.get("max_autonomous_tasks_per_hour")
        has_override = (
            isinstance(override_val, int)
            and not isinstance(override_val, bool)
            and override_val >= 1
        )
        result.append(
            {
                "project_id": project_id_str,
                "project_name": project.name,
                "autonomous_enabled": project.autonomous_enabled,
                "consecutive_failures": autonomous_failure_counts.get(
                    project_id_str, 0
                ),
                "last_research_at": last_research_map[project.id].isoformat()
                if last_research_map.get(project.id)
                else None,
                "tasks_spawned_last_hour": tasks_per_project.get(project.id, 0),
                "rate_limit": effective_cap,
                "fleet_default_rate_limit": fleet_default,
                "rate_limit_source": "project_override"
                if has_override
                else "fleet_default",
            }
        )

    return {"projects": result}


@router.post("/projects/{project_id}/bootstrap/retry")
@limiter.limit("5/minute")
async def retry_project_bootstrap(
    request: Request,
    project_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    current_user: dict = Depends(get_current_user),
):
    """Enqueue a recovery repo_bootstrap task for a degraded or never-bootstrapped project.

    Returns a JSON body with ``state`` describing the outcome:
    - ``spawned`` — a new task was created; ``task_id`` is present.
    - ``already_active`` — a bootstrap task is already running; no duplicate created.
    - ``already_completed`` — bootstrap_completed_at is set; no task created.
    - ``not_eligible`` — project is a self-repo or empty-repo; not bootstrappable.
    """
    from app.repo_bootstrap import spawn_bootstrap_task

    project = await _get_user_project_with_role(
        project_id, current_user["user_id"], session, "owner"
    )

    result = await spawn_bootstrap_task(
        project,
        session,
        is_first_bootstrap=False,
        spawn_reason="operator_retry",
    )
    if result.outcome == "spawned":
        await session.commit()

    return {
        "state": result.outcome,
        "task_id": result.task_id,
        "reason": result.reason,
    }


@router.post("/projects/{project_id}/bootstrap/refresh")
@limiter.limit("5/minute")
async def refresh_project_bootstrap(
    request: Request,
    project_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    current_user: dict = Depends(get_current_user),
):
    """Re-run repo_bootstrap for an already-COMPLETED project to refresh its overview.

    Unlike ``/bootstrap/retry`` (which serves degraded/never-run projects), this
    bypasses the ``bootstrap_completed_at`` not-eligible gate so an operator can
    request a fresh overview for a healthy project. It is a one-shot action: it
    does NOT clear ``bootstrap_completed_at`` and does NOT arm automatic retries.

    Returns a JSON body with ``state`` describing the outcome:
    - ``spawned`` — a new bootstrap task was created; ``task_id`` is present.
    - ``already_active`` — a bootstrap task is already running; no duplicate created.
    - ``not_eligible`` — project is a self-repo or empty-repo; not bootstrappable.
    """
    from app.repo_bootstrap import REFRESH_SPAWN_REASON, spawn_bootstrap_task

    project = await _get_user_project_with_role(
        project_id, current_user["user_id"], session, "owner"
    )

    result = await spawn_bootstrap_task(
        project,
        session,
        is_first_bootstrap=False,
        spawn_reason=REFRESH_SPAWN_REASON,
    )
    if result.outcome == "spawned":
        await session.commit()

    return {
        "state": result.outcome,
        "task_id": result.task_id,
        "reason": result.reason,
    }

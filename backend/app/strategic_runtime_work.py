"""Strategic workbench projection for passive runtime/background monitoring."""

from __future__ import annotations

import uuid
from datetime import timedelta
from types import SimpleNamespace
from typing import Any, Dict

from sqlalchemy import and_, func, or_
from sqlalchemy.sql.elements import ColumnElement
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from app.compute_backends import RUNNER_SUBPROCESS_BACKEND_NAME
from app.compute_jobs_aggregation import aggregate_compute_jobs_summary
from app.models import (
    ComputeJob,
    ComputeJobStatus,
    LocalRunner,
    Task,
    TaskStatus,
    TaskWakeRegistration,
    WakeDeliveryState,
)
from app.routers.admin_runners import _RUNNER_LIVENESS_SECONDS, _runner_health
from app.runner_project_binding import runner_allowed_for_project
from app.task_capabilities import STRATEGIC_TASK_TYPES
from app.time_utils import _utcnow
from app.workbench_state import (
    STRATEGIC_LOCAL_RUNNER_HOLD_CONTEXT_KEY,
    STRATEGIC_LOCAL_RUNNER_REQUIRED_CONTEXT_KEY,
    strategic_local_continuation_status_from_vision,
)

_ACTIVE_COMPUTE_JOB_STATUSES = (
    ComputeJobStatus.QUEUED,
    ComputeJobStatus.STARTING,
    ComputeJobStatus.RUNNING,
    ComputeJobStatus.POLLING,
)
_TERMINAL_COMPUTE_JOB_STATUSES = (
    ComputeJobStatus.SUCCEEDED,
    ComputeJobStatus.FAILED,
    ComputeJobStatus.CANCELLED,
    ComputeJobStatus.TIMED_OUT,
)
_ACTIVE_WAKE_STATES = (
    WakeDeliveryState.PENDING,
    WakeDeliveryState.FIRED,
)
_PROBLEM_WAKE_STATES = (
    WakeDeliveryState.EXPIRED,
    WakeDeliveryState.FAILED,
)
_RUNTIME_WORK_PREVIEW_LIMIT = 6
_RUNNER_CANDIDATE_LIMIT = 8
LOCAL_RUNNER_HOLD_CONTEXT_KEY = "awaiting_local_runner_since"
# Sibling compute hold: an image-declaring job that found no container-capable
# runner is parked under this key. It shares the local-runner hold class for
# counting, operator-attention surfacing, and timeout escalation.
CONTAINER_RUNNER_HOLD_CONTEXT_KEY = "awaiting_container_runner_since"
_LOCAL_STRATEGIC_RUNNING_STATUSES = frozenset(
    {
        TaskStatus.IN_PROGRESS,
        TaskStatus.EVALUATING,
        TaskStatus.BLOCKED,
        TaskStatus.PARKED,
    }
)
_CLOUD_STRATEGIC_RUNNING_STATUSES = frozenset(
    {
        TaskStatus.PROVISIONING,
        *_LOCAL_STRATEGIC_RUNNING_STATUSES,
    }
)
_CORE_STRATEGIC_CYCLE_TASK_TYPES = frozenset(
    task_type
    for task_type in ("design_doc_update", "strategic_review")
    if task_type in STRATEGIC_TASK_TYPES
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


def _isoformat(value: Any) -> str | None:
    return value.isoformat() if value else None


def _string_or_none(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    stripped = value.strip()
    return stripped or None


def _task_type(task: Task | None) -> str | None:
    if task is None:
        return None
    payload = task.first_payload or {}
    if not isinstance(payload, dict):
        return None
    return _string_or_none(payload.get("type"))


def _task_payload(task: Task | None) -> dict[str, Any]:
    if task is None:
        return {}
    payload = task.first_payload or {}
    return payload if isinstance(payload, dict) else {}


def task_awaiting_local_runner_since(task: Task | None) -> str | None:
    if task is None:
        return None
    context = task.context or {}
    raw = context.get(LOCAL_RUNNER_HOLD_CONTEXT_KEY)
    if raw is None:
        return None
    if isinstance(raw, str):
        return _string_or_none(raw)
    return str(raw)


def task_awaiting_container_runner_since(task: Task | None) -> str | None:
    if task is None:
        return None
    context = task.context or {}
    raw = context.get(CONTAINER_RUNNER_HOLD_CONTEXT_KEY)
    if raw is None:
        return None
    if isinstance(raw, str):
        return _string_or_none(raw)
    return str(raw)


def task_awaiting_strategic_local_runner_since(task: Task | None) -> str | None:
    if task is None:
        return None
    context = task.context or {}
    raw = context.get(STRATEGIC_LOCAL_RUNNER_HOLD_CONTEXT_KEY)
    if raw is None:
        return None
    if isinstance(raw, str):
        return _string_or_none(raw)
    return str(raw)


def task_requires_strategic_local_runner(task: Task | None) -> bool:
    if task is None:
        return False
    payload = _task_payload(task)
    return (
        payload.get("type") in STRATEGIC_TASK_TYPES
        and bool((task.context or {}).get(STRATEGIC_LOCAL_RUNNER_REQUIRED_CONTEXT_KEY))
    )


def task_is_strategic_local_runner_hold(task: Task | None) -> bool:
    if task is None:
        return False
    return (
        task_requires_strategic_local_runner(task)
        and task.status == TaskStatus.QUEUED
        and bool((task.context or {}).get(STRATEGIC_LOCAL_RUNNER_HOLD_CONTEXT_KEY))
    )


def task_is_local_runner_hold(task: Task | None) -> bool:
    if task is None:
        return False
    return (
        task.status == TaskStatus.QUEUED
        and not task.archived
        and _task_type(task) == "compute_experiment"
        and (
            task_awaiting_local_runner_since(task) is not None
            or task_awaiting_container_runner_since(task) is not None
        )
    )


def local_runner_hold_task_filters(project_id: uuid.UUID) -> tuple[ColumnElement[bool], ...]:
    awaiting_local = Task.context[LOCAL_RUNNER_HOLD_CONTEXT_KEY].as_string()
    awaiting_container = Task.context[CONTAINER_RUNNER_HOLD_CONTEXT_KEY].as_string()
    return (
        Task.project_id == project_id,
        Task.archived.is_(False),
        Task.status == TaskStatus.QUEUED,
        Task.requires_approval.is_not(True),
        Task.payloads[0]["type"].as_string() == "compute_experiment",
        # A compute_experiment is held either for a require-local runner or for a
        # container-capable runner (image-declaring GPU job on a Docker-less box);
        # both stalls are the same operator-facing hold class.
        or_(
            and_(awaiting_local.is_not(None), awaiting_local != ""),
            and_(awaiting_container.is_not(None), awaiting_container != ""),
        ),
    )


def strategic_local_runner_hold_task_filters(
    project_id: uuid.UUID,
) -> tuple[ColumnElement[bool], ...]:
    awaiting_since = Task.context[STRATEGIC_LOCAL_RUNNER_HOLD_CONTEXT_KEY].as_string()
    required = Task.context[STRATEGIC_LOCAL_RUNNER_REQUIRED_CONTEXT_KEY].as_boolean()
    return (
        Task.project_id == project_id,
        Task.archived.is_(False),
        Task.status == TaskStatus.QUEUED,
        Task.requires_approval.is_not(True),
        Task.payloads[0]["type"].as_string().in_(list(STRATEGIC_TASK_TYPES)),
        required.is_(True),
        awaiting_since.is_not(None),
        awaiting_since != "",
    )


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


def _int_capability(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if value > 0 else None
    if isinstance(value, float):
        return int(value) if value.is_integer() and value > 0 else None
    if isinstance(value, str):
        try:
            parsed = int(value)
        except ValueError:
            return None
        return parsed if parsed > 0 else None
    return None


def _runner_capabilities_view(capabilities: Dict[str, Any] | None) -> Dict[str, Any]:
    caps = capabilities if isinstance(capabilities, dict) else {}
    view: Dict[str, Any] = {}
    for key in ("runner_preset", "workload"):
        value = caps.get(key)
        if isinstance(value, str):
            compact = _compact_text(value, max_chars=120)
            if compact:
                view[key] = compact
    if isinstance(caps.get("has_gpu"), bool):
        view["has_gpu"] = caps["has_gpu"]
    max_concurrent = _int_capability(caps.get("max_concurrent"))
    if max_concurrent is not None:
        view["max_concurrent"] = max_concurrent
    if isinstance(caps.get("agent_available"), bool):
        view["agent_available"] = caps["agent_available"]
    task_types = caps.get("task_types")
    if task_types == "all":
        view["task_types"] = "all"
    elif isinstance(task_types, list):
        view["task_types"] = [
            compact
            for compact in (
                _compact_text(task_type, max_chars=80)
                for task_type in task_types[:24]
                if isinstance(task_type, str)
            )
            if compact
        ]
    return view


def _merge_compute_job_ids(
    capabilities: Dict[str, Any],
    active_compute_job_ids: list[str],
) -> Dict[str, Any]:
    if not active_compute_job_ids:
        return capabilities
    merged = dict(capabilities)
    seen: set[str] = set()
    job_ids: list[str] = []
    for job_id in (
        list(capabilities.get("active_compute_job_ids") or [])
        + active_compute_job_ids
    ):
        if not isinstance(job_id, str) or job_id in seen:
            continue
        seen.add(job_id)
        job_ids.append(job_id)
    merged["active_compute_job_ids"] = job_ids
    return merged


def _runner_health_with_compute_assignments(
    runner: LocalRunner,
    active_compute_job_ids: list[str],
) -> Dict[str, Any]:
    if not active_compute_job_ids:
        return _runner_health(runner)
    capabilities = _merge_compute_job_ids(
        dict(runner.capabilities or {}),
        active_compute_job_ids,
    )
    return _runner_health(
        SimpleNamespace(
            status=runner.status,
            last_seen_at=runner.last_seen_at,
            capabilities=capabilities,
            # Pass through trust-telemetry fields _runner_health reads; the
            # compute-assignment view must carry the same failure/success
            # signal as the direct view (else AttributeError on this shim).
            consecutive_job_failures=runner.consecutive_job_failures,
            last_successful_job_at=runner.last_successful_job_at,
        )
    )


async def _load_runner_compute_job_assignments(
    session: AsyncSession,
    runner_ids: list[uuid.UUID],
) -> dict[str, list[str]]:
    if not runner_ids:
        return {}
    runner_id_strings = [str(runner_id) for runner_id in runner_ids]
    runner_target_expr = ComputeJob.polling_metadata["runner_target_id"].as_string()
    rows = (
        await session.execute(
            select(ComputeJob.id, runner_target_expr)
            .where(
                ComputeJob.backend == RUNNER_SUBPROCESS_BACKEND_NAME,
                ComputeJob.status.in_(_ACTIVE_COMPUTE_JOB_STATUSES),
                runner_target_expr.in_(runner_id_strings),
            )
            .order_by(ComputeJob.created_at.asc())
        )
    ).all()
    by_runner: dict[str, list[str]] = {runner_id: [] for runner_id in runner_id_strings}
    for job_id, raw_runner_id in rows:
        runner_id = str(raw_runner_id)
        if runner_id in by_runner:
            by_runner[runner_id].append(str(job_id))
    return by_runner


def _runner_health_view(health: Dict[str, Any]) -> Dict[str, Any]:
    view: Dict[str, Any] = {}
    for key in ("device_status", "device_status_reason", "stale_reason"):
        compact = _compact_text(health.get(key), max_chars=120)
        if compact:
            view[key] = compact
    for key in ("is_live",):
        if isinstance(health.get(key), bool):
            view[key] = health[key]
    for key in (
        "seconds_since_last_seen",
        "active_task_count",
        "active_compute_job_count",
        "active_work_count",
        "max_concurrent",
        "available_slots",
        "liveness_window_seconds",
    ):
        value = health.get(key)
        if value is None:
            view[key] = None
        elif isinstance(value, (int, float)) and not isinstance(value, bool):
            view[key] = int(value)
    agent_available = health.get("agent_available")
    view["agent_available"] = (
        agent_available if isinstance(agent_available, bool) else None
    )
    return view


def _runner_blocked_reason(
    runner: LocalRunner,
    health: Dict[str, Any],
    required_task_types: frozenset[str],
) -> str | None:
    if not _runner_handles_task_types(runner, required_task_types):
        return "capability_mismatch"
    if not health.get("is_live"):
        return health.get("stale_reason") or "runner_offline"
    if health.get("agent_available") is False:
        return "agent_unavailable"
    available_slots = health.get("available_slots")
    if available_slots is not None and int(available_slots) <= 0:
        return "at_capacity"
    return None


def _runner_candidate_view(
    runner: LocalRunner,
    health: Dict[str, Any],
    required_task_types: frozenset[str],
    *,
    active_runner_id: str | None,
    preferred_runner_id: str | None,
) -> Dict[str, Any]:
    eligible = _runner_is_dispatch_eligible(runner, health, required_task_types)
    runner_id = str(runner.id)
    return {
        "id": runner_id,
        "hostname": _compact_text(runner.hostname, max_chars=160),
        "status": _compact_text(runner.status, max_chars=80),
        "last_seen_at": _isoformat(runner.last_seen_at),
        "created_at": _isoformat(runner.created_at),
        "capabilities": _runner_capabilities_view(runner.capabilities),
        "health": _runner_health_view(health),
        "handles_active_task_type": _runner_handles_task_types(
            runner,
            required_task_types,
        ),
        "eligible_for_active_task": eligible,
        "is_preferred": bool(preferred_runner_id and runner_id == preferred_runner_id),
        "is_active_runner": bool(active_runner_id and runner_id == active_runner_id),
        "blocked_reason": None
        if eligible
        else _runner_blocked_reason(runner, health, required_task_types),
    }


def _runner_candidate_sort_key(candidate: Dict[str, Any]) -> tuple[Any, ...]:
    health = (
        candidate.get("health")
        if isinstance(candidate.get("health"), dict)
        else {}
    )
    seconds = health.get("seconds_since_last_seen")
    if not isinstance(seconds, (int, float)):
        seconds = 10**9
    return (
        not bool(candidate.get("is_active_runner")),
        not bool(candidate.get("is_preferred")),
        not bool(candidate.get("eligible_for_active_task")),
        not bool(health.get("is_live")),
        seconds,
        str(candidate.get("hostname") or ""),
    )


def _active_strategic_task_types(task: Task | None) -> frozenset[str]:
    if task is None:
        return _CORE_STRATEGIC_CYCLE_TASK_TYPES
    payload = _task_payload(task)
    task_type = payload.get("type")
    if isinstance(task_type, str) and task_type in STRATEGIC_TASK_TYPES:
        return frozenset({task_type})
    return _CORE_STRATEGIC_CYCLE_TASK_TYPES


def _task_summary(
    task: Task | None,
    *,
    include_awaiting_since: bool = False,
) -> Dict[str, Any] | None:
    if task is None:
        return None
    summary = {
        "id": str(task.id),
        "status": _enum_value(task.status),
        "title": task.title,
        "task_type": _task_type(task),
        "latest_status_message": task.latest_status_message,
        "started_at": _isoformat(task.started_at),
        "created_at": _isoformat(task.created_at),
        "updated_at": _isoformat(task.updated_at),
    }
    if include_awaiting_since:
        summary["awaiting_since"] = (
            task_awaiting_local_runner_since(task)
            or task_awaiting_strategic_local_runner_since(task)
        )
    blocked_reason = _compact_text(task.blocked_reason, max_chars=180)
    if blocked_reason:
        summary["blocked_reason"] = blocked_reason
    park_reason = _compact_text(task.park_reason, max_chars=180)
    if park_reason:
        summary["park_reason"] = park_reason
    return summary


def _local_operator_status(
    mode: str,
    fallback_reason: str | None,
    active_task: Task | None,
) -> Dict[str, Any]:
    """Map runner dispatch detail to the operator-facing Strategic vocabulary."""

    if active_task is not None:
        if mode == "local":
            return {"status": "working", "location": "locally", "reason": None}
        if mode == "cloud":
            if (
                active_task.vm_instance_id
                and fallback_reason != "cloud_dispatch_pending"
            ):
                return {
                    "status": "working",
                    "location": "cloud",
                    "reason": fallback_reason,
                }
            return {
                "status": "unavailable",
                "location": "cloud" if active_task.vm_instance_id else "unavailable",
                "reason": fallback_reason,
            }
        return {
            "status": "unavailable",
            "location": "locally",
            "reason": fallback_reason,
        }
    if mode == "local_ready":
        return {"status": "idle", "location": "locally", "reason": None}
    if mode == "cloud":
        return {
            "status": "unavailable",
            "location": "unavailable",
            "reason": fallback_reason,
        }
    return {
        "status": "unavailable",
        "location": "locally",
        "reason": fallback_reason,
    }


def _compute_job_preview(job: ComputeJob, task: Task | None) -> Dict[str, Any]:
    cost = job.final_cost_usd if job.final_cost_usd is not None else job.accrued_cost_usd
    payload = _task_payload(task)
    preview: Dict[str, Any] = {
        "id": str(job.id),
        "task_id": str(job.task_id) if job.task_id else None,
        "task_title": _compact_text(task.title, max_chars=120) if task else None,
        "task_type": payload.get("type"),
        "status": _enum_value(job.status),
        "backend": job.backend,
        "machine_type": job.machine_type,
        "accelerator_type": job.accelerator_type,
        "accelerator_count": job.accelerator_count,
        "spend_cap_usd": job.spend_cap_usd,
        "cost_usd": float(cost) if cost is not None else None,
        "created_at": _isoformat(job.created_at),
        "updated_at": _isoformat(job.updated_at),
        "started_at": _isoformat(job.started_at),
        "completed_at": _isoformat(job.completed_at),
        "last_heartbeat_at": _isoformat(job.last_heartbeat_at),
        "next_poll_at": _isoformat(job.next_poll_at),
        "terminal_reason": _compact_text(job.terminal_reason, max_chars=180),
    }
    if task is not None:
        plan_output = task.plan_output or {}
        if isinstance(plan_output, dict):
            compute_result = plan_output.get("compute_result") or {}
            if isinstance(compute_result, dict):
                criterion = compute_result.get("criterion")
                if isinstance(criterion, dict) and "met" in criterion:
                    criterion_met = criterion.get("met")
                    if criterion_met is True:
                        outcome_label = "met"
                    elif criterion_met is False and criterion.get("measurable") is not False:
                        outcome_label = "unmet"
                    else:
                        outcome_label = "inconclusive"
                    hypothesis = compute_result.get("hypothesis")
                    hypothesis_held = (
                        hypothesis.get("held")
                        if isinstance(hypothesis, dict)
                        else None
                    )
                    preview["experiment_outcome"] = {
                        "outcome_label": outcome_label,
                        "criterion_met": criterion_met,
                        "criterion_summary": _compact_text(
                            criterion.get("summary") or criterion.get("description"),
                            max_chars=200,
                        ),
                        "hypothesis_held": hypothesis_held,
                    }
    return preview


def _wake_preview(
    registration: TaskWakeRegistration,
    task: Task | None,
) -> Dict[str, Any]:
    return {
        "id": str(registration.id),
        "task_id": str(registration.task_id),
        "task_title": _compact_text(task.title, max_chars=120) if task else None,
        "task_status": _enum_value(task.status) if task else None,
        "wake_source_kind": registration.wake_source_kind,
        "wake_source_ref": registration.wake_source_ref,
        "wake_action": _enum_value(registration.wake_action),
        "delivery_state": _enum_value(registration.delivery_state),
        "requested_at": _isoformat(registration.requested_at),
        "deadline_at": _isoformat(registration.deadline_at),
        "fired_at": _isoformat(registration.fired_at),
        "delivered_at": _isoformat(registration.delivered_at),
        "attempt_count": registration.attempt_count,
        "result_state": registration.result_state,
        "result_detail": _compact_text(registration.result_detail, max_chars=180),
    }


def _criterion_counts_from_tasks(tasks: list[Task]) -> Dict[str, int]:
    measured = 0
    met = 0
    for task in tasks:
        plan_output = task.plan_output or {}
        if not isinstance(plan_output, dict):
            continue
        compute_result = plan_output.get("compute_result") or {}
        if not isinstance(compute_result, dict):
            continue
        criterion = compute_result.get("criterion")
        if not isinstance(criterion, dict) or "met" not in criterion:
            continue
        measured += 1
        if criterion.get("met") is True:
            met += 1
    return {"criterion_measured": measured, "criterion_met": met}


async def load_project_runners(
    session: AsyncSession,
    project_id: uuid.UUID,
) -> list[LocalRunner]:
    """Fetch the project's non-deleted allow-listed runners, newest-seen first.

    Call once per strategic-state assembly and pass the result to
    build_strategic_runtime_summary and
    build_operator_attention to avoid duplicate DB round-trips.
    """
    return list(
        (
            await session.execute(
                select(LocalRunner)
                .where(
                    runner_allowed_for_project(project_id),
                    LocalRunner.status != "deleted",
                )
                .order_by(
                    LocalRunner.last_seen_at.desc(),
                    LocalRunner.created_at.desc(),
                )
            )
        )
        .scalars()
        .all()
    )


async def build_strategic_runtime_summary(
    session: AsyncSession,
    project_id: uuid.UUID,
    *,
    active_task: Task | None,
    project_vision: Dict[str, Any] | None = None,
    operator_user_id: str | None = None,
    runner_rows: list[LocalRunner] | None = None,
) -> Dict[str, Any]:
    """Project local-runner readiness plus passive background-work state."""

    if runner_rows is None:
        runner_rows = await load_project_runners(session, project_id)
    readiness_task_types = _active_strategic_task_types(active_task)
    strategic_runners = [
        runner for runner in runner_rows if _runner_handles_any_strategic_work(runner)
    ]
    active_compute_jobs_by_runner = await _load_runner_compute_job_assignments(
        session,
        [runner.id for runner in strategic_runners],
    )
    readiness_capable_runners = [
        runner
        for runner in strategic_runners
        if _runner_handles_task_types(runner, readiness_task_types)
    ]
    health_by_runner = {
        str(runner.id): _runner_health_with_compute_assignments(
            runner,
            active_compute_jobs_by_runner.get(str(runner.id), []),
        )
        for runner in strategic_runners
    }
    runner_by_id = {str(runner.id): runner for runner in strategic_runners}

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
    active_task_requires_local = task_requires_strategic_local_runner(active_task)
    active_task_cloud_violation = (
        active_task_requires_local
        and active_task is not None
        and active_task.status in _CLOUD_STRATEGIC_RUNNING_STATUSES
        and isinstance(active_task.vm_instance_id, str)
        and active_task.vm_instance_id
        and not active_task.vm_instance_id.startswith("runner:")
    )
    if (
        active_task_requires_local
        and not active_task_pinned
        and not active_task_cloud_violation
        and active_task_runtime
    ):
        active_task_runtime = {**active_task_runtime, "vm_instance_id": None}

    continuation_status = strategic_local_continuation_status_from_vision(
        project_vision,
        operator_user_id=operator_user_id,
    )
    preferred_runner_id = continuation_status.get("preferred_runner_id")
    if not isinstance(preferred_runner_id, str) or not preferred_runner_id.strip():
        preferred_runner_id = None
    else:
        preferred_runner_id = preferred_runner_id.strip()

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
    elif active_task_cloud_violation:
        mode = "cloud"
        fallback_reason = "local_required_cloud_dispatch"
    elif active_task_requires_local:
        mode = "pending_local"
        if not strategic_runners:
            fallback_reason = "no_runner"
        elif not readiness_capable_runners:
            fallback_reason = "capability_mismatch"
        elif readiness_live_runner_count == 0:
            fallback_reason = "runner_offline"
        elif agent_unavailable_count > 0:
            fallback_reason = "agent_unavailable"
        elif busy_runner_count > 0:
            fallback_reason = "at_capacity"
        else:
            fallback_reason = "runner_unavailable"
    elif active_task is not None and active_task.vm_instance_id:
        mode = "cloud"
        fallback_reason = (
            "cloud_dispatch"
            if active_task.status in _CLOUD_STRATEGIC_RUNNING_STATUSES
            else "cloud_dispatch_pending"
        )
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
    operator_status = _local_operator_status(mode, fallback_reason, active_task)
    if continuation_status["enabled"] and continuation_status["fresh"]:
        if preferred_runner_id:
            preferred_health = health_by_runner.get(preferred_runner_id)
            preferred_runner = runner_by_id.get(preferred_runner_id)
            if preferred_health is None:
                continuation_status["reason"] = "preferred_runner_missing"
            elif preferred_runner is not None:
                preferred_blocked_reason = _runner_blocked_reason(
                    preferred_runner,
                    preferred_health,
                    readiness_task_types,
                )
                continuation_status["reason"] = (
                    preferred_blocked_reason or "preferred_runner_live"
                )
            else:
                continuation_status["reason"] = (
                    preferred_health.get("stale_reason") or "preferred_runner_offline"
                )
        elif eligible_live_count > 0:
            continuation_status["reason"] = "local_runner_ready"
        elif live_runner_count > 0:
            continuation_status["reason"] = "local_runner_live"
        elif strategic_runners:
            continuation_status["reason"] = "runner_offline"
        else:
            continuation_status["reason"] = "no_runner"

    runner_candidates = sorted(
        (
            _runner_candidate_view(
                runner,
                health_by_runner[str(runner.id)],
                readiness_task_types,
                active_runner_id=active_runner_id,
                preferred_runner_id=preferred_runner_id,
            )
            for runner in strategic_runners
        ),
        key=_runner_candidate_sort_key,
    )[:_RUNNER_CANDIDATE_LIMIT]

    return {
        "local_runner": {
            "mode": mode,
            "status": operator_status["status"],
            "location": operator_status["location"],
            "status_reason": operator_status["reason"],
            "strategic_runner_count": len(strategic_runners),
            "readiness_capable_runner_count": len(readiness_capable_runners),
            "live_runner_count": live_runner_count,
            "eligible_live_count": eligible_live_count,
            "stale_runner_count": stale_runner_count,
            "busy_runner_count": busy_runner_count,
            "agent_unavailable_count": agent_unavailable_count,
            "active_runner_id": active_runner_id,
            "active_task_pinned": active_task_pinned,
            "active_task_requires_local": active_task_requires_local,
            "active_task_awaiting_local_runner_since": (
                task_awaiting_strategic_local_runner_since(active_task)
            ),
            "active_task_runtime": active_task_runtime,
            "fallback_reason": fallback_reason,
            "liveness_window_seconds": _RUNNER_LIVENESS_SECONDS,
            "continuation": continuation_status,
            "runners": runner_candidates,
        },
        "work": await build_runtime_work_summary(session, project_id),
    }


async def build_runtime_work_summary(
    session: AsyncSession,
    project_id: uuid.UUID,
) -> Dict[str, Any]:
    """Return the bounded runtime-work read model used by Strategic state."""

    window = timedelta(hours=6)
    cutoff = _utcnow() - window
    compute_summary = await aggregate_compute_jobs_summary(
        session,
        project_id=project_id,
        include_finished_within=window,
        all_terminal=False,
    )
    status_counts = compute_summary.get("outcome_breakdown") or {}

    active_job_rows = (
        await session.execute(
            select(ComputeJob)
            .where(
                ComputeJob.project_id == project_id,
                ComputeJob.status.in_(_ACTIVE_COMPUTE_JOB_STATUSES),
            )
            .order_by(ComputeJob.updated_at.desc(), ComputeJob.created_at.desc())
            .limit(_RUNTIME_WORK_PREVIEW_LIMIT)
        )
    ).scalars().all()
    recent_terminal_rows = (
        await session.execute(
            select(ComputeJob)
            .where(
                ComputeJob.project_id == project_id,
                ComputeJob.status.in_(_TERMINAL_COMPUTE_JOB_STATUSES),
                ComputeJob.completed_at.is_not(None),
                ComputeJob.completed_at >= cutoff,
            )
            .order_by(ComputeJob.completed_at.desc(), ComputeJob.updated_at.desc())
            .limit(_RUNTIME_WORK_PREVIEW_LIMIT)
        )
    ).scalars().all()
    preview_jobs = active_job_rows + [
        job
        for job in recent_terminal_rows
        if all(job.id != active.id for active in active_job_rows)
    ]
    preview_jobs = preview_jobs[:_RUNTIME_WORK_PREVIEW_LIMIT]
    preview_task_ids = {job.task_id for job in preview_jobs if job.task_id is not None}
    tasks_by_id: Dict[uuid.UUID, Task] = {}
    if preview_task_ids:
        tasks_by_id = {
            task.id: task
            for task in (
                await session.execute(select(Task).where(Task.id.in_(preview_task_ids)))
            )
            .scalars()
            .all()
        }

    criterion_task_ids = (
        await session.execute(
            select(ComputeJob.task_id)
            .where(
                ComputeJob.project_id == project_id,
                ComputeJob.task_id.is_not(None),
                (
                    ComputeJob.status.in_(_ACTIVE_COMPUTE_JOB_STATUSES)
                    | (
                        ComputeJob.status.in_(_TERMINAL_COMPUTE_JOB_STATUSES)
                        & ComputeJob.completed_at.is_not(None)
                        & (ComputeJob.completed_at >= cutoff)
                    )
                ),
            )
            .group_by(ComputeJob.task_id)
        )
    ).scalars().all()
    criterion_tasks: list[Task] = []
    if criterion_task_ids:
        criterion_tasks = (
            await session.execute(select(Task).where(Task.id.in_(criterion_task_ids)))
        ).scalars().all()
    criterion_summary = _criterion_counts_from_tasks(criterion_tasks)

    active_spend = (
        await session.execute(
            select(
                func.coalesce(
                    func.sum(
                        func.coalesce(
                            ComputeJob.accrued_cost_usd,
                            ComputeJob.final_cost_usd,
                        )
                    ),
                    0.0,
                )
            ).where(
                ComputeJob.project_id == project_id,
                ComputeJob.status.in_(_ACTIVE_COMPUTE_JOB_STATUSES),
            )
        )
    ).scalar()
    recent_terminal_spend = (
        await session.execute(
            select(
                func.coalesce(
                    func.sum(
                        func.coalesce(
                            ComputeJob.final_cost_usd,
                            ComputeJob.accrued_cost_usd,
                        )
                    ),
                    0.0,
                )
            ).where(
                ComputeJob.project_id == project_id,
                ComputeJob.status.in_(_TERMINAL_COMPUTE_JOB_STATUSES),
                ComputeJob.completed_at.is_not(None),
                ComputeJob.completed_at >= cutoff,
            )
        )
    ).scalar()

    approval_statuses = [
        TaskStatus.QUEUED,
        TaskStatus.PROVISIONING,
        TaskStatus.IN_PROGRESS,
        TaskStatus.EVALUATING,
        TaskStatus.BLOCKED,
        TaskStatus.PARKED,
    ]
    # Merge held_approvals data + count into one round trip using window function.
    held_result = await session.execute(
        select(Task, func.count().over().label("_total_count"))
        .where(
            Task.project_id == project_id,
            Task.archived.is_(False),
            Task.requires_approval.is_(True),
            Task.status.in_(approval_statuses),
        )
        .order_by(Task.priority.asc(), Task.created_at.asc())
        .limit(_RUNTIME_WORK_PREVIEW_LIMIT)
    )
    _held_rows = held_result.all()
    held_tasks = [row[0] for row in _held_rows]
    held_task_count = _held_rows[0][1] if _held_rows else 0

    local_runner_hold_filters = local_runner_hold_task_filters(project_id)
    strategic_local_runner_hold_filters = strategic_local_runner_hold_task_filters(
        project_id
    )
    # Merge local-runner hold data + count into one round trip per filter.
    _compute_hold_result = await session.execute(
        select(Task, func.count().over().label("_total_count"))
        .where(*local_runner_hold_filters)
        .order_by(Task.priority.asc(), Task.created_at.asc())
        .limit(_RUNTIME_WORK_PREVIEW_LIMIT)
    )
    _compute_hold_rows = _compute_hold_result.all()
    compute_local_runner_hold_tasks = [row[0] for row in _compute_hold_rows]
    local_runner_hold_count = _compute_hold_rows[0][1] if _compute_hold_rows else 0
    _strategic_hold_result = await session.execute(
        select(Task, func.count().over().label("_total_count"))
        .where(*strategic_local_runner_hold_filters)
        .order_by(Task.priority.asc(), Task.created_at.asc())
        .limit(_RUNTIME_WORK_PREVIEW_LIMIT)
    )
    _strategic_hold_rows = _strategic_hold_result.all()
    strategic_local_runner_hold_tasks = [row[0] for row in _strategic_hold_rows]
    strategic_local_runner_hold_count = _strategic_hold_rows[0][1] if _strategic_hold_rows else 0
    local_runner_hold_tasks = [
        *compute_local_runner_hold_tasks,
        *[
            task
            for task in strategic_local_runner_hold_tasks
            if all(task.id != existing.id for existing in compute_local_runner_hold_tasks)
        ],
    ]
    local_runner_hold_tasks.sort(
        key=lambda task: (task.priority, task.created_at or _utcnow())
    )
    local_runner_hold_tasks = local_runner_hold_tasks[:_RUNTIME_WORK_PREVIEW_LIMIT]

    wake_rows = (
        await session.execute(
            select(TaskWakeRegistration)
            .where(
                TaskWakeRegistration.project_id == project_id,
                TaskWakeRegistration.delivery_state.in_(_ACTIVE_WAKE_STATES),
            )
            .order_by(
                TaskWakeRegistration.fired_at.desc().nullslast(),
                TaskWakeRegistration.requested_at.desc(),
            )
            .limit(_RUNTIME_WORK_PREVIEW_LIMIT)
        )
    ).scalars().all()
    recent_problem_wake_filter = (
        (TaskWakeRegistration.requested_at >= cutoff)
        | (
            TaskWakeRegistration.deadline_at.is_not(None)
            & (TaskWakeRegistration.deadline_at >= cutoff)
        )
        | (
            TaskWakeRegistration.fired_at.is_not(None)
            & (TaskWakeRegistration.fired_at >= cutoff)
        )
        | (
            TaskWakeRegistration.delivered_at.is_not(None)
            & (TaskWakeRegistration.delivered_at >= cutoff)
        )
    )
    wake_problem_rows = (
        await session.execute(
            select(TaskWakeRegistration)
            .where(
                TaskWakeRegistration.project_id == project_id,
                TaskWakeRegistration.delivery_state.in_(_PROBLEM_WAKE_STATES),
                recent_problem_wake_filter,
            )
            .order_by(
                TaskWakeRegistration.delivered_at.desc().nullslast(),
                TaskWakeRegistration.deadline_at.desc().nullslast(),
                TaskWakeRegistration.requested_at.desc(),
            )
            .limit(_RUNTIME_WORK_PREVIEW_LIMIT)
        )
    ).scalars().all()
    active_wake_counts = (
        await session.execute(
            select(TaskWakeRegistration.delivery_state, func.count())
            .where(
                TaskWakeRegistration.project_id == project_id,
                TaskWakeRegistration.delivery_state.in_(_ACTIVE_WAKE_STATES),
            )
            .group_by(TaskWakeRegistration.delivery_state)
        )
    ).all()
    problem_wake_counts = (
        await session.execute(
            select(TaskWakeRegistration.delivery_state, func.count())
            .where(
                TaskWakeRegistration.project_id == project_id,
                TaskWakeRegistration.delivery_state.in_(_PROBLEM_WAKE_STATES),
                recent_problem_wake_filter,
            )
            .group_by(TaskWakeRegistration.delivery_state)
        )
    ).all()
    wake_count_map = {
        _enum_value(state): int(count or 0)
        for state, count in [*active_wake_counts, *problem_wake_counts]
    }
    preview_wakes = wake_rows + [
        row
        for row in wake_problem_rows
        if all(row.id != active.id for active in wake_rows)
    ]
    preview_wakes = preview_wakes[:_RUNTIME_WORK_PREVIEW_LIMIT]
    wake_task_ids = {row.task_id for row in preview_wakes}
    wake_tasks_by_id: Dict[uuid.UUID, Task] = {}
    if wake_task_ids:
        wake_tasks_by_id = {
            task.id: task
            for task in (
                await session.execute(select(Task).where(Task.id.in_(wake_task_ids)))
            )
            .scalars()
            .all()
        }

    return {
        "compute_jobs": {
            "window_minutes": int(window.total_seconds() // 60),
            "counts": {
                "active": int(compute_summary.get("active_jobs") or 0),
                "terminal_recent": int(compute_summary.get("terminal_jobs") or 0),
                "total": int(compute_summary.get("total_jobs") or 0),
                "queued": int(status_counts.get("QUEUED") or 0),
                "starting": int(status_counts.get("STARTING") or 0),
                "running": int(status_counts.get("RUNNING") or 0),
                "polling": int(status_counts.get("POLLING") or 0),
                "succeeded_recent": int(status_counts.get("SUCCEEDED") or 0),
                "failed_recent": int(status_counts.get("FAILED") or 0),
                "cancelled_recent": int(status_counts.get("CANCELLED") or 0),
                "timed_out_recent": int(status_counts.get("TIMED_OUT") or 0),
                "criterion_measured": int(
                    criterion_summary.get("criterion_measured") or 0
                ),
                "criterion_met": int(criterion_summary.get("criterion_met") or 0),
            },
            "spend": {
                "active_accrued_usd": float(active_spend or 0.0),
                "recent_terminal_usd": float(recent_terminal_spend or 0.0),
                "window_total_usd": float(compute_summary.get("total_spend_usd") or 0.0),
            },
            "items": [
                _compute_job_preview(job, tasks_by_id.get(job.task_id))
                for job in preview_jobs
            ],
        },
        "held_approvals": {
            "count": int(held_task_count or 0),
            "items": [_task_summary(task) for task in held_tasks],
        },
        "local_runner_holds": {
            "count": int(local_runner_hold_count or 0)
            + int(strategic_local_runner_hold_count or 0),
            "compute_count": int(local_runner_hold_count or 0),
            "strategic_count": int(strategic_local_runner_hold_count or 0),
            "items": [
                _task_summary(task, include_awaiting_since=True)
                for task in local_runner_hold_tasks
            ],
        },
        "parked_wakes": {
            "counts": {
                "active": int(wake_count_map.get("PENDING") or 0)
                + int(wake_count_map.get("FIRED") or 0),
                "pending": int(wake_count_map.get("PENDING") or 0),
                "fired": int(wake_count_map.get("FIRED") or 0),
                "problem": int(wake_count_map.get("EXPIRED") or 0)
                + int(wake_count_map.get("FAILED") or 0),
                "expired": int(wake_count_map.get("EXPIRED") or 0),
                "failed": int(wake_count_map.get("FAILED") or 0),
            },
            "items": [
                _wake_preview(row, wake_tasks_by_id.get(row.task_id))
                for row in preview_wakes
            ],
        },
    }

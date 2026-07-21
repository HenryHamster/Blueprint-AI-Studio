"""Admin task-management API router (``/api/admin``).

Hosts the operator-facing task endpoints: listing and inspecting tasks
(including serial-console output and PR file diffs), creating tasks, bulk
archive/action operations, and the review-control actions (request changes,
request a fresh reviewer, reset iteration cap, archive/unarchive, patch). All
routes require an authenticated user via the router-level ``get_current_user``
dependency. Task-mutation logic is delegated to ``admin_task_mutations`` and
shared access checks to ``admin_helpers``.
"""

from fastapi import APIRouter, Depends, HTTPException, Query, BackgroundTasks, Request
from pydantic import BaseModel, Field, field_validator, model_validator
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select
from typing import Dict, Any, List
import os
import uuid
import json
import logging
import asyncio
from datetime import datetime, timedelta, timezone
from sqlalchemy import func, update, case, delete, and_, or_
from sqlalchemy.orm.attributes import flag_modified
from sqlalchemy.orm import load_only

from app.database import get_session, resolve_async_engine, resolve_dialect_name
from app.inline_llm import (
    call_inline_llm_json,
    inline_llm_unavailable_reason,
    resolve_inline_llm_settings,
)
from app.shared_state import (
    derive_task_branch as _derive_task_branch,
    backfill_branch_columns,
    get_project_integration_branch,
    get_project_promotion_target as _get_project_promotion_target_for_task,
    get_project_task_branch_namespace,
    limiter,
    resolve_task_branch as _resolve_task_branch,
    runtime_config as _runtime_config,
)
from app.models import (
    Task,
    TaskStatus,
    TaskHistory,
    Project,
    ProjectMember,
    LocalRunner,
    ProjectMessage,
    Artifact,
    ArtifactType,
    StrategicCycle,
    TaskWakeRegistration,
    DeploymentState,
    DeploymentStatus,
    _utcnow,
    get_task_total_vm_cost_usd_or_none,
)
from app.auth import get_current_user
from app.task_locking import task_lock

from app.payload_schemas import (
    ComputeSuccessCriterion,
    TaskType,
    validate_task_payload,
)
from app.task_capabilities import (
    BACKEND_MANAGED_TASK_TYPES,
    USER_LAUNCHABLE_TASK_TYPES,
)
from app.compute_experiment_contract import (
    _validate_compute_launch_contract,
    resolve_compute_spend_approval,
    validate_compute_runner_target_enrollment,
)
from app.compute_jobs import clamp_spend_cap_usd, clamp_deadline_seconds
from app.vm_cost_rates import (
    get_compute_rate_per_second,
    resolve_compute_machine_type,
    validate_compute_request,
)
from app.resource_envelope import resolve_project_hardware_policy
from app.runner_project_binding import runner_allowed_for_project
from app.schemas import (
    TaskEvaluationSummary,
    TaskEvaluationSummaryItem,
    redact_task_payloads,
)
from app.project_message_projection import compact_text, message_preview_dict
from app.task_outcomes import build_task_evaluation_summary
from app.task_direction_refs import first_task_direction_id
from app.task_movement import build_task_movement_explanation
from app.compute import _capture_serial_for_vm, get_vm_zone, get_vm_status
from app.admin_helpers import (  # noqa: F401
    _ROLE_LEVEL,
    _user_project_ids,
    _member_project_ids,
    _resolve_repo_project_for_write,
    _get_user_project,
    _get_user_project_with_role,
    _get_user_task,
    _get_user_task_with_role,
)
from app.admin_task_mutations import (
    CANCEL_CASCADE_CHILD_STATUSES,
    CANCEL_WAITING_DEPENDENT_STATUSES,
    _handle_rollup_pr_approval,
    _handle_task_pr_approval,
    modify_task_apply,
)
from app.task_blocked import reset_task_for_fresh_attempt
from app.llm_cost_attribution import _task_llm_cost as _calc_task_llm_cost
from app.routers.admin_operator_triggers import (  # noqa: F401
    _record_failure_result,
    router as _operator_triggers_router,
)

# Per-loop max attempt budgets. lint/test mirror agent/src/constants.ts
# (MAX_LINT_FIX_ATTEMPTS=5, MAX_TEST_FIX_ATTEMPTS=3); "check" has no
# agent-side constant — 5 is a heuristic default used only for the
# high_iter > max_iter/2 trouble heuristic in this file.
_LOOP_MAX_ATTEMPTS: Dict[str, int] = {"lint": 5, "test": 3, "check": 5}
_FIX_LOOP_SUCCESS_REASONS = frozenset({"passed", "early_unchanged"})


def _plan_subtasks(
    plan_output: Dict[str, Any] | None,
) -> List[Dict[str, Any]] | None:
    """Return slim subtask list (title + dependency indices) for task card display."""
    if not isinstance(plan_output, dict):
        return None
    subtasks = plan_output.get("subtasks")
    if not isinstance(subtasks, list):
        return None
    return [
        {
            "title": s.get("title", ""),
            "depends_on_indices": s.get("depends_on_indices", []),
        }
        for s in subtasks
        if isinstance(s, dict)
    ]


def _fix_loop_summary(
    history: Dict[str, Any] | None,
) -> List[Dict[str, Any]] | None:
    """Return compact per-loop trouble entries from fix_loop_history, or None when healthy."""
    if not history:
        return None
    flagged = []
    for loop_name, attempts in history.items():
        if not isinstance(attempts, list) or not attempts:
            continue
        iterations = len(attempts)
        last = attempts[-1]
        last_exit_reason = last.get("exit_reason") if isinstance(last, dict) else None
        non_success = last_exit_reason not in _FIX_LOOP_SUCCESS_REASONS
        max_iter = _LOOP_MAX_ATTEMPTS.get(loop_name, 5)
        high_iter = iterations > max_iter / 2
        if non_success or high_iter:
            flagged.append(
                {
                    "loop": loop_name,
                    "iterations": iterations,
                    "last_exit_reason": last_exit_reason,
                    "non_success": non_success,
                }
            )
    return flagged if flagged else None


def _enum_value(value: Any) -> Any:
    return getattr(value, "value", value)


def _serialize_task_wake_registration(
    registration: TaskWakeRegistration | None,
) -> Dict[str, Any] | None:
    if registration is None:
        return None

    return {
        "wake_source_kind": registration.wake_source_kind,
        "wake_source_ref": registration.wake_source_ref,
        "delivery_state": _enum_value(registration.delivery_state),
        "deadline_at": registration.deadline_at.isoformat()
        if registration.deadline_at
        else None,
        "fired_at": registration.fired_at.isoformat()
        if registration.fired_at
        else None,
        "result_state": registration.result_state,
        "result_detail": registration.result_detail,
    }


def _build_task_evaluation_summary(
    task: Task,
    *,
    merge_test_result: Dict[str, Any] | None = None,
) -> Dict[str, Any] | None:
    """Wrap the registry-backed evaluation summary into the wire schema.

    The per-task-type summary logic lives in
    :func:`app.task_outcomes.build_task_evaluation_summary` — a registry of
    builders keyed by task-type — so registering a new evaluator-backed type is
    one map entry there rather than a new branch here. This shim validates the
    normalized ``{status, verdict, evidence, highlights}`` dict through the
    ``TaskEvaluationSummary`` pydantic model so the wire contract is unchanged.
    """
    summary = build_task_evaluation_summary(task, merge_test_result=merge_test_result)
    if summary is None:
        return None
    return TaskEvaluationSummary(
        status=summary["status"],
        verdict=summary["verdict"],
        evidence=[TaskEvaluationSummaryItem(**item) for item in summary["evidence"]],
        highlights=[
            TaskEvaluationSummaryItem(**item) for item in summary["highlights"]
        ],
    ).model_dump()


logger = logging.getLogger(__name__)


# NOTE: _terminal_cleanup_bg now lives in app.admin_task_mutations alongside
# the tested modify_task_apply helper that schedules it. The duplicate copy
# that used to live here was removed when modify_task was routed onto the
# helper (QP3).


router = APIRouter(
    prefix="/api/admin", tags=["admin"], dependencies=[Depends(get_current_user)]
)


TERMINAL_STATUSES = [TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.CANCELLED]

# Hard cap on /admin/tasks page size. A fully-populated task row can carry
# up to ~100KB of research_findings plus unbounded status_log, so 500 rows
# x large-tail payload stays safely under Cloud Run's 32 MB response limit.
ADMIN_TASKS_MAX_LIMIT = 500


def _fresh_attempt_boundary(task: Task) -> datetime | None:
    """Return the UTC-naive boundary after which current-attempt logs count."""

    raw = (task.context or {}).get("_fresh_attempt_started_at")
    if not isinstance(raw, str) or not raw.strip():
        return None
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone(timezone.utc).replace(tzinfo=None)
    return parsed


def _artifact_is_current_attempt(
    task: Task,
    artifact_created_at: datetime | None,
) -> bool:
    boundary = _fresh_attempt_boundary(task)
    if boundary is None:
        return True
    if artifact_created_at is None:
        return False
    created_at = artifact_created_at
    if created_at.tzinfo is not None:
        created_at = created_at.astimezone(timezone.utc).replace(tzinfo=None)
    return created_at >= boundary

# Columns loaded for the task list endpoint. Excludes the three JSONB blobs that
# are only accessed in the detail endpoint: attempt_history_db, api_call_summary,
# format_compliance_summary. Also excludes Task.payloads — the full payloads blob
# carries agent prompt text and research_findings (up to ~100KB per task) that list
# views never render. Instead, a slim batch query extracts only the display fields
# (type, pr_number, spend_cap_usd) from payloads[0] after the main task fetch.
_TASK_LIST_COLS = [
    Task.id, Task.project_id, Task.title, Task.description, Task.status,
    Task.vm_instance_id, Task.latest_status_message,
    Task.created_by, Task.requires_approval, Task.parent_task_id, Task.depends_on,
    Task.plan_output, Task.created_at, Task.started_at, Task.last_heartbeat,
    Task.last_metrics_json, Task.last_tool_use_at, Task.attempt_count,
    Task.blocker_kind, Task.blocked_reason, Task.blocked_at, Task.recheck_after,
    Task.parked_at, Task.park_reason, Task.context, Task.archived,
    Task.vm_runtime_cost_banked_db, Task.vm_provisioned_at, Task.vm_stopped_at,
    Task.post_commit_pending, Task.updated_at,
    Task.vm_machine_type, Task.vm_spot, Task.prompt_fingerprint,
]

# Fields extracted from payloads[0] for the list endpoint (no large blobs).
_TASK_LIST_PAYLOAD_DISPLAY_FIELDS = ("type", "pr_number", "spend_cap_usd")


@router.get("/tasks")
async def get_all_tasks(
    # le is generous for backward compat with clients that persisted larger
    # page sizes; the effective cap is ADMIN_TASKS_MAX_LIMIT below, which
    # bounds response size so it can't exceed Cloud Run's 32 MB body limit
    # as per-task payloads (research_findings, status_log) grow.
    limit: int = Query(50, ge=1, le=10000),
    offset: int = Query(0, ge=0),
    status: List[TaskStatus] = Query(default=[]),
    terminal_only: bool = Query(False),
    archived: bool | None = Query(None),
    search: str | None = Query(None, max_length=200),
    created_after: datetime | None = Query(None),
    direction_id: str | None = Query(None),
    task_type: str | None = Query(None),
    project_id: uuid.UUID | None = Query(None),
    spawned_by: str | None = Query(None, max_length=100),
    approval_needed: bool | None = Query(None),
    include_backend_managed: bool = Query(False, include_in_schema=False),
    session: AsyncSession = Depends(get_session),
    current_user: dict = Depends(get_current_user),
):
    from app.research_completion import resolve_research_findings_for_surface

    limit = min(limit, ADMIN_TASKS_MAX_LIMIT)

    # Build shared filter conditions
    filters = [Task.project_id.in_(_user_project_ids(current_user["user_id"]))]
    if not include_backend_managed:
        task_type_expr = Task.payloads[0]["type"].as_string()
        filters.append(
            or_(
                task_type_expr.is_(None),
                task_type_expr.not_in(sorted(BACKEND_MANAGED_TASK_TYPES)),
            )
        )
    if project_id is not None:
        filters.append(Task.project_id == project_id)
    if status:
        filters.append(Task.status.in_(status))
    elif terminal_only:
        filters.append(Task.status.in_(TERMINAL_STATUSES))
    if archived is not None:
        filters.append(Task.archived == archived)
    if search:
        escaped = search.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        search_pattern = f"%{escaped}%"
        filters.append(
            (Task.title.ilike(search_pattern, escape="\\"))
            | (Task.description.ilike(search_pattern, escape="\\"))
            | (Task.latest_status_message.ilike(search_pattern, escape="\\"))
        )
    if created_after:
        # Task.created_at is TIMESTAMPTZ (alembic 0061) but the
        # NaiveUTCDateTime ORM TypeDecorator returns naive UTC; this strip
        # is now redundant but kept as defense-in-depth in case a future
        # query param arrives tz-aware (e.g. `...Z` or `...+00:00`) before
        # passing through the TypeDecorator's read path.
        if created_after.tzinfo is not None:
            created_after = created_after.astimezone(timezone.utc).replace(tzinfo=None)
        filters.append(Task.created_at >= created_after)
    if task_type:
        filters.append(Task.payloads[0]["type"].as_string() == task_type)
    if spawned_by:
        filters.append(Task.context["spawned_by"].as_string() == spawned_by)
    if approval_needed is not None:
        # requires_approval is a stored boolean column (not application-computed),
        # so this narrows server-side. Mirrors the web console's approval-needed
        # triage filter. Omitted (None) -> no change to existing behavior.
        filters.append(Task.requires_approval == approval_needed)

    if direction_id:
        # Push direction matching to SQL to eliminate the Python cursor scan.
        # Dialect-aware: PostgreSQL uses indexed JSONB @> containment; SQLite
        # falls back to json_extract for the common single-level keys.
        if resolve_dialect_name(session) == "sqlite":
            _jx = func.json_extract
            direction_filter = or_(
                _jx(Task.context, "$.direction_id") == direction_id,
                _jx(Task.context, "$.current_direction_id") == direction_id,
                _jx(Task.payloads, "$[0].direction_id") == direction_id,
                _jx(Task.payloads, "$[0].current_direction_id") == direction_id,
                _jx(Task.payloads, "$[0].source_direction_id") == direction_id,
                _jx(Task.payloads, "$[0].target_direction_id") == direction_id,
                # directions array (first entry only — SQLite lacks array containment)
                _jx(Task.payloads, "$[0].directions[0].id") == direction_id,
                # plan_output keys
                _jx(Task.plan_output, "$.source_direction_id") == direction_id,
                _jx(Task.plan_output, "$.findings[0].direction_id") == direction_id,
            )
        else:
            from sqlalchemy import cast as _cast
            from sqlalchemy.dialects.postgresql import JSONB as _JSONB
            direction_filter = or_(
                # context keys — covered by GIN index ix_task_context_gin
                Task.context.op("@>")(_cast({"direction_id": direction_id}, _JSONB)),
                Task.context.op("@>")(_cast({"current_direction_id": direction_id}, _JSONB)),
                # payload top-level direction keys
                Task.payloads[0]["direction_id"].as_string() == direction_id,
                Task.payloads[0]["current_direction_id"].as_string() == direction_id,
                Task.payloads[0]["source_direction_id"].as_string() == direction_id,
                Task.payloads[0]["target_direction_id"].as_string() == direction_id,
                # payload directions-array containment
                Task.payloads.op("@>")(_cast([{"directions": [{"id": direction_id}]}], _JSONB)),
                # plan_output direction keys and nested findings
                Task.plan_output["source_direction_id"].as_string() == direction_id,
                Task.plan_output.op("@>")(_cast(
                    {"findings": [{"direction_id": direction_id}]}, _JSONB
                )),
            )
        filters.append(direction_filter)

    count_query = select(func.count()).select_from(Task)
    for f in filters:
        count_query = count_query.where(f)
    total = (await session.execute(count_query)).scalar() or 0

    query = (
        select(Task)
        .options(load_only(*_TASK_LIST_COLS, raiseload=True))
        .order_by(Task.created_at.desc())
        .offset(offset)
        .limit(limit)
    )
    for f in filters:
        query = query.where(f)

    result = await session.execute(query)
    tasks = result.scalars().all()

    # Join artifacts to include all artifacts per task
    task_ids = [t.id for t in tasks]

    # Fetch slim payload display fields (type, pr_number, spend_cap_usd) without
    # loading the full payloads blob (which carries large agent prompt text and
    # research_findings). Uses the same JSONB subscript notation as the direction
    # filter above — SQLAlchemy maps this to json_extract on SQLite automatically.
    slim_payload_map: Dict[str, Dict[str, Any]] = {}
    if task_ids:
        slim_stmt = select(
            Task.id,
            Task.payloads[0]["type"].as_string().label("payload_type"),
            Task.payloads[0]["pr_number"].as_integer().label("payload_pr_number"),
            Task.payloads[0]["spend_cap_usd"].as_float().label("payload_spend_cap_usd"),
        ).where(Task.id.in_(task_ids))
        for _row in (await session.execute(slim_stmt)).all():
            _slim: Dict[str, Any] = {}
            if _row.payload_type is not None:
                _slim["type"] = _row.payload_type
            if _row.payload_pr_number is not None:
                _slim["pr_number"] = _row.payload_pr_number
            if _row.payload_spend_cap_usd is not None:
                _slim["spend_cap_usd"] = _row.payload_spend_cap_usd
            slim_payload_map[str(_row.id)] = _slim
    artifacts_map: Dict[str, List[Dict[str, Any]]] = {}
    pr_map: Dict[str, str] = {}
    logs_map: Dict[str, str] = {}
    research_artifact_logs_map: Dict[str, str] = {}
    merge_test_map: Dict[str, Dict[str, Any]] = {}
    commit_map: Dict[str, Dict[str, Any]] = {}
    if task_ids:
        artifact_stmt = (
            select(Artifact)
            .options(
                load_only(
                    Artifact.id,
                    Artifact.task_id,
                    Artifact.artifact_type,
                    Artifact.pr_url,
                    Artifact.commit_hash,
                    Artifact.repo_url,
                    Artifact.storage_uri,
                    Artifact.iteration,
                    Artifact.created_at,
                    raiseload=True,
                )
            )
            .where(Artifact.task_id.in_(task_ids))
            .order_by(Artifact.created_at.asc())
            .limit(len(task_ids) * 20)
        )
        artifacts = (await session.execute(artifact_stmt)).scalars().all()
        for a in artifacts:
            tid = str(a.task_id)
            if tid not in artifacts_map:
                artifacts_map[tid] = []
            artifacts_map[tid].append(
                {
                    "artifact_type": a.artifact_type,
                    "pr_url": a.pr_url,
                    "commit_hash": a.commit_hash,
                    "repo_url": a.repo_url,
                    # Additive: storage-backed artifacts expose their gs:// URI so
                    # operator surfaces can resolve a signed download. Always present
                    # (null for code/log artifacts) — existing consumers ignore it.
                    "storage_uri": a.storage_uri,
                    "iteration": a.iteration,
                    "created_at": a.created_at.isoformat() if a.created_at else None,
                }
            )
            if a.artifact_type == ArtifactType.PULL_REQUEST and a.pr_url:
                pr_map[tid] = a.pr_url
            if a.artifact_type == ArtifactType.GIT_COMMIT and a.commit_hash:
                commit_map[tid] = {"commit_hash": a.commit_hash, "repo_url": a.repo_url}

        # Fetch logs columns separately to avoid loading large blobs in the main query.
        # A per-(task_id, artifact_type) ROW_NUMBER() window keeps only the most-recent
        # row of each type per task, so a few high-activity tasks with many ERROR_LOG /
        # MERGE_TEST_RESULT rows can't exhaust a global budget and starve other tasks.
        _logs_rn = (
            func.row_number()
            .over(
                partition_by=[Artifact.task_id, Artifact.artifact_type],
                order_by=Artifact.created_at.desc(),
            )
            .label("rn")
        )
        _ranked_logs = (
            select(
                Artifact.task_id,
                Artifact.artifact_type,
                Artifact.logs,
                Artifact.created_at,
                _logs_rn,
            )
            .where(
                Artifact.task_id.in_(task_ids),
                Artifact.artifact_type.in_(
                    [
                        ArtifactType.ERROR_LOG,
                        ArtifactType.RESEARCH_FINDINGS,
                        ArtifactType.MERGE_TEST_RESULT,
                    ]
                ),
            )
            .subquery()
        )
        logs_stmt = select(_ranked_logs).where(_ranked_logs.c.rn == 1)
        logs_artifacts = (await session.execute(logs_stmt)).all()
        _tasks_by_id = {str(task.id): task for task in tasks}
        for a in logs_artifacts:
            tid = str(a.task_id)
            if (
                a.artifact_type == ArtifactType.ERROR_LOG
                and a.logs
                and tid not in logs_map
                and _artifact_is_current_attempt(_tasks_by_id[tid], a.created_at)
            ):
                logs_map[tid] = a.logs[:2000]
            if a.artifact_type == ArtifactType.RESEARCH_FINDINGS and a.logs and tid not in research_artifact_logs_map:
                research_artifact_logs_map[tid] = a.logs[:100000]
            if a.artifact_type == ArtifactType.MERGE_TEST_RESULT and a.logs and tid not in merge_test_map:
                try:
                    parsed = await asyncio.to_thread(json.loads, a.logs[:100000])
                    if isinstance(parsed, dict):
                        merge_test_map[tid] = parsed
                except (json.JSONDecodeError, AttributeError):
                    pass

    # Fetch task history (last 10 entries per task). A per-task ROW_NUMBER()
    # window keeps each task's 10 most-recent rows, so a single high-activity
    # task can't exhaust a global budget and starve other tasks of history.
    history_map: Dict[str, List[Dict[str, Any]]] = {}
    if task_ids:
        row_number = (
            func.row_number()
            .over(
                partition_by=TaskHistory.task_id,
                order_by=TaskHistory.timestamp.desc(),
            )
            .label("rn")
        )
        ranked = (
            select(TaskHistory, row_number)
            .where(TaskHistory.task_id.in_(task_ids))
            .subquery()
        )
        history_stmt = (
            select(ranked).where(ranked.c.rn <= 10).order_by(ranked.c.timestamp.desc())
        )
        all_history = (await session.execute(history_stmt)).all()
        for h in all_history:
            tid = str(h.task_id)
            if tid not in history_map:
                history_map[tid] = []
            history_map[tid].append(
                {
                    "timestamp": h.timestamp.isoformat() if h.timestamp else None,
                    "action": h.action,
                    "details": h.details,
                }
            )

    # Build parent→children map from the fetched result set (no extra DB query)
    children_map: Dict[str, List[str]] = {}
    for t in tasks:
        if t.parent_task_id:
            parent_key = str(t.parent_task_id)
            if parent_key not in children_map:
                children_map[parent_key] = []
            children_map[parent_key].append(str(t.id))

    # Return tasks with artifact data attached
    from app.failure_classification import classify_failure

    result_list = []
    for t in tasks:
        _slim_payload = slim_payload_map.get(str(t.id), {})
        # Stash slim payload so task.first_payload returns it without accessing
        # the deferred Task.payloads column (which is not in _TASK_LIST_COLS).
        object.__setattr__(t, "_projected_first_payload", _slim_payload)
        task_type = _slim_payload.get("type") or "create"
        task_dict = {
            "id": str(t.id),
            "project_id": str(t.project_id),
            "title": t.title,
            "description": t.description,
            "status": t.status,
            "task_type": task_type,
            "vm_instance_id": t.vm_instance_id,
            "latest_status_message": t.latest_status_message,
            "created_by": t.created_by,
            "requires_approval": t.requires_approval,
            "parent_task_id": str(t.parent_task_id) if t.parent_task_id else None,
            "depends_on": t.depends_on or [],
            "plan_subtasks": _plan_subtasks(t.plan_output),
            "created_at": t.created_at.isoformat() if t.created_at else None,
            "started_at": t.started_at.isoformat() if t.started_at else None,
            "last_heartbeat": t.last_heartbeat.isoformat()
            if t.last_heartbeat
            else None,
            # Audit R3-W2 (H1): expose heartbeat-hot columns directly so the
            # admin-ui System Metrics panel reads from the column source-of-truth.
            "last_metrics_json": t.last_metrics_json,
            "last_tool_use_at": t.last_tool_use_at.isoformat()
            if t.last_tool_use_at
            else None,
            "attempt_count": t.attempt_count,
            # Durable BLOCKED-state metadata (alembic 0137). Nullable for any
            # non-BLOCKED or pre-migration task; the admin UI guards on presence.
            "blocker_kind": t.blocker_kind,
            "blocked_reason": t.blocked_reason,
            "blocked_at": t.blocked_at.isoformat() if t.blocked_at else None,
            "recheck_after": t.recheck_after.isoformat() if t.recheck_after else None,
            # Durable PARKED-state metadata (alembic 0199). Nullable for any
            # non-PARKED or pre-migration task; the admin UI guards on presence.
            "parked_at": t.parked_at.isoformat() if t.parked_at else None,
            "park_reason": t.park_reason,
            "payloads": [_slim_payload] if _slim_payload else [],
            "context": {
                k: v
                for k, v in (t.context or {}).items()
                if not k.startswith("max_plan_")
            },
            # Slim, read-only mirror of the no-CI gate's UNVERIFIED-merge reason
            # (review_loop["unverified_merge_reason"]). None for verified /
            # normal merges. See the detail endpoint for rationale.
            "unverified_merge_reason": (t.context or {})
            .get("review_loop", {})
            .get("unverified_merge_reason"),
            "archived": t.archived,
            "artifacts": artifacts_map.get(str(t.id), []),
            "pr_url": pr_map.get(str(t.id)),
            "error_logs": logs_map.get(str(t.id)),
            "research_findings": resolve_research_findings_for_surface(
                t.plan_output, research_artifact_logs_map.get(str(t.id))
            )
            if task_type == "research"
            else None,
            "evaluation_summary": _build_task_evaluation_summary(
                t, merge_test_result=merge_test_map.get(str(t.id))
            ),
            "commit_hash": commit_map.get(str(t.id), {}).get("commit_hash"),
            "repo_url": commit_map.get(str(t.id), {}).get("repo_url"),
            "history": history_map.get(str(t.id), []),
            "child_task_ids": children_map.get(str(t.id), []),
            "failure_class": classify_failure(t)
            if t.status in (TaskStatus.FAILED, TaskStatus.CANCELLED)
            and t.latest_status_message
            else None,
            "estimated_cost_usd": get_task_total_vm_cost_usd_or_none(t),
            "fix_loop_summary": None,
            "post_commit_pending": t.post_commit_pending,
        }
        result_list.append(task_dict)

    # Global counts scoped to the caller's member projects — single query via CASE/WHEN
    _counts_row = (
        await session.execute(
            select(
                func.count(
                    case(
                        (
                            Task.status.in_(
                                [TaskStatus.IN_PROGRESS, TaskStatus.PROVISIONING]
                            ),
                            1,
                        )
                    )
                ),
                func.count(case((Task.status == TaskStatus.EVALUATING, 1))),
                func.count(case((Task.status == TaskStatus.QUEUED, 1))),
            )
            .select_from(Task)
            .where(Task.project_id.in_(_user_project_ids(current_user["user_id"])))
        )
    ).one()
    active_count, evaluating_count, queued_count = _counts_row

    response: Dict[str, Any] = {
        "tasks": result_list,
        "total": total,
        "active_count": active_count,
        "evaluating_count": evaluating_count,
        "queued_count": queued_count,
    }

    if project_id is not None:
        _scoped_row = (
            await session.execute(
                select(
                    func.count(
                        case(
                            (
                                Task.status.in_(
                                    [TaskStatus.IN_PROGRESS, TaskStatus.PROVISIONING]
                                ),
                                1,
                            )
                        )
                    ),
                    func.count(case((Task.status == TaskStatus.EVALUATING, 1))),
                    func.count(case((Task.status == TaskStatus.QUEUED, 1))),
                )
                .select_from(Task)
                .where(
                    Task.project_id == project_id,
                    Task.project_id.in_(_user_project_ids(current_user["user_id"])),
                )
            )
        ).one()
        scoped_active, scoped_evaluating, scoped_queued = _scoped_row
        response["scoped_active_count"] = scoped_active
        response["scoped_evaluating_count"] = scoped_evaluating
        response["scoped_queued_count"] = scoped_queued

    return response


@router.get("/tasks/counts")
async def get_task_counts(
    project_id: uuid.UUID | None = Query(None),
    session: AsyncSession = Depends(get_session),
    current_user: dict = Depends(get_current_user),
):
    """Cheap aggregate counts endpoint for the header badge.

    Returns only active/evaluating/queued counts with no Task rows loaded.
    Used by non-pipeline tabs so the header stays current without the
    full-row poll overhead.
    """
    user_project_ids_expr = _user_project_ids(current_user["user_id"])
    global_row = (
        await session.execute(
            select(
                func.count(
                    case(
                        (
                            Task.status.in_(
                                [TaskStatus.IN_PROGRESS, TaskStatus.PROVISIONING]
                            ),
                            1,
                        )
                    )
                ),
                func.count(case((Task.status == TaskStatus.EVALUATING, 1))),
                func.count(case((Task.status == TaskStatus.QUEUED, 1))),
            )
            .select_from(Task)
            .where(Task.project_id.in_(user_project_ids_expr))
        )
    ).one()
    active_count, evaluating_count, queued_count = global_row

    response: Dict[str, Any] = {
        "active_count": active_count,
        "evaluating_count": evaluating_count,
        "queued_count": queued_count,
    }

    if project_id is not None:
        scoped_row = (
            await session.execute(
                select(
                    func.count(
                        case(
                            (
                                Task.status.in_(
                                    [TaskStatus.IN_PROGRESS, TaskStatus.PROVISIONING]
                                ),
                                1,
                            )
                        )
                    ),
                    func.count(case((Task.status == TaskStatus.EVALUATING, 1))),
                    func.count(case((Task.status == TaskStatus.QUEUED, 1))),
                )
                .select_from(Task)
                .where(
                    Task.project_id == project_id,
                    Task.project_id.in_(user_project_ids_expr),
                )
            )
        ).one()
        scoped_active, scoped_evaluating, scoped_queued = scoped_row
        response["scoped_active_count"] = scoped_active
        response["scoped_evaluating_count"] = scoped_evaluating
        response["scoped_queued_count"] = scoped_queued

    return response


class BulkArchiveRequest(BaseModel):
    task_ids: List[str] | None = None
    filter: str | None = None  # 'completed' or 'terminal'


@router.post("/tasks/bulk-archive")
async def bulk_archive_tasks(
    request: BulkArchiveRequest,
    session: AsyncSession = Depends(get_session),
    current_user: dict = Depends(get_current_user),
):
    # Archiving is a mutation: scope to projects where the caller is member+,
    # matching the sibling bulk-action handler. A viewer is read-only and must
    # not be able to bulk-archive another member's tasks.
    user_project_ids = _member_project_ids(current_user["user_id"])
    if request.task_ids is not None:

        def _safe_uuid(raw: str) -> uuid.UUID | None:
            try:
                return uuid.UUID(raw)
            except (ValueError, AttributeError):
                return None

        valid_uuids = [u for raw_id in request.task_ids if (u := _safe_uuid(raw_id))]
        if not valid_uuids:
            return {"archived_count": 0}
        # Use SKIP LOCKED to avoid contending with concurrent orchestrator transactions
        # holding row locks. Skipping locked tasks is safe — terminal tasks normally aren't
        # locked, and if one is, we don't want to block archiving the rest of the column.
        select_stmt = (
            select(Task.id)
            .where(
                Task.id.in_(valid_uuids),
                Task.status.in_(TERMINAL_STATUSES),
                Task.archived.is_(False),
                Task.project_id.in_(user_project_ids),
            )
            .with_for_update(skip_locked=True)
        )
    elif request.filter in ("completed", "terminal"):
        statuses = (
            [TaskStatus.COMPLETED]
            if request.filter == "completed"
            else TERMINAL_STATUSES
        )
        select_stmt = (
            select(Task.id)
            .where(
                Task.archived.is_(False),
                Task.status.in_(statuses),
                Task.project_id.in_(user_project_ids),
            )
            .limit(5000)
            .with_for_update(skip_locked=True)
        )
    else:
        raise HTTPException(
            status_code=400,
            detail="Must provide task_ids or a valid filter ('completed' or 'terminal')",
        )

    lockable_ids = (await session.execute(select_stmt)).scalars().all()
    if not lockable_ids:
        return {"archived_count": 0}
    # Wave F4 C4: bulk Core UPDATEs bypass mapper-level `version_id_col`. F1
    # dropped that enforcement but keeps `version` as a forensic counter —
    # bump it here so concurrent-write detection via version drift remains
    # available even on archive paths.
    await session.execute(
        update(Task)
        .where(Task.id.in_(lockable_ids))
        .values(
            archived=True,
            version=Task.version + 1,
        )
    )
    await session.commit()
    return {"archived_count": len(lockable_ids), "has_more": len(lockable_ids) == 5000}


class PurgeArchiveRequest(BaseModel):
    older_than_days: int | None = (
        None  # if set, only purge tasks older than this many days
    )


@router.post("/tasks/purge-archive")
async def purge_archive_tasks(
    request: PurgeArchiveRequest,
    session: AsyncSession = Depends(get_session),
    current_user: dict = Depends(get_current_user),
):
    """Permanently delete archived tasks for the caller's projects.

    Only tasks with ``archived=True`` are eligible — live/active tasks are
    never touched. An optional ``older_than_days`` filter restricts deletion to
    tasks created more than that many days ago, providing a safety window for
    recently archived items. Up to 5000 tasks are deleted per call; callers
    should repeat until ``deleted_count`` is zero if they need to drain a large
    archive.

    Scoped to projects where the caller is member+: this PERMANENTLY deletes
    rows (the most destructive task mutation there is), so it takes the same
    floor as the sibling bulk-action/bulk-archive handlers. A viewer is
    read-only and must never be able to irreversibly destroy another member's
    task history.
    """
    user_project_ids = _member_project_ids(current_user["user_id"])

    task_type_expr = Task.payloads[0]["type"].as_string()
    where_clauses = [
        Task.archived.is_(True),
        Task.project_id.in_(user_project_ids),
        or_(
            task_type_expr.is_(None),
            task_type_expr.not_in(sorted(BACKEND_MANAGED_TASK_TYPES)),
        ),
    ]
    if request.older_than_days is not None:
        if request.older_than_days < 1:
            raise HTTPException(
                status_code=400, detail="older_than_days must be at least 1"
            )
        cutoff = _utcnow() - timedelta(days=request.older_than_days)
        where_clauses.append(Task.created_at < cutoff)

    # Fetch up to 5000 IDs to delete in one batch. SKIP LOCKED avoids racing
    # the orchestrator on tasks that happen to be transitioning (rare but safe).
    select_stmt = (
        select(Task.id)
        .where(*where_clauses)
        .limit(5000)
        .with_for_update(skip_locked=True)
    )
    ids_to_delete = (await session.execute(select_stmt)).scalars().all()
    if not ids_to_delete:
        return {"deleted_count": 0, "has_more": False}

    # Detach any surviving tasks whose parent_task_id points into the delete
    # batch. The FK (task.parent_task_id -> task.id) was created with NO ACTION
    # so the DB-level check fires at statement end — a live child referencing a
    # to-be-deleted parent causes IntegrityError and rolls back the whole purge.
    # Nulling it out preserves the live child while allowing the parent to be
    # removed. We must NOT cascade-delete non-archived children.
    await session.execute(
        update(Task)
        .where(Task.parent_task_id.in_(ids_to_delete))
        .values(parent_task_id=None)
    )
    await session.execute(delete(Task).where(Task.id.in_(ids_to_delete)))
    await session.commit()
    return {"deleted_count": len(ids_to_delete), "has_more": len(ids_to_delete) == 5000}


class BulkActionRequest(BaseModel):
    action: str  # 'approve' or 'retry'
    task_ids: List[str]


async def _bulk_retry_one_task(
    task_id: uuid.UUID,
    request_session: AsyncSession,
    preloaded_has_children: bool | None = None,
) -> str | None:
    """Run the retry mutation for a single task in its own short-lived session.

    Phase C2 (H-C3): the bulk-action retry path used to share the request
    session across all tasks AND call ``_requeue_parent_task`` (which clones
    failed children and remaps depends_on) inside that shared transaction.
    Two problems compounded: the long-held lock window blocked concurrent
    heartbeats, and a per-task failure rolled back the whole batch.

    Splitting per-task into its own session means: (a) each row lock is
    bounded by one task's mutation window, (b) a single failure doesn't
    nuke siblings, and (c) ``_requeue_parent_task`` runs scoped to one
    parent rather than queueing on the bulk transaction.

    The scoped session is built from the engine ``request_session`` resolves
    to (via :func:`resolve_async_engine`) so it shares the same engine — in
    production that's the module-level engine, in tests the test engine
    (in-memory SQLite or test Postgres). It does NOT trust ``request_session
    .bind`` directly: a session created from ``async_sessionmaker`` exposes a
    None ``.bind`` in production (the engine resolves lazily at execute-time),
    and feeding that None into a new ``async_sessionmaker`` yields an unbound
    factory whose sessions fail to resolve an engine. Mirrors the
    agent_status.py post-commit pattern (and shares its resolution helper).

    Returns the old vm_instance_id when the caller should schedule a VM
    delete after commit, or ``None`` for parent-type tasks (which clone
    children rather than spinning a VM here).
    """
    from sqlalchemy.ext.asyncio import async_sessionmaker
    from app.routers.agent import _terminated_task_ids
    from app.task_lifecycle import _requeue_parent_task

    scoped_factory = async_sessionmaker(
        resolve_async_engine(request_session),
        class_=AsyncSession,
        expire_on_commit=False,
    )
    async with scoped_factory() as scoped_session:
        async with task_lock(
            scoped_session, task_id, skip_locked=False, timeout=10
        ) as task:
            if task is None:
                # Row missing or unreachable — the caller already validated
                # membership in the claim-IDs phase, so this is a genuine
                # "deleted between phases" race; report it.
                raise HTTPException(status_code=404, detail="Task not found")

            task.status = TaskStatus.QUEUED
            # If the task was FAILED/CANCELLED, the SQLAlchemy set event added
            # task.id to _terminated_task_ids, revoking any outstanding JWT.
            # Drop the cache entry so the next provisioned VM can claim.
            _terminated_task_ids.pop(str(task.id), None)
            task_type = _task_payload_dict(task).get("type", "")
            is_parent_type = task_type in ("research", "plan")
            if preloaded_has_children is not None:
                # Use pre-fetched boolean (batch query in bulk_action_tasks)
                # to avoid a per-task child query. _requeue_parent_task does
                # its own children query internally when called.
                has_children = preloaded_has_children
            elif is_parent_type:
                has_children = bool(
                    (
                        await scoped_session.execute(
                            select(Task.id)
                            .where(Task.parent_task_id == task.id)
                            .limit(1)
                        )
                    )
                    .scalars()
                    .first()
                )
            else:
                has_children = False
            old_vm: str | None = None
            if has_children:
                await _requeue_parent_task(task, scoped_session)
            else:
                old_vm = task.vm_instance_id
                reset_task_for_fresh_attempt(task)
                task.attempt_count = 0
                task.vm_instance_id = f"agent-vm-{uuid.uuid4().hex}"
            history = TaskHistory(
                task_id=task.id,
                action="MODIFIED",
                details={"changes": {"status": "QUEUED"}, "modified_by": "BULK_ACTION"},
            )
            scoped_session.add(task)
            scoped_session.add(history)
        await scoped_session.commit()
        return old_vm


@router.post("/tasks/bulk-action")
@limiter.limit("60/minute")
async def bulk_action_tasks(
    request: Request,
    body: BulkActionRequest,
    session: AsyncSession = Depends(get_session),
    current_user: dict = Depends(get_current_user),
):
    # Wave F4 H10 — Accepted operational risk:
    # The 2-phase split below (Phase 1 claims IDs, Phase 2 mutates per-task)
    # has no orphan sweep if Cloud Run dies mid-iteration. Tasks claimed in
    # Phase 1 but not yet mutated in Phase 2 stay in their pre-bulk state;
    # the operator can re-issue the bulk action manually. This is acceptable
    # for solo-dev because Cloud Run mid-iteration crashes are rare. See
    # docs/db-deferrals.md ("Bulk-action orphan sweep") for the trigger to
    # add an automated sweep (>1 mid-iteration crash/month observed).
    if body.action not in ("approve", "retry", "cancel"):
        raise HTTPException(
            status_code=400, detail="action must be 'approve', 'retry', or 'cancel'"
        )

    results = []

    # ── Phase 1: claim IDs ──────────────────────────────────────────────────
    # Validate UUIDs and resolve membership in a single short transaction on
    # the shared request session. The pre-fetch returns light Task rows we
    # use to surface "not found" before falling through to per-task work.
    def _parse_task_uuid(raw: str) -> uuid.UUID | None:
        try:
            return uuid.UUID(raw)
        except (ValueError, AttributeError):
            return None

    valid_map: dict[str, uuid.UUID] = {}  # raw_id -> parsed UUID
    for raw_id in body.task_ids:
        parsed = _parse_task_uuid(raw_id)
        if parsed is not None:
            valid_map[raw_id] = parsed
        else:
            results.append(
                {"task_id": raw_id, "status": "error", "error": "Invalid task ID"}
            )
    if valid_map:
        member_project_ids = select(ProjectMember.project_id).where(
            ProjectMember.user_id == current_user["user_id"],
            ProjectMember.role.in_(["member", "owner"]),
        )
        fetched = {
            task.id: task
            for task in (
                await session.execute(
                    select(Task).where(
                        Task.id.in_(valid_map.values()),
                        Task.project_id.in_(member_project_ids),
                    )
                )
            )
            .scalars()
            .all()
        }
    else:
        fetched = {}

    # ── Phase 1.5: batch-prefetch children for cancel cascade ───────────────
    # Pre-fetch all active children for cancel actions in a single query so
    # the per-task cancel loop does not issue one child query per parent task.
    # The dict maps parent_task_id → list[Task] and is built before any
    # commits, so the loaded objects are fresh (no session-expiry lazy loads).
    from collections import defaultdict as _defaultdict

    _cancel_children_by_parent: dict[uuid.UUID, list] = _defaultdict(list)
    if body.action == "cancel" and valid_map:
        _child_result = await session.execute(
            select(Task).where(
                Task.parent_task_id.in_(list(valid_map.values())),
                Task.status.in_(CANCEL_CASCADE_CHILD_STATUSES),
            )
        )
        for _child in _child_result.scalars().all():
            _cancel_children_by_parent[_child.parent_task_id].append(_child)

    # Pre-fetch which retry parent-type tasks have children (single query).
    # Passed into _bulk_retry_one_task to skip its per-task child query.
    _retry_parent_has_children: dict[uuid.UUID, bool] = {}
    if body.action == "retry":
        _parent_type_ids = [
            task_obj.id
            for task_obj in fetched.values()
            if _task_payload_dict(task_obj).get("type") in ("research", "plan")
        ]
        if _parent_type_ids:
            _child_parent_rows = (
                (
                    await session.execute(
                        select(Task.parent_task_id)
                        .where(Task.parent_task_id.in_(_parent_type_ids))
                        .distinct()
                    )
                )
                .scalars()
                .all()
            )
            _parents_with_children = set(_child_parent_rows)
            for _pid in _parent_type_ids:
                _retry_parent_has_children[_pid] = _pid in _parents_with_children

    # ── Phase 2: per-task processing ────────────────────────────────────────
    # Phase C2 (H-C3): retry path runs in its own scoped session per task so
    # _requeue_parent_task is not called inside the bulk transaction's lock
    # window. approve/cancel still use the shared session — they don't call
    # _requeue_parent_task and their per-task commits already release locks
    # immediately.
    # A per-item rollback (in the except handlers below) expires EVERY ORM object
    # in `fetched` / `_cancel_children_by_parent` regardless of expire_on_commit;
    # once that happens, subsequent iterations must re-fetch fresh or their reads
    # fire a sync lazy-load -> MissingGreenlet (caught, but it cascades every
    # remaining item to a spurious "error"). `_batch_dirty` flips on first rollback.
    # Populated on first rollback: batch-fetched replacements for the expired
    # ORM objects, avoiding per-iteration session.get (PERF finding M-P6).
    _batch_dirty = False
    _stale_task_map: dict[uuid.UUID, Task] = {}
    _stale_children_map: dict[uuid.UUID, list[Task]] = {}
    for raw_id in body.task_ids:
        if raw_id not in valid_map:
            continue  # already reported as invalid
        task = fetched.get(valid_map[raw_id])
        if not task:
            results.append(
                {"task_id": raw_id, "status": "error", "error": "Task not found"}
            )
            continue
        if _batch_dirty:
            # Batch-fetch all tasks in one query after rollback instead of
            # N individual session.get(populate_existing=True) calls.
            _remaining_ids = list(valid_map.values())
            _fresh_tasks = (
                await session.execute(
                    select(Task).where(Task.id.in_(_remaining_ids)),
                    execution_options={"populate_existing": True},
                )
            ).scalars().all()
            _stale_task_map = {t.id: t for t in _fresh_tasks}
            task = _stale_task_map.get(valid_map[raw_id])
            if task is None:
                results.append(
                    {"task_id": raw_id, "status": "error", "error": "Task not found"}
                )
                continue

        try:
            if body.action == "retry":
                old_vm = await _bulk_retry_one_task(
                    valid_map[raw_id],
                    session,
                    preloaded_has_children=_retry_parent_has_children.get(
                        valid_map[raw_id]
                    ),
                )

                # Delete old VM after commit, same as modify_task
                if old_vm:
                    from app.vm_lifecycle import _ensure_vm_deleted

                    await _ensure_vm_deleted(old_vm, reason="admin_requeue")

            elif body.action == "approve":
                was_blocked = task.requires_approval
                task.requires_approval = False
                task.spend_approval_cleared_at = _utcnow()

                if was_blocked:
                    logger.info(
                        f"[ADMIN] Task {task.id} bulk-approved (was blocked: {task.latest_status_message or 'unknown reason'})"
                    )

                if was_blocked and task.status in (
                    TaskStatus.COMPLETED,
                    TaskStatus.EVALUATING,
                ):
                    ctx = task.context or {}
                    rollup_pr_number = ctx.get("rollup_pr_number")
                    approval_changes: Dict[str, Any] = {}

                    # Round 3 M10 — operator-response latency contract:
                    # The rollup-PR approval branch performs external GitHub
                    # API calls (merge_pull_request, create_pr_comment, and
                    # later _cleanup_branches) BEFORE per-task commit. The
                    # bulk endpoint's HTTP response therefore includes the
                    # full GitHub round-trip latency for each approved task
                    # (typically 2-5s, up to 30s+ during rate-limit). For N
                    # tasks the response time scales O(N) — operators should
                    # not assume sub-second latency on bulk approve. This is
                    # intentional: holding the merge external until commit
                    # would risk an inconsistency where the DB shows
                    # COMPLETED but the GitHub PR is still open.
                    if rollup_pr_number:
                        await _handle_rollup_pr_approval(
                            task,
                            ctx,
                            rollup_pr_number,
                            session,
                            approval_changes,
                        )
                    else:
                        await _handle_task_pr_approval(
                            task,
                            ctx,
                            {},
                            session,
                            approval_changes,
                        )

                history = TaskHistory(
                    task_id=task.id,
                    action="MODIFIED",
                    details={
                        "changes": {"requires_approval": False},
                        "modified_by": "BULK_ACTION",
                    },
                )
                session.add(task)
                session.add(history)
                await session.commit()

            elif body.action == "cancel":
                terminal_statuses = {
                    TaskStatus.COMPLETED,
                    TaskStatus.FAILED,
                    TaskStatus.CANCELLED,
                }
                if task.status in terminal_statuses:
                    results.append(
                        {
                            "task_id": raw_id,
                            "status": "error",
                            "error": f"Task already in terminal state {task.status}",
                        }
                    )
                    continue

                # Use pre-fetched children (batch query before loop, no N+1).
                # If a prior item rolled back, the prefetch is expired — re-fetch
                # fresh children in one batch query instead of per-parent queries.
                if _batch_dirty:
                    _parent_ids = list(valid_map.values())
                    _fresh_children = (
                        await session.execute(
                            select(Task).where(
                                Task.parent_task_id.in_(_parent_ids),
                                Task.status.in_(CANCEL_CASCADE_CHILD_STATUSES),
                            )
                        )
                    ).scalars().all()
                    _stale_children_map = _defaultdict(list)
                    for _child in _fresh_children:
                        _stale_children_map[_child.parent_task_id].append(_child)
                    active_children = _stale_children_map.get(task.id, [])
                else:
                    active_children = _cancel_children_by_parent.get(task.id, [])

                # Save VM and account info before any commit so we can access
                # them after session.commit() expires ORM object attributes.
                parent_vm_id = task.vm_instance_id
                parent_acct_id = (task.context or {}).get("max_plan_account_id")
                child_vm_info = [
                    (
                        child.vm_instance_id,
                        (child.context or {}).get("max_plan_account_id"),
                        str(child.project_id) if child.project_id else None,
                    )
                    for child in active_children
                ]

                task.status = TaskStatus.CANCELLED
                if task.vm_provisioned_at:
                    task.vm_stopped_at = _utcnow()
                task.requires_approval = False

                # Close any PR the cancelled task opened. Without this, the
                # PR sits open and blocks the parent's rollup gate. See
                # c56fa118 / PR #5983 incident (2026-05-26).
                from app.task_lifecycle import close_orphan_pr_if_any

                await close_orphan_pr_if_any(task, session, reason="admin_bulk_cancel")

                # Suppress low-priority tasks with finding_hash for 180 days
                if task.context and task.context.get("finding_hash"):
                    task_priority = task.priority if task.priority is not None else 50
                    if task_priority == 0:
                        suppress_until = (_utcnow() + timedelta(days=180)).isoformat()
                        task.context = {
                            **task.context,
                            "suppress_until": suppress_until,
                        }
                        flag_modified(task, "context")

                history = TaskHistory(
                    task_id=task.id,
                    action="MODIFIED",
                    details={
                        "changes": {"status": "CANCELLED"},
                        "modified_by": "BULK_ACTION",
                    },
                )
                session.add(task)
                session.add(history)

                # Cascade cancellation to active children while objects are
                # still fresh (before commit expires them).
                for child in active_children:
                    child.status = TaskStatus.CANCELLED
                    if child.vm_provisioned_at:
                        child.vm_stopped_at = _utcnow()
                    child.requires_approval = False
                    session.add(child)
                # Close orphan PRs on each cascaded child too. Done before
                # commit so the GitHub call is grouped with the cancel.
                for child in active_children:
                    await close_orphan_pr_if_any(
                        child, session, reason="admin_bulk_cancel_cascade"
                    )

                await session.commit()

                # Delete VMs using values saved before commit (avoids lazy
                # loads on expired ORM objects).
                from app.vm_lifecycle import _ensure_vm_deleted
                from app.max_plan_pool import release_max_plan_account

                if parent_vm_id:
                    await _ensure_vm_deleted(parent_vm_id, reason="admin_bulk_cancel")
                    if parent_acct_id:
                        await release_max_plan_account(
                            parent_acct_id,
                            project_id=str(task.project_id) if task.project_id else None,
                        )

                for child_vm_id, child_acct_id, child_project_id in child_vm_info:
                    if child_vm_id:
                        await _ensure_vm_deleted(
                            child_vm_id, reason="parent_bulk_cancelled"
                        )
                    if child_acct_id:
                        await release_max_plan_account(
                            child_acct_id,
                            project_id=child_project_id,
                        )

                if active_children:
                    logger.info(
                        f"[ADMIN] Bulk cancel cascaded to {len(active_children)} children of task {task.id}"
                    )

            results.append({"task_id": raw_id, "status": "ok"})
        except HTTPException as e:
            await session.rollback()
            _batch_dirty = True  # rollback expired the prefetched batch
            results.append({"task_id": raw_id, "status": "error", "error": e.detail})
        except Exception as e:
            await session.rollback()
            _batch_dirty = True  # rollback expired the prefetched batch
            logger.error(
                f"[ADMIN] Bulk action {body.action} failed for task {raw_id}: {e}",
                exc_info=True,
            )
            results.append(
                {"task_id": raw_id, "status": "error", "error": str(e)[:200]}
            )

    success_count = sum(1 for r in results if r["status"] == "ok")
    error_count = len(results) - success_count
    return {
        "results": results,
        "success_count": success_count,
        "error_count": error_count,
    }


def _task_llm_cost_fields(task: Task) -> dict:
    llm_cost, any_unpriced = _calc_task_llm_cost(task)
    return {
        "task_llm_cost_usd": round(llm_cost, 6) if llm_cost else None,
        "task_llm_cost_any_unpriced": any_unpriced,
    }


def _status_value(status: TaskStatus | str) -> str:
    return status.value if isinstance(status, TaskStatus) else str(status)


def _status_values(statuses: tuple[TaskStatus, ...]) -> list[str]:
    return [_status_value(status) for status in statuses]


def _task_payload_dict(task: Task | None) -> dict[str, Any]:
    if task is None:
        return {}
    payload = task.first_payload or {}
    return payload if isinstance(payload, dict) else {}


def _task_direction_id(task: Task) -> str | None:
    return first_task_direction_id(task)


def _find_task_direction_ref(task: Task, project: Project | None) -> dict[str, Any] | None:
    direction_id = _task_direction_id(task)
    if not direction_id:
        return None
    directions = project.directions if project and isinstance(project.directions, list) else []
    row = next(
        (
            direction
            for direction in directions
            if isinstance(direction, dict) and str(direction.get("id") or "") == direction_id
        ),
        None,
    )
    if not row:
        return {
            "id": direction_id,
            "text": None,
            "priority": None,
            "status": None,
        }
    return {
        "id": direction_id,
        "text": compact_text(row.get("text"), max_chars=240),
        "priority": row.get("priority"),
        "status": row.get("status") or "active",
    }


def _task_artifact_sideboard_summary(
    artifacts: list[Artifact],
    *,
    primary_pr_url: str | None,
    primary_commit: str | None,
) -> dict[str, Any]:
    by_type: dict[str, int] = {}
    commit = primary_commit
    for artifact in artifacts:
        kind = str(_enum_value(artifact.artifact_type))
        by_type[kind] = by_type.get(kind, 0) + 1
        if commit is None and artifact.commit_hash:
            commit = artifact.commit_hash
    return {
        "total": len(artifacts),
        "by_type": by_type,
        "primary_pr_url": primary_pr_url,
        "primary_commit": commit,
    }


def _strategic_cycle_sideboard_ref(
    task: Task,
    cycle: StrategicCycle | None,
) -> dict[str, Any] | None:
    if cycle is None:
        return None
    phase = None
    if cycle.phase1_task_id == task.id:
        phase = "design_doc_update"
    elif cycle.phase2_task_id == task.id:
        phase = "strategic_review"
    return {
        "id": cycle.id,
        "phase": phase,
        "outcome": cycle.outcome,
    }


def _task_sideboard_next_action(task: Task) -> dict[str, Any]:
    status = _status_value(task.status)
    target = {"surface": "task", "id": str(task.id), "task_id": str(task.id)}
    if status == "FAILED":
        return {
            "label": "Inspect failure",
            "reason": compact_text(task.latest_status_message, max_chars=220),
            "target": {**target, "section": "logs"},
        }
    if status == "BLOCKED":
        return {
            "label": "Resolve blocker",
            "reason": compact_text(task.blocked_reason, max_chars=220),
            "target": target,
        }
    if status == "PARKED":
        return {
            "label": "Review parked wake",
            "reason": compact_text(task.park_reason, max_chars=220),
            "target": target,
        }
    if task.requires_approval:
        return {
            "label": "Review approval",
            "reason": compact_text(task.latest_status_message, max_chars=220),
            "target": target,
        }
    if status == "QUEUED" and task.depends_on:
        return {
            "label": "Wait for dependencies",
            "reason": f"{len(task.depends_on)} upstream task{'s' if len(task.depends_on) != 1 else ''}",
            "target": {"surface": "pipeline", "id": str(task.id), "task_id": str(task.id)},
        }
    if status in _status_values(
        (TaskStatus.PROVISIONING, TaskStatus.IN_PROGRESS, TaskStatus.EVALUATING)
    ):
        return {
            "label": "Monitor progress",
            "reason": compact_text(task.latest_status_message, max_chars=220),
            "target": target,
        }
    return {
        "label": "Open task detail",
        "reason": compact_text(task.latest_status_message, max_chars=220),
        "target": target,
    }


def _task_runner_sideboard_context(task: Task) -> dict[str, Any]:
    ctx = task.context if isinstance(task.context, dict) else {}
    return {
        "vm_instance_id": compact_text(task.vm_instance_id, max_chars=120),
        "runner_target_id": compact_text(ctx.get("runner_target_id"), max_chars=120),
        # Operator/agent runner pin (launch-contract runner_target) and, when a
        # strict pin is holding the task, the machine-readable hold reason
        # {runner_id, strict, reason_code, detail} — so the UI can show WHY
        # nothing is dispatching instead of a silent QUEUED card.
        "preferred_runner_target_id": compact_text(
            ctx.get("preferred_runner_target_id"), max_chars=120
        ),
        "runner_pin_hold": (
            ctx.get("runner_pin_hold")
            if isinstance(ctx.get("runner_pin_hold"), dict)
            else None
        ),
        "machine_type": compact_text(task.vm_machine_type, max_chars=80),
        "spot": task.vm_spot,
        "warm_start": task.vm_warm_start,
        "provider": compact_text(task.provider, max_chars=80),
        "account_id": compact_text(task.account_id, max_chars=120),
    }


def _task_sideboard_context(
    task: Task,
    *,
    project: Project | None,
    artifacts: list[Artifact],
    child_count: int,
    related_messages: list[ProjectMessage],
    cycle: StrategicCycle | None,
    primary_pr_url: str | None,
    primary_commit: str | None,
) -> dict[str, Any]:
    repo = None
    if project and project.github_owner and project.github_repo:
        repo = f"{project.github_owner}/{project.github_repo}"
    problem_model = project.problem_model if project and isinstance(project.problem_model, dict) else {}
    intent = problem_model.get("intent") if isinstance(problem_model.get("intent"), dict) else {}
    purpose = compact_text(
        intent.get("current_focus") or intent.get("purpose"),
        max_chars=220,
    )
    dependency_count = len(task.depends_on or [])
    movement_explanation = build_task_movement_explanation(task)
    llm_cost_fields = _task_llm_cost_fields(task)
    task_type = _task_payload_dict(task).get("type", "create")
    pipeline = {
        "parent_task_id": str(task.parent_task_id) if task.parent_task_id else None,
        "child_count": child_count,
        "dependency_count": dependency_count,
        "movement_explanation": compact_text(movement_explanation, max_chars=220),
        "blocked_reason": compact_text(
            task.blocked_reason or task.park_reason,
            max_chars=220,
        ),
    }
    links: list[dict[str, Any]] = [
        {
            "id": "pipeline",
            "label": "Pipeline",
            "target": {
                "surface": "pipeline",
                "id": str(task.id),
                "task_id": str(task.id),
            },
        }
    ]
    if pipeline["parent_task_id"]:
        links.append(
            {
                "id": "parent_task",
                "label": "Parent",
                "target": {
                    "surface": "task",
                    "id": pipeline["parent_task_id"],
                    "task_id": pipeline["parent_task_id"],
                },
            }
        )
    direction = _find_task_direction_ref(task, project)
    if direction:
        links.append(
            {
                "id": "direction",
                "label": "Direction",
                "target": {
                    "surface": "project",
                    "section": "directions",
                    "id": direction["id"],
                    "direction_id": direction["id"],
                },
            }
        )
    if primary_pr_url:
        links.append(
            {
                "id": "pull_request",
                "label": "PR",
                "target": {
                    "surface": "external",
                    "id": primary_pr_url,
                    "url": primary_pr_url,
                },
            }
        )

    return {
        "task": {
            "id": str(task.id),
            "title": compact_text(task.title, max_chars=160),
            "status": _status_value(task.status),
            "task_type": compact_text(task_type, max_chars=80),
            "latest_status_message": compact_text(
                task.latest_status_message,
                max_chars=220,
            ),
            "blocker": {
                "kind": compact_text(task.blocker_kind, max_chars=80),
                "reason": compact_text(
                    task.blocked_reason or task.park_reason,
                    max_chars=220,
                ),
                "blocked_at": task.blocked_at.isoformat() if task.blocked_at else None,
                "recheck_after": task.recheck_after.isoformat()
                if task.recheck_after
                else None,
                "parked_at": task.parked_at.isoformat() if task.parked_at else None,
            },
            "runner": _task_runner_sideboard_context(task),
            "cost": {
                "estimated_vm_usd": get_task_total_vm_cost_usd_or_none(task),
                "llm_usd": llm_cost_fields["task_llm_cost_usd"],
                "llm_any_unpriced": llm_cost_fields["task_llm_cost_any_unpriced"],
            },
            "next_action": _task_sideboard_next_action(task),
        },
        "project": {
            "id": str(project.id) if project else str(task.project_id),
            "name": project.name if project else None,
            "repo": repo,
        },
        "purpose": purpose,
        "strategic": {
            "direction": direction,
            "cycle": _strategic_cycle_sideboard_ref(task, cycle),
        },
        "pipeline": pipeline,
        "artifacts": _task_artifact_sideboard_summary(
            artifacts,
            primary_pr_url=primary_pr_url,
            primary_commit=primary_commit,
        ),
        "messages": {
            "total_recent": len(related_messages),
            "items": [message_preview_dict(message) for message in related_messages],
        },
        "links": links,
    }


def _task_has_cancel_pr_context_reference(task: Task) -> bool:
    ctx = task.context or {}
    if not isinstance(ctx, dict):
        return False
    if ctx.get("_pr_url") or ctx.get("pr_url"):
        return True

    review_loop = ctx.get("review_loop") or {}
    if isinstance(review_loop, dict) and review_loop.get("pr_number") is not None:
        return True

    return bool(ctx.get("rollup_pr_url") or ctx.get("rollup_pr_number") is not None)


@router.get("/tasks/{task_id}/cancel-impact")
async def get_task_cancel_impact(
    task_id: str,
    session: AsyncSession = Depends(get_session),
    current_user: dict = Depends(get_current_user),
):
    try:
        tid = uuid.UUID(task_id)
    except ValueError:
        raise HTTPException(status_code=404, detail="Task not found")

    task = await _get_user_task_with_role(
        tid, current_user["user_id"], session, "member"
    )

    active_children = (
        (
            await session.execute(
                select(Task).where(
                    Task.parent_task_id == task.id,
                    Task.status.in_(CANCEL_CASCADE_CHILD_STATUSES),
                )
            )
        )
        .scalars()
        .all()
    )

    dependent_candidates = (
        await session.execute(
            select(Task.id, Task.depends_on).where(
                Task.project_id == task.project_id,
                Task.id != task.id,
                Task.status.in_(CANCEL_WAITING_DEPENDENT_STATUSES),
            )
        )
    ).all()
    waiting_dependent_count = sum(
        1
        for _dependent_id, depends_on in dependent_candidates
        if str(task.id) in {str(dep_id) for dep_id in (depends_on or [])}
    )

    impact_task_ids = [task.id, *(child.id for child in active_children)]
    pr_artifact_task_ids: set[str] = set()
    if impact_task_ids:
        pr_artifact_task_ids = {
            str(row)
            for row in (
                await session.execute(
                    select(Artifact.task_id).where(
                        Artifact.task_id.in_(impact_task_ids),
                        Artifact.artifact_type == ArtifactType.PULL_REQUEST,
                    )
                )
            )
            .scalars()
            .all()
        }

    pr_context_reference_count = sum(
        1
        for candidate in [task, *active_children]
        if _task_has_cancel_pr_context_reference(candidate)
    )

    return {
        "task_id": str(task.id),
        "status": _status_value(task.status),
        "active_child_cascade_count": len(active_children),
        "active_child_cascade_statuses": _status_values(CANCEL_CASCADE_CHILD_STATUSES),
        "waiting_dependent_count": waiting_dependent_count,
        "waiting_dependent_statuses": _status_values(
            CANCEL_WAITING_DEPENDENT_STATUSES
        ),
        "target_vm_instance_id": task.vm_instance_id,
        "target_has_vm_id": bool(task.vm_instance_id),
        "target_vm_provisioned": task.vm_provisioned_at is not None,
        "active_child_vm_id_count": sum(
            1 for child in active_children if child.vm_instance_id
        ),
        "active_child_vm_provisioned_count": sum(
            1 for child in active_children if child.vm_provisioned_at is not None
        ),
        "orphan_pr_cleanup_context_reference_count": pr_context_reference_count,
        "pull_request_artifact_task_count": len(pr_artifact_task_ids),
    }


@router.get("/tasks/{task_id}")
async def get_task(
    task_id: str,
    session: AsyncSession = Depends(get_session),
    current_user: dict = Depends(get_current_user),
):
    from app.research_completion import resolve_research_findings_for_surface

    try:
        tid = uuid.UUID(task_id)
    except ValueError:
        raise HTTPException(status_code=404, detail="Task not found")

    task = await _get_user_task_with_role(
        tid, current_user["user_id"], session, "member"
    )

    # Fetch all artifacts for this task (all types, not just last per type)
    artifact_stmt = (
        select(Artifact)
        .where(Artifact.task_id == tid)
        .order_by(Artifact.created_at.asc())
    )
    artifacts = (await session.execute(artifact_stmt)).scalars().all()
    artifacts_list = []
    pr_url = None
    error_logs = None
    research_findings = None
    merge_test_result = None
    commit_hash = None
    repo_url = None
    for a in artifacts:
        artifacts_list.append(
            {
                "artifact_type": a.artifact_type,
                "pr_url": a.pr_url,
                "commit_hash": a.commit_hash,
                "repo_url": a.repo_url,
                # Additive: see the task-list serializer above — exposes the
                # gs:// reference for signed-download resolution, null otherwise.
                "storage_uri": a.storage_uri,
                "iteration": a.iteration,
                "created_at": a.created_at.isoformat() if a.created_at else None,
            }
        )
        if a.artifact_type == ArtifactType.PULL_REQUEST and a.pr_url:
            pr_url = a.pr_url
        if (
            a.artifact_type == ArtifactType.ERROR_LOG
            and a.logs
            and _artifact_is_current_attempt(task, a.created_at)
        ):
            error_logs = a.logs
        if a.artifact_type == ArtifactType.RESEARCH_FINDINGS and a.logs:
            research_findings = a.logs[:100000]  # raw logs; resolved below
        if a.artifact_type == ArtifactType.MERGE_TEST_RESULT and a.logs:
            try:
                parsed = await asyncio.to_thread(json.loads, a.logs[:100000])
                if isinstance(parsed, dict):
                    merge_test_result = parsed
            except (json.JSONDecodeError, AttributeError):
                pass
        if a.artifact_type == ArtifactType.GIT_COMMIT and a.commit_hash:
            commit_hash = a.commit_hash
            repo_url = a.repo_url

    # Fetch task history capped at 200 most recent entries
    history_stmt = (
        select(TaskHistory)
        .where(TaskHistory.task_id == tid)
        .order_by(TaskHistory.timestamp.desc())
        .limit(200)
    )
    all_history = (await session.execute(history_stmt)).scalars().all()
    history = [
        {
            "timestamp": h.timestamp.isoformat() if h.timestamp else None,
            "action": h.action,
            "details": h.details,
        }
        for h in all_history
    ]

    # Fetch child task IDs (capped at 1000 to bound memory for high-fan-out parents)
    _CHILD_ID_LIMIT = 1000
    child_ids_result = await session.execute(
        select(Task.id).where(Task.parent_task_id == tid).limit(_CHILD_ID_LIMIT + 1)
    )
    _raw_child_ids = child_ids_result.scalars().all()
    child_task_ids_truncated = len(_raw_child_ids) > _CHILD_ID_LIMIT
    child_task_ids = [str(row) for row in _raw_child_ids[:_CHILD_ID_LIMIT]]
    child_count = int(
        (
            await session.execute(
                select(func.count(Task.id)).where(Task.parent_task_id == tid)
            )
        ).scalar_one()
        or 0
    )

    wake_registration_result = await session.execute(
        select(TaskWakeRegistration)
        .where(TaskWakeRegistration.task_id == tid)
        .order_by(TaskWakeRegistration.requested_at.desc())
        .limit(1)
    )
    wake_registration = wake_registration_result.scalars().first()

    project = await session.get(Project, task.project_id)
    cycle = (
        await session.execute(
            select(StrategicCycle)
            .where(
                StrategicCycle.project_id == task.project_id,
                (StrategicCycle.phase1_task_id == tid)
                | (StrategicCycle.phase2_task_id == tid),
            )
            .order_by(StrategicCycle.updated_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    if cycle is None and task.strategic_cycle_id:
        cycle = (
            await session.execute(
                select(StrategicCycle)
                .where(
                    StrategicCycle.id == task.strategic_cycle_id,
                    StrategicCycle.project_id == task.project_id,
                )
                .limit(1)
            )
        ).scalar_one_or_none()

    task_id_str = str(tid)
    operator_requested_reviewer_task_ids = select(Task.id).where(
        Task.project_id == task.project_id,
        Task.context["operator_requested_review_for"].as_string() == task_id_str,
    )
    related_messages = (
        await session.execute(
            select(ProjectMessage)
            .where(
                ProjectMessage.project_id == task.project_id,
                or_(
                    ProjectMessage.from_task_id == tid,
                    ProjectMessage.from_task_id.in_(operator_requested_reviewer_task_ids),
                    ProjectMessage.context["task_id"].as_string() == task_id_str,
                    ProjectMessage.context["source_task_id"].as_string() == task_id_str,
                    ProjectMessage.context["operator_requested_review_for"].as_string()
                    == task_id_str,
                    and_(
                        ProjectMessage.context["_convergence_link"]["kind"].as_string()
                        == "task",
                        ProjectMessage.context["_convergence_link"]["id"].as_string()
                        == task_id_str,
                    ),
                ),
            )
            .order_by(ProjectMessage.created_at.desc())
            .limit(5)
        )
    ).scalars().all()
    sideboard_context = _task_sideboard_context(
        task,
        project=project,
        artifacts=artifacts,
        child_count=child_count,
        related_messages=related_messages,
        cycle=cycle,
        primary_pr_url=pr_url,
        primary_commit=commit_hash,
    )

    task_type = _task_payload_dict(task).get("type", "create")
    movement_explanation = build_task_movement_explanation(task)
    return {
        "id": str(task.id),
        "project_id": str(task.project_id),
        "title": task.title,
        "description": task.description,
        "status": task.status,
        "task_type": task_type,
        "vm_instance_id": task.vm_instance_id,
        "latest_status_message": task.latest_status_message,
        "status_log": task.status_log or [],
        "created_by": task.created_by,
        "requires_approval": task.requires_approval,
        "parent_task_id": str(task.parent_task_id) if task.parent_task_id else None,
        "depends_on": task.depends_on or [],
        "plan_output": task.plan_output,
        "created_at": task.created_at.isoformat() if task.created_at else None,
        "started_at": task.started_at.isoformat() if task.started_at else None,
        "last_heartbeat": task.last_heartbeat.isoformat()
        if task.last_heartbeat
        else None,
        # Audit R3-W2 (H1): expose the heartbeat-hot columns directly so the
        # admin-ui System Metrics panel reads from the column source-of-truth.
        # Round 2 F retired the ctx.last_metrics JSONB mirror; this endpoint
        # was the missing piece that left the frontend silently stale.
        "last_metrics_json": task.last_metrics_json,
        "last_tool_use_at": task.last_tool_use_at.isoformat()
        if task.last_tool_use_at
        else None,
        "attempt_count": task.attempt_count,
        # Durable BLOCKED-state metadata (alembic 0137). Nullable for any
        # non-BLOCKED or pre-migration task; the admin UI guards on presence.
        "blocker_kind": task.blocker_kind,
        "blocked_reason": task.blocked_reason,
        "movement_explanation": movement_explanation,
        "blocked_at": task.blocked_at.isoformat() if task.blocked_at else None,
        "recheck_after": task.recheck_after.isoformat() if task.recheck_after else None,
        # Durable PARKED-state metadata (alembic 0199). Nullable for any
        # non-PARKED or pre-migration task; the admin UI guards on presence.
        "parked_at": task.parked_at.isoformat() if task.parked_at else None,
        "park_reason": task.park_reason,
        "wake_registration": _serialize_task_wake_registration(wake_registration),
        "payloads": redact_task_payloads(task.payloads),
        "context": {
            k: v
            for k, v in (task.context or {}).items()
            if not k.startswith("max_plan_")
        },
        # Slim, read-only mirror of the no-CI gate's UNVERIFIED-merge reason
        # (review_loop["unverified_merge_reason"]). None for verified / normal
        # merges. Lets the Task Detail drawer surface why a merge bypassed
        # verification without digging into raw nested context shape.
        "unverified_merge_reason": (task.context or {})
        .get("review_loop", {})
        .get("unverified_merge_reason"),
        "archived": task.archived,
        "artifacts": artifacts_list,
        "pr_url": pr_url,
        "error_logs": error_logs,
        "commit_hash": commit_hash,
        "repo_url": repo_url,
        "research_findings": resolve_research_findings_for_surface(
            task.plan_output, research_findings
        )
        if task_type == "research"
        else None,
        "evaluation_summary": _build_task_evaluation_summary(
            task, merge_test_result=merge_test_result
        ),
        "history": history,
        "child_task_ids": child_task_ids,
        "child_task_ids_truncated": child_task_ids_truncated,
        "estimated_cost_usd": get_task_total_vm_cost_usd_or_none(task),
        "vm_machine_type": task.vm_machine_type,
        "vm_spot": task.vm_spot,
        "vm_cold_start_ms": task.vm_cold_start_ms,
        "vm_warm_start": task.vm_warm_start,
        "api_call_summary": task.api_call_summary,
        **_task_llm_cost_fields(task),
        "post_commit_pending": task.post_commit_pending,
        "sideboard_context": sideboard_context,
    }


@router.get("/tasks/{task_id}/serial-console")
async def get_task_serial_console(
    task_id: str,
    session: AsyncSession = Depends(get_session),
    current_user: dict = Depends(get_current_user),
):
    """Fetch the live GCE serial console for a task's VM.

    Returns the current serial console output for a task that has an active VM,
    or cached serial log from the last reap if the VM is no longer available.
    Returns 404 if the task has no vm_instance_id or doesn't exist.

    Response shape:
    {
        "vm_instance_id": string,
        "zone": string | null,
        "vm_status": string | null,
        "serial_log": string | null,
        "captured_at": ISO-8601 string,
        "source": "live" | "cached"
    }
    """
    try:
        tid = uuid.UUID(task_id)
    except ValueError:
        raise HTTPException(status_code=404, detail="Task not found")

    task = await _get_user_task_with_role(
        tid, current_user["user_id"], session, "member"
    )

    # 404 if task has no VM
    if not task.vm_instance_id:
        raise HTTPException(status_code=404, detail="Task has no vm_instance_id")

    vm_id = task.vm_instance_id
    # _vm_zones is process-local; on multi-replica deploys this replica may
    # not have provisioned the VM. Fall back to the zone persisted in
    # task.context (mirrors the reaper.py provision_zone resolution pattern).
    zone = get_vm_zone(vm_id)
    if zone == "unknown":
        ctx_zone = (task.context or {}).get("provision_zone")
        if ctx_zone:
            zone = ctx_zone

    # Try to fetch live serial console
    live_serial = await asyncio.to_thread(_capture_serial_for_vm, vm_id, zone)

    # Get VM status
    vm_status = None
    if zone and zone != "unknown":
        vm_status = await asyncio.to_thread(get_vm_status, vm_id, zone)

    # Determine source and fall back to cached if needed
    serial_log = live_serial
    source = "live"
    if serial_log is None:
        # Fall back to cached serial log from reaper
        serial_log = (task.context or {}).get("last_serial_log")
        if serial_log is not None:
            source = "cached"

    return {
        "vm_instance_id": vm_id,
        "zone": zone if zone != "unknown" else None,
        "vm_status": vm_status,
        "serial_log": serial_log,
        "captured_at": _utcnow().isoformat(),
        "source": source,
    }


@router.get("/tasks/{task_id}/pr-files")
async def get_task_pr_files(
    task_id: str,
    include_patch: bool = Query(False),
    session: AsyncSession = Depends(get_session),
    current_user: dict = Depends(get_current_user),
):
    """Return changed-file summary for a task's PR.

    Fetches the PR's file list from GitHub on demand (not cached). Returns
    {files, pr_number} on success, or {files: null, reason: "no_pr"} when the
    task has no PR artifact. Set include_patch=true to include the unified diff
    text for each file (subject to GitHub's 5 MB / 3000-line patch cap).
    """
    try:
        tid = uuid.UUID(task_id)
    except ValueError:
        raise HTTPException(status_code=404, detail="Task not found")

    # Require at least member role; non-members get 404, viewers get 403.
    # include_patch exposes raw diff via the installation token — restrict to owners.
    if include_patch:
        task = await _get_user_task_with_role(
            tid, current_user["user_id"], session, min_role="owner"
        )
    else:
        task = await _get_user_task_with_role(
            tid, current_user["user_id"], session, min_role="member"
        )

    # Find the PR artifact
    artifact_result = await session.execute(
        select(Artifact).where(
            Artifact.task_id == task.id,
            Artifact.artifact_type == ArtifactType.PULL_REQUEST,
        )
    )
    artifact = artifact_result.scalars().first()

    if not artifact or not artifact.pr_url:
        return {"files": None, "pr_number": None, "reason": "no_pr"}

    pr_number = int(artifact.pr_url.rstrip("/").split("/")[-1])

    from app.routers.agent import get_github_token_for_task
    from app.github import get_pr_files

    try:
        gh_token, owner, repo = await get_github_token_for_task(task.id, session)
    except HTTPException:
        raise
    except Exception as exc:
        logger.exception(
            f"[ADMIN] Failed to get GitHub token for task {task_id}: {exc}"
        )
        raise HTTPException(status_code=502, detail="Failed to obtain GitHub token")

    files = await get_pr_files(
        gh_token,
        owner,
        repo,
        pr_number,
        project_id=str(task.project_id),
        include_patch=include_patch,
    )
    return {"files": files, "pr_number": pr_number}


class CreateTaskPayload(BaseModel):
    github_repo: str = Field(..., pattern=r"^[a-zA-Z0-9_.-]+/[a-zA-Z0-9_.-]+$")
    title: str = Field(..., min_length=1, max_length=200)
    prompt: str = Field(..., min_length=1, max_length=50000)
    base_branch: str | None = Field(
        default=None, max_length=255, pattern=r"^[a-zA-Z0-9_./-]*[a-zA-Z0-9_.-]$"
    )
    task_type: TaskType = Field(default=TaskType.create)
    pr_number: int | None = None
    branch: str | None = Field(
        default=None, max_length=255, pattern=r"^[a-zA-Z0-9_./-]*[a-zA-Z0-9_.-]$"
    )
    model: str | None = Field(default=None, min_length=1)
    effort: str | None = Field(default=None, pattern=r"^(low|medium|high|max)$")
    # Optional client-supplied idempotency key for deduping retried requests.
    # When provided, the endpoint returns the existing task if one with the same
    # key already exists in an active state, instead of creating a duplicate.
    idempotency_key: str | None = Field(
        default=None, max_length=255, pattern=r"^[a-zA-Z0-9_.-]+$"
    )
    # Hotfix path (Phase 5 / automain-LTS): set hotfix=True together with
    # base_branch="main" to mark a plan task as an expedited fix off main.
    # The agent handler branches hotfix/* off main and the orchestrator
    # back-merges main → automain after the hotfix lands.
    hotfix: bool = False
    # Optional originating ProjectMessage (e.g. a BUG_REPORT) this task is being
    # spawned from. Additive and backward compatible — omitted by existing
    # callers. When provided for an ``investigate`` task, the message's citations
    # are RE-VALIDATED against current HEAD and any "cited code is absent" signal
    # is injected into the spawned task's context + prompt so the investigating
    # agent discounts a stale report. Ignored for all other task types.
    source_message_id: uuid.UUID | None = None

    @field_validator("base_branch", "branch")
    @classmethod
    def validate_branch_name(cls, v: str | None) -> str | None:
        if v is None:
            return v
        for part in v.split("/"):
            if part in (".git", ".lock"):
                raise ValueError(f"Branch name contains reserved component: {part}")
            if part.startswith("."):
                raise ValueError(f"Branch component cannot start with a dot: {part}")
            if ".." in part:
                raise ValueError(f"Branch component cannot contain '..': {part}")
        return v

    @model_validator(mode="after")
    def validate_pr_metadata_required_for_pr_tasks(self) -> "CreateTaskPayload":
        if (
            self.task_type
            in (
                TaskType.review,
                TaskType.panel_review,
                TaskType.fix,
            )
            and self.pr_number is None
        ):
            raise ValueError(
                f"pr_number is required for task_type '{self.task_type.value}'"
            )
        if (
            self.task_type
            in (
                TaskType.review,
                TaskType.panel_review,
                TaskType.fix,
            )
            and not self.branch
        ):
            raise ValueError(
                f"branch is required for task_type '{self.task_type.value}'"
            )
        return self


class ComputeRunnerTargetPayload(BaseModel):
    """Operator pin of a compute_experiment to a specific enrolled runner.

    ``strict=False`` (default) is a soft preference: dispatch prefers this
    runner when it is live and eligible, falling through to normal selection
    otherwise. ``strict=True`` dispatches ONLY to this runner: whenever it is
    not currently eligible the task is held QUEUED with an explicit hold reason
    naming the runner and why (``task.context['runner_pin_hold']``). The runner
    must exist and be enrolled in the task's project (422 otherwise).
    """

    runner_id: str = Field(..., min_length=1, max_length=64)
    strict: bool = False

    @field_validator("runner_id")
    @classmethod
    def validate_runner_id(cls, v: str) -> str:
        try:
            return str(uuid.UUID(v.strip()))
        except ValueError:
            raise ValueError(f"runner_target.runner_id {v!r} is not a valid UUID")


class ComputeExperimentTaskPayload(BaseModel):
    github_repo: str = Field(..., pattern=r"^[a-zA-Z0-9_.-]+/[a-zA-Z0-9_.-]+$")
    title: str = Field(..., min_length=1, max_length=200)
    prompt: str = Field(..., min_length=1, max_length=50000)
    idea: str | None = Field(default=None, min_length=1, max_length=10000)
    draft_notes: str | None = Field(default=None, min_length=1, max_length=10000)
    drafted_from_refinement: bool | None = None
    base_branch: str | None = Field(
        default=None, max_length=255, pattern=r"^[a-zA-Z0-9_./-]*[a-zA-Z0-9_.-]$"
    )
    command: str | None = None
    arms: list[dict[str, str]] | None = None
    declared_metric: str | None = None
    spend_cap_usd: float
    deadline_seconds: int
    accelerator: str | None = None
    machine_type: str | None = None
    cwd: str | None = None
    env: dict[str, str] | None = None
    experiment: str | None = None
    benchmark: str | None = None
    hypothesis: str | None = None
    metric: str | None = None
    success_criterion: ComputeSuccessCriterion | None = None
    require_live_release: bool = False
    require_durable_artifact: bool = False
    provisioning_model: str | None = None
    # Operator HARD runner pin (distinct from the internal soft affinity hint):
    # a LocalRunner UUID OR a runner hostname (the operator thinks in host names
    # like ``dgx-1``). Resolved at launch against this project's registered,
    # non-deleted runners and stamped into task.context['forced_runner_target_id'];
    # dispatch then runs the job ONLY on that runner, holding QUEUED (never falling
    # back to another runner or to cloud) whenever it is offline / ineligible.
    runner_target_id: str | None = Field(default=None, max_length=255)
    # Container-aware runner fields (parity with the research/plan authoring path):
    # required for CUDA/NGC stacks (e.g. nvcr.io images) or the command runs bare.
    # The registry password is only ever persisted for runner-subprocess jobs.
    container_image: str | None = None
    container_registry_auth: dict[str, str] | None = None
    setup_step: str | None = None
    # No-progress watchdog window (peer of the runtime deadline). When set and
    # positive, a stalled job that writes no progress to BLUEPRINT_PROGRESS_FILE
    # within this many seconds is torn down early with reason `no_progress_timeout`.
    no_progress_timeout_seconds: int | None = None
    # Cross-job checkpoint resume: rehydrate a prior job's durable artifact bundle
    # into this job's workspace before the command runs. Must be a valid UUID string
    # referencing a completed compute job with a durable artifact.
    input_artifact_job_id: str | None = None
    # Optional pin to a specific enrolled runner (soft preference by default;
    # strict=true dispatches only to that runner and holds with an explicit
    # reason while it is ineligible). Validated against the project's enrolled
    # runners — a cross-project or unknown runner id is a 422.
    runner_target: ComputeRunnerTargetPayload | None = None
    idempotency_key: str | None = Field(
        default=None, max_length=255, pattern=r"^[a-zA-Z0-9_.-]+$"
    )
    # When False, suppresses the reason-and-ship follow-on at completion.
    # Operator-authored tasks (created_by="USER") default to suppressed; pass
    # spawn_followup=True to opt in. Engine-authored tasks default to spawning.
    spawn_followup: bool | None = None

    @model_validator(mode="after")
    def validate_command_or_arms(self) -> "ComputeExperimentTaskPayload":
        has_command = bool(self.command and self.command.strip())
        has_arms = bool(self.arms)
        if not has_command and not has_arms:
            raise ValueError("Either 'command' or 'arms' (with 'declared_metric') must be provided")
        if has_arms and not (self.declared_metric and self.declared_metric.strip()):
            raise ValueError("'declared_metric' is required when 'arms' is specified")
        return self

    @field_validator("provisioning_model")
    @classmethod
    def validate_provisioning_model(cls, v: str | None) -> str | None:
        if v is None:
            return v
        normalized = v.upper()
        if normalized not in {"STANDARD", "SPOT"}:
            raise ValueError("provisioning_model must be STANDARD or SPOT")
        return normalized

    @field_validator("input_artifact_job_id")
    @classmethod
    def validate_input_artifact_job_id(cls, v: str | None) -> str | None:
        # Enforce the comment's contract: must be a valid UUID string. Parse and
        # re-emit the canonical form so a malformed value is rejected at the
        # Pydantic layer (422) rather than passed through to the runner.
        if v is None:
            return v
        try:
            return str(uuid.UUID(v))
        except (ValueError, AttributeError, TypeError) as exc:
            raise ValueError("input_artifact_job_id must be a valid UUID string") from exc

    @field_validator("base_branch")
    @classmethod
    def validate_branch_name(cls, v: str | None) -> str | None:
        if v is None:
            return v
        for part in v.split("/"):
            if part in (".git", ".lock"):
                raise ValueError(f"Branch name contains reserved component: {part}")
            if part.startswith("."):
                raise ValueError(f"Branch component cannot start with a dot: {part}")
            if ".." in part:
                raise ValueError(f"Branch component cannot contain '..': {part}")
        return v


@router.post("/tasks")
@limiter.limit("60/minute")
async def create_task_manually(
    request: Request,
    payload: CreateTaskPayload,
    session: AsyncSession = Depends(get_session),
    current_user: dict = Depends(get_current_user),
):
    """
    Create a Claude Code task. Auto-creates a project for the repo if one doesn't exist.

    Hotfix path: submit task_type="plan", base_branch="main", hotfix=true to
    launch an expedited fix off main (Phase 5 / automain-LTS).  The hotfix
    flag is stored in the task payload and picked up by the agent handler,
    which branches hotfix/* off main; the orchestrator back-merges
    main → automain after the hotfix lands.
    """
    # Guard: hotfix is only meaningful for `plan` tasks. The Phase 5 back-merge
    # trigger (task_lifecycle.py) only fires for a hotfix ROLLUP merge of a
    # `plan` task → main. A create/fix hotfix has no plan and thus no rollup,
    # so its PR would merge straight to main with NO back-merge — silently
    # diverging `automain` from `main` and breaking the next promotion.
    if payload.hotfix and payload.task_type != TaskType.plan:
        raise HTTPException(
            status_code=400,
            detail=(
                "hotfix=true is only supported for task_type='plan' "
                f"(got '{payload.task_type.value}'); a non-plan hotfix would "
                "merge to main without the automain back-merge."
            ),
        )

    # Guard: the manual operator creation path may only emit task types the
    # capability registry declares ``user_launchable``. A non-launchable type
    # (e.g. ``compute_experiment``) needs a launch contract — for
    # compute_experiment, a non-empty ``command`` plus positive
    # ``spend_cap_usd`` and ``deadline_seconds`` — that this endpoint's
    # ``CreateTaskPayload`` does not carry, so accepting it would enqueue a
    # task that only fails later on the agent (``compute_launch_failed``) and at
    # the backend launch endpoint. Reject cleanly at the boundary. Gating off
    # ``USER_LAUNCHABLE_TASK_TYPES`` (the registry-derived single source of
    # truth) means a newly registered launchable/non-launchable type is honored
    # here automatically with no edit to this call site.
    if payload.task_type.value not in USER_LAUNCHABLE_TASK_TYPES:
        raise HTTPException(
            status_code=400,
            detail=(
                f"task_type '{payload.task_type.value}' is not operator-launchable "
                "and cannot be created through this endpoint. It requires a launch "
                "contract the manual creation path does not supply; launch it "
                "through its dedicated path instead."
            ),
        )

    # Parse owner/repo
    github_owner, github_repo = payload.github_repo.split("/", 1)

    # Find the project for this repo the caller may WRITE to (member+), else
    # create one. Viewers are documented read-only, so creating work on a
    # viewer-only project is a 403 — and must NOT fall through into the
    # auto-create branch below, which would hand the viewer a shadow project
    # they own. See _resolve_repo_project_for_write for why the member+ lookup
    # has to come first (duplicate projects per repo are legitimate).
    project = await _resolve_repo_project_for_write(
        session, current_user["user_id"], github_owner, github_repo
    )

    if project is None:
        project = Project(
            name=payload.github_repo,
            user_id=current_user.get("user_id", "admin"),
            github_owner=github_owner,
            github_repo=github_repo,
        )
        session.add(project)
        await session.flush()
        # [RESIDENT-REMOVAL] removed with resident subsystem
#         from app.strategic_resident_checkpointing import (
#             initialize_strategic_resident,
#         )

        await initialize_strategic_resident(  # noqa: F821
            session, project.id, locked_project=project
        )
        await session.commit()
        await session.refresh(project)
        # Auto-add owner membership
        session.add(
            ProjectMember(
                project_id=project.id, user_id=current_user["user_id"], role="owner"
            )
        )
        await session.commit()

    await backfill_branch_columns(session, project_id=project.id)

    # Auto-generate VM instance ID so orchestrator worker picks up the task
    vm_name = f"agent-vm-{uuid.uuid4().hex}"

    # Resolve base_branch: explicit client value takes precedence; omitted → project promotion_target
    resolved_base_branch = (
        payload.base_branch or _get_project_promotion_target_for_task(project)
    )

    # Build payload based on task type
    task_payload = {
        "type": payload.task_type,
        "prompt": payload.prompt,
        "base_branch": resolved_base_branch,
    }
    if payload.model is not None:
        task_payload["model"] = payload.model
    if payload.effort is not None:
        task_payload["effort"] = payload.effort
    if payload.hotfix:
        task_payload["hotfix"] = True
    # PR-based read-only/write review tasks need pr_number and branch
    if (
        payload.task_type
        in (
            TaskType.review,
            TaskType.panel_review,
            TaskType.fix,
        )
        and payload.pr_number is not None
        and payload.branch is not None
    ):
        task_payload["pr_number"] = payload.pr_number
        task_payload["branch"] = payload.branch
        if payload.task_type in (TaskType.review, TaskType.panel_review):
            task_payload["original_prompt"] = payload.prompt
    # Research tasks auto-include project directions. For ad-hoc manual
    # spawns we ship lightweight direction summaries (id/text/priority/
    # status/system + truncated research_context) — full
    # research_context + research_history can be tens of KB per direction
    # and easily blow past validate_task_payload's 190 KB cap on projects
    # with many fat directions. The agent can fetch full direction state
    # via ``./fetch direction <id>`` when it needs it.
    if payload.task_type == "research":
        active_directions = [
            d for d in (project.directions or []) if d.get("status") == "active"
        ]
        trimmed_directions = []
        for d in active_directions:
            if not isinstance(d, dict):
                continue
            ctx = d.get("research_context") or ""
            entry = {
                "id": d.get("id"),
                "text": d.get("text"),
                "priority": d.get("priority"),
                "status": d.get("status"),
            }
            if d.get("system"):
                entry["system"] = d.get("system")
            if isinstance(ctx, str) and ctx:
                entry["research_context"] = ctx[:2000]
            trimmed_directions.append(entry)
        task_payload["directions"] = trimmed_directions

    # Reproduce-on-HEAD citation gate for investigation tasks spawned from a
    # bug-report message. The decision-critical moment: a full agent run is
    # about to chase a defect. Re-validate the originating message's citations
    # against CURRENT HEAD (which may have moved since ingestion). Persist any
    # actionable provenance signal into the spawned task's context (mirroring
    # how research_completion injects finding["_provenance_warnings"]), and
    # only append the loud prompt note when the cited code is unambiguously
    # absent so the investigating agent discounts a stale report without
    # treating a partially grounded one as resolved. Best-effort: a missing
    # message or no extractable refs degrades silently (no injection).
    citation_task_context: dict | None = None
    if (
        payload.source_message_id is not None
        and payload.task_type == TaskType.investigate
    ):
        from app.source_ref_validation import (
            resolve_branch_verification_context,
            summarize_bug_report_citations,
            should_persist_message_citation_warning,
        )

        src_msg = await session.get(ProjectMessage, payload.source_message_id)
        if src_msg is not None and src_msg.project_id == project.id:
            try:
                _ctx = await resolve_branch_verification_context(session, project)
                signal = await summarize_bug_report_citations(
                    src_msg.content,
                    structured_context=src_msg.context,
                    **_ctx,
                )
            except Exception:  # noqa: BLE001 — grounding must never block task create
                logger.debug(
                    "[ADMIN] source-message citation re-validation failed",
                    exc_info=True,
                )
                signal = None
            if signal:
                _citation_absent = signal.get("citation_state") == "absent"
                # Seed a baseline reproduce-on-HEAD verdict from the citation-
                # grounding signal (no new validation pass): cited code absent
                # on HEAD definitionally does not reproduce there, so seed
                # `already_resolved`; otherwise the defect-truth is not yet
                # established, so seed `indeterminate`. Non-underscore key so the
                # review/fix respawn carry-forward preserves it and the agent
                # overwrites it with a determined verdict on terminal status.
                # See schemas.ReproduceVerdict.
                citation_task_context = {
                    "reproduce_verdict": {
                        "status": (
                            "already_resolved" if _citation_absent else "indeterminate"
                        ),
                        "reason": (
                            "seeded from bug-report citation grounding: cited code "
                            "absent on HEAD"
                            if _citation_absent
                            else "seeded baseline: reproduce-on-HEAD not yet determined"
                        ),
                    }
                }
                # Persist the provenance signal whenever it carries actionable
                # detail (absent verdict, or a non-absent verdict that still has
                # broken/ungrounded refs). The shared helper centralizes that
                # policy so present/indeterminate noise is dropped consistently.
                # The investigating agent renders it into its prompt via
                # `formatInvestigateCitationProvenance` (agent/src/prompt.ts) —
                # the single prompt-assembly point, so BOTH the fully-absent
                # discount note and the partially-grounded "verify these specific
                # refs, defect still actionable" note reach the agent regardless
                # of which spawn path created the task (operator or strategist).
                # No prompt-string mutation here: doing it agent-side avoids
                # double-rendering on the operator path and covers context-only
                # spawn paths that never touch this code.
                if should_persist_message_citation_warning(signal):
                    citation_task_context["_provenance_warnings"] = signal

    try:
        task_payload = validate_task_payload(task_payload)
    except ValueError as exc:
        logger.warning(
            "Validation error in task payload: %s: %s", type(exc).__name__, exc
        )
        raise HTTPException(status_code=422, detail="Invalid request")

    # Guard: cap active tasks per project to prevent queue explosion
    active_count = (
        await session.execute(
            select(func.count(Task.id)).where(
                Task.project_id == project.id,
                Task.status.in_(
                    [
                        TaskStatus.QUEUED,
                        TaskStatus.PROVISIONING,
                        TaskStatus.IN_PROGRESS,
                        TaskStatus.EVALUATING,
                    ]
                ),
            )
        )
    ).scalar() or 0
    if active_count >= 200:
        raise HTTPException(
            status_code=429,
            detail=f"Project has {active_count} active tasks (limit: 200)",
        )

    # Idempotency check: if client supplied an idempotency_key, check for
    # existing active task with that key and return it instead of creating a duplicate.
    # The stored key is namespaced with the project id so a client key that collides
    # with an active task in ANOTHER project cannot match here (the
    # ux_task_active_idempotency_key unique index is global) — that would otherwise
    # leak the foreign task's fields and silently skip creating the caller's task.
    # Mirrors review_pipeline._pipeline_task_idempotency_key's project-scoping convention.
    scoped_idempotency_key = (
        f"admin_launcher:{project.id}:{payload.idempotency_key}"
        if payload.idempotency_key
        else None
    )
    if scoped_idempotency_key:
        existing = (
            (
                await session.execute(
                    select(Task).where(
                        Task.idempotency_key == scoped_idempotency_key,
                        Task.status.in_(
                            [
                                TaskStatus.QUEUED,
                                TaskStatus.PROVISIONING,
                                TaskStatus.IN_PROGRESS,
                                TaskStatus.EVALUATING,
                                TaskStatus.BLOCKED,
                                TaskStatus.PARKED,
                            ]
                        ),
                    )
                )
            )
            .scalars()
            .first()
        )
        if existing:
            return {
                "id": str(existing.id),
                "project_id": str(existing.project_id),
                "title": existing.title,
                "status": existing.status,
                "vm_instance_id": existing.vm_instance_id,
                "is_duplicate": True,
            }

    new_task = Task(
        project_id=project.id,
        title=payload.title,
        description=payload.prompt,
        payloads=[task_payload],
        vm_instance_id=vm_name,
        status=TaskStatus.QUEUED,
        created_by="USER",
        launched_by_user_id=current_user["user_id"],
        requires_approval=False,
        idempotency_key=scoped_idempotency_key,
        **({"context": citation_task_context} if citation_task_context else {}),
    )
    session.add(new_task)
    try:
        await session.commit()
    except Exception as e:
        # Handle unique constraint race: concurrent insert with same idempotency_key.
        # Roll back and fetch the existing task that won the race.
        await session.rollback()
        if scoped_idempotency_key and "ux_task_active_idempotency_key" in str(e):
            existing = (
                (
                    await session.execute(
                        select(Task).where(
                            Task.idempotency_key == scoped_idempotency_key,
                            Task.status.in_(
                                [
                                    TaskStatus.QUEUED,
                                    TaskStatus.PROVISIONING,
                                    TaskStatus.IN_PROGRESS,
                                    TaskStatus.EVALUATING,
                                    TaskStatus.BLOCKED,
                                    TaskStatus.PARKED,
                                ]
                            ),
                        )
                    )
                )
                .scalars()
                .first()
            )
            if existing:
                return {
                    "id": str(existing.id),
                    "project_id": str(existing.project_id),
                    "title": existing.title,
                    "status": existing.status,
                    "vm_instance_id": existing.vm_instance_id,
                    "is_duplicate": True,
                }
        raise
    await session.refresh(new_task)

    # Log history
    history = TaskHistory(
        task_id=new_task.id,
        action="CREATED",
        details={
            "created_by": "USER",
            "method": "admin_launcher",
            "github_repo": payload.github_repo,
            "base_branch": resolved_base_branch,
        },
    )
    session.add(history)
    await session.commit()

    return {
        "id": str(new_task.id),
        "project_id": str(project.id),
        "title": new_task.title,
        "status": new_task.status,
        "vm_instance_id": new_task.vm_instance_id,
    }


class ComputeExperimentDraftRequest(BaseModel):
    idea: str = Field(..., min_length=1, max_length=10000)
    github_repo: str | None = Field(
        default=None, pattern=r"^[a-zA-Z0-9_.-]+/[a-zA-Z0-9_.-]+$"
    )
    base_branch: str | None = Field(default=None, max_length=255)
    # Optional prior draft for multi-turn refinement: if present the LLM receives
    # the prior response as context so follow-ups like "make it 3 arms" are coherent.
    prior_draft: dict | None = None


class ComputeExperimentDraftSuccessCriterion(BaseModel):
    metric: str
    operator: str
    threshold: float


class ComputeExperimentDraft(BaseModel):
    draft_available: bool
    notes: str | None = Field(default=None, max_length=1000)
    title: str | None = Field(default=None, max_length=200)
    prompt: str | None = Field(default=None, max_length=50000)
    hypothesis: str | None = Field(default=None, max_length=500)
    command: str | None = Field(default=None, max_length=50000)
    arms: list[dict[str, str]] | None = None
    declared_metric: str | None = None
    spend_cap_usd: float | None = None
    deadline_seconds: int | None = None
    experiment: str | None = None
    benchmark: str | None = None
    metric: str | None = None
    success_criterion: ComputeExperimentDraftSuccessCriterion | None = None
    accelerator: str | None = None
    machine_type: str | None = None


_DISABLE_DRAFT_LLM_ENV = "DISABLE_COMPUTE_EXPERIMENT_DRAFT_LLM"

_DRAFT_LLM_SYSTEM = """You are an expert at designing compute experiments for ML/research projects.
Given a plain-language research idea, produce a structured JSON launch contract for a Blueprint compute experiment.
Return ONLY valid JSON — no markdown fences, no prose before or after. Use this exact schema:
{
  "title": "<short descriptive title>",
  "prompt": "<agent prompt: what the experiment should do and measure>",
  "hypothesis": "<what you expect to find>",
  "command": "<shell command to run the experiment, OR null if using arms>",
  "arms": null,
  "declared_metric": null,
  "spend_cap_usd": <positive float, default 2.0>,
  "deadline_seconds": <positive integer, default 1800>,
  "experiment": "<experiment identifier slug>",
  "benchmark": null,
  "metric": "<primary metric name if applicable, else null>",
  "success_criterion": {
    "metric": "<metric name>",
    "operator": ">=",
    "threshold": <numeric threshold>
  },
  "accelerator": null,
  "machine_type": null
}
Rules:
- Use either "command" (single run) or "arms" (list of {name, command} dicts for multi-arm sweep), never both.
- When arms is non-null, declared_metric must be set to the metric name all arms share.
- metric names must match ^[A-Za-z0-9_./-]+$.
- arm names must match ^[A-Za-z0-9_-]+$.
- spend_cap_usd must be a positive number; deadline_seconds must be a positive integer.
- success_criterion is all-or-nothing: either all three fields (metric, operator, threshold) are present, or omit the key entirely (null).
- Use sane defaults: spend_cap_usd=2.0, deadline_seconds=1800 when the idea doesn't imply otherwise.
- Keep the command runnable in a Python repo environment (PYTHONPATH set to repo root, no network outside pip).
"""


def _parse_draft_json(raw: str) -> dict | None:
    """Parse LLM output as JSON, tolerating fenced output."""
    import re as _re

    text = (raw or "").strip()
    if text.startswith("```"):
        text = _re.sub(r"^```[a-zA-Z0-9]*\n?", "", text)
        text = _re.sub(r"\n?```$", "", text).strip()
    try:
        obj = json.loads(text)
        return obj if isinstance(obj, dict) else None
    except (json.JSONDecodeError, ValueError):
        start = text.find("{")
        end = text.rfind("}")
        if start == -1 or end <= start:
            return None
        try:
            obj = json.loads(text[start : end + 1])
            return obj if isinstance(obj, dict) else None
        except (json.JSONDecodeError, ValueError):
            return None


def _coerce_draft(raw: dict) -> ComputeExperimentDraft:
    """Coerce and sanitize the LLM's raw dict into a valid ComputeExperimentDraft."""
    import re as _re

    # fullmatch, not match: `$` also matches just before a trailing newline, so
    # `.match()` accepts a key ending in a newline and lets it into an arm name /
    # metric key that is later interpolated into generated bash (the same hole as
    # the gates in compute_experiment_contract).
    _METRIC_RE = _re.compile(r"^[A-Za-z0-9_./-]+$")
    _ARM_NAME_RE = _re.compile(r"^[A-Za-z0-9_-]+$")

    def _safe_metric(v: object) -> str | None:
        if isinstance(v, str) and _METRIC_RE.fullmatch(v):
            return v
        return None

    success_criterion: ComputeExperimentDraftSuccessCriterion | None = None
    sc_raw = raw.get("success_criterion")
    if isinstance(sc_raw, dict):
        m = _safe_metric(sc_raw.get("metric"))
        op = sc_raw.get("operator")
        thr = sc_raw.get("threshold")
        if m and op in (">=", "<=", ">", "<", "==", "!=") and isinstance(thr, (int, float)):
            success_criterion = ComputeExperimentDraftSuccessCriterion(
                metric=m, operator=str(op), threshold=float(thr)
            )

    arms_raw = raw.get("arms")
    arms: list[dict[str, str]] | None = None
    if isinstance(arms_raw, list) and arms_raw:
        coerced_arms = []
        for arm in arms_raw:
            if isinstance(arm, dict):
                name = arm.get("name", "")
                cmd = arm.get("command", "")
                if isinstance(name, str) and _ARM_NAME_RE.fullmatch(name) and isinstance(cmd, str):
                    coerced_arms.append({"name": name, "command": cmd})
        arms = coerced_arms if coerced_arms else None

    spend_cap = raw.get("spend_cap_usd")
    if not isinstance(spend_cap, (int, float)) or spend_cap <= 0:
        spend_cap = 2.0

    deadline = raw.get("deadline_seconds")
    if not isinstance(deadline, int) or deadline <= 0:
        try:
            deadline = int(deadline)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            deadline = 1800
        if deadline <= 0:
            deadline = 1800

    declared_metric = _safe_metric(raw.get("declared_metric")) if arms else None

    def _trunc(v: object, limit: int) -> str | None:
        return str(v)[:limit] if isinstance(v, str) else None

    return ComputeExperimentDraft(
        draft_available=True,
        title=_trunc(raw.get("title"), 200),
        prompt=_trunc(raw.get("prompt"), 50000),
        hypothesis=_trunc(raw.get("hypothesis"), 500),
        command=_trunc(raw.get("command"), 50000) if not arms else None,
        arms=arms,
        declared_metric=declared_metric,
        spend_cap_usd=float(spend_cap),
        deadline_seconds=int(deadline),
        experiment=raw.get("experiment") if isinstance(raw.get("experiment"), str) else None,
        benchmark=raw.get("benchmark") if isinstance(raw.get("benchmark"), str) else None,
        metric=_safe_metric(raw.get("metric")),
        success_criterion=success_criterion,
        accelerator=raw.get("accelerator") if isinstance(raw.get("accelerator"), str) else None,
        machine_type=raw.get("machine_type") if isinstance(raw.get("machine_type"), str) else None,
    )


async def _call_draft_llm(
    idea: str,
    github_repo: str | None,
    base_branch: str | None,
    prior_draft: dict | None = None,
) -> dict | None:
    """Call inline LLM to produce a draft compute-experiment contract. Returns dict or None."""
    context_parts = [f"Research idea: {idea}"]
    if github_repo:
        context_parts.append(f"Repository: {github_repo}")
    if base_branch:
        context_parts.append(f"Base branch: {base_branch}")
    user_message = "\n".join(context_parts)

    if prior_draft is not None:
        # Multi-turn: inject the prior draft as the assistant's previous response so
        # the LLM can refine specific fields without starting from scratch.
        messages: list[dict] = [
            {"role": "user", "content": "Generate an experiment draft."},
            {"role": "assistant", "content": json.dumps(prior_draft)},
            {"role": "user", "content": user_message},
        ]
    else:
        messages = [{"role": "user", "content": user_message}]
    return await call_inline_llm_json(
        "compute_draft",
        user_message,
        system=_DRAFT_LLM_SYSTEM,
        messages=messages,
    )


@router.post("/tasks/compute-experiment/draft")
@limiter.limit("20/minute")
async def compute_experiment_draft(
    request: Request,
    payload: ComputeExperimentDraftRequest,
    current_user: dict = Depends(get_current_user),
) -> ComputeExperimentDraft:
    """Return a structured draft compute-experiment launch contract from a plain-language idea.

    Pure read-only: does NOT persist anything, does NOT launch a task or compute job.
    Calls the configured inline LLM to produce the draft; degrades gracefully when the key is absent or the call fails.
    """
    if os.environ.get(_DISABLE_DRAFT_LLM_ENV, "").strip() == "1":
        return ComputeExperimentDraft(
            draft_available=False,
            notes="Draft LLM is disabled via environment configuration.",
        )

    if resolve_inline_llm_settings("compute_draft").disabled:
        return ComputeExperimentDraft(
            draft_available=False,
            notes="Draft LLM is disabled via environment configuration.",
        )
    unavailable = inline_llm_unavailable_reason("compute_draft")
    if unavailable:
        return ComputeExperimentDraft(
            draft_available=False,
            notes=f"Draft generation is unavailable: {unavailable}.",
        )

    raw = await _call_draft_llm(
        payload.idea,
        payload.github_repo,
        payload.base_branch,
        prior_draft=payload.prior_draft,
    )
    if raw is None:
        return ComputeExperimentDraft(
            draft_available=False,
            notes="Draft generation failed: the LLM did not return a parseable response.",
        )

    return _coerce_draft(raw)


class ComputeExperimentPreflightPayload(BaseModel):
    github_repo: str = Field(..., pattern=r"^[a-zA-Z0-9_.-]+/[a-zA-Z0-9_.-]+$")
    accelerator: str | None = None
    machine_type: str | None = None
    spend_cap_usd: float
    deadline_seconds: int
    provisioning_model: str | None = None

    @field_validator("provisioning_model")
    @classmethod
    def validate_provisioning_model(cls, v: str | None) -> str | None:
        if v is None:
            return v
        normalized = v.upper()
        if normalized not in {"STANDARD", "SPOT"}:
            raise ValueError("provisioning_model must be STANDARD or SPOT")
        return normalized


@router.post("/tasks/compute-experiment/preflight")
@limiter.limit("60/minute")
async def compute_experiment_preflight(
    request: Request,
    payload: ComputeExperimentPreflightPayload,
    session: AsyncSession = Depends(get_session),
    current_user: dict = Depends(get_current_user),
):
    """Return projected cost and hardware-policy verdict for a compute experiment selection."""
    github_owner, github_repo = payload.github_repo.split("/", 1)

    stmt = select(Project).where(
        Project.github_owner == github_owner,
        Project.github_repo == github_repo,
        Project.id.in_(_user_project_ids(current_user["user_id"])),
    )
    project = (await session.execute(stmt)).scalars().first()

    allowed_machines: list[str] | None
    allowed_accels: list[str] | None
    if project is not None:
        allowed_machines, allowed_accels = resolve_project_hardware_policy(project)
    else:
        allowed_machines, allowed_accels = None, None

    spec, shape_rejection = validate_compute_request(
        payload.accelerator,
        payload.machine_type,
    )

    resolved_machine_type = resolve_compute_machine_type(
        accelerator=payload.accelerator,
        machine_type=payload.machine_type,
    )

    effective_spend_cap_usd = clamp_spend_cap_usd(payload.spend_cap_usd)
    effective_deadline_seconds = clamp_deadline_seconds(payload.deadline_seconds)

    rate_per_second: float | None = None
    rate_per_hour: float | None = None
    projected_cost_usd: float | None = None
    max_runtime_seconds: int | None = None
    if shape_rejection is None:
        spot = payload.provisioning_model == "SPOT"
        rate_per_second = get_compute_rate_per_second(
            machine_type=resolved_machine_type,
            accelerator=payload.accelerator,
            spot=spot,
        )
        rate_per_hour = rate_per_second * 3600
        projected_cost_usd = rate_per_second * effective_deadline_seconds
        if rate_per_second > 0:
            max_runtime_seconds = int(effective_spend_cap_usd / rate_per_second)

    # Policy verdict: use the canonical accelerator name (spec.name) to mirror
    # the comparison done in _check_project_hardware_policy.
    canonical_accelerator = spec.name if spec is not None else None
    in_policy = True
    reason: str | None = None
    if shape_rejection is None:
        if canonical_accelerator is not None:
            if (
                allowed_accels is not None
                and canonical_accelerator not in allowed_accels
            ):
                in_policy = False
                reason = (
                    f"accelerator {canonical_accelerator!r} is not in the project's "
                    f"allowed_accelerator_types {allowed_accels!r}"
                )
        else:
            if (
                allowed_machines is not None
                and resolved_machine_type not in allowed_machines
            ):
                in_policy = False
                reason = (
                    f"machine type {resolved_machine_type!r} is not in the project's "
                    f"allowed_machine_types {allowed_machines!r}"
                )

    return {
        "resolved_machine_type": resolved_machine_type,
        "rate_per_second": rate_per_second,
        "rate_per_hour": rate_per_hour,
        "projected_cost_usd": projected_cost_usd,
        "max_runtime_seconds": max_runtime_seconds,
        "effective_spend_cap_usd": effective_spend_cap_usd,
        "effective_deadline_seconds": effective_deadline_seconds,
        "allowed_accelerator_types": allowed_accels,
        "allowed_machine_types": allowed_machines,
        "in_policy": in_policy,
        "reason": reason,
        "shape_rejection": shape_rejection,
    }


async def _resolve_forced_runner_target(
    session: AsyncSession, project_id: uuid.UUID, runner_target: str
) -> str:
    """Resolve an operator runner target (UUID or hostname) to a LocalRunner UUID.

    The operator may pass either a LocalRunner UUID or a host name (e.g. ``dgx-1``).
    Resolution is scoped to the project's allow-listed, non-deleted runners. A runner
    that is currently OFFLINE still resolves — the operator may launch ahead of the
    box coming up; only membership (enrollment or explicit secondary grant) and
    non-deletion are required. Returns the resolved runner UUID string. Raises HTTP
    422 when the target does not resolve to a runner allowed for this project.
    """
    target = (runner_target or "").strip()
    if not target:
        raise HTTPException(
            status_code=422,
            detail="runner_target_id must be a non-empty runner UUID or hostname",
        )
    # Prefer a UUID match (operator pasted a runner id) before a hostname match.
    parsed: uuid.UUID | None = None
    try:
        parsed = uuid.UUID(target)
    except (ValueError, AttributeError, TypeError):
        parsed = None
    if parsed is not None:
        runner = (
            (
                await session.execute(
                    select(LocalRunner).where(
                        LocalRunner.id == parsed,
                        runner_allowed_for_project(project_id),
                        LocalRunner.status != "deleted",
                    )
                )
            )
            .scalars()
            .first()
        )
        if runner is not None:
            return str(runner.id)
    # Fall back to a hostname match; when several rows share a hostname (re-enroll),
    # take the most recently seen so a live box wins over a stale duplicate.
    matches = (
        (
            await session.execute(
                select(LocalRunner)
                .where(
                    runner_allowed_for_project(project_id),
                    LocalRunner.hostname == target,
                    LocalRunner.status != "deleted",
                )
                .order_by(LocalRunner.last_seen_at.desc())
            )
        )
        .scalars()
        .all()
    )
    if matches:
        return str(matches[0].id)
    raise HTTPException(
        status_code=422,
        detail={
            "error": "forced_runner_not_found",
            "message": (
                f"runner_target_id {target!r} does not resolve to a registered, "
                "non-deleted runner allowed for this project"
            ),
            "requested": target,
        },
    )


@router.post("/tasks/compute-experiment")
@limiter.limit("60/minute")
async def create_compute_experiment_task(
    request: Request,
    payload: ComputeExperimentTaskPayload,
    session: AsyncSession = Depends(get_session),
    current_user: dict = Depends(get_current_user),
):
    """Create an operator-launched compute_experiment with a full launch contract."""
    github_owner, github_repo = payload.github_repo.split("/", 1)

    # Same write gate as POST /tasks, and for a strictly stronger reason: this
    # endpoint mints a compute_experiment with a full launch contract, i.e. it
    # spends real GPU budget against the project's spend_cap_usd. Membership
    # alone is not authorization to spend — without the member+ floor a viewer
    # denied POST /tasks/{id}/rerun could simply launch a fresh GPU job here
    # instead (and, via the auto-create branch, mint a project they own).
    project = await _resolve_repo_project_for_write(
        session, current_user["user_id"], github_owner, github_repo
    )

    if not project:
        # Mark the auto-created project as bootstrap-complete and non-autonomous:
        # compute_experiment is a one-off operator job and does not need repo
        # bootstrapping or autonomous research cycles.  Without these flags the
        # bootstrap-recovery sweep would spawn a competing repo_bootstrap task
        # and the autonomous-research gate (_bootstrap_pending) would defer
        # spawning for the project, both of which can starve the dispatch slot
        # that the compute_experiment task is waiting for.
        _now_iso = _utcnow().isoformat()
        project = Project(
            name=payload.github_repo,
            user_id=current_user.get("user_id", "admin"),
            github_owner=github_owner,
            github_repo=github_repo,
            autonomous_enabled=False,
            build_config={"bootstrap_completed_at": _now_iso},
        )
        session.add(project)
        await session.flush()
        # [RESIDENT-REMOVAL] removed with resident subsystem
#         from app.strategic_resident_checkpointing import (
#             initialize_strategic_resident,
#         )

        await initialize_strategic_resident(  # noqa: F821
            session, project.id, locked_project=project
        )
        await session.commit()
        await session.refresh(project)
        session.add(
            ProjectMember(
                project_id=project.id, user_id=current_user["user_id"], role="owner"
            )
        )
        await session.commit()

    await backfill_branch_columns(session, project_id=project.id)

    # Resolve an operator HARD runner pin (UUID or hostname) against this project's
    # allow-listed runners BEFORE persisting the task. A 422 here means the target
    # is unknown/foreign — fail fast rather than create a task that can never run.
    forced_runner_id: str | None = None
    if payload.runner_target_id is not None and payload.runner_target_id.strip():
        forced_runner_id = await _resolve_forced_runner_target(
            session, project.id, payload.runner_target_id
        )

    resolved_base_branch = (
        payload.base_branch or _get_project_promotion_target_for_task(project)
    )
    launch_contract: dict[str, Any] = {
        "spend_cap_usd": payload.spend_cap_usd,
        "deadline_seconds": payload.deadline_seconds,
    }
    if payload.arms is not None:
        launch_contract["arms"] = payload.arms
        launch_contract["declared_metric"] = payload.declared_metric
    else:
        launch_contract["command"] = payload.command
    if payload.success_criterion is not None:
        launch_contract["success_criterion"] = payload.success_criterion.model_dump()
    provenance: dict[str, Any] = {}
    if payload.idea is not None:
        provenance["idea"] = payload.idea
    if payload.draft_notes is not None:
        provenance["draft_notes"] = payload.draft_notes
    if payload.drafted_from_refinement is not None:
        provenance["drafted_from_refinement"] = payload.drafted_from_refinement
    if provenance:
        launch_contract["provenance"] = provenance
    for opt_key in (
        "accelerator",
        "machine_type",
        "cwd",
        "env",
        "experiment",
        "benchmark",
        "hypothesis",
        "metric",
        "provisioning_model",
        "container_image",
        "container_registry_auth",
        "setup_step",
        "no_progress_timeout_seconds",
        "input_artifact_job_id",
    ):
        val = getattr(payload, opt_key)
        if val is not None:
            launch_contract[opt_key] = val
    if payload.runner_target is not None:
        launch_contract["runner_target"] = payload.runner_target.model_dump()

    ok, reason = _validate_compute_launch_contract(launch_contract, require_smoke=False)
    if not ok:
        raise HTTPException(status_code=422, detail=reason)

    # A runner pin must name one of THIS project's allow-listed runners — reject
    # unknown and unauthorized runner ids at authoring time with a clear 422
    # instead of persisting a task that can never (strict) or silently won't
    # (soft) dispatch where the operator expects.
    if payload.runner_target is not None:
        runner_target_error = await validate_compute_runner_target_enrollment(
            session, project.id, launch_contract["runner_target"]
        )
        if runner_target_error is not None:
            raise HTTPException(status_code=422, detail=runner_target_error)

    # Apply the same accelerator / machine-shape validation as compute-job
    # creation BEFORE persisting a Task, so invalid GPU launch contracts fail
    # fast with a clear 422 instead of creating a task that can never launch.
    # Accelerator-only requests for supported accelerators stay valid and are
    # canonicalized downstream when the agent launches the actual compute job.
    accel_spec, gpu_reason = validate_compute_request(
        launch_contract.get("accelerator"),
        launch_contract.get("machine_type"),
    )
    if gpu_reason is not None:
        raise HTTPException(status_code=422, detail=gpu_reason)

    # Enforce the project's hardware policy before persisting the Task so a
    # project-disallowed accelerator or machine type fails at dispatch time
    # rather than silently passing the form and dying later in create_compute_job.
    _lc_accelerator = launch_contract.get("accelerator")
    _canonical_accelerator = accel_spec.name if accel_spec is not None else None
    _resolved_mt = resolve_compute_machine_type(
        accelerator=_lc_accelerator,
        machine_type=launch_contract.get("machine_type"),
    )
    _allowed_machines, _allowed_accels = resolve_project_hardware_policy(project)
    if _canonical_accelerator is not None:
        if (
            _allowed_accels is not None
            and _canonical_accelerator not in _allowed_accels
        ):
            raise HTTPException(
                status_code=422,
                detail={
                    "error": "project_hardware_policy_violated",
                    "message": (
                        f"accelerator {_canonical_accelerator!r} is not in the project's "
                        f"allowed_accelerator_types {_allowed_accels!r}"
                    ),
                    "requested": _canonical_accelerator,
                    "allowed": _allowed_accels,
                },
            )
    else:
        if _allowed_machines is not None and _resolved_mt not in _allowed_machines:
            raise HTTPException(
                status_code=422,
                detail={
                    "error": "project_hardware_policy_violated",
                    "message": (
                        f"machine type {_resolved_mt!r} is not in the project's "
                        f"allowed_machine_types {_allowed_machines!r}"
                    ),
                    "requested": _resolved_mt,
                    "allowed": _allowed_machines,
                },
            )

    task_payload_raw = {
        "type": "compute_experiment",
        "prompt": payload.prompt,
        "title": payload.title,
        "base_branch": resolved_base_branch,
        "command": launch_contract["command"],
        "spend_cap_usd": float(launch_contract["spend_cap_usd"]),
        "deadline_seconds": int(launch_contract["deadline_seconds"]),
        **(
            {"success_criterion": launch_contract["success_criterion"]}
            if launch_contract.get("success_criterion") is not None
            else {}
        ),
        **(
            {"provenance": launch_contract["provenance"]}
            if launch_contract.get("provenance") is not None
            else {}
        ),
        **{
            k: launch_contract[k]
            for k in (
                "accelerator",
                "machine_type",
                "cwd",
                "env",
                "experiment",
                "benchmark",
                "hypothesis",
                "metric",
                "provisioning_model",
                "arms",
                "declared_metric",
                "container_image",
                "container_registry_auth",
                "setup_step",
                "no_progress_timeout_seconds",
                "input_artifact_job_id",
                "runner_target",
            )
            if launch_contract.get(k) is not None
        },
        **({"live_proof": True} if payload.require_live_release else {}),
        **(
            {"require_durable_artifact": True}
            if payload.require_durable_artifact
            else {}
        ),
        **({"spawn_followup": payload.spawn_followup} if payload.spawn_followup is not None else {}),
    }
    try:
        task_payload = validate_task_payload(task_payload_raw)
    except ValueError as exc:
        logger.warning(
            "Validation error in task payload: %s: %s", type(exc).__name__, exc
        )
        raise HTTPException(status_code=422, detail="Invalid request")

    active_count = (
        await session.execute(
            select(func.count(Task.id)).where(
                Task.project_id == project.id,
                Task.status.in_(
                    [
                        TaskStatus.QUEUED,
                        TaskStatus.PROVISIONING,
                        TaskStatus.IN_PROGRESS,
                        TaskStatus.EVALUATING,
                    ]
                ),
            )
        )
    ).scalar() or 0
    if active_count >= 200:
        raise HTTPException(
            status_code=429,
            detail=f"Project has {active_count} active tasks (limit: 200)",
        )

    scoped_idempotency_key = (
        f"admin_compute_launcher:{project.id}:{payload.idempotency_key}"
        if payload.idempotency_key
        else None
    )
    if scoped_idempotency_key:
        existing = (
            (
                await session.execute(
                    select(Task).where(
                        Task.idempotency_key == scoped_idempotency_key,
                        Task.status.in_(
                            [
                                TaskStatus.QUEUED,
                                TaskStatus.PROVISIONING,
                                TaskStatus.IN_PROGRESS,
                                TaskStatus.EVALUATING,
                                TaskStatus.BLOCKED,
                                TaskStatus.PARKED,
                            ]
                        ),
                    )
                )
            )
            .scalars()
            .first()
        )
        if existing:
            return {
                "id": str(existing.id),
                "project_id": str(existing.project_id),
                "title": existing.title,
                "status": existing.status,
                "vm_instance_id": existing.vm_instance_id,
                "is_duplicate": True,
            }

    requires_approval, _ = resolve_compute_spend_approval(
        runtime_config=_runtime_config,
        direction=None,
        spend_cap_usd=float(launch_contract["spend_cap_usd"]),
    )

    vm_name = f"agent-vm-{uuid.uuid4().hex}"
    task_context: dict[str, Any] = {}
    if payload.runner_target is not None:
        # Existing dispatch seam: _select_eligible_runner prefers this runner
        # when eligible. Strictness is read from the payload's runner_target at
        # dispatch time (orchestrator_runner_dispatch._payload_runner_target).
        task_context["preferred_runner_target_id"] = payload.runner_target.runner_id
    if forced_runner_id:
        # Operator HARD pin (runner_target_id): _resolve_forced_runner_target
        # already validated this runner is allowed for the project; dispatch
        # refuses any other runner. Distinct from the soft preferred pin above.
        task_context["forced_runner_target_id"] = forced_runner_id
    new_task = Task(
        project_id=project.id,
        title=payload.title,
        description=payload.prompt,
        payloads=[task_payload],
        vm_instance_id=vm_name,
        status=TaskStatus.QUEUED,
        created_by="USER",
        launched_by_user_id=current_user["user_id"],
        requires_approval=requires_approval,
        idempotency_key=scoped_idempotency_key,
        context=task_context,
    )
    session.add(new_task)
    try:
        await session.commit()
    except Exception as e:
        await session.rollback()
        if scoped_idempotency_key and "ux_task_active_idempotency_key" in str(e):
            existing = (
                (
                    await session.execute(
                        select(Task).where(
                            Task.idempotency_key == scoped_idempotency_key,
                            Task.status.in_(
                                [
                                    TaskStatus.QUEUED,
                                    TaskStatus.PROVISIONING,
                                    TaskStatus.IN_PROGRESS,
                                    TaskStatus.EVALUATING,
                                    TaskStatus.BLOCKED,
                                    TaskStatus.PARKED,
                                ]
                            ),
                        )
                    )
                )
                .scalars()
                .first()
            )
            if existing:
                return {
                    "id": str(existing.id),
                    "project_id": str(existing.project_id),
                    "title": existing.title,
                    "status": existing.status,
                    "vm_instance_id": existing.vm_instance_id,
                    "is_duplicate": True,
                }
        raise
    await session.refresh(new_task)

    history = TaskHistory(
        task_id=new_task.id,
        action="CREATED",
        details={
            "created_by": "USER",
            "method": "admin_compute_experiment_launcher",
            "github_repo": payload.github_repo,
            "base_branch": resolved_base_branch,
        },
    )
    session.add(history)
    await session.commit()

    return {
        "id": str(new_task.id),
        "project_id": str(project.id),
        "title": new_task.title,
        "status": new_task.status,
        "vm_instance_id": new_task.vm_instance_id,
        "requires_approval": new_task.requires_approval,
    }


class RerunComputeExperimentPayload(BaseModel):
    success_criterion: ComputeSuccessCriterion | None = None
    spend_cap_usd: float | None = Field(default=None, gt=0)
    deadline_seconds: int | None = Field(default=None, gt=0)
    # Optional forced runner pin override (UUID or hostname). When omitted the
    # re-run INHERITS the source task's forced pin (if any); when supplied it
    # replaces it (resolved against the project's registered runners).
    runner_target_id: str | None = Field(default=None, max_length=255)


@router.post("/tasks/{task_id}/rerun")
@limiter.limit("60/minute")
async def rerun_compute_experiment(
    request: Request,
    task_id: uuid.UUID,
    payload: RerunComputeExperimentPayload,
    session: AsyncSession = Depends(get_session),
    current_user: dict = Depends(get_current_user),
):
    """Re-run a compute_experiment task, optionally overriding criterion/spend/deadline.

    Clones the source task's launch contract, applies caller-supplied overrides,
    validates the result, and creates a new QUEUED task with ``parent_task_id``
    pointing at the source so the two runs are linked.
    """
    # Re-running a compute_experiment launches new GPU spend — member+ only
    # (viewers are read-only).
    source = await _get_user_task_with_role(
        task_id, current_user["user_id"], session, "member"
    )

    source_payload = _task_payload_dict(source)
    if not source_payload or source_payload.get("type") != "compute_experiment":
        raise HTTPException(
            status_code=422,
            detail="Task is not a compute_experiment and cannot be re-run via this endpoint",
        )

    # Clone the source payload and apply caller overrides.
    new_payload_raw: dict[str, Any] = dict(source_payload)
    if payload.spend_cap_usd is not None:
        new_payload_raw["spend_cap_usd"] = float(payload.spend_cap_usd)
    if payload.deadline_seconds is not None:
        new_payload_raw["deadline_seconds"] = int(payload.deadline_seconds)
    if payload.success_criterion is not None:
        new_payload_raw["success_criterion"] = payload.success_criterion.model_dump()

    # Rebuild launch_contract to re-run validation.
    launch_contract: dict[str, Any] = {
        "spend_cap_usd": new_payload_raw["spend_cap_usd"],
        "deadline_seconds": new_payload_raw["deadline_seconds"],
    }
    if new_payload_raw.get("arms") is not None:
        launch_contract["arms"] = new_payload_raw["arms"]
        if new_payload_raw.get("declared_metric") is not None:
            launch_contract["declared_metric"] = new_payload_raw["declared_metric"]
    else:
        launch_contract["command"] = new_payload_raw.get("command", "")
    if new_payload_raw.get("success_criterion") is not None:
        launch_contract["success_criterion"] = new_payload_raw["success_criterion"]

    ok, reason = _validate_compute_launch_contract(launch_contract, require_smoke=False)
    if not ok:
        raise HTTPException(status_code=422, detail=reason)

    try:
        task_payload = validate_task_payload(new_payload_raw)
    except ValueError as exc:
        logger.warning("Rerun payload validation error: %s: %s", type(exc).__name__, exc)
        raise HTTPException(status_code=422, detail="Invalid re-run payload")

    # Load the project to check spend-approval threshold.
    project = await session.get(Project, source.project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="Project not found")

    # Forced runner pin: an explicit override resolves against the project's
    # runners; otherwise inherit the source task's forced pin (if any).
    if payload.runner_target_id is not None and payload.runner_target_id.strip():
        forced_runner_id: str | None = await _resolve_forced_runner_target(
            session, project.id, payload.runner_target_id
        )
    else:
        raw_inherited = (source.context or {}).get("forced_runner_target_id")
        forced_runner_id = (
            str(raw_inherited).strip()
            if isinstance(raw_inherited, str) and raw_inherited.strip()
            else None
        )

    requires_approval, _ = resolve_compute_spend_approval(
        runtime_config=_runtime_config,
        direction=None,
        spend_cap_usd=float(new_payload_raw["spend_cap_usd"]),
    )

    vm_name = f"agent-vm-{uuid.uuid4().hex}"
    new_task = Task(
        project_id=source.project_id,
        title=source.title,
        description=source.description,
        payloads=[task_payload],
        vm_instance_id=vm_name,
        status=TaskStatus.QUEUED,
        created_by="USER",
        launched_by_user_id=current_user["user_id"],
        requires_approval=requires_approval,
        parent_task_id=source.id,
        context=(
            {"forced_runner_target_id": forced_runner_id} if forced_runner_id else {}
        ),
    )
    session.add(new_task)
    await session.commit()
    await session.refresh(new_task)

    history = TaskHistory(
        task_id=new_task.id,
        action="CREATED",
        details={
            "created_by": "USER",
            "method": "admin_compute_experiment_rerun",
            "source_task_id": str(source.id),
        },
    )
    session.add(history)
    await session.commit()

    return {
        "id": str(new_task.id),
        "project_id": str(project.id),
        "title": new_task.title,
        "status": new_task.status,
        "vm_instance_id": new_task.vm_instance_id,
        "requires_approval": new_task.requires_approval,
        "parent_task_id": str(source.id),
    }


class RequestChangesPayload(BaseModel):
    feedback: str = Field(..., min_length=1, max_length=5000)
    action: str = Field(default="fix", pattern=r"^(fix|review)$")


@router.post("/tasks/{task_id}/request-changes")
async def request_changes(
    task_id: uuid.UUID,
    payload: RequestChangesPayload,
    session: AsyncSession = Depends(get_session),
    current_user: dict = Depends(get_current_user),
):
    """Request changes on a task awaiting approval. Spawns a fix or re-review task."""
    task = await _get_user_task_with_role(
        task_id, current_user["user_id"], session, "member"
    )
    if not task.requires_approval:
        raise HTTPException(status_code=400, detail="Task is not awaiting approval")

    # Find the PR
    artifact = (
        (
            await session.execute(
                select(Artifact).where(
                    Artifact.task_id == task_id,
                    Artifact.artifact_type == ArtifactType.PULL_REQUEST,
                )
            )
        )
        .scalars()
        .first()
    )
    if not artifact or not artifact.pr_url:
        raise HTTPException(status_code=400, detail="No PR found for this task")

    pr_number = int(artifact.pr_url.rstrip("/").split("/")[-1])
    task_payload = _task_payload_dict(task)
    _project = await session.get(Project, task.project_id)
    branch = _resolve_task_branch(task, get_project_task_branch_namespace(_project))
    base_branch = task_payload.get("base_branch") or get_project_integration_branch(
        _project
    )

    from app.review_pipeline import REVIEW_FIX_GOAL_PREAMBLE, _spawn_pipeline_task

    if payload.action == "fix":
        spawned, _ = await _spawn_pipeline_task(
            task,
            "fix",
            session,
            pr_number,
            branch,
            base_branch,
            fix_prompt=f"{REVIEW_FIX_GOAL_PREAMBLE}A human reviewer flagged this PR. Feedback:\n\n{payload.feedback}",
            original_task_id=str(task.id),
        )
    else:
        spawned, _ = await _spawn_pipeline_task(
            task,
            "review",
            session,
            pr_number,
            branch,
            base_branch,
            original_prompt=task_payload.get("prompt", task.description or ""),
            original_task_id=str(task.id),
        )

    if spawned:
        task.requires_approval = False
        task.latest_status_message = (
            f"Changes requested — spawned {payload.action} task"
        )
        session.add(task)
        await session.commit()
        return {"spawned_task_id": str(spawned.id), "action": payload.action}

    raise HTTPException(status_code=500, detail="Failed to spawn task (iteration cap?)")


_FRESH_REVIEWER_COOLDOWN_SECS = 3600  # 1 hour

# Maps a task's payload ``type`` to the branch prefix the agent uses when
# deriving the work branch. Mirrors the agent's branchPrefix wiring
# (agent/src/handlers/standard.ts: create/qa->task, refactor->refactor,
# docs->docs, deps->deps; handlers/specialized.ts->migrate). Fix tasks reuse
# an existing branch carried in the payload, so they never need this fallback.
# Branch derivation moved to app.shared_state.derive_task_branch so callers
# outside admin (rollup_management, task_lifecycle) can share the same map
# without circular imports.


@router.post("/tasks/{task_id}/request-fresh-reviewer")
@limiter.limit("10/minute")
async def request_fresh_reviewer(
    request: Request,
    task_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    current_user: dict = Depends(get_current_user),
):
    """Spawn an operator-requested fresh-reviewer pass against a completed task's PR diff."""
    task = await _get_user_task_with_role(
        task_id, current_user["user_id"], session, "member"
    )

    ctx = dict(task.context or {})
    last_req = ctx.get("_operator_fresh_reviewer_requested_at")
    if last_req:
        elapsed = (
            datetime.now(timezone.utc) - datetime.fromisoformat(last_req)
        ).total_seconds()
        if elapsed < _FRESH_REVIEWER_COOLDOWN_SECS:
            raise HTTPException(
                status_code=429,
                detail="Cooldown active — a fresh-reviewer pass was recently requested",
            )

    artifact = (
        (
            await session.execute(
                select(Artifact).where(
                    Artifact.task_id == task_id,
                    Artifact.artifact_type == ArtifactType.PULL_REQUEST,
                )
            )
        )
        .scalars()
        .first()
    )
    if not artifact or not artifact.pr_url:
        raise HTTPException(status_code=400, detail="No PR found for this task")

    pr_number = int(artifact.pr_url.rstrip("/").split("/")[-1])
    task_payload = _task_payload_dict(task)
    review_loop = ctx.get("review_loop") or {}
    _project = await session.get(Project, task.project_id)
    branch = (
        review_loop.get("branch")
        or task_payload.get("branch")
        or _derive_task_branch(
            task_id,
            task_payload.get("type"),
            get_project_task_branch_namespace(_project),
        )
    )
    base_branch = task_payload.get("base_branch") or get_project_integration_branch(
        _project
    )
    related_files = task_payload.get("related_files") or ctx.get("related_files") or []

    reviewer_task = Task(
        project_id=task.project_id,
        title=f"Operator fresh review: {task.title or str(task_id)[:8]}",
        description=f"Operator-requested fresh-reviewer pass for task {task_id}",
        payloads=[
            {
                "type": "create",
                "prompt": f"Operator-requested fresh-reviewer pass for task {task_id}.",
                "pr_number": pr_number,
                "branch": branch,
                "base_branch": base_branch,
                "related_files": related_files,
            }
        ],
        context={
            "operator_requested_review_for": str(task_id),
            "operator_requested_by": str(current_user["user_id"]),
        },
        vm_instance_id=f"agent-vm-{uuid.uuid4().hex}",
        status=TaskStatus.QUEUED,
        created_by="USER",
        launched_by_user_id=current_user["user_id"],
    )
    session.add(reviewer_task)

    ctx["_operator_fresh_reviewer_requested_at"] = datetime.now(
        timezone.utc
    ).isoformat()
    task.context = ctx
    flag_modified(task, "context")
    session.add(task)

    await session.commit()

    return {"spawned_task_id": str(reviewer_task.id)}


@router.post("/tasks/{task_id}/archive")
async def archive_task(
    task_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    current_user: dict = Depends(get_current_user),
):
    task = await _get_user_task_with_role(
        task_id, current_user["user_id"], session, "member"
    )
    if task.status not in TERMINAL_STATUSES:
        raise HTTPException(
            status_code=400,
            detail=f"Cannot archive task with status '{task.status}'. Only terminal tasks (COMPLETED, FAILED, CANCELLED) can be archived.",
        )
    task.archived = True
    session.add(task)
    await session.commit()
    await session.refresh(task)
    return task


@router.post("/tasks/{task_id}/reset-iteration-cap")
async def reset_iteration_cap(
    task_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    current_user: dict = Depends(get_current_user),
):
    """Reset the review-fix iteration cap for a PR.

    Archives FAILED review/fix tasks for the PR (drops them from the per-PR
    global count) and resets the per-chain iteration counter on ``task``.
    Use when the cap tripped on infra/transient failures rather than real
    review iterations, to let the pipeline retry without manual merge.

    Does NOT re-spawn tasks automatically — after reset, re-trigger the
    rollup (POST /tasks/{task_id}/rollup) or let the next orchestrator cycle
    pick up the parking task.
    """
    task = await _get_user_task_with_role(
        task_id, current_user["user_id"], session, "member"
    )

    ctx = task.context or {}
    review_loop = ctx.get("review_loop", {}) or {}
    pr_number = review_loop.get("pr_number") or ctx.get("rollup_pr_number")
    if not pr_number:
        raise HTTPException(
            status_code=400,
            detail="Task has no PR number in context.review_loop.pr_number or context.rollup_pr_number",
        )

    # Archive FAILED review/fix tasks on this PR so they drop from the global count.
    # _pr_number_match is dialect-conditional: on SQLite, .as_string() JSON_QUOTE-
    # double-quotes the int and never matches, so reuse the shared helper which
    # falls back to func.json_extract for SQLite.
    from app.ci_completion import _pr_number_match

    failed_result = await session.execute(
        select(Task)
        .where(
            Task.project_id == task.project_id,
            Task.status == TaskStatus.FAILED,
            Task.archived.is_(False),
            _pr_number_match(session, pr_number),
        )
        .options(load_only(Task.id, Task.archived, Task.payloads, raiseload=True))
    )
    archived_count = 0
    for t in failed_result.scalars().all():
        fp = _task_payload_dict(t)
        if fp.get("type") not in ("review", "fix"):
            continue
        t.archived = True
        session.add(t)
        archived_count += 1

    # Reset per-chain iteration counter on the parking task
    new_ctx = dict(ctx)
    new_review_loop = dict(review_loop)
    previous_iteration = new_review_loop.get("iteration", 0)
    new_review_loop["iteration"] = 0
    new_review_loop["iteration_reset_at"] = _utcnow().isoformat()
    new_review_loop["iteration_reset_by"] = current_user.get(
        "email", current_user["user_id"]
    )
    new_ctx["review_loop"] = new_review_loop
    task.context = new_ctx
    flag_modified(task, "context")

    # Clear requires_approval if it was set by the cap path
    was_requires_approval = task.requires_approval
    if task.requires_approval:
        task.requires_approval = False

    session.add(task)
    await session.commit()
    await session.refresh(task)
    logger.info(
        f"[ADMIN] Iteration cap reset on task {task.id} (PR #{pr_number}): "
        f"archived {archived_count} failed review/fix tasks, "
        f"per-chain iteration {previous_iteration}→0"
    )
    return {
        "task_id": str(task.id),
        "pr_number": pr_number,
        "archived_count": archived_count,
        "previous_per_chain_iteration": previous_iteration,
        "requires_approval_cleared": was_requires_approval,
    }


@router.post("/tasks/{task_id}/unarchive")
async def unarchive_task(
    task_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    current_user: dict = Depends(get_current_user),
):
    task = await _get_user_task_with_role(
        task_id, current_user["user_id"], session, "member"
    )
    task.archived = False
    session.add(task)
    await session.commit()
    await session.refresh(task)
    return task


@router.patch("/tasks/{task_id}")
@limiter.limit("60/minute")
async def modify_task(
    request: Request,
    task_id: uuid.UUID,
    payload: Dict[str, Any],
    background_tasks: BackgroundTasks,
    force_kill: bool = Query(False),
    session: AsyncSession = Depends(get_session),
    current_user: dict = Depends(get_current_user),
):
    task = await _get_user_task_with_role(
        task_id, current_user["user_id"], session, "member"
    )

    # All mutation logic lives in the tested helper modify_task_apply
    # (app/admin_task_mutations.py). The HTTP handler keeps only the
    # auth/project-access check above; everything else — status transitions,
    # cascade cancellation, requeue, approval flows, simple fields,
    # depends_on cycle detection, commit + rollup trigger — is applied there.
    return await modify_task_apply(
        task=task,
        payload=payload,
        background_tasks=background_tasks,
        session=session,
        force_kill=force_kill,
    )


router.include_router(_operator_triggers_router)


# --- Deployment State & Health ---


def _get_zone_health_summary() -> dict:
    try:
        from app.compute import get_zone_health_summary

        return get_zone_health_summary()
    except Exception:  # noqa: BLE001
        return {"total_zones": 0, "available": 0, "exhausted": []}


async def build_deploy_state_payload(session: AsyncSession) -> dict:
    """Assemble the deploy state payload. Used by both HTTP endpoint and WS broadcasts."""
    from app.shared_state import deploy_monitoring, external_health, runtime_config
    import app.deploy_health as _deploy_health_module

    state = await session.get(DeploymentState, 1)
    if not state:
        return {
            "error": "No deployment state found — service may not have initialized yet"
        }

    # Calculate monitoring window progress
    window_progress = None
    if deploy_monitoring["window_start"]:
        duration = (
            deploy_monitoring["extended_window"]
            if deploy_monitoring["is_extended"]
            else deploy_monitoring["window_duration"]
        )
        elapsed = (_utcnow() - deploy_monitoring["window_start"]).total_seconds()
        window_progress = {
            "elapsed_seconds": int(elapsed),
            "total_seconds": duration,
            "percent": min(100, int(elapsed / duration * 100)),
        }

    # Calculate recent failure rate
    results = deploy_monitoring["task_results"]
    failure_rate = 0.0
    if results:
        failures = sum(1 for r in results if r[1] == "failure")
        failure_rate = failures / len(results)

    current_vms = runtime_config["max_concurrent_vms"]
    target_vms = state.runtime_max_concurrent_vms
    ramp_in_progress = (
        state.status in (DeploymentStatus.MONITORING, DeploymentStatus.STABLE)
        and state.pre_pause_max_vms is None
        and target_vms is not None
        and 0 < current_vms < target_vms
    )
    ramp_step_size = min(2, target_vms - current_vms) if ramp_in_progress else 0

    # external_health holds a live datetime in last_check; copy and coerce so
    # WebSocket consumers using send_json (which lacks FastAPI's encoder)
    # don't choke on "Object of type datetime is not JSON serializable".
    _last_check = external_health.get("last_check")
    external_health_serializable = {
        **external_health,
        "last_check": _last_check.isoformat()
        if isinstance(_last_check, datetime)
        else _last_check,
    }
    health_recovery_history = list(
        getattr(_deploy_health_module, "_health_recovery_history", [])
    )
    (
        health_recovery_streak_required,
        health_recovery_flap_escalated,
    ) = _deploy_health_module.get_recovery_threshold_state()

    return {
        "current_revision": state.current_revision,
        "current_sha": state.current_sha,
        "deployed_at": state.deployed_at.isoformat() if state.deployed_at else None,
        "status": state.status,
        "last_known_good_revision": state.last_known_good_revision,
        "last_known_good_sha": state.last_known_good_sha,
        "rolled_back_from_sha": state.rolled_back_from_sha,
        "rollback_reason": state.rollback_reason,
        "rollback_count": state.rollback_count,
        "pause_reason": state.pause_reason,
        "paused_at": state.paused_at.isoformat() if state.paused_at else None,
        "reason": state.pause_reason
        if state.status == DeploymentStatus.PAUSED
        else None,
        "pre_pause_max_vms": state.pre_pause_max_vms,
        "monitoring_window": window_progress,
        "failure_rate": round(failure_rate, 3),
        "task_results_count": len(results),
        "external_health": external_health_serializable,
        "max_concurrent_vms": runtime_config["max_concurrent_vms"],
        "current_vms": current_vms,
        "target_vms": target_vms,
        "ramp_in_progress": ramp_in_progress,
        "ramp_step_size": ramp_step_size,
        "zone_health": _get_zone_health_summary(),
        "gcp_project_id": os.getenv("GCP_PROJECT_ID", ""),
        "health_recovery_streak": sum(1 for x in health_recovery_history if x),
        "health_recovery_streak_required": health_recovery_streak_required,
        "health_recovery_flap_escalated": health_recovery_flap_escalated,
        "health_recovery_history": health_recovery_history,
        "health_recovery_history_size": getattr(
            _deploy_health_module, "_HEALTH_HISTORY_SIZE", 5
        ),
        "health_failure_streak": getattr(
            _deploy_health_module, "_pause_failure_streak", 0
        ),
        "health_failure_streak_threshold": 3,
        "health_check_interval_seconds": 15
        if runtime_config["max_concurrent_vms"] == 0
        else 60,
        "max_auto_pause_duration_seconds": runtime_config.get(
            "max_auto_pause_duration_seconds", 7200
        ),
    }

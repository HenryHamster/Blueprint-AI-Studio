"""Narrow project-scoped mutation surface for external operator agents.

The ``project-operator`` ServiceCredential scope reaches only this router and
the bounded ``agent-access`` reads.  Every operation has a strict body contract,
rechecks ProjectMember authorization, and delegates to the canonical lifecycle
implementation with authority-bearing fields supplied by the server.
"""

from __future__ import annotations

import json
import uuid
from typing import Annotated, Any, Literal

from fastapi import (
    APIRouter,
    BackgroundTasks,
    Depends,
    HTTPException,
    Query,
    Request,
    Response,
)
from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from app.admin import (
    RequestChangesPayload,
    _get_user_project_with_role,
    _get_user_task_with_role,
    create_task_manually,
    get_task_cancel_impact,
    request_changes,
)
from app.admin_task_contracts import CreateTaskPayload
from app.admin_task_mutations import modify_task_apply
from app.auth import get_current_user
from app.database import get_session
from app.models import Project, ProjectMessage, Task, TaskHistory, TaskStatus
from app.payload_schemas import AcceptanceEvidence, TaskType
from app.routers.admin_notifications import _MessageResolve, resolve_message
from app.routers.admin_project_strategic_session import (
    StrategicResidentSessionStart,
    StrategicSessionMessageCreate,
    create_project_strategic_session_message,
    get_project_strategic_conversation,
    start_project_strategic_resident_session,
)
from app.routers.agent_access import _bounded_agent_section
from app.routers.directions import (
    COMPLETION_CRITERIA_MAX,
    DIRECTION_TEXT_MAX,
    _CreateMessage,
    _DirectionPayload,
    _remove_direction_impl,
    _update_direction_impl,
    add_direction,
    create_message,
    trigger_strategic_cycle,
)
from app.shared_state import get_project_integration_branch
from app.task_capabilities import USER_LAUNCHABLE_TASK_TYPES, task_capability_contract


router = APIRouter(
    prefix="/api/operator-access/v1",
    tags=["operator-access"],
    dependencies=[Depends(get_current_user)],
)

_RETRYABLE_TASK_STATUSES = frozenset(
    {
        TaskStatus.FAILED,
        TaskStatus.CANCELLED,
        TaskStatus.BLOCKED,
        TaskStatus.EVALUATING,
        TaskStatus.PARKED,
    }
)
_CANCELLABLE_TASK_STATUSES = frozenset(
    {
        TaskStatus.QUEUED,
        TaskStatus.PROVISIONING,
        TaskStatus.IN_PROGRESS,
        TaskStatus.EVALUATING,
        TaskStatus.BLOCKED,
        TaskStatus.PARKED,
    }
)
_MESSAGE_TYPES = Literal["NOTE", "FEEDBACK", "BUG_REPORT", "QUESTION", "BLOCKER"]
_OPERATOR_BOUNDED_RESPONSE_MAX_BYTES = 48_000
_DirectionId = Annotated[
    str,
    StringConstraints(
        strip_whitespace=True,
        min_length=1,
        max_length=100,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9_.:-]*$",
    ),
]
_DirectionPath = Annotated[str, Field(min_length=1, max_length=500)]


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class OperatorEmptyBody(_StrictModel):
    """Explicitly reject caller-supplied authority on bodyless actions."""


class OperatorTaskCreate(_StrictModel):
    title: str = Field(min_length=1, max_length=200)
    prompt: str = Field(min_length=1, max_length=50_000)
    task_type: TaskType = TaskType.create
    pr_number: int | None = Field(default=None, ge=1)
    branch: str | None = Field(
        default=None,
        min_length=1,
        max_length=255,
        pattern=r"^[a-zA-Z0-9_./-]*[a-zA-Z0-9_.-]$",
    )
    base_branch: str | None = Field(
        default=None,
        min_length=1,
        max_length=255,
        pattern=r"^[a-zA-Z0-9_./-]*[a-zA-Z0-9_.-]$",
    )
    idempotency_key: str | None = Field(
        default=None, max_length=255, pattern=r"^[a-zA-Z0-9_.-]+$"
    )
    direction_id: _DirectionId | None = None
    criterion_ids: list[str] | None = Field(default=None, max_length=50)
    depends_on: list[uuid.UUID] = Field(default_factory=list, max_length=50)
    require_deps_landed_on: str | None = Field(
        default=None,
        min_length=1,
        max_length=255,
        pattern=r"^[a-zA-Z0-9_./-]*[a-zA-Z0-9_.-]$",
    )
    release_base_branch: str | None = Field(
        default=None,
        min_length=1,
        max_length=255,
        pattern=r"^[a-zA-Z0-9_./-]*[a-zA-Z0-9_.-]$",
    )
    acceptance_evidence: AcceptanceEvidence | None = None
    work_class: Literal["exploratory", "confirmatory", "productization"] | None = None
    stop_or_pivot_condition: str | None = Field(
        default=None, min_length=1, max_length=4_000
    )

    @model_validator(mode="after")
    def reject_unscoped_task_authority(self) -> OperatorTaskCreate:
        if self.task_type == TaskType.migrate:
            raise ValueError("task_type 'migrate' is outside project-operator scope")
        if self.task_type.value not in USER_LAUNCHABLE_TASK_TYPES:
            raise ValueError(f"task_type '{self.task_type.value}' is not operator-launchable")
        empirical_fields_present = any(
            value is not None
            for value in (self.work_class, self.stop_or_pivot_condition)
        )
        if empirical_fields_present and self.task_type not in (
            TaskType.research,
            TaskType.web_research,
        ):
            raise ValueError(
                "empirical work contract fields are only supported for research "
                "and web_research tasks"
            )
        if self.task_type in (TaskType.review, TaskType.fix) and self.pr_number is None:
            raise ValueError(
                f"pr_number is required for task_type '{self.task_type.value}'"
            )
        if self.task_type in (TaskType.review, TaskType.fix) and not self.branch:
            raise ValueError(
                f"branch is required for task_type '{self.task_type.value}'"
            )
        if self.criterion_ids is not None:
            normalized: list[str] = []
            for raw_key in self.criterion_ids:
                if not isinstance(raw_key, str) or not raw_key.strip():
                    raise ValueError(
                        "criterion_ids entries must be non-empty strings"
                    )
                key = raw_key.strip()
                if len(key) > 64:
                    raise ValueError(
                        "criterion_ids entries must be at most 64 characters"
                    )
                if key in normalized:
                    raise ValueError(
                        f"criterion_ids contains duplicate key {key!r}"
                    )
                normalized.append(key)
            self.criterion_ids = normalized
            if normalized and (
                self.acceptance_evidence is None
                or self.acceptance_evidence.kind != "named_test"
            ):
                raise ValueError(
                    "criterion_ids require named_test acceptance_evidence"
                )
        return self


class OperatorTaskRequeue(_StrictModel):
    guidance: str = Field(min_length=1, max_length=5_000)


class OperatorTaskCancel(_StrictModel):
    reason: str | None = Field(default=None, min_length=1, max_length=2_000)
    confirm_cascade: bool = False


class OperatorRequestChanges(_StrictModel):
    feedback: str = Field(min_length=1, max_length=5_000)
    action: Literal["fix", "review"] = "fix"


class OperatorDirectionCreate(_StrictModel):
    text: str = Field(min_length=1, max_length=DIRECTION_TEXT_MAX)
    priority: Literal["high", "medium", "low"] = "medium"
    paths: list[_DirectionPath] = Field(default_factory=list, max_length=50)
    completion_criteria: str | None = Field(
        default=None, max_length=COMPLETION_CRITERIA_MAX
    )
    staged: bool = False
    idempotency_key: str | None = Field(default=None, min_length=1, max_length=255)


class OperatorDirectionUpdate(_StrictModel):
    text: str | None = Field(
        default=None, min_length=1, max_length=DIRECTION_TEXT_MAX
    )
    priority: Literal["high", "medium", "low"] | None = None
    paths: list[_DirectionPath] | None = Field(default=None, max_length=50)
    completion_criteria: str | None = Field(
        default=None, max_length=COMPLETION_CRITERIA_MAX
    )

    @model_validator(mode="after")
    def require_change(self) -> OperatorDirectionUpdate:
        if not self.model_fields_set:
            raise ValueError("at least one direction field is required")
        return self


class OperatorDirectionRetarget(_StrictModel):
    version: int = Field(ge=1)


class OperatorMessageCreate(_StrictModel):
    content: str = Field(min_length=1, max_length=50_000)
    message_type: _MESSAGE_TYPES = "NOTE"
    idempotency_key: str | None = Field(default=None, min_length=1, max_length=255)


class OperatorMessageAnswer(_StrictModel):
    resolution: str = Field(min_length=1, max_length=50_000)


class OperatorStrategicCycle(_StrictModel):
    phase: Literal["design_doc", "strategic_review"] = "design_doc"


class OperatorStrategicMessage(_StrictModel):
    content: str = Field(min_length=1, max_length=50_000)
    message_type: _MESSAGE_TYPES = "NOTE"
    idempotency_key: str = Field(min_length=1, max_length=255)
    reply_to_message_id: uuid.UUID | None = None


def _ensure_project_accepts_work(project: Project) -> None:
    if project.archived:
        raise HTTPException(status_code=409, detail="Archived project cannot start new work")


async def _member_project(
    project_id: uuid.UUID, current_user: dict, session: AsyncSession
) -> Project:
    return await _get_user_project_with_role(
        project_id, current_user["user_id"], session, "member"
    )


async def _member_task(
    task_id: uuid.UUID, current_user: dict, session: AsyncSession
) -> Task:
    return await _get_user_task_with_role(
        task_id, current_user["user_id"], session, "member"
    )


async def _reject_cross_project_task_children(
    task: Task, session: AsyncSession
) -> None:
    foreign_child = (
        await session.execute(
            select(Task.id)
            .where(
                Task.parent_task_id == task.id,
                Task.project_id != task.project_id,
            )
            .limit(1)
        )
    ).scalar_one_or_none()
    if foreign_child is not None:
        raise HTTPException(
            status_code=409,
            detail="Task has an invalid cross-project child; operation refused",
        )


def _bounded_operator_payload(value: Any, *, schema_version: str) -> dict[str, Any]:
    projected, truncated, redactions = _bounded_agent_section(
        value,
        "failure_evidence",
        serialized_strings=True,
    )
    response = {
        "schema_version": schema_version,
        "complete": not truncated,
        "limits": {
            "response_bytes": _OPERATOR_BOUNDED_RESPONSE_MAX_BYTES,
            "data_value_budget_bytes": 40_000,
        },
        "truncation": {"response_bytes_or_fields": truncated},
        "redaction": {
            "applied": redactions > 0,
            "fields_or_values": redactions,
            "policy": "credential fields and credential-like values are removed",
        },
        "data": projected,
    }
    serialized_bytes = len(
        json.dumps(
            response, separators=(",", ":"), ensure_ascii=False, default=str
        ).encode("utf-8")
    )
    if serialized_bytes > _OPERATOR_BOUNDED_RESPONSE_MAX_BYTES:
        response.update(
            {
                "complete": False,
                "truncation": {"response_bytes_or_fields": True},
                "redaction": {
                    "applied": True,
                    "fields_or_values": redactions,
                    "policy": "response omitted to enforce the hard byte budget",
                },
                "data": None,
            }
        )
    return response


def _operator_task_receipt(value: Any) -> dict[str, Any]:
    source = value if isinstance(value, dict) else {}
    return {
        key: source.get(key)
        for key in (
            "id",
            "project_id",
            "title",
            "status",
            "task_type",
            "attempt_count",
            "requires_approval",
            "is_duplicate",
            "idempotent",
            "direction_id",
            "criterion_ids",
            "criterion_ids_explicit",
            "criterion_release_version_id",
            "base_branch",
            "depends_on",
            "require_deps_landed_on",
            "release_base_branch",
        )
        if key in source
    }


def _operator_cancel_impact(value: Any) -> dict[str, Any]:
    source = value if isinstance(value, dict) else {}
    return {
        key: source.get(key)
        for key in (
            "task_id",
            "status",
            "active_child_cascade_count",
            "active_child_cascade_statuses",
            "waiting_dependent_count",
            "waiting_dependent_statuses",
            "target_has_vm_id",
            "target_vm_provisioned",
            "active_child_vm_id_count",
            "active_child_vm_provisioned_count",
            "orphan_pr_cleanup_context_reference_count",
            "pull_request_artifact_task_count",
        )
        if key in source
    }


def _operator_direction_receipt(value: Any) -> dict[str, Any]:
    source = value if isinstance(value, dict) else {}
    direction = source.get("direction")
    direction = direction if isinstance(direction, dict) else {}
    receipt = {
        "direction": {
            key: direction.get(key)
            for key in (
                "id",
                "text",
                "priority",
                "status",
                "paths",
                "completion_criteria",
                "staged_for_version",
                "staged_at",
                "added_at",
            )
            if key in direction
        }
    }
    if "status" in source:
        receipt["status"] = source.get("status")
    return receipt


def _operator_message_receipt(
    value: Any, *, project_id: uuid.UUID
) -> dict[str, Any]:
    source = value if isinstance(value, dict) else {}
    return {
        key: source.get(key)
        for key in (
            "id",
            "status",
            # ``arming`` is the server's verdict on whether ANYTHING will read
            # this message (see strategic_trigger.maybe_arm_operator_refresh).
            # ``not_armed`` means the steer was durably stored but no strategic
            # cycle will ever consume it. Dropping it here made the operator CLI
            # structurally unable to distinguish a steer that reached the engine
            # from one that silently went nowhere, so it must stay in the
            # receipt: an operator message is never accepted into an unread
            # lane without the reason travelling back with it.
            "arming",
            "reply_message_id",
            "wakeup_state",
            "input_revision",
        )
        if key in source
    } | {"project_id": str(project_id)}


@router.get("/task-capabilities")
async def get_operator_task_capabilities() -> dict[str, Any]:
    contract = task_capability_contract()
    allowed = {
        name: capability
        for name, capability in contract.items()
        if capability.get("user_launchable") and name != TaskType.migrate.value
    }
    return {"task_capabilities": allowed}


@router.post("/projects/{project_id}/tasks", status_code=201)
async def create_operator_task(
    request: Request,
    project_id: uuid.UUID,
    payload: OperatorTaskCreate,
    session: AsyncSession = Depends(get_session),
    current_user: dict = Depends(get_current_user),
):
    project = await _member_project(project_id, current_user, session)
    _ensure_project_accepts_work(project)
    github_owner = str(project.github_owner or "").strip()
    github_repo = str(project.github_repo or "").strip()
    if not github_owner or not github_repo:
        raise HTTPException(status_code=409, detail="Project repository is not configured")
    integration_branch = get_project_integration_branch(project)
    for field_name in (
        "base_branch",
        "require_deps_landed_on",
        "release_base_branch",
    ):
        declared_branch = getattr(payload, field_name)
        if declared_branch is not None and declared_branch != integration_branch:
            raise HTTPException(
                status_code=400,
                detail=(
                    f"{field_name} must name the project integration branch "
                    f"{integration_branch!r}"
                ),
            )
    canonical_fields = payload.model_dump(exclude_none=True)
    canonical_fields["base_branch"] = payload.base_branch or integration_branch
    canonical = CreateTaskPayload(
        github_repo=f"{github_owner}/{github_repo}",
        project_id=project.id,
        **canonical_fields,
    )
    result = await create_task_manually(
        request=request,
        payload=canonical,
        session=session,
        current_user=current_user,
    )
    return _operator_task_receipt(result)


@router.post("/tasks/{task_id}/requeue")
async def requeue_operator_task(
    task_id: uuid.UUID,
    payload: OperatorTaskRequeue,
    background_tasks: BackgroundTasks,
    session: AsyncSession = Depends(get_session),
    current_user: dict = Depends(get_current_user),
):
    task = await _member_task(task_id, current_user, session)
    await _reject_cross_project_task_children(task, session)
    project = await session.get(Project, task.project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="Project not found")
    _ensure_project_accepts_work(project)
    if task.status not in _RETRYABLE_TASK_STATUSES:
        raise HTTPException(
            status_code=409,
            detail=f"Task status {task.status} is not retryable by project-operator",
        )
    result = await modify_task_apply(
        task,
        {"status": TaskStatus.QUEUED, "requeue_prompt_suffix": payload.guidance},
        background_tasks,
        session,
        force_kill=False,
    )
    return _operator_task_receipt(result)


@router.get("/tasks/{task_id}/cancel-impact")
async def get_operator_task_cancel_impact(
    task_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    current_user: dict = Depends(get_current_user),
):
    task = await _member_task(task_id, current_user, session)
    await _reject_cross_project_task_children(task, session)
    impact = await get_task_cancel_impact(
        task_id=str(task.id), session=session, current_user=current_user
    )
    return _operator_cancel_impact(impact)


@router.post("/tasks/{task_id}/cancel")
async def cancel_operator_task(
    task_id: uuid.UUID,
    payload: OperatorTaskCancel,
    background_tasks: BackgroundTasks,
    session: AsyncSession = Depends(get_session),
    current_user: dict = Depends(get_current_user),
):
    task = await _member_task(task_id, current_user, session)
    if task.status == TaskStatus.CANCELLED:
        return {
            "id": str(task.id),
            "project_id": str(task.project_id),
            "status": TaskStatus.CANCELLED,
            "idempotent": True,
        }
    if task.status not in _CANCELLABLE_TASK_STATUSES:
        raise HTTPException(
            status_code=409,
            detail=f"Task status {task.status} cannot be cancelled by project-operator",
        )
    await _reject_cross_project_task_children(task, session)
    raw_impact = await get_task_cancel_impact(
        task_id=str(task.id), session=session, current_user=current_user
    )
    impact = _operator_cancel_impact(raw_impact)
    cascade_count = int(impact.get("active_child_cascade_count") or 0) + int(
        impact.get("waiting_dependent_count") or 0
    )
    if cascade_count and not payload.confirm_cascade:
        raise HTTPException(
            status_code=409,
            detail={
                "error": "cancel_cascade_confirmation_required",
                "cascade_count": cascade_count,
                "impact": impact,
            },
        )
    result = await modify_task_apply(
        task,
        {"status": TaskStatus.CANCELLED},
        background_tasks,
        session,
        force_kill=False,
    )
    if payload.reason:
        session.add(
            TaskHistory(
                task_id=task.id,
                action="PROJECT_OPERATOR_CANCEL_REASON",
                details={
                    "reason": payload.reason,
                    "actor_user_id": current_user["user_id"],
                    "service_credential": current_user.get("service_credential"),
                },
            )
        )
        await session.commit()
    return {**_operator_task_receipt(result), "cancel_impact": impact}


@router.post("/tasks/{task_id}/request-changes")
async def request_operator_task_changes(
    task_id: uuid.UUID,
    payload: OperatorRequestChanges,
    session: AsyncSession = Depends(get_session),
    current_user: dict = Depends(get_current_user),
):
    task = await _member_task(task_id, current_user, session)
    project = await session.get(Project, task.project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="Project not found")
    _ensure_project_accepts_work(project)
    return await request_changes(
        task_id=task_id,
        payload=RequestChangesPayload(**payload.model_dump()),
        session=session,
        current_user=current_user,
    )


@router.post("/projects/{project_id}/directions")
async def create_operator_direction(
    project_id: uuid.UUID,
    payload: OperatorDirectionCreate,
    session: AsyncSession = Depends(get_session),
    current_user: dict = Depends(get_current_user),
):
    project = await _member_project(project_id, current_user, session)
    _ensure_project_accepts_work(project)
    canonical = _DirectionPayload(**payload.model_dump())
    result = await add_direction(
        project_id=project_id,
        payload=canonical,
        session=session,
        current_user=current_user,
    )
    return _operator_direction_receipt(result)


@router.patch("/projects/{project_id}/directions/{direction_id}")
async def update_operator_direction(
    project_id: uuid.UUID,
    direction_id: _DirectionId,
    payload: OperatorDirectionUpdate,
    session: AsyncSession = Depends(get_session),
    current_user: dict = Depends(get_current_user),
):
    project = await _member_project(project_id, current_user, session)
    _ensure_project_accepts_work(project)
    result = await _update_direction_impl(
        project_id,
        direction_id,
        payload.model_dump(exclude_unset=True),
        session=session,
        current_user=current_user,
        required_current_status="staged",
    )
    return _operator_direction_receipt(result)


@router.post("/projects/{project_id}/directions/{direction_id}/activate")
async def activate_operator_direction(
    project_id: uuid.UUID,
    direction_id: _DirectionId,
    payload: OperatorEmptyBody,
    session: AsyncSession = Depends(get_session),
    current_user: dict = Depends(get_current_user),
):
    project = await _member_project(project_id, current_user, session)
    _ensure_project_accepts_work(project)
    result = await _update_direction_impl(
        project_id,
        direction_id,
        {"status": "active"},
        session=session,
        current_user=current_user,
        required_current_status="staged",
    )
    return _operator_direction_receipt(result)


@router.post("/projects/{project_id}/directions/{direction_id}/retarget")
async def retarget_operator_direction(
    project_id: uuid.UUID,
    direction_id: _DirectionId,
    payload: OperatorDirectionRetarget,
    session: AsyncSession = Depends(get_session),
    current_user: dict = Depends(get_current_user),
):
    project = await _member_project(project_id, current_user, session)
    _ensure_project_accepts_work(project)
    result = await _update_direction_impl(
        project_id,
        direction_id,
        {"staged_for_version": payload.version},
        session=session,
        current_user=current_user,
        required_current_status="staged",
    )
    return _operator_direction_receipt(result)


@router.post("/projects/{project_id}/directions/{direction_id}/cancel")
async def cancel_operator_direction(
    project_id: uuid.UUID,
    direction_id: _DirectionId,
    payload: OperatorEmptyBody,
    session: AsyncSession = Depends(get_session),
    current_user: dict = Depends(get_current_user),
):
    result = await _remove_direction_impl(
        project_id,
        direction_id,
        session=session,
        current_user=current_user,
        required_current_status="staged",
        cancel_queued_tasks=False,
    )
    return result


@router.post("/projects/{project_id}/messages")
async def create_operator_message(
    project_id: uuid.UUID,
    payload: OperatorMessageCreate,
    session: AsyncSession = Depends(get_session),
    current_user: dict = Depends(get_current_user),
):
    project = await _member_project(project_id, current_user, session)
    _ensure_project_accepts_work(project)
    canonical = _CreateMessage(
        content=payload.content,
        message_type=payload.message_type,
        idempotency_key=payload.idempotency_key,
    )
    result = await create_message(
        project_id=str(project_id),
        payload=canonical,
        session=session,
        user=current_user,
    )
    return _operator_message_receipt(result, project_id=project.id)


@router.post("/projects/{project_id}/messages/{message_id}/answer")
async def answer_operator_message(
    project_id: uuid.UUID,
    message_id: uuid.UUID,
    payload: OperatorMessageAnswer,
    session: AsyncSession = Depends(get_session),
    current_user: dict = Depends(get_current_user),
):
    project = await _member_project(project_id, current_user, session)
    _ensure_project_accepts_work(project)
    message = await session.get(ProjectMessage, message_id)
    if message is None or message.project_id != project.id:
        raise HTTPException(status_code=404, detail="Message not found")
    result = await resolve_message(
        message_id=message_id,
        payload=_MessageResolve(resolution=payload.resolution),
        session=session,
        current_user=current_user,
    )
    return _operator_message_receipt(result, project_id=project.id)


@router.post("/projects/{project_id}/strategic-cycle")
async def start_operator_strategic_cycle(
    project_id: uuid.UUID,
    payload: OperatorStrategicCycle,
    session: AsyncSession = Depends(get_session),
    current_user: dict = Depends(get_current_user),
):
    project = await _member_project(project_id, current_user, session)
    _ensure_project_accepts_work(project)
    return await trigger_strategic_cycle(
        project_id=project_id,
        phase=payload.phase,
        session=session,
        current_user=current_user,
    )


@router.post("/projects/{project_id}/strategic/start")
async def start_operator_strategic(
    request: Request,
    project_id: uuid.UUID,
    payload: OperatorEmptyBody,
    session: AsyncSession = Depends(get_session),
    current_user: dict = Depends(get_current_user),
):
    project = await _member_project(project_id, current_user, session)
    _ensure_project_accepts_work(project)
    result = await start_project_strategic_resident_session(
        request=request,
        project_id=project_id,
        payload=StrategicResidentSessionStart(),
        session=session,
        current_user=current_user,
    )
    return _bounded_operator_payload(
        result, schema_version="blueprint.project-operator.strategic-start.v1"
    )


@router.post("/projects/{project_id}/strategic/messages")
async def continue_operator_strategic(
    request: Request,
    project_id: uuid.UUID,
    payload: OperatorStrategicMessage,
    session: AsyncSession = Depends(get_session),
    current_user: dict = Depends(get_current_user),
):
    project = await _member_project(project_id, current_user, session)
    _ensure_project_accepts_work(project)
    canonical = StrategicSessionMessageCreate(
        content=payload.content,
        message_type=payload.message_type,
        idempotency_key=payload.idempotency_key,
        reply_to_message_id=payload.reply_to_message_id,
    )
    result = await create_project_strategic_session_message(
        request=request,
        project_id=project_id,
        payload=canonical,
        session=session,
        current_user=current_user,
    )
    return _operator_message_receipt(result, project_id=project.id)


@router.get("/projects/{project_id}/strategic-conversation")
async def get_operator_strategic_conversation(
    request: Request,
    project_id: uuid.UUID,
    after_sequence: int | None = Query(default=None, ge=0),
    limit: int = Query(default=50, ge=1, le=50),
    session: AsyncSession = Depends(get_session),
    current_user: dict = Depends(get_current_user),
):
    response = Response()
    result = await get_project_strategic_conversation(
        request=request,
        project_id=project_id,
        response=response,
        include_message_id=None,
        before_sequence=None,
        after_sequence=after_sequence,
        limit=limit,
        idempotency_key=None,
        client_idempotency_key=None,
        search=None,
        if_none_match=None,
        session=session,
        current_user=current_user,
    )
    if isinstance(result, Response):
        raise HTTPException(status_code=502, detail="Unexpected empty Strategic response")
    return _bounded_operator_payload(
        result, schema_version="blueprint.project-operator.strategic-conversation.v1"
    )

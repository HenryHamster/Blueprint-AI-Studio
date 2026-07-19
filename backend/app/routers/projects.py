"""Project CRUD, deploy, WebSocket, and status endpoints."""

import asyncio
import hmac
import json
import logging
import time
import uuid
from typing import Optional

from fastapi import (
    APIRouter,
    BackgroundTasks,
    Depends,
    HTTPException,
    Query,
    Request,
    WebSocket,
    WebSocketDisconnect,
)
from sqlalchemy import or_
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from app.auth import (
    get_current_user,
    verify_agent_jwt,
    create_ws_ticket,
    verify_ws_ticket,
)
from app.database import get_session, AsyncSessionLocal
from app.models import (
    Artifact,
    ArtifactType,
    Project,
    ProjectMember,
    ProjectStatus,
    Task,
    TaskStatus,
    get_task_total_vm_cost_usd_or_none,
)
from app.max_plan_pool import filter_user_task_context
from app.payload_schemas import validate_task_payload
from app.task_capabilities import BACKEND_MANAGED_TASK_TYPES
from app.research_completion import resolve_research_findings_for_surface
from app.schemas import DeployPayload
from app.shared_state import backfill_branch_columns, limiter
from app.broadcast import manager, broadcast_to_project, _sanitize_log
from app.event_fanout import enqueue_agent_log
from app.strategic_events import emit_strategic_session_delta
from app.strategic_resident_agent_binding import RESIDENT_CLAIM_NONCE_CONTEXT_KEY

logger = logging.getLogger(__name__)

_AGENT_WS_TERMINAL_STATUSES = (
    TaskStatus.COMPLETED,
    TaskStatus.FAILED,
    TaskStatus.CANCELLED,
    TaskStatus.BLOCKED,
    # PARKED releases the agent VM/JWT immediately (like BLOCKED), so the agent
    # websocket should close — there is no live agent to relay to.
    TaskStatus.PARKED,
)


def _agent_ws_binding_invalid(task: Task | None, payload: dict) -> bool:
    """Return true when a live WebSocket no longer matches its task claim."""

    if task is None or task.status in _AGENT_WS_TERMINAL_STATUSES:
        return True
    token_vm = payload.get("vm_instance_id")
    if token_vm and task.vm_instance_id and task.vm_instance_id != token_vm:
        return True
    if payload.get("task_type") != "strategic_resident_turn":
        return False
    persisted_nonce = str(
        (task.context or {}).get(RESIDENT_CLAIM_NONCE_CONTEXT_KEY) or ""
    )
    presented_nonce = str(payload.get("claim_nonce") or "")
    return (
        not persisted_nonce
        or not presented_nonce
        or not hmac.compare_digest(presented_nonce, persisted_nonce)
    )


def _truncate_vm_instance_id(vm_instance_id: object) -> str:
    if vm_instance_id is None:
        return "none"
    value = str(vm_instance_id)
    if len(value) <= 12:
        return value
    return f"{value[:12]}..."


def _log_agent_auth_reject(
    reason: str,
    *,
    path: str,
    task_id: object,
    token_vm_instance_id: object = None,
    token_project_id: object = None,
    error: object | None = None,
) -> None:
    logger.info(
        "[AGENT_AUTH_REJECT] reason=%s path=%s task_id=%s token_vm_instance_id=%s token_project_id=%s error=%s",
        reason,
        path,
        task_id,
        _truncate_vm_instance_id(token_vm_instance_id),
        token_project_id or "none",
        error if error is not None else "none",
    )


async def _safe_ws_close(ws: WebSocket, code: int, reason: str = "") -> None:
    """Close a WebSocket, tolerating the double-close race.

    starlette raises two distinct messages for already-closed sockets:
      * ``RuntimeError('Unexpected ASGI message "websocket.close",
        after sending "websocket.close"')`` — peer hung up mid-auth and
        ``receive_json``'s WebSocketDisconnect silently sent a close frame.
      * ``RuntimeError('Cannot call "send" once a close message has been
        sent.')`` — produced when the underlying send loop has already
        finalised; surfaced 2026-05-04 from the 90s idle-timeout path
        (projects.py:341) when the socket was already half-closed.

    A third shape surfaced 2026-06-14 from the agent auth-handshake abort path
    (peer sends ``WebSocketDisconnect 1006`` during ``receive_json``): the
    follow-up ``ws.close()`` reaches uvicorn's ``asgi_send`` ->
    ``websockets.legacy.protocol`` on an already-torn-down transport and raises
    a bare ``AttributeError`` (no message to match on). Cosmetic for the same
    reason — the socket is gone — so swallow it too.

    Both are cosmetic for our purposes — the connection is gone either
    way, and the finally block will still run ``manager.disconnect``.
    Any other RuntimeError is real and re-raised.
    """
    try:
        await ws.close(code=code, reason=reason)
    except RuntimeError as exc:
        msg = str(exc)
        if "websocket.close" in msg or "close message has been sent" in msg:
            return
        raise
    except AttributeError:
        # Closing an already-finalised socket; the transport attrs the close
        # frame would touch are gone. The connection is already dead.
        return


router = APIRouter(tags=["projects"])


@router.post("/api/deploy")
@limiter.limit("5/minute")
async def deploy_blueprint(
    request: Request,
    payload: DeployPayload,
    background_tasks: BackgroundTasks,
    session: AsyncSession = Depends(get_session),
    current_user: dict = Depends(get_current_user),
):
    # 1. Create a new Project
    project = Project(
        name=payload.name,
        user_id=current_user["user_id"],
        status=ProjectStatus.DEPLOYING,
        github_owner=payload.github_owner,
        github_repo=payload.github_repo,
    )
    session.add(project)
    await session.flush()  # Assign project.id without committing

    # Auto-add owner membership
    session.add(
        ProjectMember(
            project_id=project.id, user_id=current_user["user_id"], role="owner"
        )
    )

    # 2. Create Tasks based on the flexible payloads
    created_tasks = []

    # Create a parent task for the entire deployment
    parent_task = Task(
        project_id=project.id,
        title=f"Deploy {payload.name}",
        description="Parent task orchestrating the deployment.",
        status=TaskStatus.IN_PROGRESS,
        created_by="USER",
        launched_by_user_id=current_user["user_id"],
    )
    session.add(parent_task)
    await session.flush()  # Assign parent_task.id without committing

    for p in payload.payloads:
        try:
            p = validate_task_payload(p)
        except ValueError as exc:
            logger.warning(
                "Validation error in task payload: %s: %s", type(exc).__name__, exc
            )
            raise HTTPException(status_code=422, detail="Invalid request")
        # GCP instance names must match a specific regex (lowercase, hyphens, numbers)
        vm_name = f"agent-vm-{uuid.uuid4().hex}"

        task = Task(
            project_id=project.id,
            title=p.get("title", "Untitled Subtask"),
            description=p.get("description", ""),
            parent_task_id=parent_task.id,
            payloads=[p],
            vm_instance_id=vm_name,
            status=TaskStatus.QUEUED,
            created_by="ORCHESTRATOR",
            launched_by_user_id=current_user["user_id"],
        )
        session.add(task)
        created_tasks.append(task)

    from app.strategic_resident_checkpointing import initialize_strategic_resident

    await initialize_strategic_resident(
        session, project.id, locked_project=project
    )
    await session.commit()

    # Populate branch columns from GitHub immediately so that the first task
    # spawned via this path resolves the correct base branch rather than
    # silently falling back to 'main' for repos with a different default.
    await backfill_branch_columns(session, project_id=project.id)
    await session.commit()

    # 3. The Orchestrator Worker will automatically pick up QUEUED tasks
    # and provision the VMs in the background.

    return {
        "status": "accepted",
        "project_id": project.id,
        "message": f"Project created. {len(created_tasks)} tasks queued for provisioning.",
    }


# --- Frontend Read APIs ---


@router.get("/api/projects")
@limiter.limit("30/minute")
async def list_projects(
    request: Request,
    limit: int = Query(50, ge=1, le=100),
    offset: int = Query(0, ge=0),
    status: Optional[ProjectStatus] = None,
    session: AsyncSession = Depends(get_session),
    current_user: dict = Depends(get_current_user),
):
    """
    List all projects for the authenticated user.
    Select only lightweight columns — large JSONB columns (directions, vision,
    design_doc, problem_model, self_reflection_log, strategic_memory_notes) and
    running_update (Text) are excluded here and available via the single-project
    GET endpoint.
    """
    stmt = select(
        Project.id,
        Project.name,
        Project.created_at,
        Project.status,
        Project.user_id,
        Project.github_owner,
        Project.github_repo,
        Project.is_self_repo,
        Project.autonomous_enabled,
        Project.archived,
        Project.build_config,
    ).where(
        Project.id.in_(
            select(ProjectMember.project_id).where(
                ProjectMember.user_id == current_user["user_id"]
            )
        )
    )
    if status:
        stmt = stmt.where(Project.status == status)

    stmt = stmt.order_by(Project.created_at.desc()).offset(offset).limit(limit)
    rows = (await session.execute(stmt)).all()
    projects = [row._asdict() for row in rows]
    return {
        "projects": projects,
        "pagination": {"limit": limit, "offset": offset, "count": len(projects)},
    }


@router.get("/api/projects/{project_id}")
@limiter.limit("60/minute")
async def get_project(
    request: Request,
    project_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    current_user: dict = Depends(get_current_user),
):
    """Retrieve project metadata and configuration.

    Requires project membership. Returns the full Project object including
    name, repository details, and configuration.

    Path Parameters:
        project_id: UUID of the project to retrieve.

    Returns:
        Project: Full project details (name, github_owner, github_repo, etc.)

    Raises:
        HTTPException(404): Project not found or user lacks membership.
    """
    # Check membership
    member = (
        await session.execute(
            select(ProjectMember).where(
                ProjectMember.project_id == project_id,
                ProjectMember.user_id == current_user["user_id"],
            )
        )
    ).scalar_one_or_none()
    if not member:
        raise HTTPException(status_code=404, detail="Project not found or unauthorized")

    project = await session.get(Project, project_id)
    if not project:
        raise HTTPException(status_code=404, detail="Project not found or unauthorized")

    return project


@router.get("/api/projects/{project_id}/tasks")
@limiter.limit("60/minute")
async def get_project_tasks(
    request: Request,
    project_id: uuid.UUID,
    limit: int = Query(50, ge=1, le=100),
    offset: int = Query(0, ge=0),
    status: Optional[TaskStatus] = None,
    direction_id: Optional[str] = None,
    task_type: Optional[str] = None,
    include_backend_managed: bool = Query(False, include_in_schema=False),
    session: AsyncSession = Depends(get_session),
    current_user: dict = Depends(get_current_user),
):
    """Retrieve paginated tasks with artifacts and computed metrics.

    Lists all tasks for a project with optional filtering by status, direction,
    and task type. Fetches associated artifacts (PRs, commits, logs, research
    findings) and computes VM duration and cost estimates.

    Path Parameters:
        project_id: UUID of the project.

    Query Parameters:
        limit: Number of tasks to return (1-100, default 50).
        offset: Pagination offset (default 0).
        status: Filter by TaskStatus (QUEUED, IN_PROGRESS, COMPLETED, etc.).
        direction_id: Filter tasks belonging to a specific research direction.
        task_type: Filter by task type from first payload (create, refactor, etc.).

    Returns:
        {
            "tasks": [
                {
                    "id": str, "title": str, "status": TaskStatus,
                    "task_type": str, "vm_instance_id": str,
                    "payloads": list, "created_at": datetime,
                    "parent_task_id": str | null, "context": dict,
                    "artifacts": list[Artifact], "research_findings": list | null,
                    "duration_seconds": float | null, "estimated_cost_usd": float | null
                }
            ],
            "pagination": {"limit": int, "offset": int, "count": int}
        }

    Rate limit: 60 requests/minute.
    """
    # Verify project membership
    member = (
        await session.execute(
            select(ProjectMember).where(
                ProjectMember.project_id == project_id,
                ProjectMember.user_id == current_user["user_id"],
            )
        )
    ).scalar_one_or_none()
    if not member:
        raise HTTPException(status_code=404, detail="Project not found or unauthorized")
    project = await session.get(Project, project_id)
    if not project:
        raise HTTPException(status_code=404, detail="Project not found or unauthorized")

    # Fetch tasks
    tasks_stmt = select(Task).where(Task.project_id == project_id)
    if not include_backend_managed:
        task_type_expr = Task.payloads[0]["type"].as_string()
        tasks_stmt = tasks_stmt.where(
            or_(
                task_type_expr.is_(None),
                task_type_expr.not_in(sorted(BACKEND_MANAGED_TASK_TYPES)),
            )
        )
    if status:
        tasks_stmt = tasks_stmt.where(Task.status == status)
    if direction_id:
        tasks_stmt = tasks_stmt.where(
            Task.context["direction_id"].as_string() == direction_id
        )
    if task_type:
        tasks_stmt = tasks_stmt.where(Task.payloads[0]["type"].as_string() == task_type)

    tasks_stmt = tasks_stmt.order_by(Task.created_at.asc()).offset(offset).limit(limit)
    tasks = (await session.execute(tasks_stmt)).scalars().all()

    # Fetch artifacts for these tasks
    task_ids = [t.id for t in tasks]
    artifacts = []
    if task_ids:
        artifacts_stmt = (
            select(Artifact).where(Artifact.task_id.in_(task_ids)).limit(2000)
        )
        artifacts = (await session.execute(artifacts_stmt)).scalars().all()
        if len(artifacts) == 2000:
            logger.warning(
                "get_project_tasks: artifact fetch hit 2000-row limit for task_ids=%s; results may be truncated",
                task_ids,
            )

    # Group artifacts by task_id
    artifacts_by_task = {}
    research_map = {}
    for a in artifacts:
        if a.task_id not in artifacts_by_task:
            artifacts_by_task[a.task_id] = []
        artifacts_by_task[a.task_id].append(a)
        if a.artifact_type == ArtifactType.RESEARCH_FINDINGS and a.logs:
            research_map[a.task_id] = a.logs[:100000]

    # Combine
    result = []
    for t in tasks:
        if t.vm_provisioned_at and t.vm_stopped_at:
            duration_seconds = (t.vm_stopped_at - t.vm_provisioned_at).total_seconds()
        else:
            duration_seconds = None
        estimated_cost_usd = get_task_total_vm_cost_usd_or_none(t)
        task_dict = {
            "id": t.id,
            "title": t.title,
            "status": t.status,
            "task_type": (t.first_payload).get("type", "create"),
            "vm_instance_id": t.vm_instance_id,
            "payloads": t.payloads,
            "created_at": t.created_at,
            "parent_task_id": str(t.parent_task_id) if t.parent_task_id else None,
            "context": filter_user_task_context(t.context),
            "artifacts": artifacts_by_task.get(t.id, []),
            "research_findings": resolve_research_findings_for_surface(
                t.plan_output, research_map.get(t.id)
            )
            if (t.first_payload).get("type") == "research"
            else None,
            "duration_seconds": duration_seconds,
            "estimated_cost_usd": estimated_cost_usd,
        }
        result.append(task_dict)

    return {
        "tasks": result,
        "pagination": {"limit": limit, "offset": offset, "count": len(result)},
    }


@router.post("/api/projects/{project_id}/ws-ticket")
@limiter.limit("30/minute")
async def generate_ws_ticket(
    request: Request,
    project_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    current_user: dict = Depends(get_current_user),
):
    """Generate a WebSocket authentication ticket.

    Creates a short-lived JWT token that grants WebSocket access to a project's
    real-time log stream. The ticket is single-use and expires after initial
    validation.

    Path Parameters:
        project_id: UUID of the project.

    Returns:
        {"ticket": str}: Signed JWT token for WebSocket auth.

    Rate limit: 30 requests/minute.
    """
    member = (
        await session.execute(
            select(ProjectMember).where(
                ProjectMember.project_id == project_id,
                ProjectMember.user_id == current_user["user_id"],
            )
        )
    ).scalar_one_or_none()
    if not member:
        raise HTTPException(status_code=404, detail="Project not found or unauthorized")

    ticket = create_ws_ticket(current_user["user_id"], str(project_id))
    return {"ticket": ticket}


@router.websocket("/api/projects/{project_id}/ws")
async def websocket_endpoint(websocket: WebSocket, project_id: str):
    """Establish a WebSocket connection for real-time project updates.

    Handles two-way communication with clients for live logs, status changes,
    and deployment state. Requires WebSocket ticket authentication.

    Path Parameters:
        project_id: UUID of the project to monitor.

    Authentication:
        First message must be JSON: {"ticket": "<ws_ticket>"}
        The ticket is obtained via POST /api/projects/{project_id}/ws-ticket

    Messages received by server:
        {"type": "ping"}: Keep-alive (returns pong)
        Other message types are logged but not processed.

    Messages sent to clients:
        {"event": "connected", "data": {"project_id": str}}
        {"event": "agent_log", "data": {"task_id": str, "log": str}}
        {"event": "deploy_state", "data": {...}}

    Idle timeout: 90 seconds (detects silent drops from load balancers)
    Message rate limit: 20 messages/second
    Max message size: 10 KB

    Closes with code 1008 on auth failure or membership loss.
    """
    await websocket.accept()

    try:
        auth_message = await asyncio.wait_for(websocket.receive_json(), timeout=5.0)
        ticket = auth_message.get("ticket")
        if not ticket:
            await _safe_ws_close(websocket, code=1008, reason="Missing ticket")
            return

        payload = verify_ws_ticket(ticket)
        if payload.get("project_id") != project_id:
            await _safe_ws_close(websocket, code=1008, reason="Project ID mismatch")
            return
    except asyncio.TimeoutError:
        await _safe_ws_close(websocket, code=1008, reason="Authentication timeout")
        return
    except Exception:
        await _safe_ws_close(websocket, code=1008, reason="Authentication failed")
        return

    user_id = payload.get("sub")
    last_membership_check = time.time()

    if not await manager.connect(websocket, project_id):
        return
    try:
        await websocket.send_json(
            {"event": "connected", "data": {"project_id": project_id}}
        )
        # Send current deploy state immediately so the client hydrates without waiting for polling
        try:
            from app.admin import build_deploy_state_payload

            async with AsyncSessionLocal() as ds_session:
                deploy_payload = await build_deploy_state_payload(ds_session)
            await websocket.send_json({"event": "deploy_state", "data": deploy_payload})
        except Exception as e:
            logger.warning(f"[WS] Failed to send initial deploy state on connect: {e}")
        last_message_time = time.time()
        message_count = 0
        while True:
            # Keep the connection open and listen for any messages from the client
            # 90-second idle timeout — detects silent drops from load balancers/proxies
            try:
                data = await asyncio.wait_for(websocket.receive_text(), timeout=90.0)
            except asyncio.TimeoutError:
                await _safe_ws_close(websocket, code=1001, reason="Idle timeout")
                return

            manager.update_activity(websocket)

            if len(data) > 10000:  # 10KB limit
                await _safe_ws_close(websocket, code=1009, reason="Message too big")
                return

            # Handle ping messages — infrastructure keepalive, not counted toward rate limit
            try:
                parsed = json.loads(data)
                if isinstance(parsed, dict) and parsed.get("type") == "ping":
                    # Client can close between receive and send; suppress the
                    # resulting ConnectionClosed/WebSocketDisconnect so the
                    # finally block handles cleanup instead of logging a crash.
                    # Catches websockets.ConnectionClosedError (raw library
                    # exception), WebSocketDisconnect (starlette wrapper), and
                    # RuntimeError (starlette state errors).
                    try:
                        await websocket.send_json({"event": "pong", "data": {}})
                    except Exception as _ws_exc:
                        logger.debug(
                            f"[WS] pong send failed, client disconnected: {_ws_exc}"
                        )
                        return
                    continue
            except (json.JSONDecodeError, AttributeError):
                pass

            # Simple rate limiting: 10 messages per second
            current_time = time.time()
            if current_time - last_message_time < 1.0:
                message_count += 1
                if message_count > 10:
                    await _safe_ws_close(
                        websocket, code=1008, reason="Rate limit exceeded"
                    )
                    return
            else:
                last_message_time = current_time
                message_count = 1

            # Re-validate project membership at most once per minute
            if (time.time() - last_membership_check) >= 60:
                async with AsyncSessionLocal() as ms:
                    member = (
                        await ms.execute(
                            select(ProjectMember).where(
                                ProjectMember.project_id == project_id,
                                ProjectMember.user_id == user_id,
                            )
                        )
                    ).scalar_one_or_none()
                if not member:
                    await _safe_ws_close(websocket, code=1008, reason="Access revoked")
                    return
                last_membership_check = time.time()
            # We can handle client-to-server messages here if needed
    except WebSocketDisconnect:
        pass
    finally:
        await manager.disconnect(websocket, project_id)


# --- Agent APIs ---


@router.websocket("/api/agent/ws/{task_id}")
async def agent_websocket_endpoint(websocket: WebSocket, task_id: uuid.UUID):
    """
    WebSocket endpoint for agents to stream high-throughput logs and real-time events.
    """
    path = f"/api/agent/ws/{task_id}"
    await websocket.accept()

    # Require the client to send the token as the first message
    try:
        auth_message = await asyncio.wait_for(websocket.receive_json(), timeout=5.0)
        token = auth_message.get("token")
        if not token:
            _log_agent_auth_reject(
                "ws_initial_auth_failure_missing_token",
                path=path,
                task_id=task_id,
            )
            await _safe_ws_close(websocket, code=1008, reason="Missing token")
            return

        payload = verify_agent_jwt(token)
        if payload.get("sub") != str(task_id):
            _log_agent_auth_reject(
                "ws_initial_auth_failure_task_id_mismatch",
                path=path,
                task_id=task_id,
                token_vm_instance_id=payload.get("vm_instance_id"),
                token_project_id=payload.get("project_id"),
            )
            await _safe_ws_close(websocket, code=1008, reason="Task ID mismatch")
            return
    except asyncio.TimeoutError:
        _log_agent_auth_reject(
            "ws_initial_auth_failure_timeout",
            path=path,
            task_id=task_id,
        )
        await _safe_ws_close(websocket, code=1008, reason="Authentication timeout")
        return
    except Exception:
        _log_agent_auth_reject(
            "ws_initial_auth_failure",
            path=path,
            task_id=task_id,
        )
        await _safe_ws_close(websocket, code=1008, reason="Authentication failed")
        return

    # Get project_id for this task
    async with AsyncSessionLocal() as session:
        stmt = select(Task).where(Task.id == task_id)
        task = (await session.execute(stmt)).scalars().first()
        if not task:
            _log_agent_auth_reject(
                "ws_task_missing",
                path=path,
                task_id=task_id,
                token_vm_instance_id=payload.get("vm_instance_id"),
                token_project_id=payload.get("project_id"),
            )
            await _safe_ws_close(websocket, code=1008)
            return

        from app.routers.agent import _terminated_task_ids  # noqa: PLC0415

        if (
            str(task.id) in _terminated_task_ids
            or task.status in _AGENT_WS_TERMINAL_STATUSES
        ):
            _log_agent_auth_reject(
                "ws_task_terminal",
                path=path,
                task_id=task_id,
                token_vm_instance_id=payload.get("vm_instance_id"),
                token_project_id=payload.get("project_id"),
            )
            await _safe_ws_close(
                websocket, code=1008, reason="Task is no longer active"
            )
            return

        token_project = payload.get("project_id")
        if token_project and token_project != str(task.project_id):
            _log_agent_auth_reject(
                "ws_project_mismatch",
                path=path,
                task_id=task_id,
                token_vm_instance_id=payload.get("vm_instance_id"),
                token_project_id=token_project,
            )
            await _safe_ws_close(websocket, code=1008, reason="Project mismatch")
            return

        token_vm = payload.get("vm_instance_id")
        if token_vm and task.vm_instance_id and token_vm != task.vm_instance_id:
            _log_agent_auth_reject(
                "ws_vm_instance_mismatch",
                path=path,
                task_id=task_id,
                token_vm_instance_id=token_vm,
                token_project_id=token_project,
            )
            await _safe_ws_close(websocket, code=1008, reason="VM instance mismatch")
            return
        if payload.get("task_type") == "strategic_resident_turn":
            task_context = task.context if isinstance(task.context, dict) else {}
            persisted_nonce = str(
                task_context.get(RESIDENT_CLAIM_NONCE_CONTEXT_KEY) or ""
            )
            presented_nonce = str(payload.get("claim_nonce") or "")
            if (
                not persisted_nonce
                or not presented_nonce
                or not hmac.compare_digest(presented_nonce, persisted_nonce)
            ):
                _log_agent_auth_reject(
                    "ws_resident_claim_nonce_stale",
                    path=path,
                    task_id=task_id,
                    token_vm_instance_id=token_vm,
                    token_project_id=token_project,
                )
                await _safe_ws_close(
                    websocket, code=1008, reason="Resident claim superseded"
                )
                return

        project_id = str(task.project_id)

    if not await manager.check_and_add_ip(websocket):
        return

    _terminated_task_ids_ref = _terminated_task_ids
    _RECHECK_INTERVAL = 5  # timer tick: seconds between termination checks
    # DB fallback runs on every tick so a deny-list miss (after restart,
    # listener gap, or cache desync) still revokes within the 5s window the
    # direction requires. Per-connection DB load: ~1 SELECT every 5s.
    _DB_RECHECK_INTERVAL = _RECHECK_INTERVAL
    last_recheck = 0.0  # force first DB recheck on first tick

    receive_task: Optional[asyncio.Task] = None
    try:
        last_message_time = time.time()
        message_count = 0
        receive_task = asyncio.create_task(websocket.receive_text())
        while True:
            # Race message receipt against a timer so termination is detected
            # even when the agent sends no messages.
            timer_task = asyncio.create_task(asyncio.sleep(_RECHECK_INTERVAL))
            done, _pending = await asyncio.wait(
                {receive_task, timer_task},
                return_when=asyncio.FIRST_COMPLETED,
            )

            if receive_task in done:
                # A message arrived — cancel the unused timer.
                timer_task.cancel()
                try:
                    await timer_task
                except asyncio.CancelledError:
                    pass

                try:
                    text_data = receive_task.result()
                except WebSocketDisconnect:
                    return
                except Exception:
                    return

                # Termination recheck: deny-list fast path + DB fallback
                if str(task_id) in _terminated_task_ids_ref:
                    _log_agent_auth_reject(
                        "ws_deny_list_hit",
                        path=path,
                        task_id=task_id,
                        token_vm_instance_id=payload.get("vm_instance_id"),
                        token_project_id=payload.get("project_id"),
                    )
                    await _safe_ws_close(
                        websocket, code=1008, reason="Task is no longer active"
                    )
                    return
                now = time.time()
                if now - last_recheck >= _DB_RECHECK_INTERVAL:
                    async with AsyncSessionLocal() as rs:
                        fresh = await rs.get(Task, task_id)
                    if _agent_ws_binding_invalid(fresh, payload):
                        _log_agent_auth_reject(
                            "ws_periodic_recheck_terminal",
                            path=path,
                            task_id=task_id,
                            token_vm_instance_id=payload.get("vm_instance_id"),
                            token_project_id=payload.get("project_id"),
                        )
                        await _safe_ws_close(
                            websocket, code=1008, reason="Task is no longer active"
                        )
                        return
                    last_recheck = now

                if len(text_data) > 100000:  # 100KB limit for agent logs
                    await _safe_ws_close(websocket, code=1009, reason="Message too big")
                    return

                # Simple rate limiting: 20 messages per second
                current_time = time.time()
                if current_time - last_message_time < 1.0:
                    message_count += 1
                    if message_count > 20:
                        await _safe_ws_close(
                            websocket, code=1008, reason="Rate limit exceeded"
                        )
                        return
                else:
                    last_message_time = current_time
                    message_count = 1

                try:
                    data = json.loads(text_data)
                except json.JSONDecodeError:
                    receive_task = asyncio.create_task(websocket.receive_text())
                    continue
                if (
                    payload.get("task_type") == "strategic_resident_turn"
                    and data.get("type") != "log"
                ):
                    await _safe_ws_close(
                        websocket,
                        code=1008,
                        reason="Resident frame type is not authorized",
                    )
                    return

                if data.get("type") == "log":
                    sanitized = _sanitize_log(data.get("content", ""))
                    await broadcast_to_project(
                        project_id,
                        "agent_log",
                        {"task_id": str(task_id), "log": sanitized},
                    )
                    # Additionally buffer the line for batched cross-instance
                    # relay so an operator whose viewer WebSocket landed on a
                    # DIFFERENT Cloud Run instance also sees the live log. No-op
                    # on single-instance / non-Postgres backends (the line is
                    # already delivered locally above), so no double delivery.
                    enqueue_agent_log(project_id, str(task_id), sanitized)
                elif data.get("type") == "status":
                    # Optionally handle status updates via WS too
                    pass
                elif data.get("type") == "strategic_session":
                    # Resident strategic-session live transcript delta. The
                    # 100KB frame-size cap and the 20/sec rate-limit accounting
                    # above already applied to this frame exactly like the log
                    # path. emit_strategic_session_delta secret-sanitizes
                    # ``text`` (broadcast._sanitize_log) and bounds the whole
                    # payload under the NOTIFY ceiling internally, so we only
                    # forward a mapping payload; anything else is ignored safely.
                    session_payload = data.get("payload")
                    if isinstance(session_payload, dict):
                        await emit_strategic_session_delta(
                            project_id, session_payload
                        )
                # Unknown frame types fall through and are ignored safely.

                receive_task = asyncio.create_task(websocket.receive_text())

            else:
                # Timer fired before a message arrived — run termination check.
                # receive_task is still pending; keep it for the next iteration.

                # Fast path: deny-list
                if str(task_id) in _terminated_task_ids_ref:
                    _log_agent_auth_reject(
                        "ws_deny_list_hit",
                        path=path,
                        task_id=task_id,
                        token_vm_instance_id=payload.get("vm_instance_id"),
                        token_project_id=payload.get("project_id"),
                    )
                    receive_task.cancel()
                    await _safe_ws_close(
                        websocket, code=1008, reason="Task is no longer active"
                    )
                    return

                # Slow path: DB recheck (rate-limited)
                now = time.time()
                if now - last_recheck >= _DB_RECHECK_INTERVAL:
                    async with AsyncSessionLocal() as rs:
                        fresh = await rs.get(Task, task_id)
                    if _agent_ws_binding_invalid(fresh, payload):
                        _log_agent_auth_reject(
                            "ws_periodic_recheck_terminal",
                            path=path,
                            task_id=task_id,
                            token_vm_instance_id=payload.get("vm_instance_id"),
                            token_project_id=payload.get("project_id"),
                        )
                        receive_task.cancel()
                        await _safe_ws_close(
                            websocket, code=1008, reason="Task is no longer active"
                        )
                        return
                    last_recheck = now

    except WebSocketDisconnect:
        pass
    finally:
        if receive_task is not None and not receive_task.done():
            receive_task.cancel()
        manager.remove_ip(websocket)


@router.post("/api/projects/{project_id}/cancel")
@limiter.limit("10/minute")
async def cancel_project(
    request: Request,
    project_id: uuid.UUID,
    background_tasks: BackgroundTasks,
    session: AsyncSession = Depends(get_session),
    current_user: dict = Depends(get_current_user),
):
    """Cancel all pending tasks and terminate associated VMs.

    Halts the entire project, marking pending tasks (QUEUED, PROVISIONING,
    IN_PROGRESS, EVALUATING) as CANCELLED, and immediately terminates all
    running VMs in the background. Requires project owner role.

    Path Parameters:
        project_id: UUID of the project to cancel.

    Returns:
        {
            "status": "cancelled" | "ignored",
            "message": str
        }

    Status codes:
        200: Project cancelled successfully or already terminal
        401: Unauthorized
        404: Project not found or user lacks owner role

    Rate limit: 10 requests/minute.
    """
    from app.vm_lifecycle import _ensure_vm_deleted  # noqa: PLC0415
    from app.admin import _get_user_project_with_role  # noqa: PLC0415

    project = await _get_user_project_with_role(
        project_id, current_user["user_id"], session, min_role="owner"
    )

    if project.status in [
        ProjectStatus.COMPLETED,
        ProjectStatus.FAILED,
        ProjectStatus.CANCELLED,
    ]:
        return {
            "status": "ignored",
            "message": f"Project is already in terminal state: {project.status}",
        }

    project.status = ProjectStatus.CANCELLED
    session.add(project)

    tasks_stmt = select(Task).where(
        Task.project_id == project_id,
        Task.status.in_(
            [
                TaskStatus.QUEUED,
                TaskStatus.PROVISIONING,
                TaskStatus.IN_PROGRESS,
                TaskStatus.EVALUATING,
                # PARKED tasks are non-terminal and must be torn down on project
                # cancel rather than left waiting on a wake source that the
                # cancelled project will never fire.
                TaskStatus.PARKED,
            ]
        ),
    )

    # Paginated fetch to avoid unbounded in-memory load for projects with
    # thousands of active tasks.
    _CANCEL_BATCH_SIZE = 5000
    active_tasks: list[Task] = []
    vm_ids_to_delete: list[str] = []
    _last_seen_id: Optional[int] = None
    while True:
        stmt = tasks_stmt
        if _last_seen_id is not None:
            stmt = stmt.where(Task.id > _last_seen_id)
        stmt = stmt.order_by(Task.id).limit(_CANCEL_BATCH_SIZE)
        _batch = (
            (await session.execute(stmt))
            .scalars()
            .all()
        )
        if not _batch:
            break
        for task in _batch:
            task.status = TaskStatus.CANCELLED
            session.add(task)
            if task.vm_instance_id:
                vm_ids_to_delete.append(task.vm_instance_id)
            await broadcast_to_project(
                str(project_id),
                "task_updated",
                {"task_id": str(task.id), "status": TaskStatus.CANCELLED},
            )
        _last_seen_id = _batch[-1].id
        active_tasks.extend(_batch)

    await session.commit()
    await broadcast_to_project(
        str(project_id), "project_updated", {"status": ProjectStatus.CANCELLED}
    )

    # Delete VMs after commit
    for vm_id in vm_ids_to_delete:
        await _ensure_vm_deleted(vm_id, reason="project_cancel")

    return {
        "status": "cancelled",
        "message": f"Project {project_id} and {len(active_tasks)} tasks cancelled.",
    }


@router.get("/api/projects/{project_id}/status")
@limiter.limit("60/minute")
async def get_project_status(
    request: Request,
    project_id: uuid.UUID,
    limit: int = Query(200, ge=1, le=500),
    offset: int = Query(0, ge=0),
    session: AsyncSession = Depends(get_session),
    current_user: dict = Depends(get_current_user),
):
    """
    Endpoint for the frontend to poll or stream the current status of the deployment
    and agent activities.
    """
    member = (
        await session.execute(
            select(ProjectMember).where(
                ProjectMember.project_id == project_id,
                ProjectMember.user_id == current_user["user_id"],
            )
        )
    ).scalar_one_or_none()
    if not member:
        raise HTTPException(status_code=404, detail="Project not found or unauthorized")
    project = await session.get(Project, project_id)

    if not project:
        raise HTTPException(status_code=404, detail="Project not found or unauthorized")

    active_statuses = [
        TaskStatus.QUEUED,
        TaskStatus.PROVISIONING,
        TaskStatus.IN_PROGRESS,
        TaskStatus.EVALUATING,
        # PARKED is in-flight work waiting on an external wake source — surface
        # it as active so operators see parked tasks rather than losing them.
        TaskStatus.PARKED,
    ]
    tasks_stmt = (
        select(Task)
        .where(Task.project_id == project_id, Task.status.in_(active_statuses))
        .order_by(Task.created_at.desc())
        .offset(offset)
        .limit(limit + 1)
    )
    tasks = list((await session.execute(tasks_stmt)).scalars().all())
    truncated = len(tasks) > limit
    if truncated:
        tasks = tasks[:limit]

    return {
        "project_id": project.id,
        "name": project.name,
        "status": project.status,
        "truncated": truncated,
        "tasks": [
            {
                "task_id": t.id,
                "status": t.status,
                "vm_instance_id": t.vm_instance_id,
                "attempt_count": t.attempt_count,
            }
            for t in tasks
        ],
    }

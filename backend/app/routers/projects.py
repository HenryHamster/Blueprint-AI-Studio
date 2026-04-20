"""Project CRUD, deploy, WebSocket, and status endpoints."""
import asyncio
import json
import logging
import time
import uuid
from typing import Optional

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query, Request, WebSocket, WebSocketDisconnect
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from app.auth import get_current_user, verify_agent_jwt, create_ws_ticket, verify_ws_ticket
from app.database import get_session, AsyncSessionLocal
from app.models import (
    Artifact,
    ArtifactType,
    Project,
    ProjectMember,
    ProjectStatus,
    Task,
    TaskStatus,
)
from app.payload_schemas import validate_task_payload
from app.schemas import DeployPayload
from app.shared_state import limiter
from app.broadcast import manager, broadcast_to_project, _sanitize_log
from app.routers.admin_analytics import ON_DEMAND_RATE, SPOT_RATE

logger = logging.getLogger(__name__)


async def _safe_ws_close(ws: WebSocket, code: int, reason: str = "") -> None:
    """Close a WebSocket, tolerating the double-close race.

    starlette raises ``RuntimeError('Unexpected ASGI message "websocket.close",
    after sending "websocket.close"')`` when the peer already hung up mid-auth
    — ``receive_json`` raised ``WebSocketDisconnect``, which silently sent a
    close frame, and our explicit close then tries to send a second one. That
    specific race is cosmetic; any other RuntimeError is real and re-raised.
    """
    try:
        await ws.close(code=code, reason=reason)
    except RuntimeError as exc:
        if "websocket.close" in str(exc):
            return
        raise


router = APIRouter(tags=["projects"])


@router.post("/api/deploy")
@limiter.limit("5/minute")
async def deploy_blueprint(
    request: Request,
    payload: DeployPayload,
    background_tasks: BackgroundTasks,
    session: AsyncSession = Depends(get_session),
    current_user: dict = Depends(get_current_user)
):
    # 1. Create a new Project
    project = Project(
        name=payload.name,
        user_id=current_user["user_id"],
        status=ProjectStatus.DEPLOYING,
        github_owner=payload.github_owner,
        github_repo=payload.github_repo
    )
    session.add(project)
    await session.flush()  # Assign project.id without committing

    # Auto-add owner membership
    session.add(ProjectMember(project_id=project.id, user_id=current_user["user_id"], role="owner"))

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
            raise HTTPException(status_code=422, detail=str(exc))
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

    await session.commit()

    # 3. The Orchestrator Worker will automatically pick up QUEUED tasks
    # and provision the VMs in the background.

    return {
        "status": "accepted",
        "project_id": project.id,
        "message": f"Project created. {len(created_tasks)} tasks queued for provisioning."
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
    current_user: dict = Depends(get_current_user)
):
    """
    List all projects for the authenticated user.
    """
    stmt = select(Project).where(Project.id.in_(
        select(ProjectMember.project_id).where(ProjectMember.user_id == current_user["user_id"])
    ))
    if status:
        stmt = stmt.where(Project.status == status)

    stmt = stmt.order_by(Project.created_at.desc()).offset(offset).limit(limit)
    projects = (await session.execute(stmt)).scalars().all()
    return {
        "projects": projects,
        "pagination": {
            "limit": limit,
            "offset": offset,
            "count": len(projects)
        }
    }

@router.get("/api/projects/{project_id}")
@limiter.limit("60/minute")
async def get_project(
    request: Request,
    project_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    current_user: dict = Depends(get_current_user)
):
    """
    Get a specific project's details.
    """
    # Check membership
    member = (await session.execute(
        select(ProjectMember).where(ProjectMember.project_id == project_id, ProjectMember.user_id == current_user["user_id"])
    )).scalar_one_or_none()
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
    session: AsyncSession = Depends(get_session),
    current_user: dict = Depends(get_current_user)
):
    """
    Get all tasks and their artifacts for a specific project.
    """
    # Verify project membership
    member = (await session.execute(
        select(ProjectMember).where(ProjectMember.project_id == project_id, ProjectMember.user_id == current_user["user_id"])
    )).scalar_one_or_none()
    if not member:
        raise HTTPException(status_code=404, detail="Project not found or unauthorized")
    project = await session.get(Project, project_id)
    if not project:
        raise HTTPException(status_code=404, detail="Project not found or unauthorized")

    # Fetch tasks
    tasks_stmt = select(Task).where(Task.project_id == project_id)
    if status:
        tasks_stmt = tasks_stmt.where(Task.status == status)
    if direction_id:
        tasks_stmt = tasks_stmt.where(Task.context["direction_id"].as_string() == direction_id)
    if task_type:
        tasks_stmt = tasks_stmt.where(Task.payloads[0]["type"].as_string() == task_type)

    tasks_stmt = tasks_stmt.order_by(Task.created_at.asc()).offset(offset).limit(limit)
    tasks = (await session.execute(tasks_stmt)).scalars().all()

    # Fetch artifacts for these tasks
    task_ids = [t.id for t in tasks]
    artifacts = []
    if task_ids:
        artifacts_stmt = select(Artifact).where(Artifact.task_id.in_(task_ids))
        artifacts = (await session.execute(artifacts_stmt)).scalars().all()

    # Group artifacts by task_id
    artifacts_by_task = {}
    research_map = {}
    for a in artifacts:
        if a.task_id not in artifacts_by_task:
            artifacts_by_task[a.task_id] = []
        artifacts_by_task[a.task_id].append(a)
        if a.artifact_type == ArtifactType.RESEARCH_FINDINGS and a.logs:
            try:
                parsed = json.loads(a.logs[:100000])
                research_map[a.task_id] = parsed if isinstance(parsed, list) else parsed.get('logs', [])
            except (json.JSONDecodeError, AttributeError):
                pass

    # Combine
    result = []
    for t in tasks:
        if t.vm_provisioned_at and t.vm_stopped_at:
            duration_seconds = (t.vm_stopped_at - t.vm_provisioned_at).total_seconds()
            rate = SPOT_RATE if t.vm_spot else ON_DEMAND_RATE
            estimated_cost_usd = duration_seconds * rate
        else:
            duration_seconds = None
            estimated_cost_usd = None
        task_dict = {
            "id": t.id,
            "title": t.title,
            "status": t.status,
            "task_type": (t.first_payload).get("type", "create"),
            "vm_instance_id": t.vm_instance_id,
            "payloads": t.payloads,
            "created_at": t.created_at,
            "parent_task_id": str(t.parent_task_id) if t.parent_task_id else None,
            "context": {k: v for k, v in (t.context or {}).items() if not k.startswith("max_plan_")},
            "artifacts": artifacts_by_task.get(t.id, []),
            "research_findings": research_map.get(t.id),
            "duration_seconds": duration_seconds,
            "estimated_cost_usd": estimated_cost_usd,
        }
        result.append(task_dict)

    return {
        "tasks": result,
        "pagination": {
            "limit": limit,
            "offset": offset,
            "count": len(result)
        }
    }

@router.post("/api/projects/{project_id}/ws-ticket")
@limiter.limit("30/minute")
async def generate_ws_ticket(
    request: Request,
    project_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    current_user: dict = Depends(get_current_user)
):
    """
    Generates a short-lived ticket for authenticating WebSocket connections.
    """
    member = (await session.execute(
        select(ProjectMember).where(ProjectMember.project_id == project_id, ProjectMember.user_id == current_user["user_id"])
    )).scalar_one_or_none()
    if not member:
        raise HTTPException(status_code=404, detail="Project not found or unauthorized")

    ticket = create_ws_ticket(current_user["user_id"], str(project_id))
    return {"ticket": ticket}

@router.websocket("/api/projects/{project_id}/ws")
async def websocket_endpoint(websocket: WebSocket, project_id: str):
    """
    WebSocket endpoint for real-time updates.
    The frontend connects to this to receive live status changes and logs.
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

    if not await manager.connect(websocket, project_id):
        return
    try:
        await websocket.send_json({"event": "connected", "data": {"project_id": project_id}})
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

            if len(data) > 10000: # 10KB limit
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
                        logger.debug(f"[WS] pong send failed, client disconnected: {_ws_exc}")
                        return
                    continue
            except (json.JSONDecodeError, AttributeError):
                pass

            # Simple rate limiting: 10 messages per second
            current_time = time.time()
            if current_time - last_message_time < 1.0:
                message_count += 1
                if message_count > 10:
                    await _safe_ws_close(websocket, code=1008, reason="Rate limit exceeded")
                    return
            else:
                last_message_time = current_time
                message_count = 1
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
    await websocket.accept()

    # Require the client to send the token as the first message
    try:
        auth_message = await asyncio.wait_for(websocket.receive_json(), timeout=5.0)
        token = auth_message.get("token")
        if not token:
            await _safe_ws_close(websocket, code=1008, reason="Missing token")
            return

        payload = verify_agent_jwt(token)
        if payload.get("sub") != str(task_id):
            await _safe_ws_close(websocket, code=1008, reason="Task ID mismatch")
            return
    except asyncio.TimeoutError:
        await _safe_ws_close(websocket, code=1008, reason="Authentication timeout")
        return
    except Exception:
        await _safe_ws_close(websocket, code=1008, reason="Authentication failed")
        return

    # Get project_id for this task
    async with AsyncSessionLocal() as session:
        stmt = select(Task).where(Task.id == task_id)
        task = (await session.execute(stmt)).scalars().first()
        if not task:
            await _safe_ws_close(websocket, code=1008)
            return
        project_id = str(task.project_id)

    if not await manager.check_and_add_ip(websocket):
        return

    try:
        last_message_time = time.time()
        message_count = 0
        while True:
            text_data = await websocket.receive_text()
            if len(text_data) > 100000: # 100KB limit for agent logs
                await _safe_ws_close(websocket, code=1009, reason="Message too big")
                return

            # Simple rate limiting: 20 messages per second
            current_time = time.time()
            if current_time - last_message_time < 1.0:
                message_count += 1
                if message_count > 20:
                    await _safe_ws_close(websocket, code=1008, reason="Rate limit exceeded")
                    return
            else:
                last_message_time = current_time
                message_count = 1

            try:
                data = json.loads(text_data)
            except json.JSONDecodeError:
                continue

            if data.get("type") == "log":
                await broadcast_to_project(project_id, "agent_log", {
                    "task_id": str(task_id),
                    "log": _sanitize_log(data.get("content", ""))
                })
            elif data.get("type") == "status":
                # Optionally handle status updates via WS too
                pass
    except WebSocketDisconnect:
        pass
    finally:
        manager.remove_ip(websocket)


@router.post("/api/projects/{project_id}/cancel")
@limiter.limit("10/minute")
async def cancel_project(
    request: Request,
    project_id: uuid.UUID,
    background_tasks: BackgroundTasks,
    session: AsyncSession = Depends(get_session),
    current_user: dict = Depends(get_current_user)
):
    """
    Halts the project, marks pending tasks as cancelled, and immediately terminates all associated running VMs.
    """
    from app.main import _ensure_vm_deleted  # noqa: PLC0415

    member = (await session.execute(
        select(ProjectMember).where(ProjectMember.project_id == project_id, ProjectMember.user_id == current_user["user_id"])
    )).scalar_one_or_none()
    if not member:
        raise HTTPException(status_code=404, detail="Project not found or unauthorized")
    project = await session.get(Project, project_id)
    if not project:
        raise HTTPException(status_code=404, detail="Project not found or unauthorized")

    if project.status in [ProjectStatus.COMPLETED, ProjectStatus.FAILED, ProjectStatus.CANCELLED]:
        return {"status": "ignored", "message": f"Project is already in terminal state: {project.status}"}

    project.status = ProjectStatus.CANCELLED
    session.add(project)

    tasks_stmt = select(Task).where(
        Task.project_id == project_id,
        Task.status.in_([TaskStatus.QUEUED, TaskStatus.PROVISIONING, TaskStatus.IN_PROGRESS, TaskStatus.EVALUATING])
    )
    active_tasks = (await session.execute(tasks_stmt)).scalars().all()

    vm_ids_to_delete = []
    for task in active_tasks:
        task.status = TaskStatus.CANCELLED
        session.add(task)
        if task.vm_instance_id:
            vm_ids_to_delete.append(task.vm_instance_id)

        await broadcast_to_project(
            str(project_id),
            "task_updated",
            {"task_id": str(task.id), "status": TaskStatus.CANCELLED}
        )

    await session.commit()
    await broadcast_to_project(str(project_id), "project_updated", {"status": ProjectStatus.CANCELLED})

    # Delete VMs after commit
    for vm_id in vm_ids_to_delete:
        await _ensure_vm_deleted(vm_id, reason="project_cancel")

    return {"status": "cancelled", "message": f"Project {project_id} and {len(active_tasks)} tasks cancelled."}

@router.get("/api/projects/{project_id}/status")
@limiter.limit("60/minute")
async def get_project_status(
    request: Request,
    project_id: uuid.UUID,
    limit: int = Query(200, ge=1, le=500),
    offset: int = Query(0, ge=0),
    session: AsyncSession = Depends(get_session),
    current_user: dict = Depends(get_current_user)
):
    """
    Endpoint for the frontend to poll or stream the current status of the deployment
    and agent activities.
    """
    member = (await session.execute(
        select(ProjectMember).where(ProjectMember.project_id == project_id, ProjectMember.user_id == current_user["user_id"])
    )).scalar_one_or_none()
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
                "attempt_count": t.attempt_count
            } for t in tasks
        ]
    }

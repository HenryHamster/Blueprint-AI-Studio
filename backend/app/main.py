import os
from contextlib import asynccontextmanager
from fastapi import FastAPI, Depends, HTTPException, Header, BackgroundTasks, Request, Query, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse, FileResponse
from fastapi.staticfiles import StaticFiles
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field
from typing import List, Dict, Any, Optional
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select
import uuid
import asyncio
import json
import httpx
from datetime import datetime, timedelta

from app.database import init_db, get_session, AsyncSessionLocal, engine
from app.models import Project, Task, Artifact, TaskHistory, ProjectStatus, TaskStatus, ArtifactType, UserIntegration
from app.compute import provision_agent_vm, delete_agent_vm, cleanup_orphaned_vms
from app.storage import generate_upload_signed_url, upload_json_payload, ARTIFACT_BUCKET_NAME
from app.secrets_manager import store_user_secret, get_user_secret
from app.auth import get_current_user, verify_agent_jwt, create_ws_ticket, verify_ws_ticket
from app.telemetry import setup_telemetry
from app.admin import router as admin_router
from app.github import (
    validate_fine_grained_pat,
    generate_installation_access_token,
    generate_github_app_jwt,
    GITHUB_WEBHOOK_SECRET,
    GITHUB_CLIENT_ID,
    exchange_oauth_code,
    get_user_installations,
    get_github_user,
    update_commit_status,
    create_pr_comment,
    create_pr_review_comment,
    create_issue,
    get_issues,
    create_branch,
    create_pull_request
)
import hmac
import hashlib
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.util import get_remote_address
from slowapi.errors import RateLimitExceeded

# Initialize the rate limiter
# In a multi-instance production environment (like Cloud Run), 
# you would configure this to use Redis (e.g., storage_uri="redis://...")
# For now, we use the default in-memory storage.
limiter = Limiter(key_func=get_remote_address)

# --- Runtime Config (adjustable via admin API) ---
_runtime_config = {
    "max_concurrent_vms": int(os.getenv("MAX_CONCURRENT_VMS", "5")),
}

# --- Max Plan Account Pool ---
# Each account: {id, name, credentials_json, active_tasks, total_tasks}
# credentials_json is the raw content of ~/.claude/credentials.json from `claude login`
_max_plan_pool: List[Dict[str, Any]] = []

def select_max_plan_account() -> Optional[Dict[str, Any]]:
    """Select the Max plan account with the lowest active task count."""
    available = [a for a in _max_plan_pool if a.get("enabled", True)]
    if not available:
        return None
    return min(available, key=lambda a: a["active_tasks"])

def release_max_plan_account(account_id: str):
    """Decrement active tasks for an account when a task finishes."""
    for account in _max_plan_pool:
        if account["id"] == account_id:
            account["active_tasks"] = max(0, account["active_tasks"] - 1)
            break

# --- In-Memory Event Bus for WebSockets ---
class ConnectionManager:
    def __init__(self):
        self.active_connections: Dict[str, List[WebSocket]] = {}
        self.ip_connections: Dict[str, int] = {}
        self.MAX_CONNECTIONS_PER_IP = 20

    async def check_and_add_ip(self, websocket: WebSocket) -> bool:
        client_ip = websocket.headers.get("x-forwarded-for", websocket.client.host if websocket.client else "unknown").split(",")[0].strip()
        if client_ip != "unknown":
            if self.ip_connections.get(client_ip, 0) >= self.MAX_CONNECTIONS_PER_IP:
                await websocket.close(code=1008, reason="Too many connections from this IP")
                return False
            self.ip_connections[client_ip] = self.ip_connections.get(client_ip, 0) + 1
        return True

    def remove_ip(self, websocket: WebSocket):
        client_ip = websocket.headers.get("x-forwarded-for", websocket.client.host if websocket.client else "unknown").split(",")[0].strip()
        if client_ip != "unknown" and client_ip in self.ip_connections:
            self.ip_connections[client_ip] -= 1
            if self.ip_connections[client_ip] <= 0:
                del self.ip_connections[client_ip]

    async def connect(self, websocket: WebSocket, project_id: str) -> bool:
        if not await self.check_and_add_ip(websocket):
            return False

        if project_id not in self.active_connections:
            self.active_connections[project_id] = []
        self.active_connections[project_id].append(websocket)
        return True

    def disconnect(self, websocket: WebSocket, project_id: str):
        if project_id in self.active_connections:
            if websocket in self.active_connections[project_id]:
                self.active_connections[project_id].remove(websocket)
            if not self.active_connections[project_id]:
                del self.active_connections[project_id]
        
        self.remove_ip(websocket)

    async def broadcast(self, project_id: str, event_type: str, data: dict):
        if project_id in self.active_connections:
            message = {"event": event_type, "data": data}
            # Create a copy of the list to avoid RuntimeError if modified during iteration
            for connection in list(self.active_connections[project_id]):
                try:
                    await connection.send_json(message)
                except Exception:
                    self.disconnect(connection, project_id)

manager = ConnectionManager()

async def broadcast_to_project(project_id: str, event_type: str, data: dict):
    """Broadcasts a message to all WebSocket clients listening to a specific project."""
    await manager.broadcast(project_id, event_type, data)

def verify_agent_token(x_agent_token: str = Header(..., description="Secret token for agent authentication")):
    """
    Validates the short-lived JWT passed by the agent VM.
    """
    payload = verify_agent_jwt(x_agent_token)
    return payload

async def reaper_service():
    """
    Background worker that scans for orphaned VMs.
    Marks them as FAILED and issues a delete command to GCP if they haven't sent a heartbeat in 5 minutes.
    """
    while True:
        try:
            async with AsyncSessionLocal() as session:
                stmt = select(Task).where(
                    Task.status.in_([TaskStatus.PROVISIONING, TaskStatus.IN_PROGRESS])
                )
                active_tasks = (await session.execute(stmt)).scalars().all()
                
                orphaned_tasks = []
                now = datetime.utcnow()
                five_minutes_ago = now - timedelta(minutes=5)
                ten_minutes_ago = now - timedelta(minutes=10)
                
                for task in active_tasks:
                    # Skip logical/parent tasks that have no VM assigned
                    if not task.vm_instance_id:
                        continue
                    if task.last_heartbeat:
                        if task.last_heartbeat < five_minutes_ago:
                            orphaned_tasks.append(task)
                    elif task.started_at and task.started_at < ten_minutes_ago:
                        # Allow 10 minutes for initial provisioning before requiring a heartbeat
                        orphaned_tasks.append(task)
                    elif not task.started_at and task.created_at < ten_minutes_ago:
                        # Fallback if started_at is missing for some reason
                        orphaned_tasks.append(task)
                
                for task in orphaned_tasks:
                    print(f"[REAPER] Terminating orphaned task {task.id} (VM: {task.vm_instance_id})")
                    
                    if task.vm_instance_id:
                        # Fire and forget the delete API call
                        asyncio.create_task(delete_agent_vm(task.vm_instance_id))
                    
                    if task.attempt_count < 3:
                        print(f"[REAPER] Retrying task {task.id} (Attempt {task.attempt_count + 1}/3)")
                        task.attempt_count += 1
                        task.status = TaskStatus.QUEUED
                        task.vm_instance_id = f"agent-vm-{uuid.uuid4().hex[:8]}"
                        task.last_heartbeat = None
                        session.add(task)
                        
                        await broadcast_to_project(
                            str(task.project_id), 
                            "task_updated", 
                            {"task_id": str(task.id), "status": TaskStatus.QUEUED, "attempt_count": task.attempt_count}
                        )
                    else:
                        print(f"[REAPER] Task {task.id} failed after 3 attempts.")
                        task.status = TaskStatus.FAILED
                        session.add(task)
                        
                        # Broadcast failure to frontend
                        await broadcast_to_project(
                            str(task.project_id), 
                            "task_updated", 
                            {"task_id": str(task.id), "status": TaskStatus.FAILED}
                        )
                
                if orphaned_tasks:
                    await session.commit()
                    
        except asyncio.CancelledError:
            print("[REAPER] Service shutting down.")
            break
        except Exception as e:
            print(f"[REAPER] Error: {e}")
            
        # Sleep for 1 minute before checking again
        try:
            await asyncio.sleep(60)
        except asyncio.CancelledError:
            print("[REAPER] Service shutting down.")
            break

async def gcp_cleanup_service():
    """
    Background worker that runs every 15 minutes to ensure GCP VMs are in sync with the database.
    It deletes any VMs prefixed with 'agent-vm-' that do not have an active task.
    """
    while True:
        try:
            async with AsyncSessionLocal() as session:
                # Get all active tasks that should have a VM running
                stmt = select(Task.vm_instance_id).where(
                    Task.status.in_([TaskStatus.PROVISIONING, TaskStatus.IN_PROGRESS])
                )
                active_vm_ids = (await session.execute(stmt)).scalars().all()
                active_vm_ids = [vid for vid in active_vm_ids if vid]
                
                # Call compute.py to clean up
                await cleanup_orphaned_vms(active_vm_ids)
                
        except asyncio.CancelledError:
            print("[GCP CLEANUP] Service shutting down.")
            break
        except Exception as e:
            print(f"[GCP CLEANUP] Error: {e}")
            
        try:
            await asyncio.sleep(900) # Run every 15 minutes
        except asyncio.CancelledError:
            print("[GCP CLEANUP] Service shutting down.")
            break

async def orchestrator_worker():
    """
    Background worker that polls for QUEUED tasks and provisions VMs.
    This ensures tasks are not lost if the server restarts before provisioning.
    """
    while True:
        queued_tasks = []
        try:
            async with AsyncSessionLocal() as session:
                # Enforce concurrency limit before picking up new tasks
                from sqlalchemy import func
                active_count = (await session.execute(
                    select(func.count()).select_from(Task).where(
                        Task.status.in_([TaskStatus.PROVISIONING, TaskStatus.IN_PROGRESS])
                    )
                )).scalar() or 0
                slots_available = max(0, _runtime_config["max_concurrent_vms"] - active_count)
                if slots_available == 0:
                    await asyncio.sleep(5)
                    continue

                # Find tasks that are QUEUED, not awaiting approval, and haven't been picked up yet
                stmt = select(Task).where(
                    Task.status == TaskStatus.QUEUED,
                    Task.requires_approval == False
                ).limit(min(slots_available, 10))
                queued_tasks = (await session.execute(stmt)).scalars().all()
                
                for task in queued_tasks:
                    print(f"[ORCHESTRATOR] Picking up queued task {task.id} (VM: {task.vm_instance_id})")

                    # Select Max plan account (primary) or fall back to API key
                    account = select_max_plan_account()
                    claude_credentials = None
                    account_id = None
                    if account:
                        account["active_tasks"] += 1
                        account["total_tasks"] += 1
                        account_id = account["id"]
                        claude_credentials = account.get("credentials_json")
                        print(f"[ORCHESTRATOR] Using Max plan account '{account['name']}' (active: {account['active_tasks']})")
                    else:
                        print(f"[ORCHESTRATOR] No Max plan accounts available, falling back to API key")

                    # Store which account this task uses for cleanup on completion
                    task.context = {**(task.context or {}), "max_plan_account_id": account_id}

                    # Mark as PROVISIONING to prevent other workers from picking it up
                    task.status = TaskStatus.PROVISIONING
                    task.started_at = datetime.utcnow()
                    session.add(task)
                    await session.commit()

                    # Trigger GCP Compute Engine provisioning in the background
                    async def _provision_and_handle(vm_id: str, t_id: uuid.UUID, p_id: uuid.UUID, payloads: list, creds: str = None):
                        payload_uri = None
                        if payloads:
                            try:
                                payload_uri = await upload_json_payload(str(t_id), payloads)
                            except Exception as e:
                                print(f"[ORCHESTRATOR] Failed to upload payload for task {t_id}: {e}")

                        success = await provision_agent_vm(vm_id, t_id, payload_uri, claude_credentials=creds)
                        if not success:
                            print(f"[ORCHESTRATOR] Failed to provision VM {vm_id} for task {t_id}")
                            # Mark as failed immediately to avoid 10 min wait
                            try:
                                async with AsyncSessionLocal() as inner_session:
                                    failed_task = await inner_session.get(Task, t_id)
                                    if failed_task:
                                        failed_task.status = TaskStatus.FAILED
                                        inner_session.add(failed_task)
                                        await inner_session.commit()
                                        await broadcast_to_project(
                                            str(p_id), 
                                            "task_updated", 
                                            {"task_id": str(t_id), "status": TaskStatus.FAILED}
                                        )
                            except Exception as e:
                                print(f"[ORCHESTRATOR] Error marking task {t_id} as failed: {e}")
                            
                    asyncio.create_task(_provision_and_handle(task.vm_instance_id, task.id, task.project_id, task.payloads, claude_credentials))
                    
                    await broadcast_to_project(
                        str(task.project_id), 
                        "task_updated", 
                        {"task_id": str(task.id), "status": TaskStatus.PROVISIONING}
                    )
                    
        except asyncio.CancelledError:
            print("[ORCHESTRATOR] Worker shutting down.")
            break
        except Exception as e:
            print(f"[ORCHESTRATOR] Error: {e}")
            
        # Sleep based on whether we found tasks
        try:
            if queued_tasks:
                await asyncio.sleep(0.5) # Yield control briefly if we are processing a backlog
            else:
                await asyncio.sleep(5) # Sleep longer if queue is empty
        except asyncio.CancelledError:
            print("[ORCHESTRATOR] Worker shutting down.")
            break

@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup: Initialize DB tables
    await init_db()

    # Start the Reaper Service background task
    reaper_task = asyncio.create_task(reaper_service())
    
    # Start the GCP Cleanup Service background task
    gcp_cleanup_task = asyncio.create_task(gcp_cleanup_service())
    
    # Start the Orchestrator Worker background task
    orchestrator_task = asyncio.create_task(orchestrator_worker())
    
    yield
    # Shutdown: Clean up resources if needed
    reaper_task.cancel()
    gcp_cleanup_task.cancel()
    orchestrator_task.cancel()

app = FastAPI(
    title="Blueprint Orchestrator",
    description="Secure Orchestration & State Management",
    lifespan=lifespan
)

# Telemetry must be set up before the app starts (cannot add middleware during lifespan)
setup_telemetry(app, engine)

app.include_router(admin_router)

# Attach the rate limiter to the app
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)

@app.exception_handler(HTTPException)
async def http_exception_handler(request: Request, exc: HTTPException):
    return JSONResponse(
        status_code=exc.status_code,
        content={"error": {"code": exc.status_code, "message": exc.detail}},
    )

@app.exception_handler(RequestValidationError)
async def validation_exception_handler(request: Request, exc: RequestValidationError):
    return JSONResponse(
        status_code=422,
        content={"error": {"code": 422, "message": "Validation Error", "details": exc.errors()}},
    )

@app.exception_handler(Exception)
async def generic_exception_handler(request: Request, exc: Exception):
    print(f"Unhandled exception: {exc}")
    return JSONResponse(
        status_code=500,
        content={"error": {"code": 500, "message": "Internal Server Error"}},
    )

# Add CORS Middleware so the frontend can call these APIs
# In production, set ALLOWED_ORIGINS to the specific frontend domain (e.g., "https://my-app.com")
# Multiple origins can be comma-separated.
allowed_origins_env = os.getenv("ALLOWED_ORIGINS", "")
if allowed_origins_env and allowed_origins_env != "*":
    allowed_origins = [origin.strip() for origin in allowed_origins_env.split(",") if origin.strip()]
else:
    # Default to local development ports and AI Studio URLs if not specified
    allowed_origins = [
        "http://localhost:3000",
        "http://localhost:5173",
        "http://127.0.0.1:3000",
        "http://127.0.0.1:5173",
    ]
    app_url = os.getenv("APP_URL")
    if app_url:
        allowed_origins.append(app_url.rstrip("/"))
    shared_app_url = os.getenv("SHARED_APP_URL")
    if shared_app_url:
        allowed_origins.append(shared_app_url.rstrip("/"))

app.add_middleware(
    CORSMiddleware,
    allow_origins=allowed_origins,
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
    allow_headers=["Authorization", "Content-Type", "Accept", "Origin", "X-Requested-With"],
)

class DeployPayload(BaseModel):
    name: str
    payloads: List[Dict[str, Any]]
    github_owner: Optional[str] = Field(None, pattern="^[a-zA-Z0-9_.-]+$")
    github_repo: Optional[str] = Field(None, pattern="^[a-zA-Z0-9_.-]+$")

class AgentStatusPayload(BaseModel):
    task_id: uuid.UUID
    status: TaskStatus
    artifact_type: Optional[ArtifactType] = None
    commit_hash: Optional[str] = None
    repo_url: Optional[str] = None
    pr_url: Optional[str] = None
    storage_uri: Optional[str] = None
    logs: Optional[str] = Field(None, max_length=100000)

class UploadUrlRequest(BaseModel):
    task_id: uuid.UUID
    filename: str

class HeartbeatRequest(BaseModel):
    task_id: uuid.UUID
    status_message: Optional[str] = None

class SpawnTaskRequest(BaseModel):
    task_id: uuid.UUID
    payloads: List[Dict[str, Any]]
    requires_approval: bool = False
    context: Optional[Dict[str, Any]] = None

class GitHubCommitStatusRequest(BaseModel):
    task_id: uuid.UUID
    owner: str
    repo: str
    sha: str = Field(..., pattern="^[a-fA-F0-9]{40}$")
    state: str = Field(..., pattern="^(error|failure|pending|success)$")
    description: str = ""
    context: str = "blueprint-agent"
    target_url: str = ""

class GitHubPRCommentRequest(BaseModel):
    task_id: uuid.UUID
    owner: str
    repo: str
    pr_number: int
    body: str

class GitHubPRReviewCommentRequest(BaseModel):
    task_id: uuid.UUID
    owner: str
    repo: str
    pr_number: int
    commit_id: str = Field(..., pattern="^[a-fA-F0-9]{40}$")
    path: str
    line: int
    body: str

class GitHubCreateIssueRequest(BaseModel):
    task_id: uuid.UUID
    owner: str
    repo: str
    title: str
    body: str = ""
    assignees: Optional[List[str]] = None

class GitHubGetIssuesRequest(BaseModel):
    task_id: uuid.UUID
    owner: str
    repo: str
    state: str = Field("open", pattern="^(open|closed|all)$")

class GitHubCreateBranchRequest(BaseModel):
    task_id: uuid.UUID
    owner: str
    repo: str
    branch_name: str = Field(..., pattern="^[a-zA-Z0-9_./-]+$")
    sha: str = Field(..., pattern="^[a-fA-F0-9]{40}$")

class GitHubCreatePRRequest(BaseModel):
    task_id: uuid.UUID
    owner: str
    repo: str
    title: str
    head: str = Field(..., pattern="^[a-zA-Z0-9_./:-]+$")
    base: str = Field(..., pattern="^[a-zA-Z0-9_./:-]+$")
    body: str = ""

@app.get("/")
def read_root():
    return {"status": "online", "service": "Blueprint Orchestrator"}

@app.post("/api/deploy")
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
    await session.commit()
    await session.refresh(project)
    
    # 2. Create Tasks based on the flexible payloads
    created_tasks = []
    
    # Create a parent task for the entire deployment
    parent_task = Task(
        project_id=project.id,
        title=f"Deploy {payload.name}",
        description="Parent task orchestrating the deployment.",
        status=TaskStatus.IN_PROGRESS,
        created_by="USER"
    )
    session.add(parent_task)
    await session.commit()
    await session.refresh(parent_task)
    
    for p in payload.payloads:
        # GCP instance names must match a specific regex (lowercase, hyphens, numbers)
        vm_name = f"agent-vm-{uuid.uuid4().hex[:8]}"
        
        task = Task(
            project_id=project.id,
            title=p.get("title", "Untitled Subtask"),
            description=p.get("description", ""),
            parent_task_id=parent_task.id,
            payloads=[p],
            vm_instance_id=vm_name,
            status=TaskStatus.QUEUED,
            created_by="ORCHESTRATOR"
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

@app.get("/api/projects")
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
    stmt = select(Project).where(Project.user_id == current_user["user_id"])
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

@app.get("/api/projects/{project_id}")
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
    stmt = select(Project).where(Project.id == project_id, Project.user_id == current_user["user_id"])
    project = (await session.execute(stmt)).scalars().first()
    
    if not project:
        raise HTTPException(status_code=404, detail="Project not found or unauthorized")
        
    return project

@app.get("/api/projects/{project_id}/tasks")
@limiter.limit("60/minute")
async def get_project_tasks(
    request: Request,
    project_id: uuid.UUID,
    limit: int = Query(50, ge=1, le=100),
    offset: int = Query(0, ge=0),
    status: Optional[TaskStatus] = None,
    session: AsyncSession = Depends(get_session),
    current_user: dict = Depends(get_current_user)
):
    """
    Get all tasks and their artifacts for a specific project.
    """
    # Verify project exists and belongs to user
    project_stmt = select(Project).where(Project.id == project_id, Project.user_id == current_user["user_id"])
    project = (await session.execute(project_stmt)).scalars().first()
    if not project:
        raise HTTPException(status_code=404, detail="Project not found or unauthorized")
        
    # Fetch tasks
    tasks_stmt = select(Task).where(Task.project_id == project_id)
    if status:
        tasks_stmt = tasks_stmt.where(Task.status == status)
        
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
    for a in artifacts:
        if a.task_id not in artifacts_by_task:
            artifacts_by_task[a.task_id] = []
        artifacts_by_task[a.task_id].append(a)
        
    # Combine
    result = []
    for t in tasks:
        task_dict = {
            "id": t.id,
            "status": t.status,
            "vm_instance_id": t.vm_instance_id,
            "payloads": t.payloads,
            "created_at": t.created_at,
            "artifacts": artifacts_by_task.get(t.id, [])
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

@app.post("/api/projects/{project_id}/ws-ticket")
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
    # Admins can access any project's WebSocket
    if current_user.get("role") == "admin":
        stmt = select(Project).where(Project.id == project_id)
    else:
        stmt = select(Project).where(Project.id == project_id, Project.user_id == current_user["user_id"])
    project = (await session.execute(stmt)).scalars().first()
    if not project:
        raise HTTPException(status_code=404, detail="Project not found or unauthorized")
        
    ticket = create_ws_ticket(current_user["user_id"], str(project_id))
    return {"ticket": ticket}

@app.websocket("/api/projects/{project_id}/ws")
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
            await websocket.close(code=1008, reason="Missing ticket")
            return
            
        payload = verify_ws_ticket(ticket)
        if payload.get("project_id") != project_id:
            await websocket.close(code=1008, reason="Project ID mismatch")
            return
    except asyncio.TimeoutError:
        await websocket.close(code=1008, reason="Authentication timeout")
        return
    except Exception:
        await websocket.close(code=1008, reason="Authentication failed")
        return

    if not await manager.connect(websocket, project_id):
        return
    try:
        await websocket.send_json({"event": "connected", "data": {"project_id": project_id}})
        import time
        last_message_time = time.time()
        message_count = 0
        while True:
            # Keep the connection open and listen for any messages from the client
            data = await websocket.receive_text()
            if len(data) > 10000: # 10KB limit
                await websocket.close(code=1009, reason="Message too big")
                return
            
            # Simple rate limiting: 10 messages per second
            current_time = time.time()
            if current_time - last_message_time < 1.0:
                message_count += 1
                if message_count > 10:
                    await websocket.close(code=1008, reason="Rate limit exceeded")
                    return
            else:
                last_message_time = current_time
                message_count = 1
            # We can handle client-to-server messages here if needed
    except WebSocketDisconnect:
        manager.disconnect(websocket, project_id)

# --- Agent APIs ---

@app.websocket("/api/agent/ws/{task_id}")
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
            await websocket.close(code=1008, reason="Missing token")
            return
            
        payload = verify_agent_jwt(token)
        if payload.get("sub") != str(task_id):
            await websocket.close(code=1008, reason="Task ID mismatch")
            return
    except asyncio.TimeoutError:
        await websocket.close(code=1008, reason="Authentication timeout")
        return
    except Exception:
        await websocket.close(code=1008, reason="Authentication failed")
        return

    # Get project_id for this task
    async with AsyncSessionLocal() as session:
        stmt = select(Task).where(Task.id == task_id)
        task = (await session.execute(stmt)).scalars().first()
        if not task:
            await websocket.close(code=1008)
            return
        project_id = str(task.project_id)

    if not await manager.check_and_add_ip(websocket):
        return

    try:
        import time
        last_message_time = time.time()
        message_count = 0
        while True:
            text_data = await websocket.receive_text()
            if len(text_data) > 100000: # 100KB limit for agent logs
                await websocket.close(code=1009, reason="Message too big")
                return
            
            # Simple rate limiting: 20 messages per second
            current_time = time.time()
            if current_time - last_message_time < 1.0:
                message_count += 1
                if message_count > 20:
                    await websocket.close(code=1008, reason="Rate limit exceeded")
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
                    "log": data.get("content")
                })
            elif data.get("type") == "status":
                # Optionally handle status updates via WS too
                pass
    except WebSocketDisconnect:
        pass
    finally:
        manager.remove_ip(websocket)

@app.post("/api/agent/claim")
async def claim_task(
    session: AsyncSession = Depends(get_session),
    token: dict = Depends(verify_agent_token)
):
    stmt = select(Task).where(
        Task.vm_instance_id == token.get("vm_instance_id"),
        Task.status.in_([TaskStatus.QUEUED, TaskStatus.PROVISIONING])
    )
    result = await session.execute(stmt)
    task = result.scalars().first()
    
    if not task:
        raise HTTPException(status_code=404, detail="No queued task found for this VM instance.")
        
    task.status = TaskStatus.IN_PROGRESS
    session.add(task)
    await session.commit()
    await session.refresh(task)
    
    # Broadcast status change to frontend
    await broadcast_to_project(
        str(task.project_id), 
        "task_updated", 
        {"task_id": str(task.id), "status": TaskStatus.IN_PROGRESS}
    )
    
    # Strip internal fields (max_plan_account_id) from context before returning to agent
    safe_context = {k: v for k, v in (task.context or {}).items() if not k.startswith("max_plan_")}
    return {
        "task_id": str(task.id),
        "title": task.title,
        "description": task.description,
        "payloads": task.payloads,
        "context": safe_context
    }

@app.post("/api/agent/artifacts/upload-url")
async def get_upload_url(
    req: UploadUrlRequest,
    session: AsyncSession = Depends(get_session),
    token: dict = Depends(verify_agent_token)
):
    """
    Agents call this to get a temporary Signed URL to upload screenshots/logs directly to GCS.
    """
    if str(req.task_id) != token.get("sub"):
        raise HTTPException(status_code=403, detail="Task ID mismatch")
        
    stmt = select(Task).where(Task.id == req.task_id)
    task = (await session.execute(stmt)).scalars().first()
    
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")
        
    import os
    safe_filename = os.path.basename(req.filename)
    url = await generate_upload_signed_url(str(req.task_id), safe_filename)
    storage_uri = f"gs://{ARTIFACT_BUCKET_NAME}/tasks/{req.task_id}/{safe_filename}"
    
    return {
        "upload_url": url,
        "storage_uri": storage_uri
    }

@app.post("/api/agent/heartbeat")
async def agent_heartbeat(
    req: HeartbeatRequest,
    session: AsyncSession = Depends(get_session),
    token: dict = Depends(verify_agent_token)
):
    """
    Endpoint for agents to send a periodic heartbeat.
    """
    if str(req.task_id) != token.get("sub"):
        raise HTTPException(status_code=403, detail="Task ID mismatch")
        
    stmt = select(Task).where(Task.id == req.task_id)
    task = (await session.execute(stmt)).scalars().first()
    
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")
        
    # On first heartbeat, clean up temporary GCS credentials (agent has downloaded them)
    if task.last_heartbeat is None:
        from app.compute import delete_credentials_from_gcs
        asyncio.create_task(delete_credentials_from_gcs(str(task.id)))

    task.last_heartbeat = datetime.utcnow()
    if req.status_message:
        task.latest_status_message = req.status_message

    session.add(task)
    await session.commit()

    return {"status": "ok"}

@app.post("/api/agent/spawn")
async def spawn_subtask(
    req: SpawnTaskRequest,
    session: AsyncSession = Depends(get_session),
    token: dict = Depends(verify_agent_token)
):
    """
    Endpoint for agents to request the creation of a new sub-task/VM.
    """
    if str(req.task_id) != token.get("sub"):
        raise HTTPException(status_code=403, detail="Task ID mismatch")
        
    # Get parent task
    parent_task = await session.get(Task, req.task_id)
    if not parent_task:
        raise HTTPException(status_code=404, detail="Parent task not found")

    new_tasks = []
    for payload in req.payloads:
        new_task = Task(
            project_id=parent_task.project_id,
            title=payload.get("title", f"Subtask of {parent_task.title}"),
            description=payload.get("description", ""),
            payloads=[payload],
            context=req.context or {},
            vm_instance_id=f"agent-vm-{uuid.uuid4().hex[:8]}",
            status=TaskStatus.QUEUED,
            created_by=f"AGENT_{parent_task.id}",
            requires_approval=req.requires_approval,
            parent_task_id=parent_task.id
        )
        session.add(new_task)
        new_tasks.append(new_task)
        
    await session.commit()
    
    # Log history for each new task
    for task in new_tasks:
        await session.refresh(task)
        history = TaskHistory(
            task_id=task.id,
            action="SPAWNED",
            details={
                "spawned_by": f"AGENT_{parent_task.id}",
                "requires_approval": req.requires_approval,
                "context": req.context
            }
        )
        session.add(history)
        
    await session.commit()

    return {
        "message": f"Successfully spawned {len(new_tasks)} subtasks",
        "task_ids": [str(t.id) for t in new_tasks],
        "requires_approval": req.requires_approval
    }

@app.post("/api/agent/status")
async def update_agent_status(
    payload: AgentStatusPayload, 
    background_tasks: BackgroundTasks,
    session: AsyncSession = Depends(get_session),
    token: dict = Depends(verify_agent_token)
):
    """
    Endpoint for agents to report progress, save git artifacts, and update task status.
    """
    if str(payload.task_id) != token.get("sub"):
        raise HTTPException(status_code=403, detail="Task ID mismatch")
        
    # 1. Fetch task
    stmt = select(Task).where(Task.id == payload.task_id)
    result = await session.execute(stmt)
    task = result.scalars().first()
    
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")
        
    # 2. Update task status
    task.status = payload.status
    # Preserve logs on the task when no artifact is created (e.g., "no changes" completion)
    if payload.logs and not payload.artifact_type:
        task.latest_status_message = payload.logs[:2000]
    session.add(task)
    
    # Broadcast status change
    await broadcast_to_project(
        str(task.project_id), 
        "task_updated", 
        {"task_id": str(task.id), "status": payload.status}
    )
    
    # 3. Create Artifact if provided
    if payload.artifact_type:
        artifact = Artifact(
            task_id=task.id,
            artifact_type=payload.artifact_type,
            commit_hash=payload.commit_hash,
            repo_url=payload.repo_url,
            pr_url=payload.pr_url,
            storage_uri=payload.storage_uri,
            logs=payload.logs
        )
        session.add(artifact)
        
        # Broadcast artifact creation (e.g., new logs or screenshot available)
        await broadcast_to_project(
            str(task.project_id), 
            "artifact_created", 
            {
                "task_id": str(task.id), 
                "artifact_type": payload.artifact_type,
                "logs": payload.logs,
                "storage_uri": payload.storage_uri
            }
        )
        
    await session.commit()
    
    # 4. External VM Termination
    # If the task is finished or failed, immediately kill the VM to save costs
    if payload.status in [TaskStatus.COMPLETED, TaskStatus.FAILED]:
        # Release Max plan account slot
        acct_id = (task.context or {}).get("max_plan_account_id")
        if acct_id:
            release_max_plan_account(acct_id)

        if task.vm_instance_id:
            background_tasks.add_task(delete_agent_vm, task.vm_instance_id)
            
        # Retry logic for failed tasks
        if payload.status == TaskStatus.FAILED and task.attempt_count < 3:
            task.attempt_count += 1
            task.status = TaskStatus.QUEUED
            task.vm_instance_id = f"agent-vm-{uuid.uuid4().hex[:8]}"
            task.last_heartbeat = None
            session.add(task)
            
            await session.commit()
            
            await broadcast_to_project(
                str(task.project_id), 
                "task_updated", 
                {"task_id": str(task.id), "status": TaskStatus.QUEUED, "attempt_count": task.attempt_count}
            )
            return {"status": "retrying", "message": f"Task {task.id} failed, retrying (Attempt {task.attempt_count}/3)"}
            
        # 5. Project State Aggregation
        # Only consider executable tasks (those with a VM) for aggregation.
        # Parent/logical tasks are updated to match the project outcome.
        all_tasks_stmt = select(Task).where(Task.project_id == task.project_id)
        all_tasks = (await session.execute(all_tasks_stmt)).scalars().all()
        executable_statuses = [t.status for t in all_tasks if t.vm_instance_id]
        parent_tasks = [t for t in all_tasks if not t.vm_instance_id and t.status not in [TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.CANCELLED]]

        project_stmt = select(Project).where(Project.id == task.project_id)
        project = (await session.execute(project_stmt)).scalars().first()

        if project and project.status != ProjectStatus.CANCELLED and executable_statuses:
            new_project_status = None
            if all(s == TaskStatus.COMPLETED for s in executable_statuses):
                new_project_status = ProjectStatus.COMPLETED
            elif any(s == TaskStatus.FAILED for s in executable_statuses):
                new_project_status = ProjectStatus.FAILED
            elif all(s in [TaskStatus.COMPLETED, TaskStatus.CANCELLED] for s in executable_statuses):
                new_project_status = ProjectStatus.CANCELLED

            if new_project_status:
                project.status = new_project_status
                session.add(project)
                # Update parent/logical tasks to match the project outcome
                terminal_task_status = TaskStatus.COMPLETED if new_project_status == ProjectStatus.COMPLETED else TaskStatus.FAILED if new_project_status == ProjectStatus.FAILED else TaskStatus.CANCELLED
                for pt in parent_tasks:
                    pt.status = terminal_task_status
                    session.add(pt)
                await session.commit()
                await broadcast_to_project(str(project.id), "project_updated", {"status": new_project_status})

    return {
        "status": "received", 
        "message": f"Status update for task {payload.task_id} recorded."
    }

async def get_github_token_for_task(task_id: uuid.UUID, session: AsyncSession) -> tuple[str, str, str]:
    stmt = select(Task).where(Task.id == task_id)
    task = (await session.execute(stmt)).scalars().first()
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")
        
    project_stmt = select(Project).where(Project.id == task.project_id)
    project = (await session.execute(project_stmt)).scalars().first()
    if not project or not project.user_id:
        raise HTTPException(status_code=404, detail="Project or user not found")
        
    if not project.github_owner or not project.github_repo:
        raise HTTPException(status_code=403, detail="Project does not have an authorized GitHub repository")
        
    integration_stmt = select(UserIntegration).where(UserIntegration.user_id == project.user_id)
    integration = (await session.execute(integration_stmt)).scalars().first()

    if integration and integration.github_installation_id:
        token = await generate_installation_access_token(
            integration.github_installation_id,
            repositories=[project.github_repo]
        )
    elif integration and integration.github_pat:
        token = await get_user_secret(integration.github_pat)
    else:
        # Fallback: discover GitHub App installation for this repo directly
        # This handles admin-created tasks where no UserIntegration exists
        from app.github import get_repo_installation_token
        token = await get_repo_installation_token(project.github_owner, project.github_repo)
        if not token:
            raise HTTPException(
                status_code=400,
                detail="GitHub integration not found. Ensure the GitHub App is installed on this repository, or register a PAT via /api/github/pat."
            )
        
    return token, project.github_owner, project.github_repo

@app.get("/api/agent/github/token")
async def agent_github_get_token(
    task_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    token: dict = Depends(verify_agent_token)
):
    """
    Agent endpoint to retrieve the GitHub token for cloning repositories.
    """
    if str(task_id) != token.get("sub"):
        raise HTTPException(status_code=403, detail="Task ID mismatch")
        
    gh_token, owner, repo = await get_github_token_for_task(task_id, session)
    return {
        "status": "success", 
        "data": {
            "token": gh_token,
            "owner": owner,
            "repo": repo
        }
    }

@app.post("/api/agent/github/commit-status")
async def agent_github_commit_status(
    req: GitHubCommitStatusRequest,
    session: AsyncSession = Depends(get_session),
    token: dict = Depends(verify_agent_token)
):
    """
    Agent endpoint to update the commit status on GitHub.
    """
    if str(req.task_id) != token.get("sub"):
        raise HTTPException(status_code=403, detail="Task ID mismatch")
        
    gh_token, owner, repo = await get_github_token_for_task(req.task_id, session)
    result = await update_commit_status(
        token=gh_token,
        owner=owner,
        repo=repo,
        sha=req.sha,
        state=req.state,
        description=req.description,
        context=req.context,
        target_url=req.target_url
    )
    return {"status": "success", "data": result}

@app.post("/api/agent/github/pr-comment")
async def agent_github_pr_comment(
    req: GitHubPRCommentRequest,
    session: AsyncSession = Depends(get_session),
    token: dict = Depends(verify_agent_token)
):
    """
    Agent endpoint to post a general comment to a PR.
    """
    if str(req.task_id) != token.get("sub"):
        raise HTTPException(status_code=403, detail="Task ID mismatch")
        
    gh_token, owner, repo = await get_github_token_for_task(req.task_id, session)
    result = await create_pr_comment(
        token=gh_token,
        owner=owner,
        repo=repo,
        pr_number=req.pr_number,
        body=req.body
    )
    return {"status": "success", "data": result}

@app.post("/api/agent/github/pr-review-comment")
async def agent_github_pr_review_comment(
    req: GitHubPRReviewCommentRequest,
    session: AsyncSession = Depends(get_session),
    token: dict = Depends(verify_agent_token)
):
    """
    Agent endpoint to post an inline code review comment to a PR.
    """
    if str(req.task_id) != token.get("sub"):
        raise HTTPException(status_code=403, detail="Task ID mismatch")
        
    gh_token, owner, repo = await get_github_token_for_task(req.task_id, session)
    result = await create_pr_review_comment(
        token=gh_token,
        owner=owner,
        repo=repo,
        pr_number=req.pr_number,
        commit_id=req.commit_id,
        path=req.path,
        line=req.line,
        body=req.body
    )
    return {"status": "success", "data": result}

@app.post("/api/agent/github/issues")
async def agent_github_create_issue(
    req: GitHubCreateIssueRequest,
    session: AsyncSession = Depends(get_session),
    token: dict = Depends(verify_agent_token)
):
    """
    Agent endpoint to create a new issue.
    """
    if str(req.task_id) != token.get("sub"):
        raise HTTPException(status_code=403, detail="Task ID mismatch")
        
    gh_token, owner, repo = await get_github_token_for_task(req.task_id, session)
    result = await create_issue(
        token=gh_token,
        owner=owner,
        repo=repo,
        title=req.title,
        body=req.body,
        assignees=req.assignees
    )
    return {"status": "success", "data": result}

@app.get("/api/agent/github/issues")
async def agent_github_get_issues(
    req: GitHubGetIssuesRequest = Depends(),
    session: AsyncSession = Depends(get_session),
    token: dict = Depends(verify_agent_token)
):
    """
    Agent endpoint to retrieve issues.
    """
    if str(req.task_id) != token.get("sub"):
        raise HTTPException(status_code=403, detail="Task ID mismatch")
        
    gh_token, owner, repo = await get_github_token_for_task(req.task_id, session)
    result = await get_issues(
        token=gh_token,
        owner=owner,
        repo=repo,
        state=req.state
    )
    return {"status": "success", "data": result}

@app.post("/api/agent/github/branches")
async def agent_github_create_branch(
    req: GitHubCreateBranchRequest,
    session: AsyncSession = Depends(get_session),
    token: dict = Depends(verify_agent_token)
):
    """
    Agent endpoint to create a new branch.
    """
    if str(req.task_id) != token.get("sub"):
        raise HTTPException(status_code=403, detail="Task ID mismatch")
        
    gh_token, owner, repo = await get_github_token_for_task(req.task_id, session)
    result = await create_branch(
        token=gh_token,
        owner=owner,
        repo=repo,
        branch_name=req.branch_name,
        sha=req.sha
    )
    return {"status": "success", "data": result}

@app.post("/api/agent/github/pull-requests")
async def agent_github_create_pr(
    req: GitHubCreatePRRequest,
    session: AsyncSession = Depends(get_session),
    token: dict = Depends(verify_agent_token)
):
    """
    Agent endpoint to create a pull request.
    """
    if str(req.task_id) != token.get("sub"):
        raise HTTPException(status_code=403, detail="Task ID mismatch")
        
    gh_token, owner, repo = await get_github_token_for_task(req.task_id, session)
    result = await create_pull_request(
        token=gh_token,
        owner=owner,
        repo=repo,
        title=req.title,
        head=req.head,
        base=req.base,
        body=req.body
    )
    return {"status": "success", "data": result}

@app.post("/api/projects/{project_id}/cancel")
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
    project_stmt = select(Project).where(Project.id == project_id, Project.user_id == current_user["user_id"])
    project = (await session.execute(project_stmt)).scalars().first()
    
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

    for task in active_tasks:
        task.status = TaskStatus.CANCELLED
        session.add(task)

        if task.vm_instance_id:
            background_tasks.add_task(delete_agent_vm, task.vm_instance_id)
            
        await broadcast_to_project(
            str(project_id), 
            "task_updated", 
            {"task_id": str(task.id), "status": TaskStatus.CANCELLED}
        )
        
    await session.commit()
    await broadcast_to_project(str(project_id), "project_updated", {"status": ProjectStatus.CANCELLED})
    
    return {"status": "cancelled", "message": f"Project {project_id} and {len(active_tasks)} tasks cancelled."}

@app.get("/api/projects/{project_id}/status")
@limiter.limit("60/minute")
async def get_project_status(
    request: Request,
    project_id: uuid.UUID, 
    session: AsyncSession = Depends(get_session),
    current_user: dict = Depends(get_current_user)
):
    """
    Endpoint for the frontend to poll or stream the current status of the deployment 
    and agent activities.
    """
    project_stmt = select(Project).where(Project.id == project_id, Project.user_id == current_user["user_id"])
    project = (await session.execute(project_stmt)).scalars().first()
    
    if not project:
        raise HTTPException(status_code=404, detail="Project not found or unauthorized")
        
    tasks_stmt = select(Task).where(Task.project_id == project_id)
    tasks = (await session.execute(tasks_stmt)).scalars().all()
    
    return {
        "project_id": project.id,
        "name": project.name,
        "status": project.status, 
        "tasks": [
            {
                "task_id": t.id,
                "status": t.status,
                "vm_instance_id": t.vm_instance_id,
                "attempt_count": t.attempt_count
            } for t in tasks
        ]
    }

class GitHubPATInstallRequest(BaseModel):
    pat: str = Field(..., min_length=10)

@app.post("/api/github/pat")
@limiter.limit("5/minute")
async def register_github_pat(
    request: Request,
    req: GitHubPATInstallRequest,
    session: AsyncSession = Depends(get_session),
    current_user: dict = Depends(get_current_user)
):
    """
    Registers a GitHub Fine-Grained PAT for the current user.
    """
    # Validate PAT
    await validate_fine_grained_pat(req.pat)
    
    # Store the PAT securely in GCP Secret Manager
    secret_name = await store_user_secret(current_user["user_id"], "github-pat", req.pat)
    
    stmt = select(UserIntegration).where(UserIntegration.user_id == current_user["user_id"])
    integration = (await session.execute(stmt)).scalars().first()
    
    if integration:
        integration.github_pat = secret_name
        integration.updated_at = datetime.utcnow()
    else:
        integration = UserIntegration(
            user_id=current_user["user_id"],
            github_pat=secret_name
        )
    
    session.add(integration)
    await session.commit()
    
    return {"status": "success", "message": "GitHub PAT registered successfully."}

@app.get("/api/github/oauth/authorize")
@limiter.limit("10/minute")
async def github_oauth_authorize(
    request: Request,
    current_user: dict = Depends(get_current_user)
):
    """
    Returns the GitHub OAuth URL. The frontend redirects the user there.
    A signed state token ties the OAuth flow to the authenticated user.
    """
    if not GITHUB_CLIENT_ID:
        raise HTTPException(status_code=500, detail="GitHub OAuth not configured (GITHUB_CLIENT_ID missing)")

    # Sign a state token so the callback can verify which user initiated the flow
    from app.auth import AGENT_JWT_SECRET, AGENT_JWT_ALGORITHM
    state = jwt.encode(
        {"sub": current_user["user_id"], "exp": datetime.utcnow() + timedelta(minutes=10), "type": "github_oauth"},
        AGENT_JWT_SECRET, algorithm=AGENT_JWT_ALGORITHM
    )

    url = (
        f"https://github.com/login/oauth/authorize"
        f"?client_id={GITHUB_CLIENT_ID}"
        f"&state={state}"
    )
    return {"url": url}


@app.get("/api/github/oauth/callback")
@limiter.limit("10/minute")
async def github_oauth_callback(
    request: Request,
    code: str = Query(...),
    state: str = Query(...),
    session: AsyncSession = Depends(get_session),
):
    """
    GitHub OAuth callback. Exchanges the code for a user token, verifies the user's
    installations, and auto-registers them. This proves the user actually owns the
    GitHub account — no spoofing possible.
    """
    # Verify the state token to identify the Firebase user who started the flow
    from app.auth import AGENT_JWT_SECRET, AGENT_JWT_ALGORITHM
    try:
        state_payload = jwt.decode(state, AGENT_JWT_SECRET, algorithms=[AGENT_JWT_ALGORITHM])
        if state_payload.get("type") != "github_oauth":
            raise HTTPException(status_code=400, detail="Invalid state token")
        firebase_user_id = state_payload["sub"]
    except jwt.ExpiredSignatureError:
        raise HTTPException(status_code=400, detail="OAuth state expired — please try again")
    except jwt.PyJWTError:
        raise HTTPException(status_code=400, detail="Invalid OAuth state")

    # Exchange code for GitHub user access token
    token_data = await exchange_oauth_code(code)
    user_token = token_data["access_token"]

    # Get the GitHub user's profile to confirm identity
    gh_user = await get_github_user(user_token)
    gh_login = gh_user["login"]

    # Discover which of this App's installations the user can access
    installations = await get_user_installations(user_token)
    if not installations:
        raise HTTPException(
            status_code=400,
            detail=f"No Blueprint App installations found for GitHub user '{gh_login}'. Install the app first at https://github.com/apps"
        )

    # Use the first installation (most users have one)
    installation = installations[0]
    installation_id = str(installation["id"])
    account_name = installation["account"]["login"]

    # Save to UserIntegration
    stmt = select(UserIntegration).where(UserIntegration.user_id == firebase_user_id)
    integration = (await session.execute(stmt)).scalars().first()

    if integration:
        integration.github_installation_id = installation_id
        integration.github_account_name = account_name
        integration.updated_at = datetime.utcnow()
    else:
        integration = UserIntegration(
            user_id=firebase_user_id,
            github_installation_id=installation_id,
            github_account_name=account_name
        )

    session.add(integration)
    await session.commit()

    # Redirect to the admin UI with success
    admin_url = os.getenv("ADMIN_UI_URL", "/")
    return JSONResponse(
        status_code=200,
        content={
            "status": "success",
            "github_user": gh_login,
            "account_name": account_name,
            "installation_id": installation_id,
            "installations_count": len(installations),
        }
    )

@app.post("/api/github/webhook")
async def github_webhook(
    request: Request,
    background_tasks: BackgroundTasks,
    session: AsyncSession = Depends(get_session)
):
    """
    Receives webhooks from the GitHub App (e.g., push, pull_request).
    """
    signature = request.headers.get("x-hub-signature-256")
    if not signature or not GITHUB_WEBHOOK_SECRET:
        raise HTTPException(status_code=401, detail="Missing signature or webhook secret")
        
    payload = await request.body()
    
    # Verify signature
    expected_signature = "sha256=" + hmac.new(
        GITHUB_WEBHOOK_SECRET.encode(), payload, hashlib.sha256
    ).hexdigest()
    
    if not hmac.compare_digest(signature, expected_signature):
        raise HTTPException(status_code=401, detail="Invalid signature")
        
    event_type = request.headers.get("x-github-event")
    data = json.loads(payload)
    
    # Handle specific events
    if event_type == "push":
        # Extract repo and branch
        repo_name = data.get("repository", {}).get("full_name")
        branch = data.get("ref", "").replace("refs/heads/", "")
        
        print(f"[WEBHOOK] Received push event for {repo_name} on branch {branch}")
        
        if repo_name:
            owner, repo = repo_name.split("/", 1)
            # Find projects associated with this repo
            stmt = select(Project).where(
                Project.github_owner == owner,
                Project.github_repo == repo
            )
            projects = (await session.execute(stmt)).scalars().all()
            
            for project in projects:
                # Create a task for each project
                vm_name = f"agent-vm-{uuid.uuid4().hex[:8]}"
                task = Task(
                    project_id=project.id,
                    title=f"Push to {branch} on {repo_name}",
                    payloads=[{"type": "github_push", "branch": branch}],
                    vm_instance_id=vm_name,
                    status=TaskStatus.QUEUED,
                    created_by="WEBHOOK"
                )
                session.add(task)
                await session.commit()
                await session.refresh(task)
                
                print(f"[WEBHOOK] Triggered task {task.id} for project {project.id}")
        
    elif event_type == "pull_request":
        action = data.get("action")
        pr_number = data.get("pull_request", {}).get("number")
        repo_name = data.get("repository", {}).get("full_name")
        
        print(f"[WEBHOOK] Received PR event '{action}' for {repo_name}#{pr_number}")
        
        if repo_name and action in ["opened", "synchronize", "reopened"]:
            owner, repo = repo_name.split("/", 1)
            stmt = select(Project).where(
                Project.github_owner == owner,
                Project.github_repo == repo
            )
            projects = (await session.execute(stmt)).scalars().all()
            
            for project in projects:
                vm_name = f"agent-vm-{uuid.uuid4().hex[:8]}"
                task = Task(
                    project_id=project.id,
                    title=f"PR #{pr_number} ({action}) on {repo_name}",
                    payloads=[{"type": "github_pr", "pr_number": pr_number, "action": action}],
                    vm_instance_id=vm_name,
                    status=TaskStatus.QUEUED,
                    created_by="WEBHOOK"
                )
                session.add(task)
                await session.commit()
                await session.refresh(task)
                
                print(f"[WEBHOOK] Triggered task {task.id} for project {project.id} (PR #{pr_number})")
        
    return {"status": "received"}

# --- Static Files for Frontend (One-Line Deploy) ---
static_dir = os.path.join(os.path.dirname(__file__), "..", "static")
if os.path.exists(static_dir):
    # Mount the assets directory specifically
    assets_dir = os.path.join(static_dir, "assets")
    if os.path.exists(assets_dir):
        app.mount("/assets", StaticFiles(directory=assets_dir), name="assets")
    
    # Serve index.html for all other non-API routes (SPA fallback)
    @app.get("/{full_path:path}")
    async def serve_spa(full_path: str):
        # Don't intercept API routes
        if full_path.startswith("api/"):
            raise HTTPException(status_code=404, detail="API route not found")
            
        # Check if the requested file exists (e.g., favicon.ico, robots.txt)
        file_path = os.path.abspath(os.path.join(static_dir, full_path))
        
        # Ensure the resolved path is strictly within the static directory
        if not file_path.startswith(os.path.abspath(static_dir)):
            raise HTTPException(status_code=403, detail="Access denied")
            
        if os.path.isfile(file_path):
            return FileResponse(file_path)
            
        # Fallback to index.html for SPA routing
        index_path = os.path.join(static_dir, "index.html")
        if os.path.exists(index_path):
            return FileResponse(index_path)
            
        raise HTTPException(status_code=404, detail="Not found")

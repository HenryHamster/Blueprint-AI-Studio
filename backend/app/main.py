import os
from contextlib import asynccontextmanager
from fastapi import FastAPI, Depends, HTTPException, Header, BackgroundTasks, Request, Query, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse, FileResponse, RedirectResponse
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
from datetime import datetime, timedelta, timezone

from app.database import init_db, get_session, AsyncSessionLocal, engine
from app.models import Project, Task, Artifact, TaskHistory, ProjectStatus, TaskStatus, ArtifactType, UserIntegration, MaxPlanAccount
from app.compute import provision_agent_vm, delete_agent_vm, cleanup_orphaned_vms
from app.storage import generate_upload_signed_url, upload_json_payload, ARTIFACT_BUCKET_NAME
from app.secrets_manager import store_user_secret, get_user_secret
from app.auth import get_current_user, verify_agent_jwt, create_ws_ticket, verify_ws_ticket
from app.telemetry import setup_telemetry
from app.admin import router as admin_router
from app.security import scan_pr_for_injection
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
    get_branch_sha,
    delete_branch,
    create_pull_request,
    merge_pull_request,
    get_pull_request,
    get_pr_files,
    get_check_runs,
    close_github_client,
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

# --- Runtime Config & Autonomous State (shared with admin.py) ---
from app.shared_state import runtime_config as _runtime_config, autonomous_failure_counts as _autonomous_failure_counts
import app.shared_state as _shared_state

# --- Max Plan Account Pool ---
# In-memory cache of DB-persisted accounts. Keeps active_tasks count (runtime only).
# Each entry: {id, name, oauth_token, claude_config_json, enabled, active_tasks, total_tasks, token_created_at}
_max_plan_pool: List[Dict[str, Any]] = []

async def load_max_plan_pool():
    """Load Max plan accounts from database into in-memory pool on startup.
    Reconciles active_tasks from DB by counting tasks currently PROVISIONING/IN_PROGRESS."""
    async with AsyncSessionLocal() as session:
        result = await session.execute(select(MaxPlanAccount))
        accounts = result.scalars().all()

        # Count actually active tasks per account to reconcile after restarts
        active_tasks = (await session.execute(
            select(Task).where(Task.status.in_([TaskStatus.PROVISIONING, TaskStatus.IN_PROGRESS]))
        )).scalars().all()
        active_per_account: Dict[str, int] = {}
        for t in active_tasks:
            acct_id = (t.context or {}).get("max_plan_account_id")
            if acct_id:
                active_per_account[acct_id] = active_per_account.get(acct_id, 0) + 1

        _max_plan_pool.clear()
        for acct in accounts:
            acct_id = str(acct.id)
            _max_plan_pool.append({
                "id": acct_id,
                "name": acct.name,
                "oauth_token": acct.oauth_token,
                "claude_config_json": acct.claude_config_json,
                "enabled": acct.enabled,
                "active_tasks": active_per_account.get(acct_id, 0),
                "total_tasks": acct.total_tasks,
                "token_created_at": acct.token_created_at.isoformat() if acct.token_created_at else None,
            })
        total_active = sum(active_per_account.values())
        print(f"[STARTUP] Loaded {len(_max_plan_pool)} Max plan accounts ({total_active} active tasks reconciled)")

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




async def _spawn_research_task(project: Project, session: AsyncSession):
    """Spawn a research task for a project if conditions are met."""
    from sqlalchemy import func

    # Guard: autonomous must be enabled
    if not project.autonomous_enabled:
        return
    # Guard: must have active directions
    active_directions = [d for d in (project.directions or []) if d.get("status") == "active"]
    if not active_directions:
        return
    # Guard: no existing research task already running/queued
    active_tasks = (await session.execute(
        select(Task).where(
            Task.project_id == project.id,
            Task.status.in_([TaskStatus.QUEUED, TaskStatus.PROVISIONING, TaskStatus.IN_PROGRESS]),
        )
    )).scalars().all()
    for t in active_tasks:
        if ((t.payloads or [{}])[0]).get("type") == "research":
            return
    # Guard: rate limit autonomous tasks
    one_hour_ago = datetime.utcnow() - timedelta(hours=1)
    recent_auto = (await session.execute(
        select(func.count()).select_from(Task).where(
            Task.project_id == project.id,
            Task.created_by == "ORCHESTRATOR",
            Task.created_at >= one_hour_ago,
        )
    )).scalar() or 0
    if recent_auto >= _runtime_config["max_autonomous_tasks_per_hour"]:
        return

    # Infer base_branch from the most recent task in this project, or default to main
    recent_task = (await session.execute(
        select(Task).where(Task.project_id == project.id)
        .order_by(Task.created_at.desc()).limit(1)
    )).scalars().first()
    base_branch = ((recent_task.payloads or [{}])[0]).get("base_branch", "main") if recent_task else "main"

    new_task = Task(
        project_id=project.id,
        title="Research: autonomous improvement scan",
        description="Automated research task triggered by autonomous loop",
        payloads=[{
            "type": "research",
            "directions": active_directions,
            "base_branch": base_branch,
        }],
        vm_instance_id=f"agent-vm-{uuid.uuid4().hex[:8]}",
        status=TaskStatus.QUEUED,
        created_by="ORCHESTRATOR",
    )
    session.add(new_task)
    history = TaskHistory(
        task_id=new_task.id, action="SPAWNED",
        details={"spawned_by": "AUTONOMOUS_LOOP", "trigger": "post_merge_or_idle"}
    )
    session.add(history)
    await session.commit()
    await session.refresh(new_task)
    print(f"[AUTONOMOUS] Spawned research task {new_task.id} for project {project.id}")


async def _check_autonomous_research(owner: str, repo: str):
    """Post-merge hook: check if we should spawn a research task."""
    try:
        async with AsyncSessionLocal() as session:
            project = (await session.execute(
                select(Project).where(
                    Project.github_owner == owner,
                    Project.github_repo == repo,
                    Project.autonomous_enabled == True,
                )
            )).scalars().first()
            if not project:
                return

            # Check if queue is empty for this project
            from sqlalchemy import func
            queued = (await session.execute(
                select(func.count()).select_from(Task).where(
                    Task.project_id == project.id,
                    Task.status.in_([TaskStatus.QUEUED, TaskStatus.PROVISIONING, TaskStatus.IN_PROGRESS]),
                )
            )).scalar() or 0

            if queued == 0:
                await _spawn_research_task(project, session)
    except Exception as e:
        print(f"[AUTONOMOUS] Error checking research trigger for {owner}/{repo}: {e}")


async def _check_idle_research():
    """Periodic check: spawn research if projects are idle with active directions."""
    now = datetime.now(timezone.utc)
    if _shared_state.last_idle_check and (now - _shared_state.last_idle_check) < timedelta(minutes=30):
        return
    _shared_state.last_idle_check = now

    try:
        async with AsyncSessionLocal() as session:
            from sqlalchemy import func

            projects = (await session.execute(
                select(Project).where(Project.autonomous_enabled == True)
            )).scalars().all()

            for project in projects:
                active_directions = [d for d in (project.directions or []) if d.get("status") == "active"]
                if not active_directions:
                    continue

                queued = (await session.execute(
                    select(func.count()).select_from(Task).where(
                        Task.project_id == project.id,
                        Task.status.in_([TaskStatus.QUEUED, TaskStatus.PROVISIONING, TaskStatus.IN_PROGRESS]),
                    )
                )).scalar() or 0

                if queued == 0:
                    await _spawn_research_task(project, session)
    except Exception as e:
        print(f"[AUTONOMOUS] Error in idle research check: {e}")


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
                    # Determine why the reaper flagged this task
                    if task.last_heartbeat:
                        age = int((now - task.last_heartbeat).total_seconds())
                        reap_reason = f"No heartbeat for {age}s (last: {task.last_heartbeat.isoformat()})"
                    elif task.started_at:
                        age = int((now - task.started_at).total_seconds())
                        reap_reason = f"No heartbeat received after {age}s since provisioning"
                    else:
                        age = int((now - task.created_at).total_seconds())
                        reap_reason = f"Never started — {age}s since creation, no heartbeat"

                    print(f"[REAPER] Terminating orphaned task {task.id} (VM: {task.vm_instance_id}) — {reap_reason}")

                    if task.vm_instance_id:
                        asyncio.create_task(delete_agent_vm(task.vm_instance_id))

                    # Release Max Plan account slot
                    acct_id = (task.context or {}).get("max_plan_account_id")
                    if acct_id:
                        release_max_plan_account(acct_id)

                    if task.attempt_count < _runtime_config["max_retries"]:
                        # Check if this is a parent with children — re-evaluate instead of re-run
                        children = (await session.execute(
                            select(Task).where(Task.parent_task_id == task.id)
                        )).scalars().all()
                        if children:
                            print(f"[REAPER] Re-evaluating parent task {task.id}")
                            await _requeue_parent_task(task, session)
                        else:
                            print(f"[REAPER] Retrying task {task.id} (Attempt {task.attempt_count + 1}/3)")
                            task.latest_status_message = f"Reaped (attempt {task.attempt_count + 1}/{_runtime_config['max_retries']}): {reap_reason}"
                            task.attempt_count += 1
                            task.status = TaskStatus.QUEUED
                            task.vm_instance_id = f"agent-vm-{uuid.uuid4().hex[:8]}"
                            task.last_heartbeat = None
                            session.add(task)

                        await broadcast_to_project(
                            str(task.project_id),
                            "task_updated",
                            {"task_id": str(task.id), "status": task.status, "attempt_count": task.attempt_count}
                        )
                    else:
                        print(f"[REAPER] Task {task.id} failed after {_runtime_config['max_retries']} attempts.")
                        task.status = TaskStatus.FAILED
                        task.latest_status_message = f"Failed after {_runtime_config['max_retries']} attempts — last reap: {reap_reason}"
                        session.add(task)

                        # Broadcast failure to frontend
                        await broadcast_to_project(
                            str(task.project_id),
                            "task_updated",
                            {"task_id": str(task.id), "status": TaskStatus.FAILED}
                        )

                if orphaned_tasks:
                    await session.commit()

                    # Trigger rollup checks for failed tasks with parents (after commit)
                    for task in orphaned_tasks:
                        if task.status == TaskStatus.FAILED and task.parent_task_id:
                            try:
                                await _check_and_trigger_rollup(task, session)
                            except Exception as e:
                                print(f"[REAPER] Rollup check failed for task {task.id}: {e}")
                    
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
            global _orchestrator_last_cycle
            _orchestrator_last_cycle = datetime.now(timezone.utc)

            async with AsyncSessionLocal() as session:
                # Fetch all active tasks once — used for both concurrency limit and counter reconciliation
                active_tasks_result = (await session.execute(
                    select(Task).where(Task.status.in_([TaskStatus.PROVISIONING, TaskStatus.IN_PROGRESS]))
                )).scalars().all()

                # Enforce concurrency limit
                active_count = len(active_tasks_result)
                slots_available = max(0, _runtime_config["max_concurrent_vms"] - active_count)
                if slots_available == 0:
                    await asyncio.sleep(5)
                    continue

                # Reconcile Max Plan active_tasks counters from DB each cycle
                active_per_account: Dict[str, int] = {}
                for t in active_tasks_result:
                    acct_id = (t.context or {}).get("max_plan_account_id")
                    if acct_id:
                        active_per_account[acct_id] = active_per_account.get(acct_id, 0) + 1
                for acct in _max_plan_pool:
                    acct["active_tasks"] = active_per_account.get(acct["id"], 0)

                # Find tasks that are QUEUED, not awaiting approval, and haven't been picked up yet
                stmt = select(Task).where(
                    Task.status == TaskStatus.QUEUED,
                    Task.requires_approval == False
                ).limit(min(slots_available, 20))  # Fetch extra to filter deps
                candidate_tasks = (await session.execute(stmt)).scalars().all()

                # Filter out tasks with unmet dependencies
                queued_tasks = []
                for task in candidate_tasks:
                    deps = task.depends_on or []
                    if not deps:
                        queued_tasks.append(task)
                        continue

                    # Check all dependencies are COMPLETED with PR merged
                    dep_uuids = [uuid.UUID(d) for d in deps]
                    dep_result = await session.execute(select(Task).where(Task.id.in_(dep_uuids)))
                    dep_tasks = dep_result.scalars().all()

                    all_met = True
                    any_failed = False
                    for dt in dep_tasks:
                        if dt.status in [TaskStatus.FAILED, TaskStatus.CANCELLED]:
                            any_failed = True
                            break
                        if dt.status != TaskStatus.COMPLETED:
                            all_met = False
                            continue
                        # For create tasks, also require PR to be merged
                        dt_type = ((dt.payloads or [{}])[0]).get("type", "create")
                        if dt_type in ("create", "") and not await _verify_pr_merged(dt, session):
                            all_met = False

                    if any_failed:
                        # Use separate session to avoid polluting the main dispatch session
                        async with AsyncSessionLocal() as cancel_session:
                            cancel_task = await cancel_session.get(Task, task.id)
                            if cancel_task:
                                cancel_task.status = TaskStatus.CANCELLED
                                cancel_task.latest_status_message = "Cancelled: dependency task failed"
                                cancel_session.add(cancel_task)
                                await cancel_session.commit()
                        await broadcast_to_project(str(task.project_id), "task_updated",
                                                   {"task_id": str(task.id), "status": TaskStatus.CANCELLED})
                        print(f"[ORCHESTRATOR] Cancelled task {task.id} — dependency failed")
                    elif all_met:
                        queued_tasks.append(task)

                # Respect slot limit after filtering
                queued_tasks = queued_tasks[:slots_available]

                print(f"[ORCHESTRATOR] Cycle: {len(queued_tasks)} queued, {active_count} active, {slots_available} slots, pool={len(_max_plan_pool)} accounts")

                for task in queued_tasks:
                    print(f"[ORCHESTRATOR] Picking up queued task {task.id} (VM: {task.vm_instance_id})")

                    # Select Max plan account (primary) or fall back to API key
                    account = select_max_plan_account()
                    oauth_token = None
                    claude_config_json = None
                    account_id = None
                    if account:
                        account_id = account["id"]
                        oauth_token = account.get("oauth_token")
                        claude_config_json = account.get("claude_config_json")
                        print(f"[ORCHESTRATOR] Using Max plan account '{account['name']}'")
                    else:
                        # No Max plan account — check for API key fallback
                        if os.getenv("ANTHROPIC_API_KEY"):
                            print(f"[ORCHESTRATOR] No Max plan accounts, using ANTHROPIC_API_KEY fallback")
                        else:
                            # No credentials at all — skip this task, don't waste a VM
                            print(f"[ORCHESTRATOR] No credentials available — skipping task {task.id}")
                            async with AsyncSessionLocal() as msg_session:
                                msg_task = await msg_session.get(Task, task.id)
                                if msg_task:
                                    msg_task.latest_status_message = "Waiting for credentials — add a Max plan account or set ANTHROPIC_API_KEY"
                                    msg_session.add(msg_task)
                                    await msg_session.commit()
                            continue

                    # Store which account this task uses for cleanup on completion
                    task.context = {**(task.context or {}), "max_plan_account_id": account_id}

                    # Mark as PROVISIONING to prevent other workers from picking it up
                    task.status = TaskStatus.PROVISIONING
                    task.started_at = datetime.utcnow()
                    session.add(task)
                    try:
                        await session.commit()
                        print(f"[ORCHESTRATOR] Task {task.id} committed as PROVISIONING — launching VM")
                    except Exception as e:
                        import traceback
                        print(f"[ORCHESTRATOR] FAILED to commit PROVISIONING for task {task.id}: {e}")
                        traceback.print_exc()
                        await session.rollback()
                        continue

                    # Trigger GCP Compute Engine provisioning in the background
                    # All values passed as args to avoid stale closure captures across loop iterations
                    async def _provision_and_handle(vm_id: str, t_id: uuid.UUID, p_id: uuid.UUID,
                                                    payloads: list, token: str, config_json: str,
                                                    acct_id_for_release: str):
                        payload_uri = None
                        if payloads:
                            try:
                                payload_uri = await upload_json_payload(str(t_id), payloads)
                            except Exception as e:
                                print(f"[ORCHESTRATOR] Failed to upload payload for task {t_id}: {e}")

                        success = await provision_agent_vm(vm_id, t_id, payload_uri,
                                                           oauth_token=token, claude_config_json=config_json)
                        if not success:
                            print(f"[ORCHESTRATOR] Failed to provision VM {vm_id} for task {t_id}")
                            # Release the Max Plan account slot
                            if acct_id_for_release:
                                release_max_plan_account(acct_id_for_release)
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

                    asyncio.create_task(_provision_and_handle(
                        task.vm_instance_id, task.id, task.project_id,
                        task.payloads, oauth_token, claude_config_json, account_id
                    ))
                    
                    await broadcast_to_project(
                        str(task.project_id),
                        "task_updated",
                        {"task_id": str(task.id), "status": TaskStatus.PROVISIONING}
                    )

                # Check tasks waiting on CI (review approved, CI pending)
                CI_WAIT_TIMEOUT_MINUTES = 30
                ci_waiting_tasks = (await session.execute(
                    select(Task).where(Task.status == TaskStatus.EVALUATING)
                )).scalars().all()
                for ci_task in ci_waiting_tasks:
                    ci_loop = (ci_task.context or {}).get("review_loop", {})
                    if not ci_loop.get("ci_waiting"):
                        continue
                    ci_pr_number = ci_loop.get("pr_number")
                    if not ci_pr_number:
                        continue

                    # Timeout: if CI has been pending too long, proceed to merge anyway
                    ci_wait_started = ci_loop.get("ci_wait_started")
                    if ci_wait_started:
                        try:
                            started = datetime.fromisoformat(ci_wait_started)
                            if (datetime.utcnow() - started).total_seconds() > CI_WAIT_TIMEOUT_MINUTES * 60:
                                print(f"[CI] PR #{ci_pr_number} CI wait timed out after {CI_WAIT_TIMEOUT_MINUTES}min")
                                ci_task.status = TaskStatus.FAILED
                                ci_task.latest_status_message = f"CI wait timed out after {CI_WAIT_TIMEOUT_MINUTES}min on PR #{ci_pr_number} — manual inspection required"
                                session.add(ci_task)
                                await session.commit()
                                continue
                        except (ValueError, TypeError) as e:
                            print(f"[CI] Malformed ci_wait_started for task {ci_task.id}: {ci_wait_started} ({e})")

                    try:
                        ci_gh_token, ci_owner, ci_repo = await get_github_token_for_task(ci_task.id, session)

                        # Re-fetch current PR head to handle force-pushes
                        pr_data = await get_pull_request(ci_gh_token, ci_owner, ci_repo, ci_pr_number)
                        pr_state = pr_data.get("state", "open")

                        # Check if PR was already merged (prevents double merge)
                        if pr_state == "closed" or pr_data.get("merged"):
                            ci_task.status = TaskStatus.COMPLETED
                            ci_task.latest_status_message = f"PR #{ci_pr_number} already merged/closed"
                            session.add(ci_task)
                            await session.commit()
                            print(f"[CI] PR #{ci_pr_number} already merged/closed, skipping")
                            continue

                        # Check for merge conflicts — CI won't trigger on conflicting PRs
                        if pr_data.get("mergeable") is False:
                            ci_payload = (ci_task.payloads or [{}])[0]
                            ci_branch = ci_loop.get("branch", "") or ci_payload.get("branch", "")
                            ci_base = ci_loop.get("base_branch", "") or ci_payload.get("base_branch", "main")
                            ci_orig_id = ci_loop.get("original_task_id", "") or ci_payload.get("original_task_id", "")
                            fix_prompt = (
                                f"PR #{ci_pr_number} has merge conflicts and CI cannot run.\n\n"
                                f"Rebase the branch onto {ci_base} and resolve all conflicts."
                            )
                            fix_task = await _spawn_pipeline_task(
                                ci_task, "fix", session, ci_pr_number, ci_branch, ci_base,
                                fix_prompt=fix_prompt, original_task_id=ci_orig_id
                            )
                            if fix_task:
                                ci_task.status = TaskStatus.COMPLETED
                                ci_task.latest_status_message = f"PR #{ci_pr_number} has merge conflicts, spawned fix → {fix_task.id}"
                                print(f"[CI] PR #{ci_pr_number} has merge conflicts, spawned fix → {fix_task.id}")
                            else:
                                ci_task.status = TaskStatus.FAILED
                                ci_task.latest_status_message = f"PR #{ci_pr_number} has merge conflicts, fix not spawned (iteration cap)"
                            session.add(ci_task)
                            await session.commit()
                            continue

                        ci_head_sha = pr_data.get("head", {}).get("sha", "")
                        if not ci_head_sha:
                            ci_task.latest_status_message = f"CI waiting on PR #{ci_pr_number} — HEAD SHA unavailable"
                            session.add(ci_task)
                            await session.commit()
                            print(f"[CI] PR #{ci_pr_number} has no HEAD SHA — skipping CI check this cycle")
                            continue

                        ci_result = await get_check_runs(ci_gh_token, ci_owner, ci_repo, ci_head_sha)
                        if ci_result["status"] != "completed":
                            details = [d["name"] for d in ci_result.get("details", []) if d.get("status") in ("queued", "in_progress")]
                            ci_task.latest_status_message = f"CI in progress on PR #{ci_pr_number}: {', '.join(details) or 'waiting for checks'}..."
                            session.add(ci_task)
                            await session.commit()
                            continue

                        ci_payload = (ci_task.payloads or [{}])[0]
                        ci_branch = ci_loop.get("branch", "") or ci_payload.get("branch", "")
                        ci_base = ci_loop.get("base_branch", "") or ci_payload.get("base_branch", "main")
                        ci_orig_id = ci_loop.get("original_task_id", "") or ci_payload.get("original_task_id", "")

                        if ci_result["conclusion"] == "success":
                            # CI passed — merge
                            merge_result = await merge_pull_request(ci_gh_token, ci_owner, ci_repo, ci_pr_number)
                            if merge_result.get("merged"):
                                await create_pr_comment(ci_gh_token, ci_owner, ci_repo, ci_pr_number,
                                    "**Blueprint CI + Review**: All checks passed, PR auto-merged.")
                                ci_task.status = TaskStatus.COMPLETED
                                ci_task.latest_status_message = f"CI passed, PR #{ci_pr_number} merged"
                                if ci_orig_id:
                                    try:
                                        orig = await session.get(Task, uuid.UUID(ci_orig_id))
                                        if orig:
                                            orig.context = {**(orig.context or {}), "pr_merged": True}
                                            if orig.status == TaskStatus.EVALUATING and not (orig.context or {}).get("review_loop", {}).get("ci_waiting"):
                                                orig.status = TaskStatus.COMPLETED
                                                orig.latest_status_message = "Rollup PR merged"
                                            session.add(orig)
                                    except Exception as e:
                                        print(f"[CI] Failed to set pr_merged on {ci_orig_id}: {e}")
                                print(f"[CI] PR #{ci_pr_number} CI passed, merged")
                            else:
                                ci_task.status = TaskStatus.COMPLETED
                                ci_task.latest_status_message = f"CI passed but merge blocked: {merge_result.get('message', 'unknown')}"
                                print(f"[CI] PR #{ci_pr_number} CI passed but merge blocked")
                        else:
                            # CI failed — check for existing active fix before spawning
                            active_tasks = (await session.execute(
                                select(Task).where(
                                    Task.status.in_([TaskStatus.QUEUED, TaskStatus.PROVISIONING, TaskStatus.IN_PROGRESS]),
                                )
                            )).scalars().all()
                            existing_fix = next(
                                (t for t in active_tasks
                                 if (t.payloads or [{}])[0].get("type") == "fix"
                                 and (t.payloads or [{}])[0].get("pr_number") == ci_pr_number),
                                None
                            )
                            if existing_fix:
                                ci_task.status = TaskStatus.COMPLETED
                                ci_task.latest_status_message = f"CI failed on PR #{ci_pr_number}, fix already active → {existing_fix.id}"
                                print(f"[CI] PR #{ci_pr_number} CI failed, fix already exists → {existing_fix.id}")
                                session.add(ci_task)
                                await session.commit()
                                continue
                            ci_errors = ci_result.get("failed_logs", "CI checks failed")
                            await create_pr_comment(ci_gh_token, ci_owner, ci_repo, ci_pr_number,
                                f"**Blueprint CI**: Checks failed.\n\n```\n{ci_errors[:2000]}\n```")
                            ci_fix_prompt = (
                                f"CI checks failed on PR #{ci_pr_number} (code review already approved).\n\n"
                                f"CI errors:\n{ci_errors}\n\n"
                                f"Fix only the build/lint/test errors — the review is already approved."
                            )
                            fix_task = await _spawn_pipeline_task(
                                ci_task, "fix", session, ci_pr_number, ci_branch, ci_base,
                                fix_prompt=ci_fix_prompt,
                                original_task_id=ci_orig_id
                            )
                            if fix_task:
                                ci_task.status = TaskStatus.COMPLETED
                                ci_task.latest_status_message = f"CI failed on PR #{ci_pr_number}, spawned fix → {fix_task.id}"
                                print(f"[CI] PR #{ci_pr_number} CI failed, spawned fix → {fix_task.id}")
                            else:
                                ci_task.status = TaskStatus.FAILED
                                ci_task.latest_status_message = f"CI failed on PR #{ci_pr_number}, fix not spawned (iteration cap)"
                                print(f"[CI] PR #{ci_pr_number} CI failed, fix spawn returned None")

                        session.add(ci_task)
                        await session.commit()
                        await broadcast_to_project(str(ci_task.project_id), "task_updated",
                                                   {"task_id": str(ci_task.id), "status": ci_task.status})
                    except Exception as e:
                        print(f"[CI] Error checking CI for task {ci_task.id}: {e}")
                        try:
                            ci_task.latest_status_message = f"CI check error: {str(e)[:200]}"
                            session.add(ci_task)
                            await session.commit()
                        except Exception:
                            pass

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
                # Check for idle autonomous research when nothing is queued
                await _check_idle_research()
        except asyncio.CancelledError:
            print("[ORCHESTRATOR] Worker shutting down.")
            break

@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup: Initialize DB tables
    await init_db()

    # Load persisted Max plan accounts into in-memory pool
    await load_max_plan_pool()

    # Start the Reaper Service background task
    reaper_task = asyncio.create_task(reaper_service())
    
    # Start the GCP Cleanup Service background task
    gcp_cleanup_task = asyncio.create_task(gcp_cleanup_service())
    
    # Start the Orchestrator Worker background task
    orchestrator_task = asyncio.create_task(orchestrator_worker())

    # Store references for admin restart endpoint
    from app.shared_state import background_tasks as _bg_tasks
    _bg_tasks["orchestrator"] = orchestrator_task
    _bg_tasks["reaper"] = reaper_task
    _bg_tasks["gcp_cleanup"] = gcp_cleanup_task

    # Monitor background tasks for silent crashes
    async def _monitor_background_tasks():
        tasks = _bg_tasks
        while True:
            await asyncio.sleep(30)
            for name, t in tasks.items():
                if t.done():
                    exc = t.exception() if not t.cancelled() else None
                    print(f"[MONITOR] Background task '{name}' DIED! Exception: {exc}")
                    # Restart the orchestrator if it died
                    if name == "orchestrator" and not t.cancelled():
                        print("[MONITOR] Restarting orchestrator worker...")
                        tasks["orchestrator"] = asyncio.create_task(orchestrator_worker())
                        orchestrator_task = tasks["orchestrator"]
    monitor_task = asyncio.create_task(_monitor_background_tasks())

    yield
    # Shutdown: Clean up resources
    reaper_task.cancel()
    gcp_cleanup_task.cancel()
    orchestrator_task.cancel()
    monitor_task.cancel()
    await close_github_client()

app = FastAPI(
    title="Blueprint Orchestrator",
    description="Secure Orchestration & State Management",
    lifespan=lifespan
)

# Telemetry must be set up before the app starts (cannot add middleware during lifespan)
setup_telemetry(app, engine)

app.include_router(admin_router)

# Timestamp updated each orchestrator cycle — used by /debug endpoint
_orchestrator_last_cycle: Optional[datetime] = None

@app.get("/api/debug/orchestrator")
async def debug_orchestrator(session: AsyncSession = Depends(get_session), current_user: dict = Depends(get_current_user)):
    """Diagnostic endpoint: check if the orchestrator loop is alive."""
    from sqlalchemy import func
    queued = (await session.execute(
        select(func.count()).select_from(Task).where(Task.status == TaskStatus.QUEUED, Task.requires_approval == False)
    )).scalar() or 0
    active = (await session.execute(
        select(func.count()).select_from(Task).where(Task.status.in_([TaskStatus.PROVISIONING, TaskStatus.IN_PROGRESS]))
    )).scalar() or 0
    ago = None
    if _orchestrator_last_cycle:
        ago = (datetime.now(timezone.utc) - _orchestrator_last_cycle).total_seconds()
    return {
        "orchestrator_alive": _orchestrator_last_cycle is not None,
        "last_cycle_seconds_ago": ago,
        "queued_tasks": queued,
        "active_tasks": active,
        "max_concurrent_vms": _runtime_config["max_concurrent_vms"],
        "pool_size": len(_max_plan_pool),
        "pool_accounts": [{"name": a["name"], "enabled": a.get("enabled"), "active": a["active_tasks"]} for a in _max_plan_pool],
    }

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
if allowed_origins_env == "*":
    allowed_origins = ["*"]
elif allowed_origins_env:
    allowed_origins = [origin.strip() for origin in allowed_origins_env.split(",") if origin.strip()]
else:
    allowed_origins = [
        "http://localhost:3000",
        "http://localhost:5173",
        "http://127.0.0.1:3000",
        "http://127.0.0.1:5173",
    ]

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
    logs: Optional[str] = Field(None, max_length=500000)

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

async def _requeue_parent_task(parent: Task, session: AsyncSession):
    """Re-evaluate a parent task by retrying its failed children.
    Cancels FAILED children and clones them as new QUEUED tasks.
    Leaves COMPLETED, CANCELLED, and active (QUEUED/IN_PROGRESS) children untouched.
    Sets parent back to EVALUATING — no VM needed."""
    children = (await session.execute(
        select(Task).where(Task.parent_task_id == parent.id)
    )).scalars().all()

    retried_count = 0
    for child in children:
        if child.status != TaskStatus.FAILED:
            continue
        # Cancel the failed child so rollup skips it
        child.status = TaskStatus.CANCELLED
        child.latest_status_message = "Parent requeued — replaced by retry"
        session.add(child)

        # Clone as a new task with fresh VM assignment
        new_child = Task(
            project_id=child.project_id,
            title=child.title,
            description=child.description,
            parent_task_id=parent.id,
            payloads=child.payloads,
            context={k: v for k, v in (child.context or {}).items()
                     if k != "max_plan_account_id"},  # Strip runtime-only fields
            depends_on=child.depends_on,
            vm_instance_id=f"agent-vm-{uuid.uuid4().hex[:8]}",
            status=TaskStatus.QUEUED,
            created_by="ORCHESTRATOR",
            requires_approval=child.requires_approval,
        )
        session.add(new_child)
        retried_count += 1

        history = TaskHistory(task_id=new_child.id, action="SPAWNED",
                              details={"spawned_by": "REQUEUE", "retrying": str(child.id)})
        session.add(history)

    parent.status = TaskStatus.EVALUATING
    parent.latest_status_message = f"Requeued — retrying {retried_count} failed subtask(s)"
    parent.attempt_count = 0
    session.add(parent)
    await session.commit()

    for child in children:
        await session.refresh(child)
    await broadcast_to_project(str(parent.project_id), "task_updated",
                               {"task_id": str(parent.id), "status": TaskStatus.EVALUATING})
    print(f"[REQUEUE] Parent {parent.id} re-evaluating — {retried_count} failed children retried")
    return retried_count


MAX_REVIEW_FIX_ITERATIONS = 30

async def _spawn_pipeline_task(
    source_task: Task, task_type: str, session: AsyncSession,
    pr_number: int, branch: str, base_branch: str,
    original_prompt: str = "", fix_prompt: str = "", original_task_id: str = ""
) -> Optional[Task]:
    """Spawn a review or fix task as part of the automated review pipeline.
    Returns None if the review-fix cycle limit has been reached."""
    # Track review-fix iteration count to prevent infinite loops
    source_loop = (source_task.context or {}).get("review_loop", {})
    source_payload = (source_task.payloads or [{}])[0]
    iteration = source_loop.get("iteration", 0) + 1

    if iteration > MAX_REVIEW_FIX_ITERATIONS:
        # Cap reached — require manual intervention
        orig_id = original_task_id or source_loop.get("original_task_id", "") or source_payload.get("original_task_id", "")
        if orig_id:
            try:
                orig = await session.get(Task, uuid.UUID(orig_id))
                if orig:
                    orig.requires_approval = True
                    orig.latest_status_message = f"Review-fix cycle reached limit ({MAX_REVIEW_FIX_ITERATIONS} iterations) — manual review required"
                    session.add(orig)
                    await session.commit()
            except Exception as e:
                print(f"[PIPELINE] Failed to mark iteration cap on {orig_id}: {e}")
        print(f"[PIPELINE] Review-fix cycle limit reached for PR #{pr_number} after {iteration - 1} iterations")
        return None

    vm_name = f"agent-vm-{uuid.uuid4().hex[:8]}"

    # Determine parent: use the original task's parent (so review/fix are siblings of the
    # create task under the plan/research parent), falling back to original task itself
    resolved_original_id = original_task_id or str(source_task.id)
    parent_id = None
    try:
        orig = await session.get(Task, uuid.UUID(resolved_original_id))
        if orig and orig.parent_task_id:
            parent_id = orig.parent_task_id
        elif orig:
            parent_id = orig.id
    except Exception:
        pass

    if task_type == "review":
        title = f"Review PR #{pr_number}"
        payload_data = {"type": "review", "pr_number": pr_number, "branch": branch,
                        "base_branch": base_branch, "original_prompt": original_prompt,
                        "original_task_id": resolved_original_id}
    else:
        title = f"Fix PR #{pr_number}"
        payload_data = {"type": "fix", "pr_number": pr_number, "branch": branch,
                        "base_branch": base_branch, "prompt": fix_prompt,
                        "original_task_id": resolved_original_id}

    new_task = Task(
        project_id=source_task.project_id,
        title=title,
        description=fix_prompt or f"Automated {task_type} for PR #{pr_number}",
        parent_task_id=parent_id,
        payloads=[payload_data],
        context={"review_loop": {"pr_number": pr_number, "branch": branch,
                                  "base_branch": base_branch,
                                  "original_task_id": resolved_original_id,
                                  "iteration": iteration}},
        vm_instance_id=vm_name,
        status=TaskStatus.QUEUED,
        created_by="ORCHESTRATOR",
    )
    session.add(new_task)
    await session.commit()
    await session.refresh(new_task)

    history = TaskHistory(task_id=new_task.id, action="SPAWNED",
                          details={"spawned_by": "REVIEW_PIPELINE", "type": task_type, "pr_number": pr_number})
    session.add(history)
    await session.commit()

    await broadcast_to_project(str(source_task.project_id), "task_updated",
                               {"task_id": str(new_task.id), "status": TaskStatus.QUEUED})
    return new_task


def _parse_plan_output(text: str) -> Optional[Dict[str, Any]]:
    """Extract structured plan JSON from agent output text."""
    import re
    try:
        # Find the start of the JSON block containing "subtasks"
        match = re.search(r'\{\s*"subtasks"\s*:\s*\[', text)
        if not match:
            return None
        start = match.start()
        # Walk forward to find the matching closing brace using bracket counting
        depth = 0
        for i in range(start, len(text)):
            if text[i] == '{':
                depth += 1
            elif text[i] == '}':
                depth -= 1
                if depth == 0:
                    candidate = text[start:i + 1]
                    return json.loads(candidate)
    except (json.JSONDecodeError, AttributeError) as e:
        print(f"[PLAN] Failed to parse plan JSON: {e}")
    return None


def _parse_research_findings(text: str) -> Optional[List[Dict[str, Any]]]:
    """Extract structured research findings JSON array from agent output text."""
    import re
    try:
        # Find JSON array containing objects with "title" and "prompt"
        json_match = re.search(r'\[\s*\{[\s\S]*"title"[\s\S]*"prompt"[\s\S]*\]', text, re.DOTALL)
        if json_match:
            findings = json.loads(json_match.group())
            if isinstance(findings, list):
                return findings
    except json.JSONDecodeError:
        # JSON may be truncated — try to recover complete objects
        try:
            json_match = re.search(r'\[\s*\{[\s\S]*"title"[\s\S]*"prompt"', text, re.DOTALL)
            if json_match:
                raw = json_match.group()
                # Find last complete object (ends with })
                last_brace = raw.rfind('}')
                if last_brace > 0:
                    truncated = raw[:last_brace + 1] + ']'
                    findings = json.loads(truncated)
                    if isinstance(findings, list):
                        print(f"[RESEARCH] Recovered {len(findings)} findings from truncated JSON")
                        return findings
        except (json.JSONDecodeError, AttributeError):
            pass
    except AttributeError:
        pass
    return None


def _validate_plan_dependencies(subtasks: List[Dict]) -> bool:
    """Validate that plan dependencies are acyclic (topological sort check)."""
    n = len(subtasks)
    visited = set()
    in_stack = set()

    def has_cycle(node: int) -> bool:
        if node in in_stack:
            return True
        if node in visited:
            return False
        visited.add(node)
        in_stack.add(node)
        for dep in subtasks[node].get("depends_on_indices", []):
            if 0 <= dep < n and has_cycle(dep):
                return True
        in_stack.discard(node)
        return False

    return not any(has_cycle(i) for i in range(n) if i not in visited)


def _log_file_overlaps(subtasks: List[Dict], label: str = "PLAN"):
    """Log warnings when subtasks share related_files (for observability)."""
    for i, a in enumerate(subtasks):
        a_files = set(a.get("related_files", []))
        if not a_files:
            continue
        for j, b in enumerate(subtasks):
            if j <= i:
                continue
            b_files = set(b.get("related_files", []))
            overlap = a_files & b_files
            if overlap:
                a_title = a.get("title", f"#{i}")
                b_title = b.get("title", f"#{j}")
                print(f"[{label}] File overlap: '{a_title}' and '{b_title}' share {', '.join(sorted(overlap))}")


async def _handle_plan_completion(task: Task, logs: str, session: AsyncSession):
    """Parse plan output and create child tasks with dependency tracking."""
    plan_data = _parse_plan_output(logs)
    if not plan_data or not plan_data.get("subtasks"):
        task.latest_status_message = "Plan agent produced no valid subtasks"
        task.status = TaskStatus.FAILED
        session.add(task)
        await session.commit()
        return

    subtasks = plan_data["subtasks"]

    # Cap subtask count to prevent runaway VM provisioning
    if len(subtasks) > 10:
        subtasks = subtasks[:10]
        print(f"[PLAN] Capped subtask count from {len(plan_data['subtasks'])} to 10")

    if not _validate_plan_dependencies(subtasks):
        task.latest_status_message = "Plan has circular dependencies"
        task.status = TaskStatus.FAILED
        session.add(task)
        await session.commit()
        return

    # Store parsed plan and set parent to EVALUATING (children executing)
    task.plan_output = plan_data
    task.status = TaskStatus.EVALUATING
    session.add(task)

    parent_base = ((task.payloads or [{}])[0]).get("base_branch", "main")
    short_id = str(task.id).replace("-", "")[:8]
    plan_branch = f"plan/{short_id}"

    # Create staging branch for this plan's subtasks
    try:
        gh_token, owner, repo = await get_github_token_for_task(task.id, session)
        sha = await get_branch_sha(gh_token, owner, repo, parent_base)
        await create_branch(gh_token, owner, repo, plan_branch, sha)
        print(f"[PLAN] Created branch {plan_branch} from {parent_base}")
    except Exception as e:
        # Branch may already exist from a previous run — reuse it
        print(f"[PLAN] Branch {plan_branch} already exists or creation failed: {e} — reusing")

    # Store branch info on the plan task for rollup later
    task.context = {**(task.context or {}), "plan_branch": plan_branch, "parent_base": parent_base}
    session.add(task)

    # First pass: create all child tasks
    child_tasks = []
    for subtask_def in subtasks:
        child = Task(
            project_id=task.project_id,
            title=subtask_def.get("title", "Untitled subtask"),
            description=subtask_def.get("prompt", ""),
            parent_task_id=task.id,
            payloads=[{
                "type": "create",
                "prompt": subtask_def.get("prompt", ""),
                "base_branch": plan_branch,
            }],
            context={
                "plan_branch": plan_branch,
                "parent_base": parent_base,
                "scope": "small",  # Plan subtasks are pre-decomposed
            },
            vm_instance_id=f"agent-vm-{uuid.uuid4().hex[:8]}",
            status=TaskStatus.QUEUED,
            created_by="ORCHESTRATOR",
            depends_on=[],
        )
        session.add(child)
        child_tasks.append(child)

    await session.commit()
    for ct in child_tasks:
        await session.refresh(ct)

    # Second pass: resolve depends_on_indices to actual UUIDs
    for i, subtask_def in enumerate(subtasks):
        dep_indices = subtask_def.get("depends_on_indices", [])
        child_tasks[i].depends_on = [
            str(child_tasks[j].id)
            for j in dep_indices
            if 0 <= j < len(child_tasks)
        ]
        session.add(child_tasks[i])

    # Log file overlap warnings for observability
    _log_file_overlaps(subtasks, "PLAN")

    await session.commit()

    # Record history
    for ct in child_tasks:
        history = TaskHistory(
            task_id=ct.id, action="SPAWNED",
            details={"spawned_by": "PLAN_DECOMPOSITION", "parent_plan_id": str(task.id)}
        )
        session.add(history)
        await broadcast_to_project(str(task.project_id), "task_updated",
                                   {"task_id": str(ct.id), "status": TaskStatus.QUEUED})

    await session.commit()
    print(f"[PLAN] Created {len(child_tasks)} subtasks from plan {task.id}")


async def _handle_research_completion(task: Task, logs: str, session: AsyncSession):
    """Parse research findings and create tasks from them."""
    findings = _parse_research_findings(logs)
    if not findings:
        task.latest_status_message = "Research completed with no actionable findings"
        session.add(task)
        await session.commit()
        return

    # Cap findings to prevent runaway task creation
    if len(findings) > 10:
        findings = findings[:10]

    original_base = ((task.payloads or [{}])[0]).get("base_branch", "main")
    short_id = str(task.id).replace("-", "")[:8]
    research_branch = f"research/{short_id}"

    # Create staging branch for this research batch
    try:
        gh_token, owner, repo = await get_github_token_for_task(task.id, session)
        sha = await get_branch_sha(gh_token, owner, repo, original_base)
        await create_branch(gh_token, owner, repo, research_branch, sha)
        print(f"[RESEARCH] Created branch {research_branch} from {original_base}")
    except Exception as e:
        # Branch may already exist from a previous run — reuse it
        print(f"[RESEARCH] Branch {research_branch} already exists or creation failed: {e} — reusing")

    # Validate dependencies if present
    if not _validate_plan_dependencies(findings):
        print(f"[RESEARCH] Findings have circular dependencies — ignoring depends_on_indices")
        for f in findings:
            f.pop("depends_on_indices", None)

    # First pass: create all child tasks
    child_tasks = []
    finding_indices = []  # Track which finding index maps to which child
    for i, finding in enumerate(findings):
        scope = finding.get("scope", "small")
        risk = finding.get("risk", "low")
        title = finding.get("title", f"Research finding #{i+1}")
        prompt = finding.get("prompt", finding.get("description", ""))

        if not prompt:
            continue

        # Only large scope needs decomposition — medium is still single-PR work
        task_type = "plan" if scope == "large" else "create"
        needs_approval = risk == "high"

        new_task = Task(
            project_id=task.project_id,
            title=title,
            description=finding.get("description", prompt[:200]),
            parent_task_id=task.id,
            payloads=[{
                "type": task_type,
                "prompt": prompt,
                "base_branch": research_branch,
            }],
            context={
                "research_task_id": str(task.id),
                "finding_index": i,
                "scope": scope,
                "risk": risk,
                "research_branch": research_branch,
                "original_base": original_base,
            },
            vm_instance_id=f"agent-vm-{uuid.uuid4().hex[:8]}",
            status=TaskStatus.QUEUED,
            created_by="ORCHESTRATOR",
            requires_approval=needs_approval,
            depends_on=[],
        )
        session.add(new_task)
        child_tasks.append(new_task)
        finding_indices.append(i)

    await session.commit()
    for ct in child_tasks:
        await session.refresh(ct)

    # Second pass: resolve depends_on_indices to actual UUIDs
    # finding_indices maps child_tasks position → findings array position
    finding_to_child = {fi: ci for ci, fi in enumerate(finding_indices)}
    for ci, fi in enumerate(finding_indices):
        dep_indices = findings[fi].get("depends_on_indices", [])
        child_tasks[ci].depends_on = [
            str(child_tasks[finding_to_child[j]].id)
            for j in dep_indices
            if j in finding_to_child
        ]
        if child_tasks[ci].depends_on:
            session.add(child_tasks[ci])

    # Log file overlap warnings for observability
    _log_file_overlaps(findings, "RESEARCH")

    await session.commit()
    task.status = TaskStatus.EVALUATING
    task.latest_status_message = f"Research created {len(child_tasks)} tasks from {len(findings)} findings"
    task.context = {**(task.context or {}), "research_branch": research_branch, "original_base": original_base}
    session.add(task)
    await session.commit()

    await broadcast_to_project(str(task.project_id), "autonomous_activity",
                               {"research_task_id": str(task.id), "tasks_created": len(child_tasks)})
    print(f"[RESEARCH] Created {len(child_tasks)} tasks from research {task.id}")


# --- Rollup & Branch Cleanup Helpers ---
# Extracted so both update_agent_status and admin PATCH can trigger rollup.

async def _verify_pr_merged(task: Task, session: AsyncSession) -> bool:
    """Check if a create task's PR is merged. Uses context cache, falls back to GitHub API.

    Uses its own session for DB writes to avoid polluting the caller's session state.
    The passed session is only used for reads (artifact lookup).
    """
    if (task.context or {}).get("pr_merged"):
        return True
    # Look for PR artifact to verify via API
    art_result = await session.execute(
        select(Artifact).where(Artifact.task_id == task.id, Artifact.artifact_type == ArtifactType.PULL_REQUEST)
    )
    artifact = art_result.scalars().first()
    if not artifact or not artifact.pr_url:
        # No PR artifact — task completed without creating a PR (e.g., no changes needed).
        # Treat as "done" so it doesn't block rollup.
        return True
    if artifact and artifact.pr_url:
        try:
            pr_number = int(artifact.pr_url.rstrip("/").split("/")[-1])
            gh_token, owner, repo = await get_github_token_for_task(task.id, session)
            pr_data = await get_pull_request(gh_token, owner, repo, pr_number)
            if pr_data.get("merged"):
                # Use separate session to persist — don't pollute caller's session
                async with AsyncSessionLocal() as write_session:
                    db_task = await write_session.get(Task, task.id)
                    if db_task:
                        db_task.context = {**(db_task.context or {}), "pr_merged": True}
                        write_session.add(db_task)
                        await write_session.commit()
                # Also update the in-memory object so caller sees the change
                task.context = {**(task.context or {}), "pr_merged": True}
                return True
        except Exception as e:
            print(f"[PR_VERIFY] Failed to verify PR merge for task {task.id}: {e}")
    return False


async def _cleanup_branches(task: Task, session: AsyncSession) -> None:
    """Delete staging and feature branches that are no longer needed."""
    ctx = task.context or {}
    gh_token = owner = repo = None

    async def _ensure_token():
        nonlocal gh_token, owner, repo
        if not gh_token:
            gh_token, owner, repo = await get_github_token_for_task(task.id, session)

    branches_to_delete = []

    # Staging branches (research/*, plan/*) are shared by ALL sibling tasks.
    # Only delete when the PARENT task completes (rollup), not on individual child completion.
    # Check if this task IS the parent (has plan_output or is a research task).
    is_parent = task.plan_output is not None or ((task.payloads or [{}])[0]).get("type") in ("research", "plan")
    if is_parent:
        staging = ctx.get("plan_branch") or ctx.get("research_branch")
        if staging:
            branches_to_delete.append(staging)

    # Feature branches after PR merge
    if ctx.get("pr_merged"):
        for payload in (task.payloads or []):
            branch = payload.get("branch")
            if branch and branch.startswith("blueprint/"):
                branches_to_delete.append(branch)

    for branch in branches_to_delete:
        try:
            await _ensure_token()
            deleted = await delete_branch(gh_token, owner, repo, branch)
            if deleted:
                print(f"[CLEANUP] Deleted branch {branch} (task {task.id})")
        except Exception as e:
            print(f"[CLEANUP] Failed to delete {branch}: {e}")


async def _check_and_trigger_rollup(child_task: Task, session: AsyncSession) -> None:
    """Check if all siblings are done and trigger rollup PR for the parent.

    Called after a child task completes, fails, or is cancelled.
    Also called from admin PATCH when status/context changes.
    """
    if not child_task.parent_task_id:
        return

    parent = await session.get(Task, child_task.parent_task_id)
    if not parent or parent.status != TaskStatus.EVALUATING:
        return

    all_children = (await session.execute(
        select(Task).where(Task.parent_task_id == parent.id)
    )).scalars().all()

    # Only consider create tasks for rollup decisions — review/fix tasks are
    # pipeline helpers that operate on the same PR as the create task
    children = [c for c in all_children
                if ((c.payloads or [{}])[0]).get("type", "create") in ("create", "")]

    # Also check if any review/fix pipeline tasks are still active — don't rollup
    # while a review/fix cycle is in progress
    pipeline_active = any(
        c.status in (TaskStatus.QUEUED, TaskStatus.PROVISIONING, TaskStatus.IN_PROGRESS)
        for c in all_children
        if ((c.payloads or [{}])[0]).get("type") in ("review", "fix")
    )

    # Check all non-cancelled create children are done with PRs merged
    all_done = True
    cancelled_count = 0
    active_count = 0
    for c in children:
        if c.status == TaskStatus.CANCELLED:
            cancelled_count += 1
            continue
        if c.status in (TaskStatus.QUEUED, TaskStatus.PROVISIONING, TaskStatus.IN_PROGRESS, TaskStatus.EVALUATING):
            active_count += 1
            all_done = False
            continue
        if c.status == TaskStatus.FAILED:
            all_done = False
            continue
        # COMPLETED — check pr_merged for create tasks
        if not await _verify_pr_merged(c, session):
            all_done = False

    # Don't rollup if there are still active tasks or active review/fix pipeline
    if active_count > 0 or pipeline_active:
        return

    # Surface what's blocking if children are done but not all merged
    if not all_done and active_count == 0 and not pipeline_active:
        unmerged = [c for c in children
                    if c.status == TaskStatus.COMPLETED and not (c.context or {}).get("pr_merged")]
        failed = [c for c in children if c.status == TaskStatus.FAILED]
        if unmerged:
            titles = [c.title[:40] for c in unmerged]
            parent.latest_status_message = f"Blocked: {len(unmerged)} subtask(s) completed but PR not merged — {', '.join(titles)}"
            print(f"[ROLLUP] Parent {parent.id} blocked — {len(unmerged)} children completed but unmerged")
        elif failed:
            parent.latest_status_message = f"{len(failed)} subtask(s) failed — requeue parent to retry"
            print(f"[ROLLUP] Parent {parent.id} blocked — {len(failed)} children failed")
        session.add(parent)
        await session.commit()
        return

    child_statuses = [c.status for c in children]

    # Edge case: all children cancelled — nothing to rollup, mark parent cancelled
    if cancelled_count == len(children):
        parent.status = TaskStatus.CANCELLED
        parent.latest_status_message = f"All {len(children)} subtasks were cancelled"
        session.add(parent)
        await session.commit()
        await _cleanup_branches(parent, session)
        await broadcast_to_project(str(parent.project_id), "task_updated",
                                   {"task_id": str(parent.id), "status": TaskStatus.CANCELLED})
        print(f"[ROLLUP] Parent {parent.id} cancelled — all children cancelled")
        return

    if all_done and (len(children) - cancelled_count) > 0:
        # All non-cancelled children merged — create rollup PR
        parent_ctx = parent.context or {}
        staging_branch = parent_ctx.get("plan_branch") or parent_ctx.get("research_branch")
        target_branch = parent_ctx.get("parent_base") or parent_ctx.get("original_base")

        completed_count = len(children) - cancelled_count
        rollup_note = ""
        if cancelled_count > 0:
            rollup_note = f"\n\n*{cancelled_count} subtask(s) were cancelled and excluded from this rollup.*"

        if staging_branch and target_branch and staging_branch != target_branch:
            try:
                gh_token, owner, repo = await get_github_token_for_task(parent.id, session)

                # Verify all PRs targeting the staging branch are resolved before rolling up
                from app.github import get_open_pull_requests
                open_prs = await get_open_pull_requests(gh_token, owner, repo, staging_branch)
                if open_prs:
                    pr_nums = [f"#{p['number']}" for p in open_prs]
                    print(f"[ROLLUP] Parent {parent.id} has {len(open_prs)} open PR(s) targeting {staging_branch}: {', '.join(pr_nums)} — waiting")
                    return

                is_target_intermediate = target_branch.startswith("research/") or target_branch.startswith("plan/")
                pr_title = f"[Blueprint] Rollup: {parent.title}"
                pr_body = f"Rollup of {completed_count} subtasks from `{staging_branch}`{rollup_note}\n\n---\n*Automated by Blueprint Pipeline*"

                pr_result = await create_pull_request(gh_token, owner, repo,
                    title=pr_title, head=staging_branch, base=target_branch, body=pr_body)
                rollup_pr_url = pr_result.get("html_url", "")
                rollup_pr_number = pr_result.get("number")
                print(f"[ROLLUP] Created PR #{rollup_pr_number}: {staging_branch} → {target_branch}")

                # Auto-merge small rollups on intermediate branches; review large ones
                # Measure by total lines changed (additions + deletions)
                try:
                    pr_detail = await get_pull_request(gh_token, owner, repo, rollup_pr_number)
                    lines_changed = pr_detail.get("additions", 0) + pr_detail.get("deletions", 0)
                except Exception:
                    lines_changed = 999  # assume large if we can't check
                is_small_rollup = lines_changed <= 100
                print(f"[ROLLUP] PR #{rollup_pr_number}: {lines_changed} lines changed, {'auto-merge' if is_small_rollup and is_target_intermediate else 'review'}")
                if is_target_intermediate and is_small_rollup:
                    merge_result = await merge_pull_request(gh_token, owner, repo, rollup_pr_number)
                    if merge_result.get("merged"):
                        parent.status = TaskStatus.COMPLETED
                        parent.context = {**parent_ctx, "pr_merged": True, "rollup_pr_url": rollup_pr_url}
                        parent.latest_status_message = f"All {completed_count} subtasks merged, rollup PR auto-merged"
                        await delete_branch(gh_token, owner, repo, staging_branch)
                        print(f"[ROLLUP] Auto-merged rollup into {target_branch}, deleted {staging_branch}")
                    else:
                        # Merge failed (conflicts) — spawn review/fix cycle instead of giving up
                        merge_msg = merge_result.get("message", "unknown")
                        parent.context = {**parent_ctx, "rollup_pr_url": rollup_pr_url, "rollup_pr_number": rollup_pr_number}
                        parent.latest_status_message = f"Rollup PR #{rollup_pr_number} merge blocked: {merge_msg} — spawning fix task"
                        try:
                            pr_files = await get_pr_files(gh_token, owner, repo, rollup_pr_number)
                            files_list = "\n".join(f"  - {f['filename']} ({f['status']})" for f in pr_files)
                        except Exception:
                            files_list = "  (could not fetch file list)"
                        fix_prompt = (
                            f"This rollup PR (#{rollup_pr_number}) merging {staging_branch} into {target_branch} has conflicts.\n"
                            f"Error: {merge_msg}\n\n"
                            f"Files in this PR:\n{files_list}\n\n"
                            f"Steps to resolve:\n"
                            f"1. Run: git fetch origin {target_branch}\n"
                            f"2. Run: git rebase origin/{target_branch}\n"
                            f"3. Resolve any conflicts — keep the intent of the subtask changes while incorporating upstream updates\n"
                            f"4. Run: git rebase --continue after resolving each file\n\n"
                            f"Preserve the intent of the existing changes."
                        )
                        fix_task = await _spawn_pipeline_task(
                            parent, "fix", session, rollup_pr_number, staging_branch, target_branch,
                            fix_prompt=fix_prompt,
                            original_task_id=str(parent.id)
                        )
                        if fix_task:
                            print(f"[ROLLUP] Merge blocked for rollup PR #{rollup_pr_number}, spawned fix → task {fix_task.id}")
                        else:
                            parent.status = TaskStatus.FAILED
                            parent.latest_status_message = f"Rollup PR #{rollup_pr_number} merge blocked, fix not spawned (iteration cap)"
                            print(f"[ROLLUP] WARNING: Rollup fix spawn returned None for PR #{rollup_pr_number}")
                else:
                    parent.latest_status_message = f"All {completed_count} subtasks merged, rollup PR #{rollup_pr_number} awaiting review"
                    parent.context = {**parent_ctx, "rollup_pr_url": rollup_pr_url, "rollup_pr_number": rollup_pr_number}
                    review_task = await _spawn_pipeline_task(
                        parent, "review", session, rollup_pr_number, staging_branch, target_branch,
                        original_prompt=parent.description or parent.title,
                        original_task_id=str(parent.id)
                    )
                    if review_task:
                        print(f"[ROLLUP] Spawned review for rollup PR #{rollup_pr_number} → task {review_task.id}")
                    else:
                        parent.status = TaskStatus.FAILED
                        parent.latest_status_message = f"Rollup PR #{rollup_pr_number} created but review not spawned (iteration cap)"
                        print(f"[ROLLUP] WARNING: Rollup review spawn returned None for PR #{rollup_pr_number}")
            except Exception as e:
                print(f"[ROLLUP] Failed to create rollup PR: {e}")
                parent.status = TaskStatus.FAILED
                parent.latest_status_message = f"All {completed_count} subtasks done but rollup PR creation failed: {str(e)[:200]}"
        else:
            parent.status = TaskStatus.COMPLETED
            parent.latest_status_message = f"All {completed_count} subtasks completed and merged"

        session.add(parent)
        await session.commit()
        await broadcast_to_project(str(parent.project_id), "task_updated",
                                   {"task_id": str(parent.id), "status": parent.status})
        print(f"[ROLLUP] Parent {parent.id} — all children done (completed={completed_count}, cancelled={cancelled_count})")

    elif any(s == TaskStatus.FAILED for s in child_statuses) and active_count == 0:
        # All done but some failed — mark parent as failed but keep staging branch
        # (it may contain merged work from successful children that can be salvaged)
        parent.status = TaskStatus.FAILED
        failed_count = sum(1 for s in child_statuses if s == TaskStatus.FAILED)
        parent.latest_status_message = f"{failed_count} of {len(children)} subtasks failed"
        session.add(parent)
        await session.commit()
        await broadcast_to_project(str(parent.project_id), "task_updated",
                                   {"task_id": str(parent.id), "status": TaskStatus.FAILED})
        print(f"[ROLLUP] Parent {parent.id} failed — {failed_count} children failed")


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
        if payload.status == TaskStatus.FAILED and task.attempt_count < _runtime_config["max_retries"]:
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

        # Circuit breaker for autonomous tasks: pause autonomy after consecutive failures
        if payload.status == TaskStatus.FAILED and task.created_by == "ORCHESTRATOR":
            project_key = str(task.project_id)
            _autonomous_failure_counts[project_key] = _autonomous_failure_counts.get(project_key, 0) + 1
            if _autonomous_failure_counts[project_key] >= 3:
                try:
                    project = await session.get(Project, task.project_id)
                    if project and project.autonomous_enabled:
                        project.autonomous_enabled = False
                        session.add(project)
                        await session.commit()
                        await broadcast_to_project(project_key, "autonomous_paused",
                                                   {"reason": "3 consecutive autonomous task failures"})
                        print(f"[AUTONOMOUS] Paused project {project_key} after 3 consecutive failures")
                except Exception as e:
                    print(f"[AUTONOMOUS] Error in circuit breaker: {e}")
        # When review/fix fails permanently, cancel the original create task so rollup
        # skips it (via cancelled-child logic) instead of hanging forever.
        # Other children's work is preserved — only this PR is excluded from the rollup.
        if payload.status == TaskStatus.FAILED:
            task_payload = (task.payloads or [{}])[0]
            task_type = task_payload.get("type", "")
            if task_type in ("review", "fix"):
                original_task_id = ((task.context or {}).get("review_loop", {}).get("original_task_id", "")
                                    or task_payload.get("original_task_id", ""))
                if original_task_id:
                    try:
                        orig = await session.get(Task, uuid.UUID(original_task_id))
                        if orig and orig.status == TaskStatus.COMPLETED:
                            orig.status = TaskStatus.CANCELLED
                            orig.latest_status_message = f"Pipeline {task_type} task failed — PR excluded from rollup"
                            session.add(orig)
                            await session.commit()
                            # Trigger rollup check so parent can proceed with remaining children
                            if orig.parent_task_id:
                                await _check_and_trigger_rollup(orig, session)
                            print(f"[PIPELINE] Cancelled original task {original_task_id} — {task_type} failed, skipping in rollup")
                    except Exception as e:
                        print(f"[PIPELINE] Failed to propagate failure to {original_task_id}: {e}")

        if payload.status == TaskStatus.COMPLETED and task.created_by == "ORCHESTRATOR":
            # Reset failure counter on success
            _autonomous_failure_counts.pop(str(task.project_id), None)

        # 5. Review Pipeline — auto-spawn review/fix tasks based on completed task type
        if payload.status == TaskStatus.COMPLETED:
            task_payload = (task.payloads or [{}])[0]
            task_type = task_payload.get("type", "create")
            review_loop = (task.context or {}).get("review_loop", {})

            # Create task completed with PR → route based on target branch
            if task_type in ("create", "") and payload.pr_url and payload.artifact_type == ArtifactType.PULL_REQUEST:
                pr_number = int(payload.pr_url.rstrip("/").split("/")[-1])
                branch = f"blueprint/task-{str(task.id).replace('-','')[:8]}"
                base_branch = task_payload.get("base_branch", "main")
                original_prompt = task_payload.get("prompt", task.description or "")
                is_intermediate = base_branch.startswith("research/") or base_branch.startswith("plan/")
                scope = (task.context or {}).get("scope", "unknown")

                if is_intermediate and scope == "small":
                    # Small scope on intermediate branch → auto-merge, skip review
                    try:
                        gh_token, owner, repo = await get_github_token_for_task(task.id, session)
                        merge_result = await merge_pull_request(gh_token, owner, repo, pr_number)
                        if merge_result.get("merged"):
                            task.context = {**(task.context or {}), "pr_merged": True}
                            session.add(task)
                            await session.commit()
                            await _cleanup_branches(task, session)
                            print(f"[PIPELINE] Auto-merged PR #{pr_number} into {base_branch} (small scope, intermediate)")
                        else:
                            print(f"[PIPELINE] Auto-merge failed for PR #{pr_number}: {merge_result.get('message')}")
                            # Fall through to review pipeline for conflict resolution
                            review_task = await _spawn_pipeline_task(
                                task, "review", session, pr_number, branch, base_branch,
                                original_prompt=original_prompt, original_task_id=str(task.id)
                            )
                            if not review_task:
                                task.latest_status_message = f"Auto-merge failed and review not spawned for PR #{pr_number} — iteration cap"
                                task.requires_approval = True
                                session.add(task)
                                await session.commit()
                                print(f"[PIPELINE] WARNING: Auto-merge failed and review spawn returned None for PR #{pr_number}")
                    except Exception as e:
                        print(f"[PIPELINE] Auto-merge error for PR #{pr_number}: {e}")
                        # Fallback: spawn review so the pipeline doesn't die
                        try:
                            fallback_review = await _spawn_pipeline_task(
                                task, "review", session, pr_number, branch, base_branch,
                                original_prompt=original_prompt, original_task_id=str(task.id)
                            )
                            if not fallback_review:
                                task.latest_status_message = f"Pipeline stalled: auto-merge failed and review not spawned (iteration cap) for PR #{pr_number}"
                                task.requires_approval = True
                                session.add(task)
                                await session.commit()
                                print(f"[PIPELINE] WARNING: Fallback review spawn returned None for PR #{pr_number}")
                        except Exception as e:
                            print(f"[PIPELINE] Fallback review spawn failed for PR #{pr_number}: {e}")
                            task.latest_status_message = f"Pipeline stalled: auto-merge and review spawn both failed — {str(e)[:200]}"
                            session.add(task)
                            await session.commit()
                else:
                    # Full pipeline: security scan → review (for main targets or medium+ scope)
                    try:
                        gh_token, owner, repo = await get_github_token_for_task(task.id, session)
                        scan_result = await scan_pr_for_injection(gh_token, owner, repo, pr_number)
                        print(f"[SECURITY] {scan_result['summary']}")
                        if scan_result["blocked"]:
                            task.requires_approval = True
                            task.latest_status_message = scan_result["summary"]
                            session.add(task)
                            await session.commit()
                        if scan_result["findings"]:
                            await create_pr_comment(gh_token, owner, repo, pr_number, scan_result["report"])
                    except Exception as e:
                        print(f"[SECURITY] Scan failed for PR #{pr_number}, proceeding with review: {e}")

                    if not task.requires_approval:
                        review_task = await _spawn_pipeline_task(
                            task, "review", session, pr_number, branch, base_branch,
                            original_prompt=original_prompt, original_task_id=str(task.id)
                        )
                        if review_task:
                            print(f"[REVIEW PIPELINE] Spawned review for PR #{pr_number} → task {review_task.id}")
                        else:
                            task.latest_status_message = f"Review not spawned for PR #{pr_number} — iteration cap reached"
                            task.requires_approval = True
                            session.add(task)
                            await session.commit()
                            print(f"[REVIEW PIPELINE] WARNING: Review spawn returned None for PR #{pr_number}")

            # Review task completed → parse verdict
            elif task_type == "review":
                pr_number = review_loop.get("pr_number") or task_payload.get("pr_number")
                branch = review_loop.get("branch") or task_payload.get("branch")
                base_branch = review_loop.get("base_branch") or task_payload.get("base_branch", "main")
                original_task_id = review_loop.get("original_task_id", "") or task_payload.get("original_task_id", "")
                original_prompt = task_payload.get("original_prompt", "")

                if not pr_number or not branch:
                    print(f"[REVIEW PIPELINE] Review task {task.id} missing pr_number ({pr_number}) or branch ({branch}) — cannot continue pipeline")
                    task.latest_status_message = "Review pipeline error: missing pr_number or branch"
                    session.add(task)
                    await session.commit()
                    return {"status": "accepted"}

                verdict_text = payload.logs or task.latest_status_message or ""
                verdict = "REQUEST_CHANGES"
                feedback = verdict_text

                # Parse verdict: scan backwards from end for last JSON with "verdict" key.
                # This is more robust than forward-scanning regex since the agent appends
                # the verdict JSON at the end of the output.
                import re
                verdict_parsed = False
                try:
                    # Find all JSON objects containing "verdict" key (handles any key order)
                    for m in re.finditer(r'\{[^{}]*"verdict"', verdict_text):
                        start = m.start()
                        depth = 0
                        for i in range(start, len(verdict_text)):
                            if verdict_text[i] == '{':
                                depth += 1
                            elif verdict_text[i] == '}':
                                depth -= 1
                                if depth == 0:
                                    try:
                                        candidate = json.loads(verdict_text[start:i+1])
                                        if "verdict" in candidate:
                                            verdict = candidate["verdict"]
                                            feedback = candidate.get("feedback", verdict_text)
                                            verdict_parsed = True
                                    except json.JSONDecodeError:
                                        pass
                                    break
                except Exception as e:
                    print(f"[REVIEW PIPELINE] Verdict parsing error for task {task.id}: {e}")

                if not verdict_parsed:
                    # Fallback: look for APPROVE/REQUEST_CHANGES as standalone words
                    upper_text = verdict_text.upper()
                    # Only match the verdict keywords, not surrounding review text
                    approve_idx = upper_text.rfind('"APPROVE"')
                    rc_idx = upper_text.rfind('"REQUEST_CHANGES"')
                    if approve_idx > rc_idx:
                        verdict = "APPROVE"
                    elif rc_idx > approve_idx:
                        verdict = "REQUEST_CHANGES"
                    print(f"[REVIEW PIPELINE] WARNING: No verdict JSON found for task {task.id} (len={len(verdict_text)}), fallback verdict={verdict}")

                # Post the review to the PR as a comment
                try:
                    gh_token, owner, repo = await get_github_token_for_task(task.id, session)
                    review_comment = f"**Blueprint Code Review** ({verdict})\n\n{feedback[:3000]}"
                    await create_pr_comment(gh_token, owner, repo, pr_number, review_comment)
                except Exception as e:
                    print(f"[REVIEW PIPELINE] Failed to post review comment: {e}")

                action_taken = False
                if verdict == "APPROVE":
                    try:
                        gh_token, owner, repo = await get_github_token_for_task(task.id, session)
                        # Validate PR head branch matches before merging
                        pr_data = await get_pull_request(gh_token, owner, repo, pr_number)
                        pr_head = pr_data.get("head", {}).get("ref", "")
                        if pr_head and pr_head != branch:
                            print(f"[REVIEW PIPELINE] Branch mismatch: reviewed '{branch}' but PR #{pr_number} head is '{pr_head}' — skipping merge")
                            await create_pr_comment(gh_token, owner, repo, pr_number,
                                f"**Blueprint Review Pipeline**: Merge aborted — reviewed branch `{branch}` does not match PR head `{pr_head}`.")
                            task.latest_status_message = f"Branch mismatch: reviewed {branch}, PR head is {pr_head}"
                            session.add(task)
                            await session.commit()
                            raise ValueError(f"Branch mismatch: {branch} vs {pr_head}")

                        # Check CI status before merging
                        head_sha = pr_data.get("head", {}).get("sha", "")
                        ci_status = await get_check_runs(gh_token, owner, repo, head_sha) if head_sha else None

                        if ci_status and ci_status["status"] != "completed":
                            # CI still running — park task in EVALUATING (no VM consumed)
                            task.status = TaskStatus.EVALUATING
                            task.latest_status_message = f"Review approved, waiting for CI on PR #{pr_number}..."
                            task.context = {**(task.context or {}), "review_loop": {
                                **review_loop, "ci_waiting": True, "verdict": "APPROVE",
                                "head_sha": head_sha, "pr_number": pr_number,
                                "branch": branch, "base_branch": base_branch,
                                "original_task_id": original_task_id,
                                "ci_wait_started": datetime.utcnow().isoformat()}}
                            session.add(task)
                            await session.commit()
                            print(f"[CI] PR #{pr_number} approved, CI pending — waiting")
                            action_taken = True
                            return {"status": "accepted"}

                        if ci_status and ci_status["conclusion"] == "failure":
                            # CI failed — spawn fix with CI errors
                            ci_errors = ci_status["failed_logs"] or "CI checks failed (no details available)"
                            await create_pr_comment(gh_token, owner, repo, pr_number,
                                f"**Blueprint CI**: Checks failed — spawning fix task.\n\n```\n{ci_errors[:2000]}\n```")
                            ci_fix_prompt = (
                                f"CI checks failed on PR #{pr_number} (code review already approved).\n\n"
                                f"CI errors:\n{ci_errors}\n\n"
                                f"Fix only the build/lint/test errors. The code review is already "
                                f"approved — don't change logic, just fix CI failures."
                            )
                            fix_task = await _spawn_pipeline_task(
                                task, "fix", session, pr_number, branch, base_branch,
                                fix_prompt=ci_fix_prompt,
                                original_task_id=original_task_id
                            )
                            if fix_task:
                                print(f"[CI] PR #{pr_number} CI failed, spawned fix → {fix_task.id}")
                            action_taken = True
                            return {"status": "accepted"}

                        # CI passed (or no CI checks exist) — proceed to merge
                        if ci_status and ci_status["conclusion"] == "success":
                            print(f"[CI] PR #{pr_number} CI passed")

                        merge_result = await merge_pull_request(gh_token, owner, repo, pr_number)

                        if merge_result.get("merged"):
                            await create_pr_comment(gh_token, owner, repo, pr_number,
                                "**Blueprint Review Pipeline**: PR auto-merged.")
                            # Mark original create task so dependents can proceed
                            if original_task_id:
                                try:
                                    orig = await session.get(Task, uuid.UUID(original_task_id))
                                    if orig:
                                        orig.context = {**(orig.context or {}), "pr_merged": True}
                                        # If this was a rollup PR review, mark the parent as COMPLETED
                                        if orig.status == TaskStatus.EVALUATING:
                                            orig.status = TaskStatus.COMPLETED
                                            orig.latest_status_message = "Rollup PR merged"
                                        session.add(orig)
                                        await session.commit()
                                        # Clean up branches (staging + feature)
                                        await _cleanup_branches(orig, session)
                                except Exception as e:
                                    print(f"[REVIEW PIPELINE] Failed to set pr_merged on {original_task_id}: {e}")
                            print(f"[REVIEW PIPELINE] PR #{pr_number} approved and merged")
                            action_taken = True
                        else:
                            # Merge blocked — spawn fix task to rebase and resolve conflicts
                            merge_msg = merge_result.get("message", "unknown")
                            # Fetch PR file list for conflict context
                            try:
                                pr_files = await get_pr_files(gh_token, owner, repo, pr_number)
                                files_list = "\n".join(f"  - {f['filename']} ({f['status']})" for f in pr_files)
                            except Exception:
                                files_list = "  (could not fetch file list)"
                            await create_pr_comment(gh_token, owner, repo, pr_number,
                                f"**Blueprint Review Pipeline**: Merge blocked — {merge_msg}. Spawning fix task to rebase and resolve conflicts.")
                            fix_prompt = (
                                f"This PR (#{pr_number}) was approved but could not be merged.\n"
                                f"Error: {merge_msg}\n\n"
                                f"Files changed in this PR:\n{files_list}\n\n"
                                f"Steps to resolve:\n"
                                f"1. Run: git fetch origin {base_branch}\n"
                                f"2. Run: git rebase origin/{base_branch}\n"
                                f"3. If rebase conflicts occur, resolve them in each file — keep the intent of the PR changes while incorporating upstream updates\n"
                                f"4. Run: git rebase --continue after resolving each conflicting file\n"
                                f"5. If rebase is too complex, abort with git rebase --abort, then use git merge origin/{base_branch} and resolve conflict markers\n\n"
                                f"Preserve the intent of the existing PR changes. Do not alter logic unless necessary to resolve conflicts."
                            )
                            fix_task = await _spawn_pipeline_task(
                                task, "fix", session, pr_number, branch, base_branch,
                                fix_prompt=fix_prompt,
                                original_task_id=original_task_id
                            )
                            if fix_task:
                                print(f"[REVIEW PIPELINE] Merge blocked for PR #{pr_number}, spawned rebase fix → task {fix_task.id}")
                            action_taken = True
                    except Exception as e:
                        print(f"[REVIEW PIPELINE] Failed to merge PR #{pr_number}: {e}")
                        task.latest_status_message = f"Review approved but merge/CI-check failed: {str(e)[:200]}"
                        session.add(task)
                        await session.commit()
                else:
                    # REQUEST_CHANGES — include CI errors if available (don't wait)
                    ci_errors_text = ""
                    try:
                        gh_token, owner, repo = await get_github_token_for_task(task.id, session)
                        pr_data = await get_pull_request(gh_token, owner, repo, pr_number)
                        head_sha = pr_data.get("head", {}).get("sha", "")
                        if head_sha:
                            ci_check = await get_check_runs(gh_token, owner, repo, head_sha)
                            if ci_check.get("conclusion") == "failure" and ci_check.get("failed_logs"):
                                ci_errors_text = f"\n\nCI build errors (also need fixing):\n{ci_check['failed_logs']}"
                    except Exception as e:
                        print(f"[REVIEW PIPELINE] Could not fetch CI errors for fix context: {e}")
                    fix_task = await _spawn_pipeline_task(
                        task, "fix", session, pr_number, branch, base_branch,
                        fix_prompt=f"A code review requested changes. Please address the following feedback:\n\n{feedback}{ci_errors_text}",
                        original_task_id=original_task_id
                    )
                    if fix_task:
                        print(f"[REVIEW PIPELINE] Spawned fix for PR #{pr_number} → task {fix_task.id}")
                    else:
                        task.latest_status_message = f"Fix not spawned for PR #{pr_number} — iteration cap reached"
                        task.requires_approval = True
                        session.add(task)
                        await session.commit()
                        print(f"[REVIEW PIPELINE] WARNING: Fix spawn returned None for PR #{pr_number}")
                    action_taken = True

                if not action_taken:
                    task.latest_status_message = f"Review completed but no action taken (verdict={verdict}) — check orchestrator logs"
                    session.add(task)
                    await session.commit()
                    print(f"[REVIEW PIPELINE] WARNING: Review {task.id} for PR #{pr_number} completed with no downstream action (verdict={verdict})")

            # Fix task completed → security scan → spawn new review
            elif task_type == "fix":
                pr_number = review_loop.get("pr_number") or task_payload.get("pr_number")
                branch = review_loop.get("branch") or task_payload.get("branch")
                base_branch = review_loop.get("base_branch") or task_payload.get("base_branch", "main")
                original_task_id = review_loop.get("original_task_id", "") or task_payload.get("original_task_id", "")

                if not pr_number or not branch:
                    print(f"[REVIEW PIPELINE] Fix task {task.id} missing pr_number ({pr_number}) or branch ({branch}) — cannot continue pipeline")
                    task.latest_status_message = "Fix pipeline error: missing pr_number or branch"
                    session.add(task)
                    await session.commit()
                    return {"status": "accepted"}

                # Security scan fix output before re-review
                scan_blocked = False
                try:
                    gh_token, owner, repo = await get_github_token_for_task(task.id, session)
                    scan_result = await scan_pr_for_injection(gh_token, owner, repo, pr_number)
                    print(f"[SECURITY] {scan_result['summary']}")
                    if scan_result["blocked"]:
                        scan_blocked = True
                        task.requires_approval = True
                        task.latest_status_message = scan_result["summary"]
                        session.add(task)
                        await session.commit()
                    if scan_result["findings"]:
                        await create_pr_comment(gh_token, owner, repo, pr_number, scan_result["report"])
                except Exception as e:
                    print(f"[SECURITY] Scan failed for fix PR #{pr_number}, proceeding: {e}")

                if not scan_blocked:
                    review_task = await _spawn_pipeline_task(
                        task, "review", session, pr_number, branch, base_branch,
                        original_task_id=original_task_id
                    )
                    if review_task:
                        print(f"[REVIEW PIPELINE] Spawned review after fix for PR #{pr_number} → task {review_task.id}")
                    else:
                        task.latest_status_message = f"Fix applied but review not spawned for PR #{pr_number} — iteration cap reached"
                        task.requires_approval = True
                        session.add(task)
                        await session.commit()
                        print(f"[REVIEW PIPELINE] WARNING: Review spawn after fix returned None for PR #{pr_number}")

            # Plan task completed → decompose into subtasks
            elif task_type == "plan":
                plan_logs = payload.logs or task.latest_status_message or ""
                await _handle_plan_completion(task, plan_logs, session)

            # Research task completed → create tasks from findings
            elif task_type == "research":
                research_logs = payload.logs or task.latest_status_message or ""
                await _handle_research_completion(task, research_logs, session)

        # 6. Parent task rollup — delegated to shared function
        if task.parent_task_id and payload.status in [TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.CANCELLED]:
            await _check_and_trigger_rollup(task, session)

        # 7. Project State Aggregation
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
    import jwt as pyjwt
    from app.auth import AGENT_JWT_SECRET, AGENT_JWT_ALGORITHM
    state = pyjwt.encode(
        {"sub": current_user["user_id"], "exp": datetime.now(timezone.utc) + timedelta(minutes=10), "type": "github_oauth"},
        AGENT_JWT_SECRET, algorithm=AGENT_JWT_ALGORITHM
    )

    url = (
        f"https://github.com/login/oauth/authorize"
        f"?client_id={GITHUB_CLIENT_ID}"
        f"&state={state}"
        f"&allow_signup=false"
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
    admin_url = os.getenv("ADMIN_UI_URL", "https://blueprint-admin-9c6b7.web.app")

    # Verify the state token to identify the Firebase user who started the flow
    import jwt as pyjwt
    from app.auth import AGENT_JWT_SECRET, AGENT_JWT_ALGORITHM
    try:
        state_payload = pyjwt.decode(state, AGENT_JWT_SECRET, algorithms=[AGENT_JWT_ALGORITHM])
        if state_payload.get("type") != "github_oauth":
            return RedirectResponse(url=f"{admin_url}?github_error=invalid_state")
        firebase_user_id = state_payload["sub"]
    except pyjwt.ExpiredSignatureError:
        return RedirectResponse(url=f"{admin_url}?github_error=state_expired")
    except pyjwt.PyJWTError:
        return RedirectResponse(url=f"{admin_url}?github_error=invalid_state")

    try:
        # Exchange code for GitHub user access token
        token_data = await exchange_oauth_code(code)
        user_token = token_data["access_token"]

        # Get the GitHub user's profile to confirm identity
        gh_user = await get_github_user(user_token)
        gh_login = gh_user["login"]

        # Discover which of this App's installations the user can access
        installations = await get_user_installations(user_token)
        if not installations:
            return RedirectResponse(url=f"{admin_url}?github_error=no_installations")

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

        return RedirectResponse(url=f"{admin_url}?github_connected={account_name}")

    except Exception as e:
        print(f"[GITHUB OAUTH] Error: {e}")
        return RedirectResponse(url=f"{admin_url}?github_error=failed")

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
    
    print(f"[WEBHOOK] Received {event_type} event")

    # Post-merge trigger: check if we should spawn autonomous research
    if event_type == "pull_request":
        action = data.get("action")
        merged = data.get("pull_request", {}).get("merged", False)

        if action == "closed" and merged:
            repo_full = data.get("repository", {}).get("full_name", "")
            if "/" in repo_full:
                owner, repo = repo_full.split("/", 1)
                background_tasks.add_task(_check_autonomous_research, owner, repo)

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
    async def serve_spa(full_path: str, request: Request = None):
        # Don't intercept API routes or WebSocket upgrade requests
        if full_path.startswith("api/"):
            raise HTTPException(status_code=404, detail="API route not found")
        if request and request.headers.get("upgrade", "").lower() == "websocket":
            raise HTTPException(status_code=404, detail="WebSocket route")
            
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

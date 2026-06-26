"""Runner-facing polling endpoints for the pull-based local fleet runner.

A runner process on operator hardware authenticates with its bearer token (see
``app.auth.get_current_runner``) and:

  * POST /api/runner/next-task — pulls the next task the orchestrator pinned to
    this runner and receives a task-scoped agent JWT. The task stays in
    PROVISIONING; the agent binary calls ``/api/agent/claim`` which performs
    the PROVISIONING → IN_PROGRESS transition (same contract as GCP VMs).
  * POST /api/runner/heartbeat — reports liveness + currently-active task ids so
    the orchestrator's dispatch cache and stale-runner sweep can track it.

No LOCAL_MODE flag is involved: the orchestrator pins QUEUED tasks to an active
registered runner (vm_instance_id='runner:{id}', context['runner_target_id']),
and this endpoint hands those pinned tasks to the polling runner.
"""

import logging
from typing import List

from fastapi import APIRouter, Depends, Response, status
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import create_agent_token, get_current_runner
from app.database import get_session
from app.models import LocalRunner, Task, TaskStatus, _utcnow
from app.task_capabilities import agent_capability_slice

logger = logging.getLogger("blueprint")

router = APIRouter(prefix="/api/runner", tags=["runner"])


class HeartbeatRequest(BaseModel):
    active_task_ids: List[str] = []


@router.post("/next-task")
async def next_task(
    response: Response,
    runner: LocalRunner = Depends(get_current_runner),
    session: AsyncSession = Depends(get_session),
):
    """Return the oldest task pinned to this runner, or 204 if none.

    Uses ``SELECT ... FOR UPDATE SKIP LOCKED`` so concurrent polls from the same
    runner never double-claim a task. On a hit a task-scoped agent JWT is minted
    and returned; the task stays in PROVISIONING so the agent binary can call
    ``/api/agent/claim`` to perform the PROVISIONING → IN_PROGRESS transition
    (identical contract to GCP-provisioned VMs).
    """
    pinned_vm_id = f"runner:{runner.id}"
    result = await session.execute(
        select(Task)
        .where(
            Task.status == TaskStatus.PROVISIONING,
            Task.vm_instance_id == pinned_vm_id,
            Task.context["runner_target_id"].as_string() == str(runner.id),
            # Exclude tasks already dispatched: started_at is set on dispatch so
            # a re-poll while the agent is booting and calling /api/agent/claim
            # skips this task instead of returning it again (dispatch-storm guard).
            Task.started_at.is_(None),
        )
        .order_by(Task.created_at.asc())
        .limit(1)
        .with_for_update(skip_locked=True)
    )
    task = result.scalars().first()
    if task is None:
        response.status_code = status.HTTP_204_NO_CONTENT
        return response

    # Mark as dispatched so re-polls skip this task while the agent boots and
    # calls /api/agent/claim.  The task stays in PROVISIONING so claim can still
    # perform the PROVISIONING → IN_PROGRESS transition (same contract as GCP VMs).
    task.started_at = _utcnow()
    session.add(task)
    await session.commit()

    task_type = (task.first_payload or {}).get("type", "")
    agent_token = create_agent_token(
        str(task.id),
        vm_instance_id=pinned_vm_id,
        task_type=task_type,
        project_id=str(task.project_id),
    )

    logger.info(
        "[RUNNERS] Dispatched task %s (type=%s) to runner %s",
        task.id,
        task_type,
        runner.id,
    )
    return {
        "task_id": str(task.id),
        "agent_token": agent_token,
        "payload": task.payloads,
        "task_type": task_type,
        "capability_slice": agent_capability_slice(task_type),
    }


@router.post("/heartbeat")
async def heartbeat(
    payload: HeartbeatRequest,
    runner: LocalRunner = Depends(get_current_runner),
    session: AsyncSession = Depends(get_session),
):
    """Refresh the runner's ``last_seen_at`` and record its active task ids.

    The active-task list is stashed on ``capabilities`` so the orchestrator's
    per-cycle dispatch cache can prefer less-loaded runners without an extra
    per-task query.
    """
    runner.last_seen_at = _utcnow()
    caps = dict(runner.capabilities or {})
    caps["active_task_ids"] = list(payload.active_task_ids)
    runner.capabilities = caps
    session.add(runner)
    await session.commit()
    return {"ok": True, "last_seen_at": runner.last_seen_at.isoformat()}

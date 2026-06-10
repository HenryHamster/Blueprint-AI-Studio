"""Shared repo_bootstrap spawn helper.

Provides an idempotent ``spawn_bootstrap_task`` function used by both the
onboarding handler (first-cycle bootstrap) and the operator retry endpoint /
background recovery sweep (recovery bootstraps).

Eligibility checks mirror the strategic spawn gate in strategic_spawning.py so
any recovery-spawned task re-engages the strategic deferral automatically.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from typing import Literal

from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from app.models import Project, Task, TaskHistory, TaskStatus

logger = logging.getLogger(__name__)

# Statuses that constitute an "active" bootstrap task (matches strategic gate).
_ACTIVE_STATUSES = [
    TaskStatus.QUEUED,
    TaskStatus.PROVISIONING,
    TaskStatus.IN_PROGRESS,
]


@dataclass
class BootstrapSpawnResult:
    """Result of a spawn_bootstrap_task call."""

    outcome: Literal["spawned", "already_active", "already_completed", "not_eligible"]
    task_id: str | None = None
    reason: str = ""


async def spawn_bootstrap_task(
    project: Project,
    session: AsyncSession,
    *,
    is_first_bootstrap: bool = False,
    spawn_reason: str = "recovery",
) -> BootstrapSpawnResult:
    """Idempotently enqueue a repo_bootstrap task for an eligible project.

    Eligibility:
    - Project must not be a self-repo (``is_self_repo`` is False).
    - Project must not be an empty repo (``build_config.repo_is_empty`` is False/absent).
    - Project must not have ``bootstrap_completed_at`` set in ``build_config``.

    Idempotency:
    - Returns ``already_active`` if any QUEUED/PROVISIONING/IN_PROGRESS
      repo_bootstrap task already exists for this project.
    - Returns ``already_completed`` when ``bootstrap_completed_at`` is set.
    - Returns ``not_eligible`` for self-repo or empty-repo projects.

    On success creates the Task row with the given ``is_first_bootstrap`` flag
    and a SPAWNED TaskHistory row, then returns ``spawned`` with the task id.
    The caller is responsible for committing the session.
    """
    build_cfg = project.build_config or {}

    if project.is_self_repo:
        return BootstrapSpawnResult(
            outcome="not_eligible",
            reason="self_repo",
        )

    if build_cfg.get("repo_is_empty"):
        return BootstrapSpawnResult(
            outcome="not_eligible",
            reason="repo_is_empty",
        )

    if build_cfg.get("bootstrap_completed_at"):
        return BootstrapSpawnResult(
            outcome="already_completed",
            reason="bootstrap_completed_at already set",
        )

    # Check for existing active bootstrap task.
    existing = (
        await session.execute(
            select(Task.id)
            .where(
                Task.project_id == project.id,
                Task.payloads[0]["type"].as_string() == "repo_bootstrap",
                Task.status.in_(_ACTIVE_STATUSES),
            )
            .limit(1)
        )
    ).first()

    if existing is not None:
        return BootstrapSpawnResult(
            outcome="already_active",
            task_id=str(existing[0]),
            reason="existing active repo_bootstrap task",
        )

    bootstrap_payload = {
        "type": "repo_bootstrap",
        "github_owner": project.github_owner or "",
        "github_repo": project.github_repo or "",
        "integration_branch": project.integration_branch or "",
        "is_first_bootstrap": is_first_bootstrap,
    }
    task = Task(
        project_id=project.id,
        title=f"Bootstrap: {project.name}",
        description=(
            "First-cycle repo bootstrap: read project structure, confirm "
            "build/test commands, seed design context"
            if is_first_bootstrap
            else "Recovery repo bootstrap: re-read project structure and seed design context"
        ),
        payloads=[bootstrap_payload],
        vm_instance_id=f"agent-vm-{uuid.uuid4().hex}",
        status=TaskStatus.QUEUED,
        created_by="ORCHESTRATOR",
        context={"bootstrap_reason": spawn_reason},
    )
    session.add(task)
    await session.flush()

    history = TaskHistory(
        task_id=task.id,
        action="SPAWNED",
        details={"spawned_by": "ORCHESTRATOR", "reason": spawn_reason},
    )
    session.add(history)

    logger.info(
        "[BOOTSTRAP] Spawned repo_bootstrap task %s for project %s (reason=%s)",
        task.id,
        project.id,
        spawn_reason,
    )
    return BootstrapSpawnResult(
        outcome="spawned",
        task_id=str(task.id),
        reason=spawn_reason,
    )

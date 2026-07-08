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
from datetime import datetime, timedelta
from typing import Literal

from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm.attributes import flag_modified
from sqlmodel import select

from app.deploy_identity import project_is_deploy_repo
from app.models import Project, Task, TaskHistory, TaskStatus
from app.time_utils import _coerce_naive_utc, _utcnow, exponential_backoff

logger = logging.getLogger(__name__)

# Statuses that constitute an "active" bootstrap task (matches strategic gate).
_ACTIVE_STATUSES = [
    TaskStatus.QUEUED,
    TaskStatus.PROVISIONING,
    TaskStatus.IN_PROGRESS,
]
_TERMINAL_RETRY_STATUSES = [
    TaskStatus.COMPLETED,
    TaskStatus.FAILED,
    TaskStatus.CANCELLED,
]
BOOTSTRAP_RECOVERY_MAX_ATTEMPTS = 3
BOOTSTRAP_RECOVERY_BASE_BACKOFF_HOURS = 1


def _parse_recovery_at(value: object) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        return _coerce_naive_utc(
            datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
        )
    except ValueError:
        return None


def coerce_recovery_attempts(value: object) -> int:
    try:
        return max(int(value or 0), 0)
    except (TypeError, ValueError):
        return 0


def next_recovery_backoff(attempts: int) -> timedelta:
    """Return the automatic bootstrap recovery delay for a degraded attempt."""
    delay_seconds = exponential_backoff(
        max(attempts, 1),
        base=BOOTSTRAP_RECOVERY_BASE_BACKOFF_HOURS * 3600,
        cap=(2 ** (BOOTSTRAP_RECOVERY_MAX_ATTEMPTS - 1)) * BOOTSTRAP_RECOVERY_BASE_BACKOFF_HOURS * 3600,
        jitter=0,
    )
    return timedelta(seconds=delay_seconds)


# A refresh is a one-shot operator action against an already-COMPLETED project.
# It bypasses the bootstrap_completed_at not-eligible gate but, unlike automatic
# recovery, does NOT arm the backoff/auto-retry loop.
REFRESH_SPAWN_REASON = "operator_refresh"


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
    automatic_recovery: bool = False,
) -> BootstrapSpawnResult:
    """Idempotently enqueue a repo_bootstrap task for an eligible project.

    Eligibility:
    - Project must not be the deploy's own repo (``project_is_deploy_repo`` is
      False — deploy-identity match, or the ``is_self_repo`` fallback).
    - Project must not be an empty repo (``build_config.repo_is_empty`` is False/absent).
    - Project must not have ``bootstrap_completed_at`` set in ``build_config``
      UNLESS this is an operator refresh (``spawn_reason == "operator_refresh"``),
      which deliberately re-runs bootstrap for an already-completed project to
      replace the prior repo_overview with a fresher one.

    Idempotency:
    - Returns ``already_active`` if any QUEUED/PROVISIONING/IN_PROGRESS
      repo_bootstrap task already exists for this project.
    - Returns ``already_completed`` when ``bootstrap_completed_at`` is set
      (except under an operator refresh, which bypasses this gate).
    - Returns ``not_eligible`` for self-repo or empty-repo projects.

    On success creates the Task row with the given ``is_first_bootstrap`` flag
    and a SPAWNED TaskHistory row, then returns ``spawned`` with the task id.
    The caller is responsible for committing the session.

    A refresh (``spawn_reason == "operator_refresh"``) is a one-shot operator
    action: it does NOT clear ``bootstrap_completed_at`` (preserving the
    first-bootstrap strategic gate semantics for brand-new projects) and does
    NOT arm the automatic-recovery backoff loop.
    """
    build_cfg = dict(project.build_config or {})
    is_refresh = spawn_reason == REFRESH_SPAWN_REASON

    if project_is_deploy_repo(project):
        return BootstrapSpawnResult(
            outcome="not_eligible",
            reason="self_repo",
        )

    if build_cfg.get("repo_is_empty"):
        return BootstrapSpawnResult(
            outcome="not_eligible",
            reason="repo_is_empty",
        )

    if build_cfg.get("bootstrap_completed_at") and not is_refresh:
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

    if is_refresh:
        # One-shot operator refresh of a completed project. Do NOT clear
        # bootstrap_completed_at (keeps the first-bootstrap strategic gate
        # intact for brand-new projects) and do NOT arm the auto-recovery
        # backoff loop — drop any stale backoff markers and record a clear
        # non-automatic recovery_state so sweeps never pick this up.
        build_cfg.pop("bootstrap_recovery_next_at", None)
        build_cfg.pop("bootstrap_recovery_exhausted_at", None)
        build_cfg["bootstrap_recovery_attempts"] = 0
        build_cfg["bootstrap_recovery_state"] = "operator_refresh"
        project.build_config = build_cfg
        flag_modified(project, "build_config")
        session.add(project)
    elif automatic_recovery:
        terminal_attempts = (
            await session.execute(
                select(Task.id).where(
                    Task.project_id == project.id,
                    Task.payloads[0]["type"].as_string() == "repo_bootstrap",
                    Task.status.in_(_TERMINAL_RETRY_STATUSES),
                )
            )
        ).all()
        recorded_attempts = coerce_recovery_attempts(
            build_cfg.get("bootstrap_recovery_attempts")
        )
        attempts = max(recorded_attempts, len(terminal_attempts))
        if attempts >= BOOTSTRAP_RECOVERY_MAX_ATTEMPTS:
            if not build_cfg.get("bootstrap_recovery_exhausted_at"):
                build_cfg["bootstrap_recovery_attempts"] = attempts
                build_cfg["bootstrap_recovery_state"] = "exhausted"
                build_cfg["bootstrap_recovery_exhausted_at"] = _utcnow().isoformat()
                project.build_config = build_cfg
                flag_modified(project, "build_config")
                session.add(project)
            return BootstrapSpawnResult(
                outcome="not_eligible",
                reason="bootstrap_recovery_exhausted",
            )
        next_recovery_at = _parse_recovery_at(
            build_cfg.get("bootstrap_recovery_next_at")
        )
        if next_recovery_at and next_recovery_at > _utcnow():
            return BootstrapSpawnResult(
                outcome="not_eligible",
                reason="bootstrap_recovery_backoff",
            )
    elif spawn_reason == "operator_retry":
        build_cfg.pop("bootstrap_recovery_next_at", None)
        build_cfg.pop("bootstrap_recovery_exhausted_at", None)
        build_cfg["bootstrap_recovery_attempts"] = 0
        build_cfg["bootstrap_recovery_state"] = "operator_retry"
        project.build_config = build_cfg
        flag_modified(project, "build_config")
        session.add(project)

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

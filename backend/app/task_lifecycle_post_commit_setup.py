"""Locking and recovery setup extracted from ``task_lifecycle`` post-commit.

This is the first post-commit stage.  It reserves strategic-message sequence
numbers, acquires the canonical Project -> Task lock pair, validates recovery
leases, and supplies a rollback/refetch closure to later stages.  Keeping the
whole setup together is important: no completion handler may run before its
recovery lease has been revalidated under the lifecycle locks.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from dataclasses import dataclass
from typing import Awaitable, Callable

from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from app.models import Project, Task, TaskStatus
from app.post_commit_errors import PostCommitLocked
from app.schemas import AgentStatusPayload
from app.task_capabilities import get_task_capability

logger = logging.getLogger(__name__)


@dataclass
class PostCommitSetupState:
    """Live state established before post-commit completion stages run."""

    task: Task
    locked_project: Project | None
    peek_capability: object | None
    peek_task_type: str
    preallocated_strategic_message_sequences: list[int]
    refetch: Callable[[], Awaitable[Task | None]]


async def run_post_commit_setup(
    *,
    task_id: uuid.UUID,
    payload: AgentStatusPayload,
    session: AsyncSession,
    expected_childless_recovery_token: str | None = None,
    expected_research_spawn_recovery_token: str | None = None,
    expected_post_commit_claim_token: str | None = None,
    expected_post_commit_status: TaskStatus | None = None,
) -> tuple[PostCommitSetupState | None, dict | None]:
    """Establish the post-commit lock/recovery state.

    Returns ``(state, None)`` when later stages may run, or ``(None, response)``
    for an idempotent early return.  The response shape intentionally matches
    the former inline returns in ``_process_post_commit``.
    """
    from app.project_message_creation import reserve_project_message_sequences
    from app.strategic_completion import strategic_completion_message_count
    from app.task_locking import lock_project_then_task

    # The post-commit unit can lock BOTH the Project and the Task row.  The
    # canonical order is Project before Task (docs/concurrency.md), including
    # for non-strategic tasks, so concurrent completion chains cannot invert
    # their lock order.
    peek_row = (
        await session.execute(
            select(Task.project_id, Task.payloads).where(Task.id == task_id)
        )
    ).first()
    if peek_row is None:
        return None, {"status": "accepted"}
    peek_project_id = peek_row[0]
    peek_payloads = peek_row[1] if isinstance(peek_row[1], list) else []
    peek_task_type = (
        str((peek_payloads[0] or {}).get("type") or "")
        if peek_payloads and isinstance(peek_payloads[0], dict)
        else ""
    )
    peek_capability = get_task_capability(peek_task_type)
    peek_completion_handler = (
        peek_capability.completion_handler if peek_capability else None
    )
    preallocated_strategic_message_sequences: list[int] = []
    if peek_completion_handler == "strategic":
        message_count = strategic_completion_message_count(payload.logs or "")
        if message_count:
            preallocated_strategic_message_sequences = (
                await reserve_project_message_sequences(
                    session,
                    peek_project_id,
                    count=message_count,
                    lock_timeout_ms=1_500,
                )
            )
            # Reservation gaps are harmless.  Do not retain its counter lock
            # while acquiring the Project -> Task authority locks.
            await session.commit()

    # ``skip_locked`` plus bounded retry keeps the status endpoint responsive
    # while another callback is briefly committing the same task.
    task = None
    locked_project: Project | None = None
    blocked_lock_scope = "task"
    for _attempt in range(3):
        if peek_project_id is not None:
            async with lock_project_then_task(
                session,
                peek_project_id,
                task_id,
                skip_locked=True,
            ) as (project, locked_task):
                if locked_task is not None:
                    task = locked_task
                    locked_project = project
                    break
                blocked_lock_scope = "project" if project is None else "task"
                if project is not None:
                    await session.rollback()
        else:
            # Defensive fallback only: Task.project_id is NOT NULL, so this
            # handles a peek racing a hard delete.
            task = (
                (
                    await session.execute(
                        select(Task)
                        .where(Task.id == task_id)
                        .with_for_update(skip_locked=True)
                    )
                )
                .scalars()
                .first()
            )
            if task is not None:
                break
        exists = (
            await session.execute(select(Task.id).where(Task.id == task_id))
        ).first()
        if exists is None:
            return None, {"status": "accepted"}
        await asyncio.sleep(2)
    if task is None:
        blocked_id = peek_project_id if blocked_lock_scope == "project" else task_id
        raise PostCommitLocked(
            f"{blocked_lock_scope} {blocked_id} row held by peer after 3 retries "
            f"while processing task {task_id}",
            lock_scope=blocked_lock_scope,
            resource_id=blocked_id,
        )

    # Every recovery marker is an exact-attempt lease, not authority to revive
    # a later cancelled/requeued task.  Validate it after acquiring the
    # canonical lifecycle locks and before dispatching any handler.
    if expected_childless_recovery_token is not None:
        recovery_meta = dict(
            (task.context or {}).get("_childless_post_commit_recovery") or {}
        )
        plan_output = task.plan_output
        claim_is_current = (
            recovery_meta.get("claim_token") == expected_childless_recovery_token
            and task.status == TaskStatus.EVALUATING
            and not task.archived
            and not task.post_commit_pending
            and (task.first_payload or {}).get("type") == "plan"
            and isinstance(plan_output, dict)
            and isinstance(plan_output.get("subtasks"), list)
            and bool(plan_output.get("subtasks"))
        )
        if claim_is_current:
            child_id = (
                await session.execute(
                    select(Task.id).where(Task.parent_task_id == task_id).limit(1)
                )
            ).scalar_one_or_none()
            claim_is_current = child_id is None
        if not claim_is_current:
            logger.info(
                "[POST_COMMIT SWEEP] Skipping stale marker-less recovery "
                "claim for task %s after lifecycle-lock revalidation",
                str(task_id)[:8],
            )
            return None, {"status": "accepted", "recovery_skipped": True}

    if expected_research_spawn_recovery_token is not None:
        from app.findings_index import FINALIZED_RESEARCH_FINDINGS_KEY
        from app.recovery_research_spawn_pending import (
            RESEARCH_SPAWN_RECOVERY_KEY,
            research_spawn_recovery_attempt_identity,
        )

        recovery_meta = dict(
            (task.context or {}).get(RESEARCH_SPAWN_RECOVERY_KEY) or {}
        )
        project_paused = bool(
            locked_project is None or locked_project.paused or locked_project.archived
        )
        claim_is_current = bool(
            recovery_meta.get("claim_token") == expected_research_spawn_recovery_token
            and recovery_meta.get("attempt_identity")
            == research_spawn_recovery_attempt_identity(task)
            and task.status in (TaskStatus.COMPLETED, TaskStatus.EVALUATING)
            and not task.archived
            and not task.post_commit_pending
            and (task.first_payload or {}).get("type") == "research"
            and (task.context or {}).get("_research_spawn_pending") is True
            and (task.context or {}).get(FINALIZED_RESEARCH_FINDINGS_KEY) is not True
            and isinstance(task.plan_output, list)
        )
        if project_paused or not claim_is_current:
            reason = "project_paused" if project_paused else "stale_claim"
            logger.info(
                "[POST_COMMIT SWEEP] Skipping marker-less research recovery "
                "for task %s (%s)",
                str(task_id)[:8],
                reason,
            )
            return None, {
                "status": "accepted",
                "recovery_skipped": True,
                "recovery_reason": reason,
            }

    if expected_post_commit_claim_token is not None:
        pending_meta = dict(
            (task.context or {}).get("_post_commit_pending_meta") or {}
        )
        claim_is_current = bool(
            task.post_commit_pending
            and not task.archived
            and task.status == expected_post_commit_status
            and pending_meta.get("claim_token") == expected_post_commit_claim_token
        )
        if not claim_is_current:
            logger.info(
                "[POST_COMMIT SWEEP] Skipping stale pending recovery claim "
                "for task %s after lifecycle-lock revalidation",
                str(task_id)[:8],
            )
            return None, {"status": "accepted", "recovery_skipped": True}

    async def rollback_and_refetch_task() -> Task | None:
        """Roll back a failed section and return a non-expired task instance."""
        try:
            await session.rollback()
        except Exception:
            logger.exception(
                "[LIFECYCLE] Rollback after post-commit section failure failed for task %s",
                task_id,
            )
        with session.no_autoflush:
            return (
                (
                    await session.execute(
                        select(Task)
                        .where(Task.id == task_id)
                        .execution_options(populate_existing=True)
                    )
                )
                .scalars()
                .first()
            )

    return PostCommitSetupState(
        task=task,
        locked_project=locked_project,
        peek_capability=peek_capability,
        peek_task_type=peek_task_type,
        preallocated_strategic_message_sequences=preallocated_strategic_message_sequences,
        refetch=rollback_and_refetch_task,
    ), None

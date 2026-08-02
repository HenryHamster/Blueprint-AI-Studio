"""Recovery helpers for deterministic Alembic pre-merge guard blocks."""

from __future__ import annotations

import logging
from typing import Any

from sqlalchemy.orm import load_only
from sqlalchemy.orm.attributes import flag_modified
from sqlmodel import select

from app.ci_merge_helpers import (
    _pr_number_match,
    revoke_review_authority_for_active_fix,
)
from app.models import Task, TaskStatus

logger = logging.getLogger(__name__)

MIGRATION_GUARD_FIX_REASON = "migration_guard"
MIGRATION_GUARD_STATE_FIX_SPAWNED = "fix_spawned"
MIGRATION_GUARD_STATE_FIX_DEFERRED = "fix_deferred"
MIGRATION_GUARD_STATE_FIX_SUPPRESSED = "fix_suppressed"
MIGRATION_GUARD_STATE_DEFERRED = "deferred"


def coerce_migration_guard_result(result: Any) -> tuple[str, str | None]:
    """Return ``("ok"|"block"|"defer", reason)`` from any guard result shape.

    Real ``enforce_premerge_migration_guard`` results carry ``.verdict``. Older
    tuple mocks/precomputed values only carry ``(ok, reason)``; a false legacy
    tuple is treated as deterministic ``block`` so tests and operator-injected
    precomputed blocks take the non-waitable path.
    """
    verdict = getattr(result, "verdict", None)
    if verdict in {"ok", "block", "defer"}:
        return verdict, getattr(result, "reason", None)

    try:
        first = result[0]
        reason = result[1]
    except (AttributeError, IndexError, KeyError, TypeError):
        try:
            ok, reason = result
        except (TypeError, ValueError) as exc:
            raise TypeError(
                "migration guard result must be verdict-like or two-value iterable"
            ) from exc
        return ("ok" if ok else "block"), reason

    if first in {"ok", "block", "defer"}:
        return first, reason
    return ("ok" if bool(first) else "block"), reason


def mark_migration_guard_deferred(
    task: Task,
    *,
    review_loop: dict,
    reason: str | None,
    context_snapshot: dict | None = None,
) -> None:
    """Record a transient guard defer while preserving the CI wait."""
    loop = dict(review_loop or {})
    loop["migration_guard_state"] = MIGRATION_GUARD_STATE_DEFERRED
    loop["migration_guard_reason"] = str(reason or "")[:2000]
    task.context = {
        **(context_snapshot if context_snapshot is not None else (task.context or {})),
        "review_loop": loop,
    }
    flag_modified(task, "context")
    task.latest_status_message = (
        f"Merge deferred — Alembic dual-head guard could not complete: {reason}"
    )[:500]
    logger.info(
        "[CI] Alembic pre-merge guard deferred for task %s: %s",
        task.id,
        reason,
    )


async def spawn_migration_guard_fix_task(
    task: Task,
    session,
    *,
    pr_number: int,
    branch: str,
    base_branch: str,
    original_task_id: str = "",
    guard_reason: str | None,
    review_loop: dict | None = None,
    context_snapshot: dict | None = None,
    complete_source_on_fix_spawn: bool = True,
) -> tuple[Task | None, bool]:
    """Spawn a targeted fix for a deterministic Alembic guard block.

    The source task is marked with a non-waitable migration-guard state and
    ``ci_waiting=False`` so the CI monitor stops polling the same green checks.
    """
    from app.review_pipeline import (
        SPAWN_REASON_DEFERRED,
        SPAWN_REASON_KEY,
        _spawn_pipeline_task,
        build_migration_guard_fix_prompt,
    )
    from app.rollup_state import _is_rollup_parent_awaiting_merge

    source_context = dict(
        context_snapshot if context_snapshot is not None else (task.context or {})
    )
    loop = dict(review_loop or source_context.get("review_loop") or {})
    task_id = task.id
    project_id = task.project_id

    def _write_block_state(
        state: str,
        *,
        fix_task_id: object | None = None,
        requires_approval: bool = False,
        ci_waiting: bool = False,
        ci_passed: bool = False,
    ) -> None:
        loop["ci_waiting"] = ci_waiting
        loop["ci_passed"] = ci_passed
        loop["migration_guard_state"] = state
        loop["migration_guard_reason"] = str(guard_reason or "")[:2000]
        if fix_task_id is not None:
            loop["migration_guard_fix_spawned"] = str(fix_task_id)
            # Compatibility with existing CI-monitor skip/backstop paths.
            loop["ci_fix_spawned"] = str(fix_task_id)
            revoked_loop = revoke_review_authority_for_active_fix(
                task,
                fix_task_id,
                review_loop=loop,
                context_snapshot=source_context,
            )
            loop.clear()
            loop.update(revoked_loop)
        elif state == MIGRATION_GUARD_STATE_FIX_SUPPRESSED:
            loop["migration_guard_fix_suppressed"] = True
        task.context = {
            **source_context,
            **dict(task.context or {}),
            "review_loop": loop,
        }
        flag_modified(task, "context")
        if requires_approval:
            task.requires_approval = True
        task.status = TaskStatus.EVALUATING

    if not branch or not base_branch:
        _write_block_state(
            MIGRATION_GUARD_STATE_FIX_SUPPRESSED, requires_approval=True
        )
        task.latest_status_message = (
            f"Merge blocked — Alembic guard needs migration chain repair on "
            f"PR #{pr_number}, but branch/base metadata is missing; manual "
            "intervention required"
        )[:500]
        session.add(task)
        return None, False

    active_tasks = (
        (
            await session.execute(
                select(Task)
                .options(
                    load_only(
                        Task.id,
                        Task.status,
                        Task.context,
                        Task.payloads,
                        Task.project_id,
                        Task.status_log,
                        raiseload=True,
                    )
                )
                .where(
                    Task.project_id == project_id,
                    Task.status.in_(
                        [
                            TaskStatus.QUEUED,
                            TaskStatus.PROVISIONING,
                            TaskStatus.IN_PROGRESS,
                            TaskStatus.EVALUATING,
                        ]
                    ),
                    Task.archived.is_(False),
                    _pr_number_match(session, pr_number),
                )
            )
        )
        .scalars()
        .all()
    )
    existing_fix = next(
        (
            t
            for t in active_tasks
            if t.id != task_id and t.first_payload.get("type") == "fix"
        ),
        None,
    )
    if existing_fix is not None:
        _write_block_state(
            MIGRATION_GUARD_STATE_FIX_SPAWNED, fix_task_id=existing_fix.id
        )
        if complete_source_on_fix_spawn and not _is_rollup_parent_awaiting_merge(
            source_context
        ):
            task.status = TaskStatus.COMPLETED
        task.latest_status_message = (
            f"PR #{pr_number} Alembic guard blocked; migration fix already "
            f"active -> {existing_fix.id}"
        )[:500]
        session.add(task)
        # This branch bypasses _spawn_pipeline_task, so make the same active-fix
        # authority transition durable before handing control back to callers.
        await session.commit()
        return existing_fix, False

    fix_prompt = build_migration_guard_fix_prompt(
        pr_number=pr_number,
        base_branch=base_branch,
        guard_reason=guard_reason or "Alembic combined-chain guard blocked",
    )
    fix_task, was_created = await _spawn_pipeline_task(
        task,
        "fix",
        session,
        pr_number,
        branch,
        base_branch,
        fix_prompt=fix_prompt,
        original_task_id=original_task_id,
        fix_reason=MIGRATION_GUARD_FIX_REASON,
        suppress_warm_handoff=True,
    )
    if fix_task is not None:
        _write_block_state(MIGRATION_GUARD_STATE_FIX_SPAWNED, fix_task_id=fix_task.id)
        if complete_source_on_fix_spawn and not _is_rollup_parent_awaiting_merge(
            source_context
        ):
            task.status = TaskStatus.COMPLETED
        task.latest_status_message = (
            f"PR #{pr_number} Alembic guard blocked — spawned migration fix "
            f"-> {fix_task.id}"
        )[:500]
        logger.warning(
            "[CI] PR #%s Alembic guard blocked — spawned migration fix %s",
            pr_number,
            fix_task.id,
        )
    else:
        spawn_reason = (task.context or {}).get(SPAWN_REASON_KEY)
        if spawn_reason == SPAWN_REASON_DEFERRED:
            _write_block_state(
                MIGRATION_GUARD_STATE_FIX_DEFERRED,
                ci_waiting=True,
                ci_passed=True,
            )
            task.latest_status_message = (
                f"PR #{pr_number} Alembic guard blocked, but migration fix "
                "spawn was deferred; next CI poll retries"
            )[:500]
            logger.info(
                "[CI] PR #%s Alembic guard fix spawn deferred; next poll retries",
                pr_number,
            )
            session.add(task)
            return None, False

        _write_block_state(
            MIGRATION_GUARD_STATE_FIX_SUPPRESSED, requires_approval=True
        )
        task.latest_status_message = (
            f"PR #{pr_number} Alembic guard blocked, but migration fix spawn was "
            "suppressed; manual intervention required"
        )[:500]
        logger.warning(
            "[CI] PR #%s Alembic guard blocked but fix spawn was suppressed",
            pr_number,
        )
    session.add(task)
    return fix_task, was_created

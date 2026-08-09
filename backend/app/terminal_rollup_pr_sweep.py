"""Periodic reconciliation for terminal rollup parents that still own a PR."""

from __future__ import annotations

import logging
from typing import Any
from time import monotonic as _monotonic

from sqlalchemy import func
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm.attributes import flag_modified
from sqlmodel import select

from app.database import resolve_dialect_name
from app.models import Task, TaskStatus
from app.time_utils import _utcnow

logger = logging.getLogger("blueprint")

TERMINAL_ROLLUP_PR_ATTENTION_KEY = "terminal_rollup_pr_attention"
TERMINAL_ROLLUP_PR_SWEEP_REQUEUES_KEY = "_terminal_pr_sweep_requeues"
TERMINAL_ROLLUP_PR_SWEEP_INTERVAL_SECONDS = 5 * 60
TERMINAL_ROLLUP_PR_SWEEP_BATCH_SIZE = 100
TERMINAL_ROLLUP_PR_SWEEP_REQUEUE_CAP = 1
PARENT_REQUEUE_CAP = 3

_last_sweep_monotonic: float | None = None


def _terminal_rollup_candidate_filter(session: AsyncSession) -> tuple[Any, ...]:
    """Return the narrow, dialect-portable candidate predicate."""

    if resolve_dialect_name(session) == "sqlite":
        return (
            func.json_extract(Task.payloads, "$[0].type").in_(("research", "plan")),
            func.json_extract(Task.context, "$.rollup_pr_number").is_not(None),
            func.coalesce(func.json_extract(Task.context, "$.pr_merged"), 0) != 1,
            func.json_extract(
                Task.context, f"$.{TERMINAL_ROLLUP_PR_ATTENTION_KEY}"
            ).is_(None),
        )
    return (
        Task.payloads[0]["type"].as_string().in_(("research", "plan")),
        Task.context["rollup_pr_number"].as_string().is_not(None),
        Task.context["pr_merged"].as_boolean().is_not(True),
        Task.context[TERMINAL_ROLLUP_PR_ATTENTION_KEY].as_string().is_(None),
    )


def _attention_message(outcome: str, *, cap_exhausted: bool = False) -> str:
    if cap_exhausted:
        return (
            "Terminal rollup PR reconciliation stopped after the automatic "
            "resurrection cap; inspect the PR and task before requeueing."
        )
    return (
        f"Terminal rollup PR reconciliation outcome {outcome}; automatic "
        "requeue was not safe, so operator review is required."
    )


def _mark_operator_attention(
    task: Task,
    outcome: str,
    *,
    cap_exhausted: bool = False,
) -> None:
    context = dict(task.context or {})
    context[TERMINAL_ROLLUP_PR_ATTENTION_KEY] = {
        "outcome": outcome,
        "cap_exhausted": cap_exhausted,
        "marked_at": _utcnow().isoformat(),
    }
    task.context = context
    task.latest_status_message = _attention_message(
        outcome, cap_exhausted=cap_exhausted
    )
    flag_modified(task, "context")


def _clear_attention_marker(task: Task) -> None:
    context = dict(task.context or {})
    context.pop(TERMINAL_ROLLUP_PR_ATTENTION_KEY, None)
    task.context = context
    flag_modified(task, "context")


def _sweep_requeue_count(task: Task) -> int:
    value = (task.context or {}).get(TERMINAL_ROLLUP_PR_SWEEP_REQUEUES_KEY, 0)
    try:
        return max(int(value), 0)
    except (TypeError, ValueError):
        return 0


def _requeue_cap_exhausted(task: Task) -> bool:
    value = (task.context or {}).get("_requeue_count", 0)
    try:
        return int(value) >= PARENT_REQUEUE_CAP
    except (TypeError, ValueError):
        return False


async def _trigger_merged_parent_classifier(
    task: Task, session: AsyncSession
) -> None:
    """Nudge the authoritative classifier after a merged PR is rearmed."""

    child = (
        (
            await session.execute(
                select(Task)
                .where(
                    Task.parent_task_id == task.id,
                    Task.status == TaskStatus.COMPLETED,
                )
                .order_by(Task.updated_at.desc())
                .limit(1)
            )
        )
        .scalars()
        .first()
    )
    if child is None:
        logger.warning(
            "[TERMINAL-ROLLUP-SWEEP] merged parent %s has no completed child "
            "to trigger classification",
            task.id,
        )
        return
    from app.rollup_management import _check_and_trigger_rollup

    try:
        await _check_and_trigger_rollup(child, session)
    except Exception:
        # The requeue is already committed. The normal EVALUATING recovery
        # sweep remains the durable backstop if this immediate nudge fails.
        logger.exception(
            "[TERMINAL-ROLLUP-SWEEP] merged parent %s classifier nudge failed",
            task.id,
        )
        await session.rollback()


async def _reconcile_one(task_id, session: AsyncSession) -> str | None:
    """Lock, reread, reconcile, and commit one candidate."""

    locked = (
        (
            await session.execute(
                select(Task)
                .where(Task.id == task_id)
                .with_for_update()
                .execution_options(populate_existing=True)
            )
        )
        .scalars()
        .first()
    )
    if locked is None:
        await session.rollback()
        return None

    context = locked.context or {}
    task_type = (locked.first_payload or {}).get("type")
    if (
        locked.status != TaskStatus.FAILED
        or task_type not in ("research", "plan")
        or not context.get("rollup_pr_number")
        or context.get("pr_merged") is True
        or context.get(TERMINAL_ROLLUP_PR_ATTENTION_KEY) is not None
    ):
        await session.rollback()
        return None

    from app.terminal_rollup_requeue import reconcile_terminal_rollup_requeue

    try:
        outcome = await reconcile_terminal_rollup_requeue(locked, session)
        auto_rearm_outcome = outcome in {
            "merged_pr_rearmed",
            "open_pr_rearmed",
            "replacement_pr_rearmed",
        }
        count = _sweep_requeue_count(locked)
        if not auto_rearm_outcome:
            _mark_operator_attention(locked, outcome)
        elif (
            count >= TERMINAL_ROLLUP_PR_SWEEP_REQUEUE_CAP
            or _requeue_cap_exhausted(locked)
        ):
            _mark_operator_attention(locked, outcome, cap_exhausted=True)
        else:
            context = dict(locked.context or {})
            context[TERMINAL_ROLLUP_PR_SWEEP_REQUEUES_KEY] = count + 1
            locked.context = context
            flag_modified(locked, "context")
            _clear_attention_marker(locked)
            locked.status = TaskStatus.QUEUED
            locked.latest_status_message = (
                f"Terminal rollup PR {outcome}; automatic requeue scheduled."
            )
            session.add(locked)
            from app.task_lifecycle import _requeue_parent_task

            await _requeue_parent_task(locked, session)
            if locked.status == TaskStatus.FAILED:
                _mark_operator_attention(locked, outcome, cap_exhausted=True)
                session.add(locked)
                await session.commit()
                return outcome
            if outcome == "merged_pr_rearmed":
                await _trigger_merged_parent_classifier(locked, session)
            return outcome
        session.add(locked)
        await session.commit()
        return outcome
    except Exception:
        await session.rollback()
        logger.exception(
            "[TERMINAL-ROLLUP-SWEEP] reconciliation failed for task %s", task_id
        )
        return None


async def sweep_terminal_rollup_prs(
    session: AsyncSession,
    *,
    batch_size: int = TERMINAL_ROLLUP_PR_SWEEP_BATCH_SIZE,
) -> dict[str, int]:
    """Reconcile a bounded batch of FAILED rollup parents with tracked PRs."""

    candidate_ids = (
        (
            await session.execute(
                select(Task.id)
                .where(
                    Task.status == TaskStatus.FAILED,
                    *_terminal_rollup_candidate_filter(session),
                )
                .order_by(Task.updated_at.asc())
                .limit(batch_size)
            )
        )
        .scalars()
        .all()
    )
    results: dict[str, int] = {}
    for task_id in candidate_ids:
        outcome = await _reconcile_one(task_id, session)
        if outcome:
            results[outcome] = results.get(outcome, 0) + 1
    return results


async def maybe_sweep_terminal_rollup_prs(session: AsyncSession) -> dict[str, int]:
    """Run the terminal sweep on its own multi-minute orchestrator cadence."""

    global _last_sweep_monotonic
    now = _monotonic()
    if (
        _last_sweep_monotonic is not None
        and now - _last_sweep_monotonic < TERMINAL_ROLLUP_PR_SWEEP_INTERVAL_SECONDS
    ):
        return {}
    _last_sweep_monotonic = now
    return await sweep_terminal_rollup_prs(session)

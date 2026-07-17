"""Crash-safe Max-plan slot release for authoritative Resident turns."""

from __future__ import annotations

import logging
import uuid
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlmodel import select

from app.max_plan_pool import get_task_llm_account_id
from app.max_plan_slot_accounting import reconcile_account_active_counts
from app.models import Task, TaskStatus
from app.strategic_resident_slot_counts import resident_account_active_counts
from app.time_utils import _utcnow


logger = logging.getLogger(__name__)
SLOT_RELEASE_STATE_KEY = "_resident_slot_release"
_TERMINAL_STATUSES = frozenset(
    {TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.CANCELLED}
)


def _canonical_identity(task: Task) -> dict[str, str] | None:
    raw_account_id = get_task_llm_account_id(task)
    if not raw_account_id:
        return None
    return {
        "account_id": str(raw_account_id),
        "project_id": str(task.project_id),
        "idempotency_key": f"strategic-resident-turn:{task.id}",
    }


def _canonical_marker(context: dict[str, Any], identity: dict[str, str]) -> bool:
    marker = context.get(SLOT_RELEASE_STATE_KEY)
    return bool(
        isinstance(marker, dict)
        and all(marker.get(key) == value for key, value in identity.items())
    )


def stage_resident_turn_slot_release(task: Task) -> None:
    """Persist release intent in the same transaction as terminal status."""

    context = dict(task.context or {})
    canonical_identity = _canonical_identity(task)
    if canonical_identity is None:
        return
    if _canonical_marker(context, canonical_identity):
        return
    context.pop("_slot_released", None)
    context[SLOT_RELEASE_STATE_KEY] = {
        "state": "pending",
        **canonical_identity,
        "staged_at": _utcnow().isoformat(),
    }
    task.context = context


def clear_resident_turn_slot_release(context: dict[str, Any]) -> None:
    """Remove release state when cloning a failed turn into a fresh Task."""

    context.pop(SLOT_RELEASE_STATE_KEY, None)
    context.pop("_slot_released", None)


def _private_factory(
    session_factory: async_sessionmaker[AsyncSession] | AsyncSession,
) -> async_sessionmaker[AsyncSession]:
    if isinstance(session_factory, AsyncSession):
        return async_sessionmaker(
            bind=session_factory.bind,
            expire_on_commit=False,
            autoflush=False,
        )
    return session_factory


async def _claim_release(
    factory: async_sessionmaker[AsyncSession],
    *,
    task_id: uuid.UUID,
    project_id: uuid.UUID,
) -> dict[str, Any] | None:
    async with factory() as session, session.begin():
        task = (
            await session.execute(
                select(Task).where(Task.id == task_id).with_for_update()
            )
        ).scalar_one_or_none()
        if (
            task is None
            or task.project_id != project_id
            or task.status not in _TERMINAL_STATUSES
        ):
            return None
        context = dict(task.context or {})
        identity = _canonical_identity(task)
        if identity is None:
            return None
        if context.get("_slot_released") is True and _canonical_marker(
            context, identity
        ) and context[SLOT_RELEASE_STATE_KEY].get("state") == "completed":
            return None
        stage_resident_turn_slot_release(task)
        context = dict(task.context or {})
        state = context.get(SLOT_RELEASE_STATE_KEY)
        if not isinstance(state, dict):
            return None
        completed = dict(state)
        completed.update(
            {
                "state": "completed",
                "release_mode": "durable_release_claimed",
                "completed_at": _utcnow().isoformat(),
            }
        )
        context[SLOT_RELEASE_STATE_KEY] = completed
        context["_slot_released"] = True
        task.context = context
        session.add(task)
        return completed


async def _record_release_mode(
    factory: async_sessionmaker[AsyncSession],
    *,
    task_id: uuid.UUID,
    project_id: uuid.UUID,
    release_mode: str,
) -> None:
    async with factory() as session, session.begin():
        task = (
            await session.execute(
                select(Task).where(Task.id == task_id).with_for_update()
            )
        ).scalar_one_or_none()
        if (
            task is None
            or task.project_id != project_id
            or task.status not in _TERMINAL_STATUSES
        ):
            return
        context = dict(task.context or {})
        identity = _canonical_identity(task)
        state = context.get(SLOT_RELEASE_STATE_KEY)
        if (
            identity is None
            or context.get("_slot_released") is not True
            or not _canonical_marker(context, identity)
            or not isinstance(state, dict)
            or state.get("state") != "completed"
        ):
            return
        completed = dict(state)
        completed["release_mode"] = release_mode
        context[SLOT_RELEASE_STATE_KEY] = completed
        context["_slot_released"] = True
        task.context = context
        session.add(task)


async def release_resident_turn_slot(
    session_factory: async_sessionmaker[AsyncSession] | AsyncSession,
    *,
    task_id: uuid.UUID,
    project_id: uuid.UUID,
) -> bool:
    """Release once after terminal commit; repair ambiguous crash boundaries."""

    factory = _private_factory(session_factory)
    state = await _claim_release(
        factory,
        task_id=task_id,
        project_id=project_id,
    )
    if state is None:
        return False
    account_id = str(state["account_id"])
    try:
        total, project_total = await resident_account_active_counts(
            factory,
            account_id=account_id,
            project_id=project_id,
        )
        release_mode = await reconcile_account_active_counts(
            account_id,
            active_tasks=total,
            project_id=str(project_id),
            project_active_tasks=project_total,
        )
        await _record_release_mode(
            factory,
            task_id=task_id,
            project_id=project_id,
            release_mode=release_mode,
        )
    except Exception:  # noqa: BLE001 -- post-commit cache repair is best-effort.
        # The durable claim prevents duplicate decrements. A local-cache miss or
        # failure is safe: the pool's periodic database reconciliation repairs
        # temporary over-counting without affecting terminal task truth.
        logger.warning("Resident slot cache reconciliation deferred for task %s", task_id, exc_info=True)
    return True


__all__ = [
    "SLOT_RELEASE_STATE_KEY",
    "clear_resident_turn_slot_release",
    "release_resident_turn_slot",
    "stage_resident_turn_slot_release",
]

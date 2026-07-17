"""Durable post-commit cleanup for Resident ownership rollback."""

from __future__ import annotations

import uuid
from copy import deepcopy
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlmodel import select

from app.models import (
    StrategicResident,
    Task,
    TaskStatus,
    TaskWakeRegistration,
    WakeDeliveryState,
)
from app.strategic_resident_cleanup_state import (
    cleanup_claim_is_live as _claim_is_live,
    cleanup_items as _cleanup_items,
    replace_cleanup_items as _replace_cleanup_items,
)
from app.time_utils import _utcnow

_CLAIMABLE = frozenset({"pending", "failed"})
_ACTIVE_WAKE_STATES = frozenset(
    {WakeDeliveryState.PENDING, WakeDeliveryState.FIRED}
)


async def _claim_cleanup(
    factory: async_sessionmaker[AsyncSession],
    project_id: uuid.UUID,
    *,
    attempted: set[str],
) -> dict[str, Any] | None:
    now = _utcnow()
    async with factory() as session, session.begin():
        resident = (
            await session.execute(
                select(StrategicResident)
                .where(StrategicResident.project_id == project_id)
                .with_for_update()
            )
        ).scalar_one_or_none()
        if resident is None or resident.engine_owner != "legacy":
            return None
        authority_generation = int(resident.authority_generation)
        items = _cleanup_items(resident)
        changed = False
        for item in items:
            task_id = str(item.get("task_id") or "")
            status = str(item.get("status") or "")
            if status == "applying" and task_id not in attempted:
                if _claim_is_live(
                    item,
                    authority_generation=authority_generation,
                    now=now,
                ):
                    continue
                item["status"] = "unknown_effect"
                item["last_error"] = (
                    "cleanup process stopped while an external effect was applying"
                )
                item["updated_at"] = now.isoformat()
                changed = True
        for item in items:
            task_id = str(item.get("task_id") or "")
            if (
                not task_id
                or task_id in attempted
                or str(item.get("status") or "") not in _CLAIMABLE
            ):
                continue
            try:
                uuid.UUID(task_id)
            except ValueError:
                item["status"] = "unknown_effect"
                item["last_error"] = "cleanup Task id is malformed"
                item["updated_at"] = now.isoformat()
                changed = True
                continue
            item["status"] = "applying"
            item["claim_id"] = str(uuid.uuid4())
            item["authority_generation"] = authority_generation
            item["attempt_count"] = int(item.get("attempt_count") or 0) + 1
            item["claimed_at"] = now.isoformat()
            item["updated_at"] = now.isoformat()
            _replace_cleanup_items(resident, items, now=now)
            session.add(resident)
            return deepcopy(item)
        if changed:
            _replace_cleanup_items(resident, items, now=now)
            session.add(resident)
        return None


async def _prepare_database_cleanup(
    factory: async_sessionmaker[AsyncSession],
    project_id: uuid.UUID,
    task_id: uuid.UUID,
) -> tuple[bool, bool]:
    """Reconcile database-owned resources before external cleanup."""

    async with factory() as session, session.begin():
        task = (
            await session.execute(
                select(Task).where(Task.id == task_id).with_for_update()
            )
        ).scalar_one_or_none()
        if task is None or task.project_id != project_id:
            return False, False
        if task.status != TaskStatus.CANCELLED:
            raise RuntimeError("cleanup Task is no longer cancelled")

        from app.task_completion import (
            _expire_migration_reservation_if_terminal,
            _release_pool_member_if_terminal,
        )

        await _expire_migration_reservation_if_terminal(
            task, TaskStatus.CANCELLED, session
        )
        await _release_pool_member_if_terminal(task, TaskStatus.CANCELLED, session)
        registrations = (
            (
                await session.execute(
                    select(TaskWakeRegistration)
                    .where(
                        TaskWakeRegistration.task_id == task_id,
                        TaskWakeRegistration.delivery_state.in_(
                            list(_ACTIVE_WAKE_STATES)
                        ),
                    )
                    .with_for_update()
                )
            )
            .scalars()
            .all()
        )
        now = _utcnow()
        for registration in registrations:
            registration.delivery_state = WakeDeliveryState.FAILED
            registration.result_state = "task_cancelled"
            registration.result_detail = "Strategic Resident ownership rolled back"
            registration.updated_at = now
            session.add(registration)
        return True, bool((task.context or {}).get("_slot_released"))


async def _run_external_cleanup(
    *,
    task_id: uuid.UUID,
    project_id: uuid.UUID,
    vm_instance_id: str | None,
    account_id: str | None,
    account_already_released: bool,
) -> tuple[bool, bool]:
    """Return ``(vm_clean, account_released)``."""

    from app.max_plan_pool import release_max_plan_account
    from app.vm_lifecycle import _ensure_vm_deleted

    account_released = account_already_released
    if account_id and not account_already_released:
        await release_max_plan_account(account_id, project_id=str(project_id))
        account_released = True
    vm_clean = await _ensure_vm_deleted(
        vm_instance_id,
        reason=f"resident_rollback:{task_id}",
    )
    return bool(vm_clean), account_released


async def _finish_cleanup(
    factory: async_sessionmaker[AsyncSession],
    project_id: uuid.UUID,
    *,
    task_id: uuid.UUID,
    claim_id: str,
    authority_generation: int,
    status: str,
    error: str | None,
    account_released: bool,
) -> bool:
    now = _utcnow()
    async with factory() as session, session.begin():
        resident = (
            await session.execute(
                select(StrategicResident)
                .where(StrategicResident.project_id == project_id)
                .with_for_update()
            )
        ).scalar_one_or_none()
        if (
            resident is None
            or resident.engine_owner != "legacy"
            or int(resident.authority_generation) != authority_generation
        ):
            return False
        items = _cleanup_items(resident)
        matched = next(
            (
                item
                for item in items
                if str(item.get("task_id") or "") == str(task_id)
                and str(item.get("status") or "") == "applying"
                and str(item.get("claim_id") or "") == claim_id
                and int(item.get("authority_generation") or -1)
                == authority_generation
            ),
            None,
        )
        if matched is None:
            return False
        task = (
            await session.execute(
                select(Task).where(Task.id == task_id).with_for_update()
            )
        ).scalar_one_or_none()
        if task is not None and task.project_id == project_id and account_released:
            context = dict(task.context or {})
            context["_slot_released"] = True
            task.context = context
            session.add(task)
        matched["status"] = status
        matched["updated_at"] = now.isoformat()
        if error:
            matched["last_error"] = error[:1000]
        else:
            matched.pop("last_error", None)
            matched["completed_at"] = now.isoformat()
        _replace_cleanup_items(resident, items, now=now)
        session.add(resident)
        return True


async def run_resident_transition_cleanup(
    factory: async_sessionmaker[AsyncSession],
    project_id: uuid.UUID,
) -> None:
    """Attempt every pending cleanup once, leaving durable retry evidence."""

    attempted: set[str] = set()
    while True:
        item = await _claim_cleanup(factory, project_id, attempted=attempted)
        if item is None:
            return
        raw_task_id = str(item.get("task_id") or "")
        attempted.add(raw_task_id)
        try:
            task_id = uuid.UUID(raw_task_id)
        except ValueError:
            continue
        claim_id = str(item["claim_id"])
        authority_generation = int(item["authority_generation"])
        account_released = False
        try:
            task_exists, account_released = await _prepare_database_cleanup(
                factory, project_id, task_id
            )
            if not task_exists:
                raise RuntimeError("cleanup Task is missing or cross-project")
            vm_clean, account_released = await _run_external_cleanup(
                task_id=task_id,
                project_id=project_id,
                vm_instance_id=(
                    str(item["vm_instance_id"])
                    if item.get("vm_instance_id")
                    else None
                ),
                account_id=(
                    str(item["account_id"]) if item.get("account_id") else None
                ),
                account_already_released=account_released,
            )
            if not vm_clean:
                raise RuntimeError("VM cleanup did not complete")
        except Exception as exc:  # noqa: BLE001 - durable retry boundary
            status = (
                "unknown_effect"
                if item.get("account_id") and not account_released
                else "failed"
            )
            await _finish_cleanup(
                factory,
                project_id,
                task_id=task_id,
                claim_id=claim_id,
                authority_generation=authority_generation,
                status=status,
                error=f"{type(exc).__name__}: {exc}",
                account_released=account_released,
            )
            continue
        await _finish_cleanup(
            factory,
            project_id,
            task_id=task_id,
            claim_id=claim_id,
            authority_generation=authority_generation,
            status="complete",
            error=None,
            account_released=account_released,
        )


async def sweep_resident_transition_cleanup(
    *,
    session_factory: async_sessionmaker[AsyncSession],
    batch_size: int = 32,
) -> int:
    """Retry bounded rollback cleanup from the recurring reaper."""

    bounded_batch = max(1, min(int(batch_size), 128))
    async with session_factory() as session:
        project_ids = list(
            (
                await session.execute(
                    select(StrategicResident.project_id)
                    .where(
                        StrategicResident.engine_owner == "legacy",
                        StrategicResident.health_status.in_(
                            ["recovering", "blocked"]
                        ),
                    )
                    .order_by(StrategicResident.updated_at.asc())
                    .limit(bounded_batch)
                )
            )
            .scalars()
            .all()
        )
    for project_id in project_ids:
        await run_resident_transition_cleanup(session_factory, project_id)
    return len(project_ids)


__all__ = [
    "run_resident_transition_cleanup",
    "sweep_resident_transition_cleanup",
]

"""Scalar database floors for Resident Max-plan slot reconciliation."""

from __future__ import annotations

import uuid

from sqlalchemy import and_, case, func, or_
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlmodel import select

from app.models import Task, TaskStatus


async def resident_account_active_counts(
    factory: async_sessionmaker[AsyncSession],
    *,
    account_id: str,
    project_id: uuid.UUID,
) -> tuple[int, int]:
    """Count active account work with a column-first legacy JSON fallback."""

    context_account = Task.context["llm_account_id"].as_string()
    legacy_context_account = Task.context["max_plan_account_id"].as_string()
    account_matches = or_(
        Task.llm_account_id == account_id,
        and_(
            Task.llm_account_id.is_(None),
            or_(
                context_account == account_id,
                and_(
                    or_(context_account.is_(None), context_account == ""),
                    legacy_context_account == account_id,
                ),
            ),
        ),
    )
    async with factory() as session:
        totals = (
            await session.execute(
                select(
                    func.count(Task.id),
                    func.coalesce(
                        func.sum(case((Task.project_id == project_id, 1), else_=0)),
                        0,
                    ),
                ).where(
                    account_matches,
                    Task.status.in_(
                        [TaskStatus.PROVISIONING, TaskStatus.IN_PROGRESS]
                    ),
                )
            )
        ).one()
    return int(totals[0]), int(totals[1])


__all__ = ["resident_account_active_counts"]

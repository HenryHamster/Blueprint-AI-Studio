"""Resolve duplicate Blueprint projects that address one GitHub repository."""

from __future__ import annotations

from sqlalchemy import func
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from app.models import Project


async def repository_project_ids(
    session: AsyncSession,
    project_id: object,
    *,
    owner: str | None = None,
    repo: str | None = None,
) -> list[object]:
    """Return all project ids sharing the resolved owner/repository identity."""
    resolved_owner = str(owner or "").strip()
    resolved_repo = str(repo or "").strip()
    if not resolved_owner or not resolved_repo:
        row = (
            await session.execute(
                select(Project.github_owner, Project.github_repo).where(
                    Project.id == project_id
                )
            )
        ).first()
        if row:
            resolved_owner = str(row[0] or "").strip()
            resolved_repo = str(row[1] or "").strip()
    if not resolved_owner or not resolved_repo:
        return [project_id]
    rows = (
        await session.execute(
            select(Project.id).where(
                func.lower(Project.github_owner) == resolved_owner.lower(),
                func.lower(Project.github_repo) == resolved_repo.lower(),
            )
        )
    ).scalars().all()
    return list(rows) or [project_id]

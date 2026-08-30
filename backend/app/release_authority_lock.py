"""Canonical Project -> ReleaseVersion authority-lock helpers."""
from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Project, ReleaseVersion

_MODULE_SIZE_GOVERNANCE_CAP: int = 160


async def lock_release_project(
    session: AsyncSession,
    project_id: Any,
    *,
    skip_locked: bool = False,
) -> Project | None:
    return (
        (
            await session.execute(
                select(Project)
                .where(Project.id == project_id)
                # Project primary keys are immutable. FOR NO KEY UPDATE keeps
                # writer serialization without blocking child FK KEY SHARE.
                .with_for_update(key_share=True, skip_locked=skip_locked)
                .execution_options(populate_existing=True)
            )
        )
        .scalars()
        .first()
    )


async def lock_release_project_key_share(
    session: AsyncSession,
    project_id: Any,
) -> Project | None:
    """Take the weakest parent anchor that still orders against Project FU."""
    return (
        (
            await session.execute(
                select(Project)
                .where(Project.id == project_id)
                .with_for_update(read=True, key_share=True)
                .execution_options(populate_existing=True)
            )
        )
        .scalars()
        .first()
    )


async def lock_project_then_version(
    session: AsyncSession,
    version_id: Any,
    *,
    skip_locked: bool = False,
) -> ReleaseVersion | None:
    """Probe identity, then lock Project -> Version or skip either contention."""
    project = await lock_release_project_for_version(
        session, version_id, skip_locked=skip_locked
    )
    if project is None:
        return None
    return (
        await session.execute(
            select(ReleaseVersion)
            .where(ReleaseVersion.id == version_id)
            .with_for_update(skip_locked=skip_locked)
            .execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()


async def lock_release_project_for_version(
    session: AsyncSession,
    version_id: Any,
    *,
    skip_locked: bool = False,
) -> Project | None:
    project_id = (
        (
            await session.execute(
                select(ReleaseVersion.project_id).where(
                    ReleaseVersion.id == version_id
                )
            )
        )
        .scalars()
        .first()
    )
    return (
        None
        if project_id is None
        else await lock_release_project(
            session, project_id, skip_locked=skip_locked
        )
    )


async def lock_project_then_version_key_share(
    session: AsyncSession,
    version_id: Any,
) -> ReleaseVersion | None:
    """Order on Project KEY SHARE, then lock Version for a bounded writer."""
    project_id = (
        await session.execute(
            select(ReleaseVersion.project_id).where(ReleaseVersion.id == version_id)
        )
    ).scalar_one_or_none()
    if (
        project_id is None
        or await lock_release_project_key_share(session, project_id) is None
    ):
        return None
    return (
        await session.execute(
            select(ReleaseVersion)
            .where(ReleaseVersion.id == version_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()


async def relock_matching_open_release(
    session: AsyncSession,
    version_id: Any,
    *,
    qualification: dict[str, Any],
    project_identity: tuple[Any, ...],
) -> tuple[Project, ReleaseVersion] | None:
    """Rejoin after external I/O only if exact release authority is unchanged."""
    project = await lock_release_project_for_version(session, version_id)
    if project is None:
        return None
    version = (
        await session.execute(
            select(ReleaseVersion)
            .where(ReleaseVersion.id == version_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()
    current_identity = (
        project.github_owner,
        project.github_repo,
        project.integration_branch,
        project.promotion_target,
    )
    if (
        version is None
        or version.status != "open"
        or dict(version.last_qualification or {}) != qualification
        or current_identity != project_identity
    ):
        return None
    return project, version

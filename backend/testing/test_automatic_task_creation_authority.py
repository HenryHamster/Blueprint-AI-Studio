"""Universal Project.pause admission fence for automatic Task creation."""

from __future__ import annotations

import asyncio
import os

import pytest
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import sessionmaker
from sqlmodel import select

from app.models import Project, Task, TaskStatus
from app.release_task_admission import require_release_task_admission
from app.task_locking import (
    AUTOMATIC_TASK_CREATION_ALLOWED,
    AUTOMATIC_TASK_CREATION_AUTONOMY_DISABLED,
    AUTOMATIC_TASK_CREATION_PROJECT_ARCHIVED,
    AUTOMATIC_TASK_CREATION_PROJECT_PAUSED,
    AUTOMATIC_TASK_CREATION_PROJECT_UNAVAILABLE,
    acquire_automatic_task_creation_authority,
)


USE_POSTGRES = os.getenv("USE_TEST_POSTGRES", "").lower() == "true" and os.getenv(
    "DATABASE_URL", ""
).startswith("postgresql")


@pytest.mark.asyncio
async def test_release_admission_retains_a_fully_loaded_project(db_session):
    """A cold release admission must not poison downstream Project reads."""

    project = Project(
        name="release-admission-full-project",
        user_id="test-user",
        promotion_target="stable",
        build_config={"release_admission_probe": True},
    )
    db_session.add(project)
    await db_session.commit()
    project_id = project.id

    Session = sessionmaker(
        bind=db_session.bind,
        class_=AsyncSession,
        expire_on_commit=False,
    )
    async with Session() as session:
        await require_release_task_admission(session, project_id)
        loaded = await session.get(Project, project_id)
        assert loaded is not None
        assert loaded.promotion_target == "stable"
        assert loaded.build_config == {"release_admission_probe": True}
        await session.rollback()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("paused", "archived", "autonomous_enabled", "require_autonomy", "expected"),
    [
        (False, False, True, False, AUTOMATIC_TASK_CREATION_ALLOWED),
        (True, False, True, False, AUTOMATIC_TASK_CREATION_PROJECT_PAUSED),
        (False, True, True, False, AUTOMATIC_TASK_CREATION_PROJECT_ARCHIVED),
        (False, False, False, False, AUTOMATIC_TASK_CREATION_ALLOWED),
        (False, False, False, True, AUTOMATIC_TASK_CREATION_AUTONOMY_DISABLED),
    ],
)
async def test_automatic_task_creation_authority_is_project_state_authoritative(
    db_session,
    paused,
    archived,
    autonomous_enabled,
    require_autonomy,
    expected,
):
    project = Project(
        name=f"automatic-admission-{paused}-{archived}",
        user_id="test-user",
        paused=paused,
        archived=archived,
        autonomous_enabled=autonomous_enabled,
    )
    db_session.add(project)
    await db_session.commit()

    decision = await acquire_automatic_task_creation_authority(
        db_session,
        project.id,
        require_autonomous_enabled=require_autonomy,
    )

    assert decision == expected
    await db_session.rollback()


@pytest.mark.asyncio
async def test_cold_authority_projection_does_not_poison_full_read_after_rollback(
    db_session,
):
    project = Project(
        name="automatic-admission-cold-projection",
        user_id="test-user",
        build_config={"integration_branch": "automain", "marker": "full-row"},
    )
    db_session.add(project)
    await db_session.commit()
    project_id = project.id
    db_session.expunge(project)

    decision = await acquire_automatic_task_creation_authority(
        db_session, project_id
    )
    assert decision == AUTOMATIC_TASK_CREATION_ALLOWED
    await db_session.rollback()

    fully_loaded = await db_session.get(Project, project_id)
    assert fully_loaded is not None
    assert fully_loaded.build_config["marker"] == "full-row"


@pytest.mark.skipif(not USE_POSTGRES, reason="row-level locking requires PostgreSQL")
@pytest.mark.asyncio
async def test_pause_lock_wins_spawn_race_without_latent_task(db_session):
    """An in-flight pause transaction makes automatic admission fail closed.

    This is the production race dispatch filtering cannot cover: one session
    owns the Project pause write while another tries to author a QUEUED child.
    SKIP LOCKED must reject the child transaction; after pause commits there is
    no latent row waiting to burst on resume.
    """

    project = Project(name="automatic-admission-pause-race", user_id="test-user")
    db_session.add(project)
    await db_session.commit()
    project_id = project.id

    Session = sessionmaker(
        bind=db_session.bind,
        class_=AsyncSession,
        expire_on_commit=False,
    )
    pause_locked = asyncio.Event()
    spawn_checked = asyncio.Event()

    async def _pause_flow() -> None:
        async with Session() as session:
            locked = (
                await session.execute(
                    select(Project)
                    .where(Project.id == project_id)
                    .with_for_update(key_share=True)
                )
            ).scalar_one()
            locked.paused = True
            session.add(locked)
            await session.flush()
            pause_locked.set()
            await asyncio.wait_for(spawn_checked.wait(), timeout=5)
            await session.commit()

    async def _spawn_flow() -> str:
        await asyncio.wait_for(pause_locked.wait(), timeout=5)
        async with Session() as session:
            decision = await acquire_automatic_task_creation_authority(
                session, project_id, skip_locked=True
            )
            if decision == AUTOMATIC_TASK_CREATION_ALLOWED:
                session.add(
                    Task(
                        project_id=project_id,
                        title="must never become latent",
                        payloads=[{"type": "create"}],
                        status=TaskStatus.QUEUED,
                        created_by="ORCHESTRATOR",
                    )
                )
                await session.commit()
            else:
                await session.rollback()
            spawn_checked.set()
            return decision

    _, decision = await asyncio.wait_for(
        asyncio.gather(_pause_flow(), _spawn_flow()), timeout=10
    )

    assert decision == AUTOMATIC_TASK_CREATION_PROJECT_UNAVAILABLE
    db_session.expire_all()
    paused = await db_session.get(Project, project_id)
    assert paused is not None and paused.paused is True
    latent = (
        await db_session.execute(
            select(Task).where(
                Task.project_id == project_id,
                Task.title == "must never become latent",
            )
        )
    ).scalar_one_or_none()
    assert latent is None


@pytest.mark.skipif(not USE_POSTGRES, reason="row-level locking requires PostgreSQL")
@pytest.mark.asyncio
async def test_autonomy_disable_lock_wins_trial_admission_race_without_latent_task(
    db_session,
):
    """A stale autonomy snapshot cannot publish a trial behind a disable write."""

    project = Project(name="automatic-admission-autonomy-race", user_id="test-user")
    db_session.add(project)
    await db_session.commit()
    project_id = project.id

    Session = sessionmaker(
        bind=db_session.bind,
        class_=AsyncSession,
        expire_on_commit=False,
    )
    autonomy_locked = asyncio.Event()
    spawn_checked = asyncio.Event()

    async def _disable_flow() -> None:
        async with Session() as session:
            locked = (
                await session.execute(
                    select(Project)
                    .where(Project.id == project_id)
                    .with_for_update(key_share=True)
                )
            ).scalar_one()
            locked.autonomous_enabled = False
            session.add(locked)
            await session.flush()
            autonomy_locked.set()
            await asyncio.wait_for(spawn_checked.wait(), timeout=5)
            await session.commit()

    async def _spawn_flow() -> str:
        await asyncio.wait_for(autonomy_locked.wait(), timeout=5)
        async with Session() as session:
            decision = await acquire_automatic_task_creation_authority(
                session,
                project_id,
                skip_locked=True,
                require_autonomous_enabled=True,
            )
            if decision == AUTOMATIC_TASK_CREATION_ALLOWED:
                session.add(
                    Task(
                        project_id=project_id,
                        title="must never become a latent criterion trial",
                        payloads=[{"type": "criterion_named_test_trial"}],
                        status=TaskStatus.QUEUED,
                        created_by="SYSTEM_AUTO_REVIEW",
                    )
                )
                await session.commit()
            else:
                await session.rollback()
            spawn_checked.set()
            return decision

    _, decision = await asyncio.wait_for(
        asyncio.gather(_disable_flow(), _spawn_flow()), timeout=10
    )

    assert decision == AUTOMATIC_TASK_CREATION_PROJECT_UNAVAILABLE
    db_session.expire_all()
    disabled = await db_session.get(Project, project_id)
    assert disabled is not None and disabled.autonomous_enabled is False
    latent = (
        await db_session.execute(
            select(Task).where(
                Task.project_id == project_id,
                Task.title == "must never become a latent criterion trial",
            )
        )
    ).scalar_one_or_none()
    assert latent is None

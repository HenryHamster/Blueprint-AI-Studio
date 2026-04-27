import asyncio
import os

import pytest
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import sessionmaker
from sqlmodel import select

from app.models import Project, Task, TaskStatus
from app.task_locking import task_lock


USE_POSTGRES = (
    os.getenv("USE_TEST_POSTGRES", "").lower() == "true"
    and os.getenv("DATABASE_URL", "").startswith("postgresql")
)


@pytest.mark.skipif(not USE_POSTGRES, reason="row-lock concurrency requires PostgreSQL")
@pytest.mark.asyncio
async def test_concurrent_dispatch_mutations_keep_both_context_updates(db_session):
    project = Project(name="lock-test", user_id="user-lock-test")
    db_session.add(project)
    await db_session.commit()

    task = Task(
        project_id=project.id,
        title="queued dispatch lock test",
        status=TaskStatus.QUEUED,
        context={"dispatch_mutations": []},
    )
    db_session.add(task)
    await db_session.commit()

    Session = sessionmaker(
        bind=db_session.bind,
        class_=AsyncSession,
        expire_on_commit=False,
    )
    first_has_lock = asyncio.Event()

    async def dispatch_mutation(label: str, hold_first_lock: bool = False) -> None:
        async with Session() as session:
            async with task_lock(session, task.id, skip_locked=False, timeout=5) as locked_task:
                assert locked_task is not None
                if hold_first_lock:
                    first_has_lock.set()
                    await asyncio.sleep(0.2)
                ctx = dict(locked_task.context or {})
                mutations = list(ctx.get("dispatch_mutations") or [])
                mutations.append(label)
                ctx["dispatch_mutations"] = mutations
                locked_task.context = ctx
                session.add(locked_task)
                await session.commit()

    async def second_dispatch() -> None:
        await first_has_lock.wait()
        await dispatch_mutation("second")

    await asyncio.gather(
        dispatch_mutation("first", hold_first_lock=True),
        second_dispatch(),
    )

    result = await db_session.execute(select(Task).where(Task.id == task.id))
    refreshed = result.scalar_one()
    assert refreshed.context["dispatch_mutations"] == ["first", "second"]

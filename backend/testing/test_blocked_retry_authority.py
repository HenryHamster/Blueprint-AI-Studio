"""Cross-row retry authority regressions for BLOCKED task recovery."""

import asyncio
import os
from contextlib import asynccontextmanager
from datetime import timedelta
from unittest.mock import AsyncMock

import pytest
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import sessionmaker
from sqlmodel import select

from app.blocked_retry_authority import (
    _VIEWPORT_ADVISORY_POLICY_RECEIPT,
    blocked_precondition_state,
    pending_rollup_precondition_cleared,
    relock_blocked_retry_mutation_authority,
)
from app.blocked_task_unblock_sweeps import (
    _BLOCKED_ESCALATED_PARK,
    _reconcile_escalated_precondition_rows,
    sweep_unblock_blocked_tasks,
)
from app.models import Project, Task, TaskStatus
from app.task_blocked import strip_fresh_attempt_context
from app.time_utils import _utcnow


USE_POSTGRES = os.getenv("USE_TEST_POSTGRES", "").lower() == "true" and os.getenv(
    "DATABASE_URL", ""
).startswith("postgresql")


def _escalated_agent_precondition(
    project: Project, *, reason: str, now, recheck_after
) -> Task:
    return Task(
        project_id=project.id,
        title="escalated agent precondition",
        description="server-clearable blocker",
        status=TaskStatus.BLOCKED,
        created_by="ORCHESTRATOR",
        requires_approval=False,
        payloads=[{"type": "create"}],
        context={
            "_blocked_escalated": True,
            "blocker": {
                "source": "agent",
                "reason": reason,
                "escalated": True,
            },
        },
        blocker_kind="precondition",
        blocked_reason=reason,
        latest_status_message=reason,
        blocked_at=now - timedelta(days=2),
        recheck_after=recheck_after,
        created_at=now - timedelta(days=2),
    )


@pytest.mark.asyncio
async def test_escalated_viewport_blocker_requeues_once_with_policy_receipt(
    db_session, mocker
):
    """A retired viewport gate is admitted once, then its receipt is durable."""

    mocker.patch("app.broadcast.broadcast_to_project", new_callable=AsyncMock)
    now = _utcnow()
    project = Project(name="blocked-viewport-reconcile", user_id="test-user")
    db_session.add(project)
    await db_session.flush()
    reason = "BLOCKER: VIEWPORT_GATE_INFRA_UNAVAILABLE: Chromium cannot launch"
    blocked = _escalated_agent_precondition(
        project,
        reason=reason,
        now=now,
        recheck_after=now + _BLOCKED_ESCALATED_PARK,
    )
    db_session.add(blocked)
    await db_session.commit()

    assert await sweep_unblock_blocked_tasks(db_session, now) == 1
    await db_session.refresh(blocked)
    assert blocked.status == TaskStatus.QUEUED
    assert (
        blocked.context["_blocked_retry_precondition_receipt"]
        == _VIEWPORT_ADVISORY_POLICY_RECEIPT
    )

    assert await sweep_unblock_blocked_tasks(db_session, now) == 0
    await db_session.refresh(blocked)
    assert blocked.status == TaskStatus.QUEUED
    assert (
        blocked.context["_blocked_retry_precondition_receipt"]
        == _VIEWPORT_ADVISORY_POLICY_RECEIPT
    )


@pytest.mark.asyncio
async def test_escalated_agent_blocker_without_server_clear_stays_far_parked(
    db_session, mocker
):
    """An ordinary parked agent blocker remains operator-owned."""

    mocker.patch("app.broadcast.broadcast_to_project", new_callable=AsyncMock)
    no_alert = mocker.patch(
        "app.alert_dispatch._maybe_create_blocked_escalation_alert",
        new_callable=AsyncMock,
    )
    now = _utcnow()
    project = Project(name="blocked-no-server-clear", user_id="test-user")
    db_session.add(project)
    await db_session.flush()
    blocked = _escalated_agent_precondition(
        project,
        reason="BLOCKER: operator-only external dependency remains unavailable",
        now=now,
        recheck_after=now + _BLOCKED_ESCALATED_PARK,
    )
    db_session.add(blocked)
    await db_session.commit()

    assert await sweep_unblock_blocked_tasks(db_session, now) == 0
    await db_session.refresh(blocked)
    assert blocked.status == TaskStatus.BLOCKED
    # Literal pin: the operator park is a multi-year horizon, not a six-hour loop.
    assert blocked.recheck_after >= now + timedelta(days=3000)
    assert blocked.context["_blocked_escalated"] is True
    no_alert.assert_not_awaited()


@pytest.mark.asyncio
async def test_parked_reconciliation_is_idempotent(db_session, mocker):
    """Admitting a hard-parked row twice only changes its schedule once."""

    now = _utcnow()
    project = Project(name="blocked-reconcile-idempotence", user_id="test-user")
    db_session.add(project)
    await db_session.flush()
    blocked = _escalated_agent_precondition(
        project,
        reason="BLOCKER: server capability is now available",
        now=now,
        recheck_after=now + _BLOCKED_ESCALATED_PARK,
    )
    db_session.add(blocked)
    await db_session.commit()

    authority = mocker.patch(
        "app.blocked_task_unblock_sweeps._blocked_precondition_state",
        new_callable=AsyncMock,
        return_value="retry",
    )
    project_ids = select(Project.id).where(Project.id == project.id)
    admitted = await _reconcile_escalated_precondition_rows(
        db_session,
        now,
        object(),
        project_ids=project_ids,
        batch_size=4,
    )
    assert admitted == {blocked.id}
    await db_session.refresh(blocked)
    assert blocked.recheck_after == now

    admitted_again = await _reconcile_escalated_precondition_rows(
        db_session,
        now,
        object(),
        project_ids=project_ids,
        batch_size=4,
    )
    assert admitted_again == set()
    authority.assert_awaited_once()


@pytest.mark.asyncio
async def test_reconciled_server_blocker_rearms_at_six_hour_cadence(
    db_session, mocker
):
    """A parked authority check that turns waiting uses the existing cadence."""

    now = _utcnow()
    project = Project(name="blocked-reconcile-cadence", user_id="test-user")
    db_session.add(project)
    await db_session.flush()
    blocked = _escalated_agent_precondition(
        project,
        reason="BLOCKER: server capability is temporarily unavailable",
        now=now,
        recheck_after=now + _BLOCKED_ESCALATED_PARK,
    )
    db_session.add(blocked)
    await db_session.commit()

    authority = mocker.patch(
        "app.blocked_task_unblock_sweeps._blocked_precondition_state",
        new_callable=AsyncMock,
        side_effect=["retry", "waiting"],
    )
    assert await sweep_unblock_blocked_tasks(db_session, now) == 0
    await db_session.refresh(blocked)
    assert blocked.status == TaskStatus.BLOCKED
    assert blocked.context["_blocked_parked_reevals"] == 1
    # Literal pin: the reachable parked path is six hours, not the 3650-day park.
    assert now + timedelta(hours=5) < blocked.recheck_after < now + timedelta(hours=7)
    assert authority.await_count == 2


@pytest.mark.asyncio
async def test_reconcile_reaches_escalated_row_past_ordinary_parked_congestion(
    db_session, mocker
):
    """A hard-parked escalated row must be admitted even when a small batch_size
    is flooded with ordinary, near-future parked rows.

    Regression for the #17430 candidate query ordering it `recheck_after ASC`
    over the WHOLE BLOCKED population: escalated rows park at the ~10y sentinel,
    the maximum possible value of that column, so ASC ranked them dead last and
    a small batch never reached them once enough 6h-horizon parked-need rows
    existed. The fix restricts candidates to rows beyond the existing 365-day
    transient-park sentinel, so ordinary short-horizon parked rows never
    compete with the target population for the LIMIT.
    """

    mocker.patch("app.broadcast.broadcast_to_project", new_callable=AsyncMock)
    now = _utcnow()
    project = Project(name="blocked-reconcile-congestion", user_id="test-user")
    db_session.add(project)
    await db_session.flush()

    # More ordinary, near-future parked rows than batch_size — these previously
    # crowded out the escalated row under ASC ordering with no horizon filter.
    for i in range(3):
        db_session.add(
            Task(
                project_id=project.id,
                title=f"ordinary parked row {i}",
                description="steady-state parked-need row",
                status=TaskStatus.BLOCKED,
                created_by="ORCHESTRATOR",
                requires_approval=False,
                payloads=[{"type": "create"}],
                context={},
                blocker_kind="dependency",
                blocked_reason="waiting on a foundation",
                latest_status_message="waiting on a foundation",
                blocked_at=now - timedelta(hours=1),
                recheck_after=now + timedelta(hours=6),
                created_at=now - timedelta(hours=1),
            )
        )

    reason = "BLOCKER: VIEWPORT_GATE_INFRA_UNAVAILABLE: Chromium cannot launch"
    escalated = _escalated_agent_precondition(
        project,
        reason=reason,
        now=now,
        recheck_after=now + _BLOCKED_ESCALATED_PARK,
    )
    db_session.add(escalated)
    await db_session.commit()

    # Pin batch_size well below the ordinary-row count so the ordering defect
    # would have been decisive rather than incidentally masked by a large batch.
    assert await sweep_unblock_blocked_tasks(db_session, now, batch_size=2) == 1
    await db_session.refresh(escalated)
    assert escalated.status == TaskStatus.QUEUED

    # A second sweep is a no-op for the already-admitted row.
    assert await sweep_unblock_blocked_tasks(db_session, now, batch_size=2) == 0


def test_file_conflict_handoff_is_attempt_local():
    context = {
        "_blocked_retry_precondition_receipt": {
            "authority": "server_file_conflict_fifo_v1",
            "kind": "file_conflict_handoff",
            "files": ["a.py"],
        }
    }
    assert "_blocked_retry_precondition_receipt" not in strip_fresh_attempt_context(
        context
    )


def _file_waiter(project: Project, *, created_at, recheck_after) -> Task:
    return Task(
        project_id=project.id,
        title="same-file waiter",
        description="write a.py",
        status=TaskStatus.BLOCKED,
        created_by="ORCHESTRATOR",
        requires_approval=False,
        payloads=[{"type": "fix"}],
        context={
            "related_files": ["a.py"],
            "_precondition_defer": {
                "key": "file conflict on a.py",
                "count": 3,
                "files": ["a.py"],
            },
        },
        blocker_kind="file_conflict",
        blocked_reason="Waiting: file conflict on a.py",
        blocked_at=created_at,
        recheck_after=recheck_after,
        created_at=created_at,
    )


@pytest.mark.asyncio
async def test_same_file_waiters_requeue_in_fifo_order(db_session, mocker):
    """A queued senior reservation prevents the younger waiter from racing it."""

    mocker.patch("app.broadcast.broadcast_to_project")
    now = _utcnow()
    project = Project(name="blocked-retry-fifo", user_id="test-user")
    db_session.add(project)
    await db_session.flush()
    older = _file_waiter(
        project,
        created_at=now - timedelta(minutes=3),
        recheck_after=now - timedelta(seconds=2),
    )
    younger = _file_waiter(
        project,
        created_at=now - timedelta(minutes=2),
        recheck_after=now - timedelta(seconds=1),
    )
    db_session.add_all([older, younger])
    await db_session.commit()

    assert await sweep_unblock_blocked_tasks(db_session, now) == 1
    await db_session.refresh(older)
    await db_session.refresh(younger)
    assert older.status == TaskStatus.QUEUED
    assert "_precondition_defer" not in older.context
    assert "_blocked_first_at" not in older.context
    assert older.context["_blocked_retry_precondition_receipt"]["files"] == ["a.py"]
    assert younger.status == TaskStatus.BLOCKED
    assert len(older.attempt_history or []) == 1
    assert not (younger.attempt_history or [])

    older.status = TaskStatus.COMPLETED
    older.context = {}
    younger.recheck_after = now - timedelta(seconds=1)
    db_session.add_all([older, younger])
    await db_session.commit()

    assert await sweep_unblock_blocked_tasks(db_session, now) == 1
    await db_session.refresh(younger)
    assert younger.status == TaskStatus.QUEUED
    assert len(younger.attempt_history or []) == 1


@pytest.mark.asyncio
async def test_pause_after_claim_wins_before_retry_commit(db_session, mocker):
    """A stop committed during precondition I/O cannot resurrect the task."""

    mocker.patch("app.broadcast.broadcast_to_project")
    now = _utcnow()
    project = Project(name="blocked-retry-pause-race", user_id="test-user")
    db_session.add(project)
    await db_session.flush()
    blocked = _file_waiter(
        project,
        created_at=now - timedelta(minutes=3),
        recheck_after=now - timedelta(seconds=1),
    )
    db_session.add(blocked)
    await db_session.commit()

    async def _pause_during_precondition(task, session, *_args, **_kwargs):
        claimed_project = await session.get(Project, task.project_id)
        claimed_project.paused = True
        session.add(claimed_project)
        await session.flush()
        return "clear"

    mocker.patch(
        "app.blocked_task_unblock_sweeps._blocked_precondition_state",
        side_effect=_pause_during_precondition,
    )

    assert await sweep_unblock_blocked_tasks(db_session, now) == 0
    await db_session.refresh(blocked)
    await db_session.refresh(project)
    assert project.paused is True
    assert blocked.status == TaskStatus.BLOCKED
    assert not (blocked.attempt_history or [])


@pytest.mark.asyncio
async def test_retry_finalization_releases_task_before_canonical_pair(
    db_session, mocker
):
    """The final authority lock starts from no Task-owning transaction."""

    now = _utcnow()
    project = Project(name="blocked-retry-lock-order", user_id="test-user")
    db_session.add(project)
    await db_session.flush()
    blocked = _file_waiter(
        project,
        created_at=now - timedelta(minutes=2),
        recheck_after=now - timedelta(seconds=1),
    )
    db_session.add(blocked)
    await db_session.commit()
    claimed = (
        await db_session.execute(
            select(Task).where(Task.id == blocked.id).with_for_update()
        )
    ).scalar_one()
    assert db_session.in_transaction()

    @asynccontextmanager
    async def _canonical_pair(session, project_id, task_id, **_kwargs):
        assert not session.in_transaction(), "advisory Task lock must be released"
        locked_project = await session.get(Project, project_id)
        locked_task = await session.get(Task, task_id)
        yield locked_project, locked_task

    mocker.patch("app.task_locking.lock_project_then_task", _canonical_pair)
    relocked = await relock_blocked_retry_mutation_authority(db_session, claimed)

    assert relocked is not None
    assert relocked.id == blocked.id


@pytest.mark.asyncio
async def test_foreign_project_rollup_cannot_hold_local_waiter(db_session):
    """Pending-rollup authority is project-scoped even for identical titles."""

    local = Project(name="local-rollup", user_id="test-user")
    foreign = Project(name="foreign-rollup", user_id="test-user")
    db_session.add_all([local, foreign])
    await db_session.flush()
    title = "same bounded rollup title"
    waiter = Task(
        project_id=local.id,
        title="local waiter",
        status=TaskStatus.BLOCKED,
        blocker_kind="pending_rollup",
        blocked_reason=f"overlaps pending rollup '{title}'",
        context={"_precondition_defer": {"key": f"pending rollup '{title}'"}},
    )
    foreign_parent = Task(
        project_id=foreign.id,
        title="foreign parent",
        status=TaskStatus.EVALUATING,
    )
    db_session.add_all([waiter, foreign_parent])
    await db_session.flush()
    foreign_child = Task(
        project_id=foreign.id,
        parent_task_id=foreign_parent.id,
        title=title,
        status=TaskStatus.COMPLETED,
        payloads=[{"type": "refactor"}],
    )
    db_session.add(foreign_child)
    await db_session.commit()

    assert await pending_rollup_precondition_cleared(waiter, db_session) is True

    local_parent = Task(
        project_id=local.id,
        title="local parent",
        status=TaskStatus.EVALUATING,
    )
    db_session.add(local_parent)
    await db_session.flush()
    local_child = Task(
        project_id=local.id,
        parent_task_id=local_parent.id,
        title=title,
        status=TaskStatus.COMPLETED,
        payloads=[{"type": "refactor"}],
    )
    db_session.add(local_child)
    await db_session.commit()

    assert await pending_rollup_precondition_cleared(waiter, db_session) is False


@pytest.mark.asyncio
async def test_foreign_project_child_cannot_hold_parent_source_reconciliation(
    db_session,
):
    """Only same-project children are authority over a parent-source blocker."""

    local = Project(name="local-parent-source", user_id="test-user")
    foreign = Project(name="foreign-parent-source", user_id="test-user")
    db_session.add_all([local, foreign])
    await db_session.flush()
    parent = Task(
        project_id=local.id,
        title="local blocked parent",
        status=TaskStatus.BLOCKED,
        context={"blocker": {"source": "parent"}},
    )
    db_session.add(parent)
    await db_session.flush()
    foreign_child = Task(
        project_id=foreign.id,
        parent_task_id=parent.id,
        title="inconsistent foreign child",
        status=TaskStatus.BLOCKED,
    )
    db_session.add(foreign_child)
    await db_session.commit()

    assert (
        await blocked_precondition_state(parent, db_session, verify_pr_merged=None)
        == "re_evaluate"
    )

    local_child = Task(
        project_id=local.id,
        parent_task_id=parent.id,
        title="authoritative local child",
        status=TaskStatus.BLOCKED,
    )
    db_session.add(local_child)
    await db_session.commit()

    assert (
        await blocked_precondition_state(parent, db_session, verify_pr_merged=None)
        == "waiting"
    )


@pytest.mark.skipif(not USE_POSTGRES, reason="row-level locking requires PostgreSQL")
@pytest.mark.asyncio
async def test_retry_race_with_canonical_project_task_owner_cannot_deadlock(
    db_session, mocker
):
    """A canonical owner may wait on the advisory Task without an inverse wait."""

    now = _utcnow()
    project = Project(name="blocked-retry-pair-race", user_id="test-user")
    db_session.add(project)
    await db_session.flush()
    blocked = _file_waiter(
        project,
        created_at=now - timedelta(minutes=2),
        recheck_after=now - timedelta(seconds=1),
    )
    db_session.add(blocked)
    await db_session.commit()

    Session = sessionmaker(
        bind=db_session.bind,
        class_=AsyncSession,
        expire_on_commit=False,
    )
    precondition_entered = asyncio.Event()
    canonical_project_locked = asyncio.Event()

    async def _pause_at_precondition(*_args, **_kwargs):
        precondition_entered.set()
        await asyncio.wait_for(canonical_project_locked.wait(), timeout=5)
        return "clear"

    mocker.patch(
        "app.blocked_task_unblock_sweeps._blocked_precondition_state",
        side_effect=_pause_at_precondition,
    )

    async def _sweep_flow() -> int:
        async with Session() as session:
            return await sweep_unblock_blocked_tasks(session, now)

    async def _canonical_flow() -> None:
        await asyncio.wait_for(precondition_entered.wait(), timeout=5)
        async with Session() as session:
            locked_project = (
                await session.execute(
                    select(Project)
                    .where(Project.id == project.id)
                    .with_for_update(key_share=True)
                )
            ).scalar_one()
            assert locked_project.id == project.id
            canonical_project_locked.set()
            locked_task = (
                await session.execute(
                    select(Task).where(Task.id == blocked.id).with_for_update()
                )
            ).scalar_one()
            locked_task.latest_status_message = "canonical owner won"
            session.add(locked_task)
            await session.commit()

    sweep_result, _ = await asyncio.wait_for(
        asyncio.gather(_sweep_flow(), _canonical_flow()), timeout=10
    )
    assert sweep_result == 0
    await db_session.refresh(blocked)
    assert blocked.status == TaskStatus.BLOCKED
    assert blocked.latest_status_message == "canonical owner won"

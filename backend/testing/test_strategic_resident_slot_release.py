"""Focused crash-boundary tests for Resident account-slot release."""

from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app import max_plan_pool
from app.models import Project, Task, TaskStatus
from app.strategic_resident_slot_release import (
    release_resident_turn_slot,
    stage_resident_turn_slot_release,
)
from app.strategic_resident_terminal import (
    commit_release_and_broadcast_failure,
    release_slot_after_reconciliation,
)


def _factory(session: AsyncSession) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(
        bind=session.bind,
        expire_on_commit=False,
        autoflush=False,
    )


def _account(account_id: str, project_id: uuid.UUID, *, active: int) -> dict:
    return {
        "id": account_id,
        "name": account_id,
        "active_tasks": active,
        "project_state": {str(project_id): {"active_tasks": active}},
    }


async def _project(session: AsyncSession, name: str) -> Project:
    project = Project(
        name=f"{name} {uuid.uuid4()}",
        user_id="operator",
        vision={},
        problem_model={},
        directions=[],
    )
    session.add(project)
    await session.commit()
    await session.refresh(project)
    return project


@pytest.mark.asyncio
async def test_cache_reconcile_failure_is_durably_claimed_without_decrement_replay(
    db_session,
    monkeypatch,
):
    project = await _project(db_session, "Resident slot replay")
    task = Task(
        project_id=project.id,
        status=TaskStatus.COMPLETED,
        llm_account_id="slot-replay-account",
        context={"llm_account_id": "slot-replay-account"},
    )
    peer = Task(
        project_id=project.id,
        status=TaskStatus.IN_PROGRESS,
        llm_account_id="slot-replay-account",
        context={"llm_account_id": "slot-replay-account"},
    )
    db_session.add(task)
    db_session.add(peer)
    await db_session.flush()
    stage_resident_turn_slot_release(task)
    await db_session.commit()
    # One additional count models a local reservation not visible in the DB yet.
    # A replay must not consume that unrelated reservation.
    account = _account("slot-replay-account", project.id, active=3)
    monkeypatch.setitem(
        max_plan_pool._account_by_id,
        "slot-replay-account",
        account,
    )

    import app.strategic_resident_slot_release as slot_release

    async def unavailable_counts(*args, **kwargs):
        raise RuntimeError("database unavailable after durable claim")

    monkeypatch.setattr(
        slot_release, "resident_account_active_counts", unavailable_counts
    )
    assert await release_resident_turn_slot(
        _factory(db_session),
        task_id=task.id,
        project_id=project.id,
    )
    assert account["active_tasks"] == 3
    assert account["project_state"][str(project.id)]["active_tasks"] == 3
    assert not await release_resident_turn_slot(
        _factory(db_session),
        task_id=task.id,
        project_id=project.id,
    )
    assert account["active_tasks"] == 3
    assert account["project_state"][str(project.id)]["active_tasks"] == 3
    async with _factory(db_session)() as verify:
        stored = await verify.get(Task, task.id)
    assert stored.context["_slot_released"] is True
    assert stored.context["_resident_slot_release"]["release_mode"] == (
        "durable_release_claimed"
    )


@pytest.mark.asyncio
async def test_release_mode_update_rejects_noncanonical_completed_marker(
    db_session,
):
    import app.strategic_resident_slot_release as slot_release

    project = await _project(db_session, "Resident release mode identity")
    task = Task(
        project_id=project.id,
        status=TaskStatus.COMPLETED,
        llm_account_id="canonical-account",
        context={
            "_slot_released": True,
            "_resident_slot_release": {
                "state": "completed",
                "account_id": "wrong-account",
                "project_id": str(project.id),
                "idempotency_key": f"strategic-resident-turn:{uuid.uuid4()}",
                "release_mode": "durable_release_claimed",
            },
        },
    )
    db_session.add(task)
    await db_session.commit()
    await slot_release._record_release_mode(
        _factory(db_session),
        task_id=task.id,
        project_id=project.id,
        release_mode="reconciled_from_database",
    )
    async with _factory(db_session)() as verify:
        stored = await verify.get(Task, task.id)
    assert stored.context["_resident_slot_release"]["release_mode"] == (
        "durable_release_claimed"
    )


@pytest.mark.asyncio
async def test_release_mode_update_rejects_nonterminal_task(db_session):
    import app.strategic_resident_slot_release as slot_release

    project = await _project(db_session, "Resident release mode terminal fence")
    task = Task(
        project_id=project.id,
        status=TaskStatus.QUEUED,
        llm_account_id="terminal-fence-account",
        context={"llm_account_id": "terminal-fence-account"},
    )
    db_session.add(task)
    await db_session.flush()
    stage_resident_turn_slot_release(task)
    marker = dict(task.context["_resident_slot_release"])
    marker.update({"state": "completed", "release_mode": "durable_release_claimed"})
    task.context = {
        **task.context,
        "_slot_released": True,
        "_resident_slot_release": marker,
    }
    await db_session.commit()

    await slot_release._record_release_mode(
        _factory(db_session),
        task_id=task.id,
        project_id=project.id,
        release_mode="reconciled_from_database",
    )
    async with _factory(db_session)() as verify:
        stored = await verify.get(Task, task.id)
    assert stored.context["_resident_slot_release"]["release_mode"] == (
        "durable_release_claimed"
    )


@pytest.mark.asyncio
async def test_release_restages_copied_or_malformed_marker_identity(
    db_session,
    monkeypatch,
):
    project = await _project(db_session, "Resident slot marker identity")
    task = Task(
        project_id=project.id,
        status=TaskStatus.COMPLETED,
        llm_account_id="slot-canonical-account",
        context={
            "llm_account_id": "slot-canonical-account",
            "_resident_slot_release": {
                "state": "pending",
                "account_id": "wrong-account",
                "project_id": str(uuid.uuid4()),
                "idempotency_key": "strategic-resident-turn:copied",
            },
            "_slot_released": True,
        },
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)
    account = _account("slot-canonical-account", project.id, active=1)
    monkeypatch.setitem(
        max_plan_pool._account_by_id,
        "slot-canonical-account",
        account,
    )

    assert await release_resident_turn_slot(
        _factory(db_session),
        task_id=task.id,
        project_id=project.id,
    )
    async with _factory(db_session)() as verify:
        stored = await verify.get(Task, task.id)
    marker = stored.context["_resident_slot_release"]
    assert marker["account_id"] == "slot-canonical-account"
    assert marker["project_id"] == str(project.id)
    assert marker["idempotency_key"] == f"strategic-resident-turn:{task.id}"


@pytest.mark.asyncio
async def test_release_does_not_trust_pending_marker_with_released_flag(
    db_session,
    monkeypatch,
):
    project = await _project(db_session, "Resident pending marker")
    task = Task(
        project_id=project.id,
        status=TaskStatus.COMPLETED,
        llm_account_id="slot-pending-account",
        context={"llm_account_id": "slot-pending-account"},
    )
    db_session.add(task)
    await db_session.flush()
    stage_resident_turn_slot_release(task)
    task.context = {**task.context, "_slot_released": True}
    await db_session.commit()
    account = _account("slot-pending-account", project.id, active=1)
    monkeypatch.setitem(max_plan_pool._account_by_id, "slot-pending-account", account)

    assert await release_resident_turn_slot(
        _factory(db_session), task_id=task.id, project_id=project.id
    )
    assert account["active_tasks"] == 0
    async with _factory(db_session)() as verify:
        stored = await verify.get(Task, task.id)
    assert stored.context["_resident_slot_release"]["state"] == "completed"


@pytest.mark.asyncio
async def test_complete_release_failure_does_not_mask_committed_outcome(monkeypatch):
    import app.strategic_resident_terminal as terminal

    async def release_fails(*args, **kwargs):
        raise RuntimeError("marker database unavailable")

    async def reconciled(session_factory, request):
        return "committed-outcome"

    monkeypatch.setattr(terminal, "release_resident_turn_slot", release_fails)
    wrapped = release_slot_after_reconciliation(reconciled)
    request = SimpleNamespace(turn_task_id=uuid.uuid4(), project_id=uuid.uuid4())
    assert await wrapped(object(), request) == "committed-outcome"


@pytest.mark.asyncio
async def test_failure_release_failure_still_broadcasts_terminal_truth(monkeypatch):
    import app.strategic_resident_terminal as terminal

    events = []

    class Session:
        committed = False

        async def commit(self):
            self.committed = True

    async def release_fails(*args, **kwargs):
        raise RuntimeError("marker database unavailable")

    async def broadcast(project_id, event_type, payload):
        events.append((project_id, event_type, payload))

    monkeypatch.setattr(terminal, "release_resident_turn_slot", release_fails)
    monkeypatch.setattr(terminal, "broadcast_to_project", broadcast)
    session = Session()
    task_id, project_id = uuid.uuid4(), uuid.uuid4()
    await commit_release_and_broadcast_failure(
        session,
        task_id=task_id,
        project_id=project_id,
        suppress_broadcast_error=False,
    )
    assert session.committed is True
    assert events == [
        (
            str(project_id),
            "task_updated",
            {"task_id": str(task_id), "status": TaskStatus.FAILED},
        )
    ]


@pytest.mark.asyncio
async def test_cross_instance_release_reconciles_total_and_project_from_db(
    db_session,
    monkeypatch,
):
    project = await _project(db_session, "Resident slot generation")
    other_project = await _project(db_session, "Resident slot other")
    terminal = Task(
        project_id=project.id,
        status=TaskStatus.FAILED,
        llm_account_id="slot-generation-account",
        context={"llm_account_id": "slot-generation-account"},
    )
    active_here = Task(
        project_id=project.id,
        status=TaskStatus.IN_PROGRESS,
        llm_account_id="slot-generation-account",
        context={"llm_account_id": "slot-generation-account"},
    )
    active_elsewhere = Task(
        project_id=other_project.id,
        status=TaskStatus.PROVISIONING,
        llm_account_id="slot-generation-account",
        context={"llm_account_id": "slot-generation-account"},
    )
    db_session.add(terminal)
    db_session.add(active_here)
    db_session.add(active_elsewhere)
    await db_session.flush()
    stage_resident_turn_slot_release(terminal)
    await db_session.commit()
    account = _account("slot-generation-account", project.id, active=2)
    monkeypatch.setitem(
        max_plan_pool._account_by_id,
        "slot-generation-account",
        account,
    )
    assert await release_resident_turn_slot(
        _factory(db_session),
        task_id=terminal.id,
        project_id=project.id,
    )
    assert account["active_tasks"] == 2
    assert account["project_state"][str(project.id)]["active_tasks"] == 1
    async with _factory(db_session)() as verify:
        stored = await verify.get(Task, terminal.id)
    assert stored.context["_slot_released"] is True
    assert stored.context["_resident_slot_release"]["release_mode"] == (
        "reconciled_from_database"
    )


@pytest.mark.asyncio
async def test_release_db_floor_counts_context_only_legacy_active_peer(
    db_session,
    monkeypatch,
):
    project = await _project(db_session, "Resident slot legacy floor")
    account_id = "slot-legacy-account"
    terminal = Task(
        project_id=project.id,
        status=TaskStatus.COMPLETED,
        llm_account_id=account_id,
        context={"llm_account_id": account_id},
    )
    legacy_peer = Task(
        project_id=project.id,
        status=TaskStatus.IN_PROGRESS,
        llm_account_id=None,
        context={"max_plan_account_id": account_id},
    )
    stale_context_peer = Task(
        project_id=project.id,
        status=TaskStatus.IN_PROGRESS,
        llm_account_id=None,
        context={
            "llm_account_id": "different-account",
            "max_plan_account_id": account_id,
        },
    )
    db_session.add(terminal)
    db_session.add(legacy_peer)
    db_session.add(stale_context_peer)
    await db_session.flush()
    stage_resident_turn_slot_release(terminal)
    await db_session.commit()
    account = _account(account_id, project.id, active=1)
    monkeypatch.setitem(max_plan_pool._account_by_id, account_id, account)

    assert await release_resident_turn_slot(
        _factory(db_session),
        task_id=terminal.id,
        project_id=project.id,
    )
    assert account["active_tasks"] == 1
    assert account["project_state"][str(project.id)]["active_tasks"] == 1


@pytest.mark.asyncio
async def test_release_preserves_one_uncommitted_local_reservation_on_replay(
    db_session,
    monkeypatch,
):
    project = await _project(db_session, "Resident slot local reservation")
    other_project = await _project(db_session, "Resident slot local other")
    terminal = Task(
        project_id=project.id,
        status=TaskStatus.FAILED,
        llm_account_id="slot-local-reservation",
        context={"llm_account_id": "slot-local-reservation"},
    )
    active_here = Task(
        project_id=project.id,
        status=TaskStatus.IN_PROGRESS,
        llm_account_id="slot-local-reservation",
        context={"llm_account_id": "slot-local-reservation"},
    )
    active_elsewhere = Task(
        project_id=other_project.id,
        status=TaskStatus.IN_PROGRESS,
        llm_account_id="slot-local-reservation",
        context={"llm_account_id": "slot-local-reservation"},
    )
    db_session.add(terminal)
    db_session.add(active_here)
    db_session.add(active_elsewhere)
    await db_session.flush()
    stage_resident_turn_slot_release(terminal)
    await db_session.commit()
    account = _account("slot-local-reservation", project.id, active=4)
    account["project_state"][str(project.id)]["active_tasks"] = 3
    monkeypatch.setitem(
        max_plan_pool._account_by_id,
        "slot-local-reservation",
        account,
    )

    assert await release_resident_turn_slot(
        _factory(db_session),
        task_id=terminal.id,
        project_id=project.id,
    )
    assert account["active_tasks"] == 3
    assert account["project_state"][str(project.id)]["active_tasks"] == 2
    assert not await release_resident_turn_slot(
        _factory(db_session),
        task_id=terminal.id,
        project_id=project.id,
    )
    assert account["active_tasks"] == 3
    assert account["project_state"][str(project.id)]["active_tasks"] == 2

"""
Tests that verify SR compute functions are called from the agent task-completion path.
"""

from unittest.mock import patch, AsyncMock

import pytest
from httpx import AsyncClient

from app.models import (
    Project,
    Task,
)


@pytest.mark.asyncio
async def test_compute_sr_false_attestation_trends_called_on_claim(
    client: AsyncClient, db_session, mocker
):
    """
    Verify that compute_sr_false_attestation_trends is called when claiming a
    create/fix/refactor task.
    """
    project = Project(name="Test Project", user_id="test-user")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = Task(
        project_id=project.id,
        payloads=[{"type": "create", "description": "test"}],
        vm_instance_id="test-vm",
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    from app.auth import create_agent_token

    token = create_agent_token(str(task.id), "test-vm")
    headers = {"x-agent-token": token}

    mock_fn = AsyncMock()
    with patch(
        "app.sr_analytics.compute_sr_false_attestation_trends",
        new=mock_fn,
    ):
        resp = await client.post("/api/agent/claim", headers=headers)
        assert resp.status_code == 200
        mock_fn.assert_awaited_once()
        call_kwargs = mock_fn.call_args.kwargs
        assert call_kwargs["project_id"] == project.id
        assert call_kwargs["session"] is not None
        assert isinstance(call_kwargs["all_tasks"], list)


@pytest.mark.asyncio
async def test_compute_sr_regression_risk_test_signal_trends_called_on_claim(
    client: AsyncClient, db_session, mocker
):
    """
    Verify that compute_sr_regression_risk_test_signal_trends is called when
    claiming a create/fix/refactor task.
    """
    project = Project(name="Test Project", user_id="test-user")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = Task(
        project_id=project.id,
        payloads=[{"type": "fix", "description": "test"}],
        vm_instance_id="test-vm",
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    from app.auth import create_agent_token

    token = create_agent_token(str(task.id), "test-vm")
    headers = {"x-agent-token": token}

    mock_fn = AsyncMock()
    with patch(
        "app.sr_analytics.compute_sr_regression_risk_test_signal_trends",
        new=mock_fn,
    ):
        resp = await client.post("/api/agent/claim", headers=headers)
        assert resp.status_code == 200
        mock_fn.assert_awaited_once()
        call_kwargs = mock_fn.call_args.kwargs
        assert call_kwargs["project_id"] == project.id
        assert call_kwargs["session"] is not None
        assert isinstance(call_kwargs["all_tasks"], list)


@pytest.mark.asyncio
async def test_compute_fresh_reviewer_trend_called_on_claim(
    client: AsyncClient, db_session, mocker
):
    """
    Verify that compute_fresh_reviewer_trend is called when claiming a
    create/fix/refactor task.
    """
    project = Project(name="Test Project", user_id="test-user")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = Task(
        project_id=project.id,
        payloads=[{"type": "refactor", "description": "test"}],
        vm_instance_id="test-vm",
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    from app.auth import create_agent_token

    token = create_agent_token(str(task.id), "test-vm")
    headers = {"x-agent-token": token}

    mock_fn = AsyncMock()
    with patch(
        "app.sr_analytics.compute_fresh_reviewer_trend",
        new=mock_fn,
    ):
        resp = await client.post("/api/agent/claim", headers=headers)
        assert resp.status_code == 200
        mock_fn.assert_awaited_once()
        call_kwargs = mock_fn.call_args.kwargs
        assert call_kwargs["project_id"] == project.id
        assert call_kwargs["session"] is not None
        assert isinstance(call_kwargs["all_tasks"], list)


@pytest.mark.asyncio
async def test_sr_compute_functions_not_called_for_non_code_change_tasks(
    client: AsyncClient, db_session, mocker
):
    """
    Verify that SR compute functions are only called for create/fix/refactor
    tasks and not for other task types (e.g., research, plan).
    """
    project = Project(name="Test Project", user_id="test-user")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = Task(
        project_id=project.id,
        payloads=[{"type": "research", "description": "test"}],
        vm_instance_id="test-vm",
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    from app.auth import create_agent_token

    token = create_agent_token(str(task.id), "test-vm")
    headers = {"x-agent-token": token}

    mock_false_attestation = AsyncMock()
    mock_regression_risk = AsyncMock()
    mock_fresh_reviewer = AsyncMock()

    with (
        patch(
            "app.sr_analytics.compute_sr_false_attestation_trends",
            new=mock_false_attestation,
        ),
        patch(
            "app.sr_analytics.compute_sr_regression_risk_test_signal_trends",
            new=mock_regression_risk,
        ),
        patch(
            "app.sr_analytics.compute_fresh_reviewer_trend",
            new=mock_fresh_reviewer,
        ),
    ):
        resp = await client.post("/api/agent/claim", headers=headers)
        assert resp.status_code == 200
        # None of these should be called for non-code-change tasks
        mock_false_attestation.assert_not_awaited()
        mock_regression_risk.assert_not_awaited()
        mock_fresh_reviewer.assert_not_awaited()


@pytest.mark.asyncio
async def test_claim_high_iter_loadonly_keeps_sr_trend_columns_eager(db_session):
    """Regression (incident 2026-05-29): claim_task loads its high-iteration
    task rows with ``load_only`` for performance and passes them to the
    SR-trend analytics (``compute_*_trend``). Those functions read
    ``strategic_cycle_id``, ``updated_at`` and ``fix_loop_history`` off the rows
    via ``getattr``. If any is deferred, the access lazy-loads outside the async
    greenlet (``MissingGreenlet``), aborts the request transaction, and 500s the
    claim at the unguarded ``session.get(Project)`` downstream (agent.py:1889).

    This asserts those columns are eager-loaded so the ``getattr`` accesses
    never trigger IO. The load_only set below MIRRORS claim_task's
    ``high_iter_stmt`` — keep them in sync: if an sr_*_trends function starts
    reading another Task attribute off these rows, add the column both here and
    in claim_task.
    """
    from sqlalchemy import inspect as sa_inspect
    from sqlalchemy.orm import load_only
    from sqlmodel import select

    from app.models import TaskStatus

    project = Project(name="LoadOnly Regression", user_id="u")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = Task(
        project_id=project.id,
        payloads=[{"type": "create", "description": "x"}],
        status=TaskStatus.COMPLETED,
        strategic_cycle_id="cycle-abc123",
        fix_loop_history={"iterations": 2},
    )
    db_session.add(task)
    await db_session.commit()

    # Mirror of claim_task's high_iter_stmt load_only (backend/app/routers/agent.py).
    stmt = (
        select(Task)
        .options(
            load_only(
                Task.id,
                Task.title,
                Task.status,
                Task.payloads,
                Task.context,
                Task.latest_status_message,
                Task.created_at,
                Task.updated_at,
                Task.project_id,
                Task.account_id,
                Task.provider,
                Task.model,
                Task.strategic_cycle_id,
                Task.fix_loop_history,
            )
        )
        .where(Task.project_id == project.id)
    )
    rows = (await db_session.execute(stmt)).scalars().all()
    assert rows, "expected the seeded task to load"

    # Every Task attribute the sr_*_trends functions read off these rows must be
    # eager-loaded — otherwise getattr() triggers a MissingGreenlet lazy-load.
    sr_trend_reads = {
        "strategic_cycle_id",
        "updated_at",
        "fix_loop_history",
        "context",
        "created_at",
    }
    for t in rows:
        unloaded = sa_inspect(t).unloaded
        leaked = sr_trend_reads & set(unloaded)
        assert not leaked, (
            f"SR-trend columns deferred by claim_task load_only (would "
            f"MissingGreenlet on access): {sorted(leaked)}"
        )
        # The exact getattr accesses the trend functions perform — must not raise.
        _ = getattr(t, "strategic_cycle_id", None)
        _ = getattr(t, "updated_at", None)
        _ = getattr(t, "fix_loop_history", None)

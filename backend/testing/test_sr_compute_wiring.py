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

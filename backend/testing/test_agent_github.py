"""Tests for the agent GitHub proxy sub-router."""

from unittest.mock import AsyncMock

import pytest
from httpx import AsyncClient

from app.main import app
from app.models import Project, Task
from app.routers.agent import verify_agent_token


EXPECTED_GITHUB_PATHS = {
    "/api/agent/github/token",
    "/api/agent/github/check-runs",
    "/api/agent/github/commit-status",
    "/api/agent/github/pr-comment",
    "/api/agent/github/pr-review-comment",
    "/api/agent/github/pr-review",
    "/api/agent/github/issues",
    "/api/agent/github/branches",
    "/api/agent/github/pull-requests",
}


@pytest.mark.asyncio
async def test_agent_github_routes_are_registered_under_api_agent():
    paths = set(app.openapi()["paths"])
    assert EXPECTED_GITHUB_PATHS <= paths


@pytest.mark.asyncio
async def test_agent_github_check_runs_uses_shared_verify_agent_token(
    client: AsyncClient, db_session, mocker
):
    project = Project(
        name="GitHub Proxy Test",
        user_id="test-user-123",
        github_owner="owner",
        github_repo="repo",
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = Task(
        project_id=project.id,
        payloads=[{"type": "review"}],
        vm_instance_id="agent-vm-gh",
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    app.dependency_overrides[verify_agent_token] = lambda: {
        "sub": str(task.id),
        "vm_instance_id": "agent-vm-gh",
    }
    mocker.patch(
        "app.routers.agent.get_github_token_for_task",
        new=AsyncMock(return_value=("gh-token", "owner", "repo")),
    )
    mocker.patch(
        "app.routers.agent_github.get_check_runs",
        new=AsyncMock(return_value={"status": "completed", "conclusion": "success"}),
    )
    try:
        resp = await client.get(
            "/api/agent/github/check-runs",
            params={"commit_sha": "a" * 40},
        )
        assert resp.status_code == 200, resp.text
        assert resp.json() == {
            "status": "success",
            "data": {
                "status": "completed",
                "conclusion": "success",
                "details": [],
            },
        }
    finally:
        app.dependency_overrides.pop(verify_agent_token, None)

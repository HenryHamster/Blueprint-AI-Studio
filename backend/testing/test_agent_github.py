"""Tests for the agent GitHub proxy sub-router."""

from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException
from httpx import AsyncClient

from app.main import app
from app.models import Project, Task, UserIntegration
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


async def _make_proxy_task(db_session, name: str = "GitHub Proxy Test") -> Task:
    project = Project(
        name=name,
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
    return task


@pytest.mark.asyncio
async def test_agent_github_check_runs_uses_shared_verify_agent_token(
    client: AsyncClient, db_session, mocker
):
    task = await _make_proxy_task(db_session)

    app.dependency_overrides[verify_agent_token] = lambda: {
        "sub": str(task.id),
        "vm_instance_id": "agent-vm-gh",
    }
    mocker.patch(
        "app.routers.agent.get_github_token_for_task",
        new=AsyncMock(return_value=("gh-token", "owner", "repo")),
    )
    # The proxy polls on the SHARED 5000/hr install-token bucket, so it must go
    # through the 30s-TTL cached wrapper (as auto_remediation / ci_resolution /
    # hotfix_backmerge / orchestrator_ci_monitor all do), not raw get_check_runs.
    cached = mocker.patch(
        "app.routers.agent_github.get_check_runs_cached",
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
        assert cached.await_count == 1
    finally:
        app.dependency_overrides.pop(verify_agent_token, None)


@pytest.mark.asyncio
async def test_agent_github_check_runs_skips_failed_log_fanout(
    client: AsyncClient, db_session, mocker
):
    """Budget guard: the handler returns only status/conclusion/details and
    DISCARDS failed_logs, so it must ask for the logs-free variant.

    On a RED sha, get_check_runs' failure fan-out costs 3-10+ extra calls
    (workflow runs + per-run jobs + per-failed-check job LOGS + annotations)
    against the shared installation-token bucket — pure waste here.
    """
    task = await _make_proxy_task(db_session, name="GitHub Proxy Budget")

    app.dependency_overrides[verify_agent_token] = lambda: {
        "sub": str(task.id),
        "vm_instance_id": "agent-vm-gh",
    }
    mocker.patch(
        "app.routers.agent.get_github_token_for_task",
        new=AsyncMock(return_value=("gh-token", "owner", "repo")),
    )
    cached = mocker.patch(
        "app.routers.agent_github.get_check_runs_cached",
        new=AsyncMock(
            return_value={
                "status": "completed",
                "conclusion": "failure",
                "details": [{"name": "ci", "status": "completed", "conclusion": "failure"}],
                "failed_logs": "",
            }
        ),
    )
    try:
        resp = await client.get(
            "/api/agent/github/check-runs",
            params={"commit_sha": "b" * 40},
        )
        assert resp.status_code == 200, resp.text
        # failed_logs is not part of the response contract — hence never fetched.
        assert "failed_logs" not in resp.json()["data"]
    finally:
        app.dependency_overrides.pop(verify_agent_token, None)

    assert cached.await_args.kwargs.get("include_failed_logs") is False, (
        "the check-runs proxy must skip the failure fan-out whose output it discards"
    )


# ─────────────────────────────────────────────────────────────────────────────
# get_github_token_for_task — non-self project token resolution
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_get_github_token_app_level_success_non_self_project(db_session, mocker):
    """App-level installation discovery resolves a token and returns the
    project's own github_owner/github_repo for a non-self-repo project."""
    from app.github_token_resolver import get_github_token_for_task, _github_token_cache

    project = Project(
        name="External Repo Token Test",
        user_id="test-user-ext",
        github_owner="external-owner",
        github_repo="external-repo",
        is_self_repo=False,
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = Task(
        project_id=project.id,
        payloads=[{"type": "create"}],
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    _github_token_cache.pop(str(project.id), None)
    mocker.patch(
        "app.github.get_repo_installation_token",
        new=AsyncMock(return_value="app-install-token"),
    )

    token, owner, repo = await get_github_token_for_task(task.id, db_session)

    assert token == "app-install-token"
    assert owner == "external-owner"
    assert repo == "external-repo"


@pytest.mark.asyncio
async def test_get_github_token_falls_back_to_pat_when_app_fails(db_session, mocker):
    """When App-level discovery raises, get_github_token_for_task falls back to
    the PAT stored in the user integration record."""
    from app.github_token_resolver import get_github_token_for_task, _github_token_cache

    project = Project(
        name="PAT Fallback Project",
        user_id="test-user-pat",
        github_owner="pat-owner",
        github_repo="pat-repo",
        is_self_repo=False,
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    integration = UserIntegration(
        user_id="test-user-pat",
        github_pat="secret-ref/pat-token",
    )
    db_session.add(integration)
    await db_session.commit()

    task = Task(
        project_id=project.id,
        payloads=[{"type": "fix"}],
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    _github_token_cache.pop(str(project.id), None)
    mocker.patch(
        "app.github.get_repo_installation_token",
        new=AsyncMock(side_effect=Exception("App discovery unavailable")),
    )
    mocker.patch(
        "app.github_token_resolver.get_user_secret",
        new=AsyncMock(return_value="plain-pat-token"),
    )

    token, owner, repo = await get_github_token_for_task(task.id, db_session)

    assert token == "plain-pat-token"
    assert owner == "pat-owner"
    assert repo == "pat-repo"


@pytest.mark.asyncio
async def test_get_github_token_falls_back_to_installation_id_when_app_fails(
    db_session, mocker
):
    """When App-level discovery fails and no PAT is set, the stored
    installation_id path is used via generate_installation_access_token."""
    from app.github_token_resolver import get_github_token_for_task, _github_token_cache

    project = Project(
        name="Installation ID Fallback Project",
        user_id="test-user-instid",
        github_owner="instid-owner",
        github_repo="instid-repo",
        is_self_repo=False,
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    integration = UserIntegration(
        user_id="test-user-instid",
        github_installation_id="inst-456",
    )
    db_session.add(integration)
    await db_session.commit()

    task = Task(
        project_id=project.id,
        payloads=[{"type": "refactor"}],
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    _github_token_cache.pop(str(project.id), None)
    mocker.patch(
        "app.github.get_repo_installation_token",
        new=AsyncMock(side_effect=Exception("App discovery unavailable")),
    )
    mocker.patch(
        "app.github_token_resolver.generate_installation_access_token",
        new=AsyncMock(return_value="instid-access-token"),
    )

    token, owner, repo = await get_github_token_for_task(task.id, db_session)

    assert token == "instid-access-token"
    assert owner == "instid-owner"
    assert repo == "instid-repo"


@pytest.mark.asyncio
async def test_get_github_token_raises_400_when_all_fallbacks_exhausted(
    db_session, mocker
):
    """HTTPException(400) is raised when App-level discovery, installation_id,
    and PAT fallbacks all fail or are absent."""
    from app.github_token_resolver import get_github_token_for_task, _github_token_cache

    project = Project(
        name="No Token Project",
        user_id="test-user-notoken",
        github_owner="notoken-owner",
        github_repo="notoken-repo",
        is_self_repo=False,
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    # No UserIntegration row at all — neither PAT nor installation_id available.
    task = Task(
        project_id=project.id,
        payloads=[{"type": "create"}],
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    _github_token_cache.pop(str(project.id), None)
    mocker.patch(
        "app.github.get_repo_installation_token",
        new=AsyncMock(side_effect=Exception("App discovery unavailable")),
    )

    with pytest.raises(HTTPException) as exc_info:
        await get_github_token_for_task(task.id, db_session)

    assert exc_info.value.status_code == 400

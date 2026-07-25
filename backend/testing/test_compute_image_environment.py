"""Focused tests for the agent-facing project compute-image resolver."""

from __future__ import annotations

import uuid

import pytest
from httpx import AsyncClient

from app.auth import create_agent_token
from app.compute_image_environment import (
    ComputeImageEnvironmentState,
    choose_dockerfile_candidate,
    resolve_project_compute_image_environment,
    _suggested_image_name,
)
from app.models import Project, Task


def _project() -> Project:
    return Project(
        id=uuid.uuid4(),
        name="Compute Environment",
        user_id="compute-environment-user",
        github_owner="example",
        github_repo="compute-environment",
    )


async def _manifest_without_image(_project: Project) -> dict:
    return {"compute": {"image": None}, "error": None}


@pytest.mark.asyncio
async def test_resolver_returns_candidate_manifest_without_blocking_bare_compute():
    project = _project()

    async def paths(_project: Project) -> list[str]:
        return ["Dockerfile", "DEN/Dockerfile", "node_modules/Dockerfile"]

    result = await resolve_project_compute_image_environment(
        object(),  # type: ignore[arg-type]
        project,
        manifest_fn=_manifest_without_image,
        path_list_fn=paths,
    )

    assert result["state"] == ComputeImageEnvironmentState.MISSING_DOCKERFILE_CANDIDATE
    assert result["candidate_dockerfile"] == "Dockerfile"
    assert result["manifest_template"]["compute"]["image"]["build"] == {
        "dockerfile": "Dockerfile"
    }


@pytest.mark.asyncio
async def test_resolver_reports_declared_recipe_as_buildable(monkeypatch):
    project = _project()

    async def manifest(_project: Project) -> dict:
        return {
            "compute": {
                "image": {
                    "name": "registry.example/project:latest",
                    "build": {"dockerfile": "DEN/Dockerfile"},
                }
            },
            "error": None,
        }

    async def unresolved(*_args, **_kwargs):
        return None, "no built digest record"

    from app import compute_backends

    monkeypatch.setattr(compute_backends, "resolve_project_baked_image_strict", unresolved)
    result = await resolve_project_compute_image_environment(
        object(),  # type: ignore[arg-type]
        project,
        manifest_fn=manifest,
    )

    assert result["state"] == ComputeImageEnvironmentState.DECLARED_NEEDS_BUILD
    assert result["action"] == "build"
    assert result["dockerfile"] == "DEN/Dockerfile"


@pytest.mark.asyncio
async def test_resolver_returns_digest_pinned_ready_image(monkeypatch):
    project = _project()

    async def manifest(_project: Project) -> dict:
        return {
            "compute": {"image": {"name": "registry.example/project:latest"}},
            "error": None,
        }

    pinned = "registry.example/project@sha256:" + "a" * 64

    async def resolved(*_args, **_kwargs):
        return pinned, None

    from app import compute_backends

    monkeypatch.setattr(compute_backends, "resolve_project_baked_image_strict", resolved)
    result = await resolve_project_compute_image_environment(
        object(),  # type: ignore[arg-type]
        project,
        manifest_fn=manifest,
    )

    assert result["state"] == ComputeImageEnvironmentState.READY
    assert result["container_image"] == pinned


@pytest.mark.asyncio
async def test_resolver_no_candidate_keeps_bare_compute_valid():
    result = await resolve_project_compute_image_environment(
        object(),  # type: ignore[arg-type]
        _project(),
        manifest_fn=_manifest_without_image,
        path_list_fn=lambda _project: _empty_paths(),
    )

    assert result["state"] == ComputeImageEnvironmentState.NO_CANDIDATE
    assert result["action"] == "use_bare_compute"
    assert result["container_image"] is None


async def _empty_paths() -> list[str]:
    return []


def test_candidate_selection_prefers_purpose_built_image_and_ignores_vendor_tree():
    assert choose_dockerfile_candidate(
        ["DEN/Dockerfile", "vendor/tool/Dockerfile", ".blueprint/Dockerfile.compute"]
    ) == ".blueprint/Dockerfile.compute"




def test_suggested_image_name_includes_owner_to_avoid_repo_name_collisions():
    first = _project()
    second = _project()
    second.github_owner = "another-owner"

    assert _suggested_image_name(first) != _suggested_image_name(second)


@pytest.mark.asyncio
async def test_agent_compute_environment_is_project_scoped(client: AsyncClient, db_session, monkeypatch):
    project = _project()
    db_session.add(project)
    await db_session.commit()
    task = Task(project_id=project.id, payloads=[{"type": "strategic_review"}], vm_instance_id="env-agent")
    db_session.add(task)
    await db_session.commit()

    async def fake_resolver(*_args, **_kwargs):
        return {"state": "no_candidate", "action": "use_bare_compute"}

    from app import compute_image_environment

    monkeypatch.setattr(compute_image_environment, "resolve_project_compute_image_environment", fake_resolver)
    token = create_agent_token(str(task.id), "env-agent", task_type="strategic_review")
    response = await client.get(
        f"/api/agent/projects/{project.id}/compute-environment",
        headers={"x-agent-token": token},
    )

    assert response.status_code == 200, response.text
    assert response.json()["data"]["action"] == "use_bare_compute"


    assert response.json()["data"]["build_status"] == "not_running"


@pytest.mark.asyncio
async def test_agent_compute_environment_retains_terminal_build_outcome(
    client: AsyncClient, db_session, monkeypatch
):
    project = _project()
    db_session.add(project)
    await db_session.commit()
    task = Task(
        project_id=project.id,
        payloads=[{"type": "strategic_review"}],
        vm_instance_id="env-agent-outcome",
    )
    db_session.add(task)
    await db_session.commit()

    async def fake_resolver(*_args, **_kwargs):
        return {"state": "declared_needs_build", "action": "build"}

    from app import compute_image_environment
    from app.routers import agent_strategic_fetch

    monkeypatch.setattr(
        compute_image_environment,
        "resolve_project_compute_image_environment",
        fake_resolver,
    )
    monkeypatch.setattr(
        agent_strategic_fetch,
        "_agent_compute_image_build_last_outcomes",
        {project.id: {"status": "deferred", "reason": "builder_busy", "completed_at": "2026-07-25T00:00:00+00:00"}},
    )
    token = create_agent_token(str(task.id), "env-agent-outcome", task_type="strategic_review")
    response = await client.get(
        f"/api/agent/projects/{project.id}/compute-environment",
        headers={"x-agent-token": token},
    )

    assert response.status_code == 200, response.text
    assert response.json()["data"]["build_status"] == "not_running"
    assert response.json()["data"]["last_build_outcome"]["reason"] == "builder_busy"
@pytest.mark.asyncio
async def test_agent_compute_image_build_rejects_non_strategic_task(client: AsyncClient, db_session):
    project = _project()
    db_session.add(project)
    await db_session.commit()
    task = Task(project_id=project.id, payloads=[{"type": "research"}], vm_instance_id="env-research")
    db_session.add(task)
    await db_session.commit()

    token = create_agent_token(str(task.id), "env-research", task_type="research")
    response = await client.post(
        f"/api/agent/projects/{project.id}/compute-image/build",
        headers={"x-agent-token": token},
    )

    assert response.status_code == 403, response.text


@pytest.mark.asyncio
async def test_agent_compute_image_build_returns_accepted_without_foreground_build(
    client: AsyncClient, db_session, monkeypatch
):
    project = _project()
    db_session.add(project)
    await db_session.commit()
    task = Task(
        project_id=project.id,
        payloads=[{"type": "strategic_review"}],
        vm_instance_id="env-strategic",
    )
    db_session.add(task)
    await db_session.commit()

    async def fake_resolver(*_args, **_kwargs):
        return {
            "state": ComputeImageEnvironmentState.DECLARED_NEEDS_BUILD,
            "action": "build",
        }

    requests: list[uuid.UUID] = []

    def fake_request(project_id: uuid.UUID) -> str:
        requests.append(project_id)
        return "started"

    from app import compute_image_environment
    from app.routers import agent_strategic_fetch

    monkeypatch.setattr(
        compute_image_environment,
        "resolve_project_compute_image_environment",
        fake_resolver,
    )
    monkeypatch.setattr(
        agent_strategic_fetch,
        "_request_agent_compute_image_build",
        fake_request,
    )
    token = create_agent_token(str(task.id), "env-strategic", task_type="strategic_review")
    response = await client.post(
        f"/api/agent/projects/{project.id}/compute-image/build",
        headers={"x-agent-token": token},
    )

    assert response.status_code == 202, response.text
    body = response.json()
    assert body["status"] == "accepted"
    assert body["environment"]["build_status"] == "in_progress"
    assert requests == [project.id]

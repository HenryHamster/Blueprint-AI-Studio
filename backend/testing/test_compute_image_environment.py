"""Focused tests for the agent-facing project compute-image resolver."""

from __future__ import annotations

import asyncio
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


    assert response.json()["data"]["build_status"] == "not_applicable"


@pytest.mark.asyncio
async def test_agent_compute_environment_reads_durable_current_terminal_outcome(
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
    from app import compute_backends
    from app.project_telemetry import set_project_telemetry

    monkeypatch.setattr(
        compute_image_environment,
        "resolve_project_compute_image_environment",
        fake_resolver,
    )
    async def matching_identity(*_args, **_kwargs):
        return {"dep_hash": "dep-current", "status": "deferred"}

    monkeypatch.setattr(
        compute_backends,
        "resolve_project_compute_image_dependency_identity",
        matching_identity,
    )
    await set_project_telemetry(
        db_session,
        project.id,
        "compute_image_build_state",
        {
            "request_id": "durable-request",
            "status": "deferred",
            "reason": "builder_busy",
            "dep_hash": "dep-current",
            "requested_at": "2026-07-25T00:00:00+00:00",
            "completed_at": "2026-07-25T00:01:00+00:00",
        },
    )
    await db_session.commit()
    token = create_agent_token(str(task.id), "env-agent-outcome", task_type="strategic_review")
    response = await client.get(
        f"/api/agent/projects/{project.id}/compute-environment",
        headers={"x-agent-token": token},
    )

    assert response.status_code == 200, response.text
    assert response.json()["data"]["build_status"] == "not_running"
    assert response.json()["data"]["build_identity_status"] == "current"
    assert response.json()["data"]["last_build_outcome"]["reason"] == "builder_busy"


@pytest.mark.asyncio
async def test_agent_compute_environment_suppresses_stale_terminal_outcome(
    client: AsyncClient, db_session, monkeypatch
):
    project = _project()
    db_session.add(project)
    await db_session.commit()
    task = Task(
        project_id=project.id,
        payloads=[{"type": "strategic_review"}],
        vm_instance_id="env-agent-stale-outcome",
    )
    db_session.add(task)
    await db_session.commit()

    async def fake_resolver(*_args, **_kwargs):
        return {"state": "declared_needs_build", "action": "build"}

    from app import compute_image_environment
    from app import compute_backends
    from app.project_telemetry import set_project_telemetry

    monkeypatch.setattr(
        compute_image_environment,
        "resolve_project_compute_image_environment",
        fake_resolver,
    )

    async def changed_identity(*_args, **_kwargs):
        return {"dep_hash": "dep-new", "status": "deferred"}

    monkeypatch.setattr(
        compute_backends,
        "resolve_project_compute_image_dependency_identity",
        changed_identity,
    )
    await set_project_telemetry(
        db_session,
        project.id,
        "compute_image_build_state",
        {"request_id": "old-request", "status": "error", "dep_hash": "dep-old"},
    )
    await db_session.commit()
    token = create_agent_token(str(task.id), "env-agent-stale-outcome", task_type="strategic_review")
    response = await client.get(
        f"/api/agent/projects/{project.id}/compute-environment",
        headers={"x-agent-token": token},
    )

    assert response.status_code == 200, response.text
    assert response.json()["data"]["build_status"] == "not_requested"
    assert response.json()["data"]["build_identity_status"] == "stale"
    assert "last_build_outcome" not in response.json()["data"]


@pytest.mark.asyncio
async def test_durable_compute_image_claim_allows_only_one_active_worker(db_session):
    """Two Cloud Run instances racing the same recipe get exactly one owner."""
    project = _project()
    db_session.add(project)
    await db_session.commit()

    from app.routers import agent_strategic_fetch

    async def claim(request_id: str) -> bool:
        return await agent_strategic_fetch._claim_agent_compute_image_build_request(
            project.id,
            request_id,
            dep_hash="dep-race",
            recipe_identity="recipe-race",
        )

    claims = await asyncio.gather(claim("worker-a"), claim("worker-b"))
    assert claims.count(True) == 1
    assert claims.count(False) == 1


@pytest.mark.asyncio
async def test_late_compute_image_worker_cannot_overwrite_newer_claim(db_session):
    project = _project()
    db_session.add(project)
    await db_session.commit()

    from app.project_telemetry import read_telemetry
    from app.routers import agent_strategic_fetch

    assert await agent_strategic_fetch._claim_agent_compute_image_build_request(
        project.id,
        "old-worker",
        dep_hash="dep-old",
        recipe_identity="recipe-old",
    )
    # A changed recipe legitimately reclaims an otherwise active old lease.
    assert await agent_strategic_fetch._claim_agent_compute_image_build_request(
        project.id,
        "new-worker",
        dep_hash="dep-new",
        recipe_identity="recipe-new",
    )
    assert not await agent_strategic_fetch._persist_agent_compute_image_build_terminal_state(
        project.id,
        "old-worker",
        status="error",
        reason="late_old_worker",
    )

    state = await read_telemetry(
        db_session, project.id, "compute_image_build_state"
    )
    assert state is not None
    assert state["request_id"] == "new-worker"
    assert state["status"] == "requested"


@pytest.mark.asyncio
async def test_expired_compute_image_lease_can_be_reclaimed(db_session):
    project = _project()
    db_session.add(project)
    await db_session.commit()

    from app.project_telemetry import set_project_telemetry
    from app.routers import agent_strategic_fetch

    await set_project_telemetry(
        db_session,
        project.id,
        "compute_image_build_state",
        {
            "request_id": "crashed-worker",
            "status": "requested",
            "dep_hash": "dep-current",
            "recipe_identity": "recipe-current",
            "requested_at": "2026-07-25T00:00:00+00:00",
            "lease_expires_at": "2026-07-25T00:01:00+00:00",
        },
    )
    await db_session.commit()

    assert await agent_strategic_fetch._claim_agent_compute_image_build_request(
        project.id,
        "recovery-worker",
        dep_hash="dep-current",
        recipe_identity="recipe-current",
    )


@pytest.mark.asyncio
async def test_compute_image_heartbeat_renews_owned_lease(db_session):
    project = _project()
    db_session.add(project)
    await db_session.commit()

    from app.routers import agent_strategic_fetch

    assert await agent_strategic_fetch._claim_agent_compute_image_build_request(
        project.id,
        "heartbeat-worker",
        dep_hash="dep-heartbeat",
        recipe_identity="recipe-heartbeat",
    )
    assert await agent_strategic_fetch._renew_agent_compute_image_build_lease(
        project.id, "heartbeat-worker"
    )
    assert not await agent_strategic_fetch._claim_agent_compute_image_build_request(
        project.id,
        "second-worker",
        dep_hash="dep-heartbeat",
        recipe_identity="recipe-heartbeat",
    )


@pytest.mark.asyncio
async def test_local_task_registry_never_overrides_durable_build_claim(
    db_session, monkeypatch
):
    """A local old task cannot block a changed-recipe durable reclaim."""
    project = _project()
    db_session.add(project)
    await db_session.commit()

    environment = {
        "state": "declared_needs_build",
        "action": "build",
        "manifest_image": "registry.example/project:latest",
        "dockerfile": "Dockerfile",
    }
    old_manifest = {
        "compute": {
            "image": {
                "name": "registry.example/project:latest",
                "build": {
                    "dockerfile": "Dockerfile",
                    "deps": ["requirements.txt"],
                    "build_args": {"CUDA": "12.1"},
                },
            }
        }
    }
    changed_manifest = {
        "compute": {
            "image": {
                "name": "registry.example/project:latest",
                "build": {
                    "dockerfile": "Dockerfile",
                    "deps": ["requirements.txt"],
                    "build_args": {"CUDA": "12.4"},
                },
            }
        }
    }

    from app import compute_backends
    from app.project_telemetry import set_project_telemetry
    from app.routers import agent_strategic_fetch

    current_manifest = old_manifest

    async def manifest_fn(_project):
        return current_manifest

    async def prehash_failure(*_args, **_kwargs):
        return {"dep_hash": None, "reason": "manifest_probe_failed"}

    launched: list[str] = []

    async def fake_runner(_project_id, request_id):
        launched.append(request_id)

    monkeypatch.setattr(
        compute_backends,
        "resolve_project_compute_image_dependency_identity",
        prehash_failure,
    )
    monkeypatch.setattr(
        agent_strategic_fetch,
        "_run_agent_requested_compute_image_build",
        fake_runner,
    )

    old_recipe_identity = agent_strategic_fetch._compute_image_recipe_identity(
        environment, old_manifest
    )
    await set_project_telemetry(
        db_session,
        project.id,
        "compute_image_build_state",
        {
            "request_id": "old-local-worker",
            "status": "requested",
            "recipe_identity": old_recipe_identity,
            "requested_at": "2026-07-25T00:00:00+00:00",
            "lease_expires_at": "2099-07-25T00:00:00+00:00",
        },
    )
    await db_session.commit()

    release_old_task = asyncio.Event()

    async def active_local_task():
        await release_old_task.wait()

    old_task = asyncio.create_task(active_local_task())
    agent_strategic_fetch._agent_compute_image_build_tasks[project.id] = old_task
    try:
        assert await agent_strategic_fetch._request_agent_compute_image_build(
            db_session, project, environment, manifest_fn=manifest_fn
        ) == "already_requested"
        assert launched == []

        current_manifest = changed_manifest
        assert await agent_strategic_fetch._request_agent_compute_image_build(
            db_session, project, environment, manifest_fn=manifest_fn
        ) == "requested"
        await asyncio.sleep(0)
        assert len(launched) == 1
    finally:
        release_old_task.set()
        await old_task
        agent_strategic_fetch._agent_compute_image_build_tasks.pop(project.id, None)


@pytest.mark.asyncio
async def test_agent_compute_environment_recovers_stale_requested_hash(
    client: AsyncClient, db_session, monkeypatch
):
    project = _project()
    db_session.add(project)
    await db_session.commit()
    task = Task(
        project_id=project.id,
        payloads=[{"type": "strategic_review"}],
        vm_instance_id="env-agent-stale-request",
    )
    db_session.add(task)
    await db_session.commit()

    environment = {"state": "declared_needs_build", "action": "build"}
    from app import compute_backends, compute_image_environment
    from app.project_telemetry import set_project_telemetry
    from app.routers import agent_strategic_fetch

    async def fake_resolver(*_args, **_kwargs):
        return dict(environment)

    async def changed_identity(*_args, **_kwargs):
        return {"dep_hash": "dep-new", "status": "deferred"}

    monkeypatch.setattr(
        compute_image_environment,
        "resolve_project_compute_image_environment",
        fake_resolver,
    )
    monkeypatch.setattr(
        compute_backends,
        "resolve_project_compute_image_dependency_identity",
        changed_identity,
    )
    await set_project_telemetry(
        db_session,
        project.id,
        "compute_image_build_state",
        {
            "request_id": "stale-request",
            "status": "requested",
            "dep_hash": "dep-old",
            "recipe_identity": agent_strategic_fetch._compute_image_recipe_identity(
                environment
            ),
            "requested_at": "2026-07-25T00:00:00+00:00",
            "lease_expires_at": "2099-07-25T00:00:00+00:00",
        },
    )
    await db_session.commit()

    token = create_agent_token(str(task.id), "env-agent-stale-request", task_type="strategic_review")
    response = await client.get(
        f"/api/agent/projects/{project.id}/compute-environment",
        headers={"x-agent-token": token},
    )

    assert response.status_code == 200, response.text
    assert response.json()["data"]["build_status"] == "not_requested"
    assert response.json()["data"]["build_identity_status"] == "stale"


@pytest.mark.asyncio
async def test_agent_compute_environment_keeps_current_prehash_failure_actionable(
    client: AsyncClient, db_session, monkeypatch
):
    project = _project()
    db_session.add(project)
    await db_session.commit()
    task = Task(
        project_id=project.id,
        payloads=[{"type": "strategic_review"}],
        vm_instance_id="env-agent-prehash-error",
    )
    db_session.add(task)
    await db_session.commit()

    environment = {"state": "declared_needs_build", "action": "build"}
    from app import compute_image_environment
    from app.project_telemetry import set_project_telemetry
    from app.routers import agent_strategic_fetch

    async def fake_resolver(*_args, **_kwargs):
        return dict(environment)

    monkeypatch.setattr(
        compute_image_environment,
        "resolve_project_compute_image_environment",
        fake_resolver,
    )
    await set_project_telemetry(
        db_session,
        project.id,
        "compute_image_build_state",
        {
            "request_id": "prehash-error",
            "status": "error",
            "recipe_identity": agent_strategic_fetch._compute_image_recipe_identity(
                environment
            ),
            "reason": "manifest_probe_failed",
            "requested_at": "2026-07-25T00:00:00+00:00",
            "completed_at": "2026-07-25T00:01:00+00:00",
        },
    )
    await db_session.commit()

    token = create_agent_token(str(task.id), "env-agent-prehash-error", task_type="strategic_review")
    response = await client.get(
        f"/api/agent/projects/{project.id}/compute-environment",
        headers={"x-agent-token": token},
    )

    assert response.status_code == 200, response.text
    assert response.json()["data"]["build_status"] == "not_running"
    assert response.json()["data"]["build_identity_status"] == "current_recipe"
    assert response.json()["data"]["last_build_outcome"]["reason"] == "manifest_probe_failed"


def _shared_session_factory(session):
    """Return a lightweight factory for testing a detached worker's DB writes."""

    class SharedSessionContext:
        async def __aenter__(self):
            return session

        async def __aexit__(self, exc_type, exc_value, traceback):
            return False

    return SharedSessionContext


@pytest.mark.asyncio
async def test_background_compute_image_build_persists_completion(db_session, monkeypatch):
    project = _project()
    db_session.add(project)
    await db_session.commit()

    from app import compute_backends
    from app.project_telemetry import read_telemetry
    from app.routers import agent_strategic_fetch

    monkeypatch.setattr(
        agent_strategic_fetch,
        "AsyncSessionLocal",
        _shared_session_factory(db_session),
    )

    async def completed(*_args, **_kwargs):
        return {"status": "rebuilt", "dep_hash": "dep-completed"}

    monkeypatch.setattr(compute_backends, "refresh_project_compute_image", completed)
    request_id = "completion-request"
    assert await agent_strategic_fetch._claim_agent_compute_image_build_request(
        project.id,
        request_id,
        dep_hash="dep-completed",
        recipe_identity="recipe-completed",
    )
    await agent_strategic_fetch._run_agent_requested_compute_image_build(
        project.id, request_id
    )

    state = await read_telemetry(
        db_session, project.id, "compute_image_build_state"
    )
    assert state is not None
    assert state["request_id"] == request_id
    assert state["status"] == "rebuilt"
    assert state["dep_hash"] == "dep-completed"
    assert state["completed_at"]


@pytest.mark.asyncio
async def test_background_compute_image_build_persists_deferred_result(
    db_session, monkeypatch
):
    project = _project()
    db_session.add(project)
    await db_session.commit()

    from app import compute_backends
    from app.project_telemetry import read_telemetry
    from app.routers import agent_strategic_fetch

    monkeypatch.setattr(
        agent_strategic_fetch,
        "AsyncSessionLocal",
        _shared_session_factory(db_session),
    )

    async def deferred(*_args, **_kwargs):
        return {
            "status": "deferred",
            "dep_hash": "dep-deferred",
            "reason": "builder_busy",
        }

    monkeypatch.setattr(compute_backends, "refresh_project_compute_image", deferred)
    request_id = "deferred-request"
    assert await agent_strategic_fetch._claim_agent_compute_image_build_request(
        project.id,
        request_id,
        dep_hash="dep-deferred",
        recipe_identity="recipe-deferred",
    )
    await agent_strategic_fetch._run_agent_requested_compute_image_build(
        project.id, request_id
    )

    state = await read_telemetry(
        db_session, project.id, "compute_image_build_state"
    )
    assert state is not None
    assert state["status"] == "deferred"
    assert state["dep_hash"] == "dep-deferred"
    assert state["reason"] == "builder_busy"


@pytest.mark.asyncio
async def test_background_compute_image_build_persists_interruption(
    db_session, monkeypatch
):
    project = _project()
    db_session.add(project)
    await db_session.commit()

    from app import compute_backends
    from app.project_telemetry import read_telemetry
    from app.routers import agent_strategic_fetch

    monkeypatch.setattr(
        agent_strategic_fetch,
        "AsyncSessionLocal",
        _shared_session_factory(db_session),
    )

    async def cancelled(*_args, **_kwargs):
        raise asyncio.CancelledError

    monkeypatch.setattr(compute_backends, "refresh_project_compute_image", cancelled)
    request_id = "cancelled-request"
    assert await agent_strategic_fetch._claim_agent_compute_image_build_request(
        project.id,
        request_id,
        dep_hash="dep-cancelled",
        recipe_identity="recipe-cancelled",
    )
    with pytest.raises(asyncio.CancelledError):
        await agent_strategic_fetch._run_agent_requested_compute_image_build(
            project.id, request_id
        )

    state = await read_telemetry(
        db_session, project.id, "compute_image_build_state"
    )
    assert state is not None
    assert state["status"] == "interrupted"
    assert state["reason"] == "build_cancelled; reaper will retry"


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

    async def fake_request(_session, project: Project, _environment, **_kwargs) -> str:
        requests.append(project.id)
        return "requested"

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
    assert body["environment"]["build_status"] == "requested"
    assert requests == [project.id]

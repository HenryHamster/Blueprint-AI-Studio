"""CI-compliance fetch and strategic cap-bump gate tests."""

from __future__ import annotations

import json

import pytest
from sqlalchemy import select

from app import ci_compliance
from app.auth import create_agent_token
from app.models import Project, Task, TaskStatus
from app.strategic_spawning import (
    _maybe_spawn_ci_compliance_cap_bump_fix,
    _spawn_strategic_review,
    ci_compliance_qualify_gate,
)

_HEAD_SHA = "a" * 40
_CAPPED_REL = "main.py"
_CAP = 10


def _limits_json(cap: int = _CAP) -> str:
    return json.dumps({"backend": {_CAPPED_REL: cap}})


def _fake_reader(module_line_count: int):
    async def _reader(path, *, token, owner, repo, ref, project_id=None):
        if path == ci_compliance._MODULE_SIZE_LIMITS_PATH:
            return _limits_json()
        if path == f"backend/app/{_CAPPED_REL}":
            return "\n".join("x" for _ in range(module_line_count))
        return None

    return _reader


async def _fake_resolve(_session, _project):
    return ("token", "owner", "repo")


async def _fake_branch_sha(*_args, **_kwargs):
    return _HEAD_SHA


def _violation_result(count: int) -> dict:
    return {
        "ref": _HEAD_SHA,
        "compliant": False,
        "violations": [
            {
                "section": "backend",
                "rel": _CAPPED_REL,
                "count": count,
                "cap": _CAP,
            }
        ],
        "checked": 1,
        "limits_available": True,
    }


async def _make_project_with_agent_task(db_session, *, module_line_count: int, mocker):
    mocker.patch(
        "app.release_promotion._resolve_github_with_session",
        new=_fake_resolve,
    )
    mocker.patch.object(
        ci_compliance.github,
        "get_repo_installation_token",
        new=mocker.AsyncMock(return_value="token"),
    )
    mocker.patch.object(ci_compliance.github, "get_branch_sha", new=_fake_branch_sha)
    mocker.patch.object(
        ci_compliance.github,
        "get_file_text_at_ref",
        new=_fake_reader(module_line_count),
    )
    mocker.patch.object(
        ci_compliance.github,
        "list_repo_tree_paths_at_ref",
        new=mocker.AsyncMock(return_value=[]),
    )

    project = Project(
        name="CI Compliance Gate",
        user_id="test-user-123",
        github_owner="owner",
        github_repo="repo",
        integration_branch="automain",
        promotion_target="main",
        autonomous_enabled=True,
        directions=[],
        vision={"purpose": "test CI-compliance gate"},
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = Task(
        project_id=project.id,
        payloads=[{"type": "strategic_review"}],
        vm_instance_id="agent-vm-ci-compliance",
        status=TaskStatus.IN_PROGRESS,
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)
    await db_session.refresh(project)
    return project, task


@pytest.mark.asyncio
async def test_ci_compliance_fetch_gate_and_cap_bump_spawn(
    client, db_session, mocker
):
    """A violating automain HEAD is fetched, gated, and remediated by one task."""
    project, agent_task = await _make_project_with_agent_task(
        db_session, module_line_count=_CAP + 3, mocker=mocker
    )

    token = create_agent_token(str(agent_task.id), "agent-vm-ci-compliance")
    response = await client.get(
        f"/api/agent/projects/{project.id}/ci-compliance",
        headers={"x-agent-token": token},
    )

    assert response.status_code == 200, response.text
    body = response.json()["data"]
    assert body["head_sha"] == _HEAD_SHA
    assert body["compliant"] is False
    assert body["violation_count"] == 1
    assert body["violations"] == [
        {
            "section": "backend",
            "rel": _CAPPED_REL,
            "count": _CAP + 3,
            "cap": _CAP,
        }
    ]

    await db_session.refresh(project)
    gate = ci_compliance_qualify_gate(project, _HEAD_SHA)
    assert gate["blocked"] is True
    assert gate["violation_count"] == 1

    spawned = await _maybe_spawn_ci_compliance_cap_bump_fix(db_session, project, gate)
    await db_session.commit()
    assert spawned is not None

    cap_bump_task = (
        await db_session.execute(select(Task).where(Task.id == spawned.id))
    ).scalar_one()
    assert cap_bump_task.status == TaskStatus.QUEUED
    assert cap_bump_task.created_by == "ORCHESTRATOR"
    assert cap_bump_task.context["spawned_by"] == "ci_compliance_cap_bump_gate"
    assert cap_bump_task.context["head_sha"] == _HEAD_SHA
    assert cap_bump_task.first_payload["type"] == "create"
    assert "python3 backend/testing/check_size_limits.py --fix" in cap_bump_task.first_payload[
        "prompt"
    ]


@pytest.mark.asyncio
async def test_strategic_review_surfaces_ci_compliance_and_dedupes_cap_bump(
    db_session, mocker
):
    """Strategic spawn carries ci_compliance and creates only one remediation."""
    project, agent_task = await _make_project_with_agent_task(
        db_session, module_line_count=_CAP + 2, mocker=mocker
    )
    agent_task.status = TaskStatus.COMPLETED
    stored_result = _violation_result(_CAP + 2)
    ci_compliance.record_ci_compliance(project, stored_result)
    db_session.add(agent_task)
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    async def _stored_current(_session, _project):
        return {
            "status": "success",
            "integration_branch": "automain",
            "head_sha": _HEAD_SHA,
            "data": stored_result,
            "computed": False,
        }

    mocker.patch(
        "app.ci_compliance.ensure_current_ci_compliance",
        new=_stored_current,
    )

    spawned = await _spawn_strategic_review(project, db_session)
    assert spawned is True
    await db_session.commit()

    tasks = (
        await db_session.execute(select(Task).where(Task.project_id == project.id))
    ).scalars().all()
    strategic_payload = next(
        task.first_payload
        for task in tasks
        if task.first_payload.get("type") == "strategic_review"
        and task.created_by == "ORCHESTRATOR"
    )
    assert strategic_payload["ci_compliance"]["head_sha"] == _HEAD_SHA
    assert strategic_payload["ci_compliance"]["compliant"] is False
    assert strategic_payload["ci_compliance"]["violation_count"] == 1

    cap_bump_tasks = [
        task
        for task in tasks
        if (task.context or {}).get("spawned_by") == "ci_compliance_cap_bump_gate"
    ]
    assert len(cap_bump_tasks) == 1
    assert cap_bump_tasks[0].context["head_sha"] == _HEAD_SHA


@pytest.mark.asyncio
async def test_compliant_head_fetch_gate_and_spawn_are_noops(client, db_session, mocker):
    """A compliant automain HEAD is surfaced but does not block or spawn a fix."""
    project, agent_task = await _make_project_with_agent_task(
        db_session, module_line_count=_CAP - 1, mocker=mocker
    )

    token = create_agent_token(str(agent_task.id), "agent-vm-ci-compliance")
    response = await client.get(
        f"/api/agent/projects/{project.id}/ci-compliance",
        headers={"x-agent-token": token},
    )

    assert response.status_code == 200, response.text
    body = response.json()["data"]
    assert body["compliant"] is True
    assert body["violation_count"] == 0
    assert body["violations"] == []

    await db_session.refresh(project)
    gate = ci_compliance_qualify_gate(project, _HEAD_SHA)
    assert gate["blocked"] is False
    assert gate["violations"] == []

    spawned = await _maybe_spawn_ci_compliance_cap_bump_fix(db_session, project, gate)
    await db_session.commit()
    assert spawned is None
    rows = (
        await db_session.execute(select(Task).where(Task.project_id == project.id))
    ).scalars().all()
    assert not any(
        (task.context or {}).get("spawned_by") == "ci_compliance_cap_bump_gate"
        for task in rows
    )


@pytest.mark.asyncio
async def test_noncompliant_qualify_request_is_blocked_and_spawns_cap_bump(
    db_session, mocker
):
    """A later qualify_request cannot bypass the CI-compliance gate."""
    from app import release_version as rv
    from app.strategic_completion import _apply_qualify_request

    project, agent_task = await _make_project_with_agent_task(
        db_session, module_line_count=_CAP + 4, mocker=mocker
    )
    version = await rv.open_version(db_session, project.id, version_number=1)
    rv.set_version_phase(version, "stabilize", reasoning="ready to qualify")
    db_session.add(version)
    await db_session.commit()
    await db_session.refresh(project)

    run_qualification = mocker.patch(
        "app.release_qualifier.run_qualification",
        new=mocker.AsyncMock(return_value={"status": "spawned"}),
    )

    await _apply_qualify_request(
        {"reason": "ready"},
        agent_task,
        project,
        db_session,
        path="test",
    )
    await db_session.commit()

    run_qualification.assert_not_awaited()
    await db_session.refresh(project)
    gate = ci_compliance_qualify_gate(project, _HEAD_SHA)
    assert gate["blocked"] is True

    tasks = (
        await db_session.execute(select(Task).where(Task.project_id == project.id))
    ).scalars().all()
    cap_bump_tasks = [
        task
        for task in tasks
        if (task.context or {}).get("spawned_by") == "ci_compliance_cap_bump_gate"
    ]
    assert len(cap_bump_tasks) == 1
    assert cap_bump_tasks[0].status == TaskStatus.QUEUED
    assert cap_bump_tasks[0].context["head_sha"] == _HEAD_SHA
    assert "python3 backend/testing/check_size_limits.py --fix" in cap_bump_tasks[
        0
    ].first_payload["prompt"]

    # Observability: the CI-compliance block is recorded durably on the version
    # row (queryable via the strategic payload / version fetch), not just logged.
    await db_session.refresh(version)
    assert version.last_qualification is not None
    blocked_trace = version.last_qualification["qualify_request_blocked"]
    assert blocked_trace["blocker"] == "ci_compliance"
    assert blocked_trace["head_sha"] == _HEAD_SHA
    assert blocked_trace["cap_bump_task"] == str(cap_bump_tasks[0].id)
    assert "qualify_request_blocked_at" in version.last_qualification

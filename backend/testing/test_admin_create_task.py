"""Tests for the hotfix guard in admin.create_task_manually.

POST /api/admin/tasks accepts a ``hotfix`` payload flag (Phase 5 / automain-LTS).
A hotfix is only meaningful for a ``plan`` task: the back-merge trigger in
``task_lifecycle.py`` (``is_rollup_merge and base_branch == "main" and hotfix``)
only fires for a hotfix ROLLUP merge of a ``plan`` task → main. A ``create`` or
``fix`` hotfix has no plan and therefore no rollup, so its PR would merge
straight to main with NO main → automain back-merge — silently diverging the
two branches and breaking the next promotion.

``create_task_manually`` must reject ``hotfix=true`` for any non-plan task_type
with an HTTP 400.
"""

import pytest
from httpx import AsyncClient

from app.models import Project, ProjectMember, ProjectStatus


async def _make_project_with_member(db_session, repo="acme/widget"):
    """Project owned by test-user-123 for the given owner/repo."""
    owner, name = repo.split("/", 1)
    project = Project(
        name=repo,
        user_id="test-user-123",
        status=ProjectStatus.DEPLOYING,
        github_owner=owner,
        github_repo=name,
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)
    db_session.add(
        ProjectMember(project_id=project.id, user_id="test-user-123", role="owner")
    )
    await db_session.commit()
    return project


@pytest.mark.asyncio
async def test_hotfix_create_task_rejected_with_400(client: AsyncClient, db_session):
    """hotfix=true on a create task → 400 (no plan → no rollup → no back-merge)."""
    await _make_project_with_member(db_session, "acme/widget")

    resp = await client.post(
        "/api/admin/tasks",
        json={
            "github_repo": "acme/widget",
            "title": "Hotfix via create",
            "prompt": "patch the bug",
            "base_branch": "main",
            "task_type": "create",
            "hotfix": True,
        },
    )

    assert resp.status_code == 400, resp.text
    # The app wraps HTTPException as {"error": {"code", "message"}} (main.py
    # http_exception_handler) — there is no top-level "detail" key.
    detail = resp.json()["error"]["message"].lower()
    assert "hotfix" in detail
    assert "plan" in detail


@pytest.mark.asyncio
async def test_hotfix_fix_task_rejected_with_400(client: AsyncClient, db_session):
    """hotfix=true on a fix task → 400. pr_number/branch are supplied so the
    request clears Pydantic validation and actually reaches the hotfix guard."""
    await _make_project_with_member(db_session, "acme/widget")

    resp = await client.post(
        "/api/admin/tasks",
        json={
            "github_repo": "acme/widget",
            "title": "Hotfix via fix",
            "prompt": "patch the bug",
            "base_branch": "main",
            "task_type": "fix",
            "pr_number": 123,
            "branch": "blueprint/task-abc",
            "hotfix": True,
        },
    )

    assert resp.status_code == 400, resp.text
    # See note above: HTTPException detail is at error.message, not "detail".
    assert "hotfix" in resp.json()["error"]["message"].lower()


@pytest.mark.asyncio
async def test_panel_review_task_requires_branch(client: AsyncClient, db_session):
    """panel_review manual creation must reject missing branch before persistence."""
    await _make_project_with_member(db_session, "acme/widget")

    resp = await client.post(
        "/api/admin/tasks",
        json={
            "github_repo": "acme/widget",
            "title": "Panel review without branch",
            "prompt": "review this PR",
            "task_type": "panel_review",
            "pr_number": 123,
        },
    )

    assert resp.status_code == 422, resp.text
    assert "branch is required for task_type 'panel_review'" in resp.text


@pytest.mark.asyncio
async def test_hotfix_plan_task_accepted(client: AsyncClient, db_session):
    """hotfix=true on a plan task is allowed — the documented hotfix path —
    and the hotfix flag is persisted in the stored task payload so the agent
    handler and the Phase 5 back-merge trigger can see it."""
    await _make_project_with_member(db_session, "acme/widget")

    resp = await client.post(
        "/api/admin/tasks",
        json={
            "github_repo": "acme/widget",
            "title": "Real hotfix",
            "prompt": "expedited fix off main",
            "base_branch": "main",
            "task_type": "plan",
            "hotfix": True,
        },
    )

    assert resp.status_code == 200, resp.text
    task_id = resp.json()["id"]

    # The hotfix flag must round-trip into the stored payload.
    get_resp = await client.get(f"/api/admin/tasks/{task_id}")
    assert get_resp.status_code == 200
    payloads = get_resp.json()["payloads"]
    assert payloads[0]["type"] == "plan"
    assert payloads[0]["hotfix"] is True


@pytest.mark.asyncio
async def test_omitted_base_branch_uses_project_promotion_target(
    client: AsyncClient, db_session
):
    """When base_branch is omitted, the resolved payload uses the project's promotion_target."""
    owner, name = "acme", "trunk-project"
    project = Project(
        name=f"{owner}/{name}",
        user_id="test-user-123",
        status=ProjectStatus.DEPLOYING,
        github_owner=owner,
        github_repo=name,
        promotion_target="trunk",
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)
    db_session.add(
        ProjectMember(project_id=project.id, user_id="test-user-123", role="owner")
    )
    await db_session.commit()

    resp = await client.post(
        "/api/admin/tasks",
        json={
            "github_repo": f"{owner}/{name}",
            "title": "Task without base_branch",
            "prompt": "do something useful",
            "task_type": "create",
            # base_branch intentionally omitted
        },
    )

    assert resp.status_code == 200, resp.text
    task_id = resp.json()["id"]

    get_resp = await client.get(f"/api/admin/tasks/{task_id}")
    assert get_resp.status_code == 200
    stored_base_branch = get_resp.json()["payloads"][0]["base_branch"]
    assert stored_base_branch == "trunk", (
        f"Expected 'trunk' (project promotion_target) but got {stored_base_branch!r}"
    )


@pytest.mark.asyncio
async def test_explicit_base_branch_preserved(client: AsyncClient, db_session):
    """When base_branch is explicitly supplied, it overrides the project promotion_target."""
    owner, name = "acme", "explicit-branch-project"
    project = Project(
        name=f"{owner}/{name}",
        user_id="test-user-123",
        status=ProjectStatus.DEPLOYING,
        github_owner=owner,
        github_repo=name,
        promotion_target="trunk",
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)
    db_session.add(
        ProjectMember(project_id=project.id, user_id="test-user-123", role="owner")
    )
    await db_session.commit()

    resp = await client.post(
        "/api/admin/tasks",
        json={
            "github_repo": f"{owner}/{name}",
            "title": "Task with explicit base_branch",
            "prompt": "work on main",
            "task_type": "create",
            "base_branch": "main",
        },
    )

    assert resp.status_code == 200, resp.text
    task_id = resp.json()["id"]

    get_resp = await client.get(f"/api/admin/tasks/{task_id}")
    assert get_resp.status_code == 200
    stored_base_branch = get_resp.json()["payloads"][0]["base_branch"]
    assert stored_base_branch == "main", (
        f"Expected explicit 'main' but got {stored_base_branch!r}"
    )


@pytest.mark.asyncio
async def test_non_hotfix_create_task_still_works(client: AsyncClient, db_session):
    """Regression guard: the hotfix check must not block an ordinary create
    task. A create task with no hotfix flag succeeds and stores NO hotfix key."""
    await _make_project_with_member(db_session, "acme/widget")

    resp = await client.post(
        "/api/admin/tasks",
        json={
            "github_repo": "acme/widget",
            "title": "Ordinary create",
            "prompt": "build the feature",
            "base_branch": "main",
            "task_type": "create",
        },
    )

    assert resp.status_code == 200, resp.text
    task_id = resp.json()["id"]

    get_resp = await client.get(f"/api/admin/tasks/{task_id}")
    assert get_resp.status_code == 200
    payloads = get_resp.json()["payloads"]
    assert payloads[0]["type"] == "create"
    assert "hotfix" not in payloads[0]


# --- Non-launchable task-type guard (reject doomed compute_experiment) -------
#
# The manual creation path only carries the generic CreateTaskPayload fields; it
# cannot supply the launch contract a compute_experiment needs (non-empty
# command + positive spend_cap_usd + positive deadline_seconds). The endpoint
# must reject any task_type the capability registry declares non-launchable BEFORE
# any Task row is created, rather than enqueueing a task that only fails later on
# the agent. The gate is registry-driven (USER_LAUNCHABLE_TASK_TYPES), so it
# generalizes to every non-launchable type, not just compute_experiment.


async def _count_tasks(db_session, project_id) -> int:
    from sqlalchemy import func, select

    from app.models import Task

    return (
        await db_session.execute(
            select(func.count(Task.id)).where(Task.project_id == project_id)
        )
    ).scalar_one()


@pytest.mark.asyncio
async def test_compute_experiment_rejected_at_creation(
    client: AsyncClient, db_session
):
    """compute_experiment is not user_launchable → 400, and no Task is created."""
    project = await _make_project_with_member(db_session, "acme/widget")

    resp = await client.post(
        "/api/admin/tasks",
        json={
            "github_repo": "acme/widget",
            "title": "Doomed compute experiment",
            "prompt": "run the experiment",
            "base_branch": "main",
            "task_type": "compute_experiment",
        },
    )

    assert resp.status_code == 400, resp.text
    # HTTPException detail surfaces at error.message (main.py http_exception_handler).
    message = resp.json()["error"]["message"].lower()
    assert "compute_experiment" in message
    assert "launchable" in message
    # The doomed task must never have been persisted.
    assert await _count_tasks(db_session, project.id) == 0


@pytest.mark.asyncio
async def test_non_launchable_type_rejected(client: AsyncClient, db_session):
    """A different non-launchable type (memo) is rejected by the same registry
    gate — proving the guard is registry-driven, not a compute_experiment
    special case."""
    project = await _make_project_with_member(db_session, "acme/widget")

    resp = await client.post(
        "/api/admin/tasks",
        json={
            "github_repo": "acme/widget",
            "title": "Memo via manual path",
            "prompt": "reason about a topic",
            "base_branch": "main",
            "task_type": "memo",
        },
    )

    assert resp.status_code == 400, resp.text
    assert "memo" in resp.json()["error"]["message"].lower()
    assert await _count_tasks(db_session, project.id) == 0


@pytest.mark.asyncio
async def test_user_launchable_type_still_accepted(client: AsyncClient, db_session):
    """Regression guard: the non-launchable gate must not over-reject. A
    user_launchable type (research) is still accepted and persisted."""
    project = await _make_project_with_member(db_session, "acme/widget")

    resp = await client.post(
        "/api/admin/tasks",
        json={
            "github_repo": "acme/widget",
            "title": "Codebase research",
            "prompt": "analyze the codebase",
            "base_branch": "main",
            "task_type": "research",
        },
    )

    assert resp.status_code == 200, resp.text
    task_id = resp.json()["id"]
    get_resp = await client.get(f"/api/admin/tasks/{task_id}")
    assert get_resp.status_code == 200
    assert get_resp.json()["payloads"][0]["type"] == "research"
    assert await _count_tasks(db_session, project.id) == 1

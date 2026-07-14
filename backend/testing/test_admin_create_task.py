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


async def _make_project_with_member(db_session, repo="acme/widget", role="owner"):
    """Project for the given owner/repo with test-user-123 as a member.

    ``role`` defaults to ``owner``; pass ``viewer``/``member`` to exercise the
    role floor on the existing-project creation path.
    """
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
        ProjectMember(project_id=project.id, user_id="test-user-123", role=role)
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


# --- Idempotency tests -------------------------------------------------------
#
# Manual task creation accepts an optional client-supplied idempotency_key.
# When provided, duplicate requests with the same key return the existing task
# instead of creating a new one. This protects against double-click, network
# retry, and proxy replay scenarios.


@pytest.mark.asyncio
async def test_idempotency_key_dedupes_duplicate_submit(
    client: AsyncClient, db_session
):
    """Two requests with the same idempotency_key return the same task."""
    await _make_project_with_member(db_session, "acme/widget")

    key = "my-unique-key-001"
    resp1 = await client.post(
        "/api/admin/tasks",
        json={
            "github_repo": "acme/widget",
            "title": "Idempotent task",
            "prompt": "do something",
            "task_type": "create",
            "idempotency_key": key,
        },
    )

    assert resp1.status_code == 200, resp1.text
    task_id_1 = resp1.json()["id"]
    # First response should not have is_duplicate flag (or it's False)
    assert not resp1.json().get("is_duplicate")

    # Retry with the same key
    resp2 = await client.post(
        "/api/admin/tasks",
        json={
            "github_repo": "acme/widget",
            "title": "Idempotent task",
            "prompt": "do something",
            "task_type": "create",
            "idempotency_key": key,
        },
    )

    assert resp2.status_code == 200, resp2.text
    task_id_2 = resp2.json()["id"]
    # Same task ID returned
    assert task_id_1 == task_id_2
    # Second response marked as duplicate
    assert resp2.json()["is_duplicate"] is True


@pytest.mark.asyncio
async def test_idempotency_key_no_second_task_created(
    client: AsyncClient, db_session
):
    """When idempotency_key dedupes, only one task is persisted."""
    project = await _make_project_with_member(db_session, "acme/widget")

    key = "my-unique-key-002"
    await client.post(
        "/api/admin/tasks",
        json={
            "github_repo": "acme/widget",
            "title": "First request",
            "prompt": "do work",
            "task_type": "create",
            "idempotency_key": key,
        },
    )

    await client.post(
        "/api/admin/tasks",
        json={
            "github_repo": "acme/widget",
            "title": "First request",
            "prompt": "do work",
            "task_type": "create",
            "idempotency_key": key,
        },
    )

    # Exactly one task created for this project
    task_count = await _count_tasks(db_session, project.id)
    assert task_count == 1


@pytest.mark.asyncio
async def test_without_idempotency_key_creates_duplicates(
    client: AsyncClient, db_session
):
    """Regression guard: without idempotency_key, identical requests create
    separate tasks (the old behavior that the fix addresses)."""
    project = await _make_project_with_member(db_session, "acme/widget")

    # First request without idempotency_key
    resp1 = await client.post(
        "/api/admin/tasks",
        json={
            "github_repo": "acme/widget",
            "title": "Unprotected task",
            "prompt": "do something",
            "task_type": "create",
        },
    )

    assert resp1.status_code == 200, resp1.text
    task_id_1 = resp1.json()["id"]

    # Second identical request without idempotency_key
    resp2 = await client.post(
        "/api/admin/tasks",
        json={
            "github_repo": "acme/widget",
            "title": "Unprotected task",
            "prompt": "do something",
            "task_type": "create",
        },
    )

    assert resp2.status_code == 200, resp2.text
    task_id_2 = resp2.json()["id"]

    # Different task IDs (separate tasks created)
    assert task_id_1 != task_id_2
    # No is_duplicate flag when idempotency_key not used
    assert not resp2.json().get("is_duplicate")

    # Two tasks persisted
    task_count = await _count_tasks(db_session, project.id)
    assert task_count == 2


@pytest.mark.asyncio
async def test_idempotency_key_scoped_per_project(client: AsyncClient, db_session):
    """A client key that collides across projects must NOT dedupe cross-project.

    The stored idempotency_key is namespaced with the project id, so the same
    user-supplied key in two different projects yields two distinct tasks and
    never leaks the foreign project's task fields (cross-project info disclosure).
    """
    project_a = await _make_project_with_member(db_session, "acme/widget")
    project_b = await _make_project_with_member(db_session, "acme/gadget")

    key = "shared-client-key-collision"
    resp_a = await client.post(
        "/api/admin/tasks",
        json={
            "github_repo": "acme/widget",
            "title": "Task in project A",
            "prompt": "do A",
            "task_type": "create",
            "idempotency_key": key,
        },
    )
    assert resp_a.status_code == 200, resp_a.text
    assert not resp_a.json().get("is_duplicate")

    resp_b = await client.post(
        "/api/admin/tasks",
        json={
            "github_repo": "acme/gadget",
            "title": "Task in project B",
            "prompt": "do B",
            "task_type": "create",
            "idempotency_key": key,
        },
    )
    assert resp_b.status_code == 200, resp_b.text
    # Same client key but different project → fresh task, no cross-project dedup
    assert not resp_b.json().get("is_duplicate")
    assert resp_a.json()["id"] != resp_b.json()["id"]
    assert resp_b.json()["project_id"] == str(project_b.id)

    # Each project has exactly its own single task
    assert await _count_tasks(db_session, project_a.id) == 1
    assert await _count_tasks(db_session, project_b.id) == 1


# --- Role floor on the existing-project path ---------------------------------
#
# Manual task creation resolves the target project by (owner, repo) scoped to the
# caller's memberships. Membership alone is NOT authorization to create work: a
# task executes code and consumes LLM/GPU budget, so the existing-project path
# requires the member+ floor (viewer is documented read-only). The auto-create
# branch is unaffected — a caller with no member-visible project for the repo
# becomes the owner of the brand-new project.


async def _count_projects_for_repo(db_session, owner, repo) -> int:
    from sqlalchemy import func, select

    return (
        await db_session.execute(
            select(func.count(Project.id)).where(
                Project.github_owner == owner, Project.github_repo == repo
            )
        )
    ).scalar_one()


@pytest.mark.asyncio
async def test_viewer_cannot_create_task_on_existing_project(
    client: AsyncClient, db_session
):
    """A viewer on the target project gets 403 and no task is enqueued."""
    project = await _make_project_with_member(db_session, "acme/widget", role="viewer")

    resp = await client.post(
        "/api/admin/tasks",
        json={
            "github_repo": "acme/widget",
            "title": "Viewer-enqueued work",
            "prompt": "spend the budget",
            "base_branch": "main",
            "task_type": "create",
        },
    )

    assert resp.status_code == 403, resp.text
    # HTTPException detail surfaces at error.message (main.py http_exception_handler).
    assert "member" in resp.json()["error"]["message"].lower()
    # No task enqueued...
    assert await _count_tasks(db_session, project.id) == 0
    # ...and the rejection must NOT have fallen through into the auto-create
    # branch, which would hand the viewer a shadow project they own.
    assert await _count_projects_for_repo(db_session, "acme", "widget") == 1


@pytest.mark.asyncio
async def test_member_can_create_task_on_existing_project(
    client: AsyncClient, db_session
):
    """The member floor must not over-reject: a member (not owner) can create."""
    project = await _make_project_with_member(db_session, "acme/widget", role="member")

    resp = await client.post(
        "/api/admin/tasks",
        json={
            "github_repo": "acme/widget",
            "title": "Member-enqueued work",
            "prompt": "do the work",
            "base_branch": "main",
            "task_type": "create",
        },
    )

    assert resp.status_code == 200, resp.text
    assert await _count_tasks(db_session, project.id) == 1


@pytest.mark.asyncio
async def test_ambiguous_repo_prefers_writable_project_over_viewer_one(
    client: AsyncClient, db_session
):
    """Two projects for one repo (viewer on A, owner of B) → create on B, not 403.

    ``ix_project_github_owner_repo`` is NOT unique and the auto-create branch
    deliberately mints a project per (caller, repo), so a user can legitimately
    be a viewer on one project for ``acme/widget`` and the owner of another. A
    membership-scoped ``.scalars().first()`` with no ORDER BY picks either row at
    random; checking the role only AFTER that pick would 403 the caller roughly
    half the time — randomly locking them out of creating tasks on their OWN
    project. The resolver must look for a member+ project FIRST.
    """
    viewer_project = await _make_project_with_member(
        db_session, "acme/widget", role="viewer"
    )
    owner_project = await _make_project_with_member(
        db_session, "acme/widget", role="owner"
    )
    assert viewer_project.id != owner_project.id
    assert await _count_projects_for_repo(db_session, "acme", "widget") == 2

    # Run several times: a nondeterministic pick would flake, not fail outright.
    for i in range(4):
        resp = await client.post(
            "/api/admin/tasks",
            json={
                "github_repo": "acme/widget",
                "title": f"Work on my own project {i}",
                "prompt": "do the work",
                "base_branch": "main",
                "task_type": "create",
            },
        )
        assert resp.status_code == 200, resp.text

    # Every task landed on the project the caller can WRITE to...
    assert await _count_tasks(db_session, owner_project.id) == 4
    # ...none on the viewer-only one...
    assert await _count_tasks(db_session, viewer_project.id) == 0
    # ...and no shadow project was minted.
    assert await _count_projects_for_repo(db_session, "acme", "widget") == 2

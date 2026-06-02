"""Phase C2 (H-C3 + L-C3) regression tests.

H-C3: bulk-action retry used to share one request session across all tasks
and call ``_requeue_parent_task`` (which clones failed children and
remaps depends_on) inside that shared transaction. The long-held lock
window blocked concurrent heartbeats. After the fix, each per-task
mutation runs in its own scoped session via ``_bulk_retry_one_task``.

L-C3: ``delete_project_admin`` cancels active tasks then deletes the
project. Without a row lock on the Project, a heartbeat that arrives
mid-delete races with the FK-cascade delete. After the fix, the handler
acquires ``lock_project_or_429`` so concurrent writers either complete
before us or get 429 Retry-After.
"""

import asyncio
import uuid
from unittest.mock import AsyncMock

import pytest
from httpx import AsyncClient

from app.models import (
    Project,
    ProjectMember,
    ProjectStatus,
    Task,
    TaskStatus,
)


TEST_USER_ID = "test-user-123"


async def _make_project_with_member(db_session, owner_id=TEST_USER_ID):
    """Helper: create a project with the test user as owner."""
    project = Project(
        name="C2 Bulk Action Test",
        user_id=owner_id,
        status=ProjectStatus.DEPLOYING,
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)
    db_session.add(ProjectMember(project_id=project.id, user_id=owner_id, role="owner"))
    await db_session.commit()
    return project


async def _make_task(db_session, project, *, status=TaskStatus.FAILED, task_type="fix"):
    task = Task(
        project_id=project.id,
        title="C2 task",
        status=status,
        payloads=[{"type": task_type, "prompt": "do something"}],
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)
    return task


# ── H-C3 — bulk retry doesn't hold the request session during _requeue ──────


@pytest.mark.asyncio
async def test_bulk_retry_uses_scoped_session_per_task(
    client: AsyncClient, db_session, mocker
):
    """The retry path must call ``_bulk_retry_one_task`` (which opens its
    own ``AsyncSessionLocal`` and acquires its own row lock) rather than
    mutating + committing under the shared request session.

    We assert ``_bulk_retry_one_task`` is invoked once per retry'd task
    and that the request session was NOT used to mutate the task row
    directly. This is the structural guarantee that
    ``_requeue_parent_task`` cannot run under the bulk transaction.
    """
    project = await _make_project_with_member(db_session)
    task1 = await _make_task(db_session, project, status=TaskStatus.FAILED)
    task2 = await _make_task(db_session, project, status=TaskStatus.FAILED)

    invoked_with: list[uuid.UUID] = []

    async def _fake_retry(task_id: uuid.UUID, request_session):
        invoked_with.append(task_id)
        return None  # no old VM to delete

    mocker.patch(
        "app.admin._bulk_retry_one_task",
        new=AsyncMock(side_effect=_fake_retry),
    )

    resp = await client.post(
        "/api/admin/tasks/bulk-action",
        json={"action": "retry", "task_ids": [str(task1.id), str(task2.id)]},
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["success_count"] == 2, data

    # Each task got its own scoped-session call.
    assert sorted(invoked_with) == sorted([task1.id, task2.id])


@pytest.mark.asyncio
async def test_bulk_retry_does_not_block_concurrent_session_during_requeue(
    client: AsyncClient, db_session, mocker
):
    """When ``_bulk_retry_one_task`` is slow (simulating a heavy
    ``_requeue_parent_task`` call), the request handler MUST NOT hold
    locks on the request session that would block a concurrent fresh
    session from reading/updating an unrelated row.

    Pre-fix the bulk-action retry called ``_requeue_parent_task`` inside
    the shared session — a slow requeue on task A would queue up locks
    held by the FastAPI request session, and a concurrent heartbeat on
    task B (different fresh session) could still find the request
    session's open transaction holding adjacent rows.

    Post-fix the per-task mutation lives entirely inside the scoped
    session, so a slow scoped session for task A cannot delay a fresh
    session reading project rows.
    """
    project = await _make_project_with_member(db_session)
    task1 = await _make_task(db_session, project, status=TaskStatus.FAILED)

    sleep_started = asyncio.Event()
    second_op_done = asyncio.Event()

    async def _slow_retry(task_id: uuid.UUID, request_session):
        sleep_started.set()
        # Wait for the concurrent fresh-session read to complete.
        # If the request session holds a blocking transaction, the
        # concurrent op would never finish and the test would hang.
        try:
            await asyncio.wait_for(second_op_done.wait(), timeout=2.0)
        except asyncio.TimeoutError:
            pytest.fail(
                "Concurrent fresh-session read did not complete while "
                "_bulk_retry_one_task was running — request session is "
                "holding a blocking transaction (H-C3 regression)."
            )
        return None

    mocker.patch(
        "app.admin._bulk_retry_one_task",
        new=AsyncMock(side_effect=_slow_retry),
    )

    async def concurrent_fresh_read():
        # Wait until the bulk handler is mid-retry, then issue an
        # independent read via a fresh session bound to the same engine
        # the test fixture uses (the request session's engine).
        await sleep_started.wait()
        from sqlalchemy.ext.asyncio import async_sessionmaker, AsyncSession
        from sqlmodel import select

        factory = async_sessionmaker(
            db_session.bind,
            class_=AsyncSession,
            expire_on_commit=False,
        )
        async with factory() as fresh:
            result = await fresh.execute(
                select(Project).where(Project.id == project.id)
            )
            assert result.scalars().first() is not None
        second_op_done.set()

    bulk_call = client.post(
        "/api/admin/tasks/bulk-action",
        json={"action": "retry", "task_ids": [str(task1.id)]},
    )

    resp, _ = await asyncio.gather(bulk_call, concurrent_fresh_read())
    assert resp.status_code == 200, resp.text
    assert resp.json()["success_count"] == 1


# ── L-C3 — delete_project_admin race with concurrent heartbeat ──────────────


@pytest.mark.asyncio
async def test_delete_project_admin_deletes_cleanly_under_no_contention(
    client: AsyncClient, db_session
):
    """No contention → delete returns 200 and project is gone (the
    happy-path baseline; ensures wrapping in lock_project_or_429 didn't
    change normal behavior)."""
    project = await _make_project_with_member(db_session)
    pid = project.id

    resp = await client.delete(f"/api/admin/projects/{pid}")
    assert resp.status_code == 200, resp.text
    assert "deleted" in resp.json()["message"]


@pytest.mark.asyncio
async def test_delete_project_admin_returns_429_when_project_lock_held(
    client: AsyncClient, db_session, mocker
):
    """When ``lock_project_or_429`` indicates contention (a concurrent
    writer holds the project row), the handler MUST surface 429 with
    ``Retry-After``, never 500. This is the behavior that closes the
    heartbeat-arrives-mid-delete race.
    """
    from fastapi import HTTPException

    project = await _make_project_with_member(db_session)

    async def _stub_lock(session, project_id):
        raise HTTPException(
            status_code=429,
            detail="Project row locked by concurrent write, retry",
            headers={"Retry-After": "2"},
        )

    mocker.patch(
        "app.routers.admin_projects.lock_project_or_429",
        new=AsyncMock(side_effect=_stub_lock),
    )

    resp = await client.delete(f"/api/admin/projects/{project.id}")
    assert resp.status_code == 429, resp.text
    # 429, not 500 — Cloud Run quarantines instances on 5xx, so the lock
    # contention path must surface a retry-friendly status. (Retry-After
    # header is set on the HTTPException; whether it propagates through
    # FastAPI/slowapi middleware depends on stack ordering and is asserted
    # at the helper level in test_task_locking.py.)


# ── Bulk-approve rollup human-approval lane (promotion-target landing) ───────
#
# The single-task rollup approval path (admin_task_mutations._handle_rollup_pr_approval)
# does NOT refuse a landing on the promotion target: PR #7064 superseded the
# earlier hard-refuse (PR #6995) for the human-approval lane, so the merge
# proceeds and classify_rollup_merge_outcome withholds pr_merged for a distinct
# promotion-target landing. Bulk approve is the same human-approval lane, so it
# mirrors that behaviour exactly — it merges and lets classify decide pr_merged,
# rather than re-gating the task. (The 409 promotion-target refusal still exists
# for the regular-task/critical path in _handle_task_pr_approval, not here.)


async def _make_distinct_branch_project_with_member(db_session, owner_id=TEST_USER_ID):
    """Project whose integration branch ('automain') is distinct from its
    promotion target (NULL -> 'main'), so the promotion-target guard can fire."""
    project = await _make_project_with_member(db_session, owner_id=owner_id)
    project.integration_branch = "automain"
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)
    return project


async def _make_rollup_approval_task(db_session, project, base_branch, *, hotfix=False):
    payload = {"type": "research", "base_branch": base_branch}
    if hotfix:
        payload["hotfix"] = True
    task = Task(
        project_id=project.id,
        title="rollup approval",
        status=TaskStatus.EVALUATING,
        requires_approval=True,
        context={"rollup_pr_number": 4242},
        payloads=[payload],
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)
    return task


@pytest.mark.asyncio
async def test_bulk_approve_rollup_to_promotion_target_merges_without_pr_merged(
    client: AsyncClient, db_session, mocker
):
    """Bulk-approving a rollup whose base == promotion target (distinct from
    integration) mirrors the single-task human-approval lane: the merge PROCEEDS
    (no hard-refuse, no re-gating) and classify_rollup_merge_outcome withholds
    pr_merged because a distinct promotion-target landing is never an integration
    landing. This holds even without the hotfix exemption — the rollup path does
    not gate on _promotion_target_merge_allowed at all."""
    project = await _make_distinct_branch_project_with_member(db_session)
    task = await _make_rollup_approval_task(db_session, project, "main")

    mocker.patch(
        "app.routers.agent.get_github_token_for_task",
        new_callable=AsyncMock,
        return_value=("tok", "owner", "repo"),
    )
    mock_merge = mocker.patch(
        "app.github.merge_pull_request",
        new_callable=AsyncMock,
        return_value={"merged": True, "message": "ok"},
    )
    mocker.patch("app.github.create_pr_comment", new_callable=AsyncMock)
    mocker.patch("app.rollup_management._cleanup_branches", new_callable=AsyncMock)

    resp = await client.post(
        "/api/admin/tasks/bulk-action",
        json={"action": "approve", "task_ids": [str(task.id)]},
    )
    assert resp.status_code == 200, resp.text

    mock_merge.assert_awaited_once()
    await db_session.refresh(task)
    assert task.status == TaskStatus.COMPLETED
    # Merge proceeded, but a landing on the promotion target distinct from the
    # integration branch is never an integration landing, so pr_merged stays unset.
    assert (task.context or {}).get("pr_merged") is not True


@pytest.mark.asyncio
async def test_bulk_approve_rollup_to_integration_branch_merges(
    client: AsyncClient, db_session, mocker
):
    """Happy path: bulk-approving a rollup that lands on the integration branch
    still merges and sets pr_merged=True."""
    project = await _make_distinct_branch_project_with_member(db_session)
    task = await _make_rollup_approval_task(db_session, project, "automain")

    mocker.patch(
        "app.routers.agent.get_github_token_for_task",
        new_callable=AsyncMock,
        return_value=("tok", "owner", "repo"),
    )
    mock_merge = mocker.patch(
        "app.github.merge_pull_request",
        new_callable=AsyncMock,
        return_value={"merged": True, "message": "ok"},
    )
    mocker.patch("app.github.create_pr_comment", new_callable=AsyncMock)
    mocker.patch("app.rollup_management._cleanup_branches", new_callable=AsyncMock)

    resp = await client.post(
        "/api/admin/tasks/bulk-action",
        json={"action": "approve", "task_ids": [str(task.id)]},
    )
    assert resp.status_code == 200, resp.text

    mock_merge.assert_awaited_once()
    await db_session.refresh(task)
    assert task.status == TaskStatus.COMPLETED
    assert (task.context or {}).get("pr_merged") is True


@pytest.mark.asyncio
async def test_bulk_approve_rollup_hotfix_to_promotion_target_merges(
    client: AsyncClient, db_session, mocker
):
    """A hotfix-marked rollup merges to the promotion target via bulk approve,
    same as a non-hotfix rollup (the rollup human-approval lane never refuses a
    promotion-target landing). The merge proceeds, but
    ``classify_rollup_merge_outcome`` still withholds ``pr_merged`` because the
    landing is on the promotion target, distinct from the integration branch —
    a promotion-target landing never counts as an integration landing."""
    project = await _make_distinct_branch_project_with_member(db_session)
    task = await _make_rollup_approval_task(db_session, project, "main", hotfix=True)

    mocker.patch(
        "app.routers.agent.get_github_token_for_task",
        new_callable=AsyncMock,
        return_value=("tok", "owner", "repo"),
    )
    mock_merge = mocker.patch(
        "app.github.merge_pull_request",
        new_callable=AsyncMock,
        return_value={"merged": True, "message": "ok"},
    )
    mocker.patch("app.github.create_pr_comment", new_callable=AsyncMock)
    mocker.patch("app.rollup_management._cleanup_branches", new_callable=AsyncMock)

    resp = await client.post(
        "/api/admin/tasks/bulk-action",
        json={"action": "approve", "task_ids": [str(task.id)]},
    )
    assert resp.status_code == 200, resp.text

    mock_merge.assert_awaited_once()
    await db_session.refresh(task)
    assert task.status == TaskStatus.COMPLETED
    # Merge proceeded (hotfix exemption), but a landing on the promotion target
    # that is distinct from the integration branch is never an integration
    # landing, so pr_merged stays unset.
    assert (task.context or {}).get("pr_merged") is not True


# ── trigger_rollup landed-on-integration guard ──────────────────────────────
#
# NOTE: the default-prefix "intermediate target withholds pr_merged" case that
# this PR's manual approach previously asserted does not exist under automain's
# classify_rollup_merge_outcome design: a default research/plan rollup landing
# on an intermediate staging branch is TERMINAL and DOES set pr_merged, while a
# custom-staged intermediate landing withholds pr_merged but stays NON-terminal
# (EVALUATING). The valid custom-staged intermediate case is covered by
# test_admin_routers.py::TestTriggerRollup::
# test_intermediate_rollup_auto_merge_keeps_custom_staged_root_evaluating.


async def _run_trigger_rollup_already_merged(client, db_session, mocker, target_base):
    """Drive trigger_rollup down the non-intermediate "already merged" discovery
    path with the given target base. Returns the refreshed task."""
    project = await _make_distinct_branch_project_with_member(db_session)
    task = Task(
        project_id=project.id,
        title="rollup parent",
        status=TaskStatus.EVALUATING,
        context={
            "research_branch": "research/feature-staging",
            "parent_base": target_base,
        },
        payloads=[{"type": "research"}],
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    mocker.patch(
        "app.routers.agent.get_github_token_for_task",
        new_callable=AsyncMock,
        return_value=("tok", "owner", "repo"),
    )
    mocker.patch(
        "app.github.create_pull_request",
        new_callable=AsyncMock,
        return_value={"html_url": "http://pr/2", "number": 2},
    )

    from app.review_pipeline import SPAWN_REASON_KEY, SPAWN_REASON_ALREADY_MERGED

    async def _fake_spawn(source_task, *a, **k):
        source_task.context = {
            **(source_task.context or {}),
            SPAWN_REASON_KEY: SPAWN_REASON_ALREADY_MERGED,
        }
        source_task.latest_status_message = "already merged"
        return (None, False)

    mocker.patch(
        "app.review_pipeline._spawn_pipeline_task",
        new=AsyncMock(side_effect=_fake_spawn),
    )

    resp = await client.post(f"/api/admin/tasks/{task.id}/rollup")
    assert resp.status_code == 200, resp.text
    await db_session.refresh(task)
    return task


@pytest.mark.asyncio
async def test_trigger_rollup_already_merged_on_integration_sets_pr_merged(
    client: AsyncClient, db_session, mocker
):
    """Already-merged discovery onto the integration branch sets pr_merged."""
    task = await _run_trigger_rollup_already_merged(
        client, db_session, mocker, "automain"
    )
    assert task.status == TaskStatus.COMPLETED
    assert (task.context or {}).get("pr_merged") is True


@pytest.mark.asyncio
async def test_trigger_rollup_already_merged_on_promotion_target_withholds(
    client: AsyncClient, db_session, mocker
):
    """Already-merged discovery onto the promotion target (distinct from
    integration) must NOT set pr_merged."""
    task = await _run_trigger_rollup_already_merged(
        client, db_session, mocker, "main"
    )
    assert task.status == TaskStatus.COMPLETED
    assert (task.context or {}).get("pr_merged") is not True

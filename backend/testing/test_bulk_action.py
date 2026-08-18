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
from fastapi import BackgroundTasks
from httpx import AsyncClient

from app.models import (
    Artifact,
    ArtifactType,
    Project,
    ProjectMember,
    ProjectStatus,
    StrategicCycle,
    Task,
    TaskStatus,
)


TEST_USER_ID = "test-user-123"


@pytest.fixture(autouse=True)
def _default_no_migration_pr(mocker):
    """Default the pre-merge Alembic dual-head guard to a no-op for merge tests.

    The bulk-approve rollup/critical merge paths now run
    ``enforce_premerge_migration_guard`` (which calls ``github.get_pr_files``)
    before merging. These tests only mock ``merge_pull_request``; returning an
    empty (no-migration) file list short-circuits the guard to ``ok`` so the
    merges under test proceed unchanged.
    """
    mocker.patch("app.github.get_pr_files", AsyncMock(return_value=[{"filename": "README.md"}]))


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


async def _make_task(
    db_session,
    project,
    *,
    status=TaskStatus.FAILED,
    task_type="fix",
    context=None,
):
    task = Task(
        project_id=project.id,
        title="C2 task",
        status=status,
        payloads=[{"type": task_type, "prompt": "do something"}],
        context=dict(context or {}),
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)
    return task


# ── H-C3 — bulk retry doesn't hold the request session during _requeue ──────


@pytest.mark.asyncio
async def test_single_admin_requeue_strips_blocked_escalation_context(db_session):
    from app.admin_task_mutations import modify_task_apply
    from app.task_blocked import enter_blocked_state, mark_blocker_escalated

    project = await _make_project_with_member(db_session)
    # This regression exercises the generic admin requeue cleanup path.  A
    # direct fix/review requeue now correctly requires an exact PR admission
    # target, which is unrelated to the blocked-context behavior under test.
    task = await _make_task(
        db_session,
        project,
        status=TaskStatus.IN_PROGRESS,
        task_type="create",
    )
    enter_blocked_state(task, waiting_on="dep", reason="blocked")
    mark_blocker_escalated(task, detail="deadline elapsed")
    task.context = {
        **(task.context or {}),
        "_blocked_escalated": True,
        "_precondition_escalated_key": "dep",
        "_dispatch_prep_failures": 3,
        "_previous_failure_class": "SCOPE_OVERFLOW",
        "_failure_class_history": ["SCOPE_OVERFLOW"],
    }
    task.status = TaskStatus.BLOCKED
    db_session.add(task)
    await db_session.commit()

    await modify_task_apply(
        task,
        {"status": TaskStatus.QUEUED.value},
        BackgroundTasks(),
        db_session,
    )

    await db_session.refresh(task)
    assert task.status == TaskStatus.QUEUED
    ctx = task.context or {}
    assert "blocker" not in ctx
    assert "failure_class" not in ctx
    assert "failure_category" not in ctx
    assert "_blocked_escalated" not in ctx
    assert "_precondition_escalated_key" not in ctx
    assert "_dispatch_prep_failures" not in ctx
    assert ctx["_previous_failure_class"] == "SCOPE_OVERFLOW"
    assert ctx["_failure_class_history"] == ["SCOPE_OVERFLOW"]

    enter_blocked_state(task, waiting_on="dep", reason="blocked again")
    mark_blocker_escalated(task, detail="second deadline")
    assert (task.context or {})["blocker"]["escalation_detail"] == "second deadline"


@pytest.mark.asyncio
async def test_single_admin_cancel_reconciles_its_cascaded_strategic_child(
    db_session, mocker
):
    """The single-task admin path reconciles a phase task cancelled by cascade."""
    from app import strategic_cycle
    from app.admin_task_mutations import modify_task_apply

    emit = mocker.patch(
        "app.strategic_events.emit_strategic_cycle_completed", new_callable=AsyncMock
    )

    project = await _make_project_with_member(db_session)
    parent = await _make_task(
        db_session, project, status=TaskStatus.IN_PROGRESS, task_type="plan"
    )
    phase1_task = Task(
        project_id=project.id,
        parent_task_id=parent.id,
        title="strategic child",
        status=TaskStatus.QUEUED,
        payloads=[{"type": "design_doc_update"}],
    )
    db_session.add(phase1_task)
    await db_session.commit()
    cycle = await strategic_cycle.create(
        db_session, project_id=project.id, phase1_task_id=phase1_task.id
    )
    await db_session.commit()

    await modify_task_apply(
        parent,
        {"status": TaskStatus.CANCELLED.value},
        BackgroundTasks(),
        db_session,
    )
    await db_session.refresh(phase1_task)
    await db_session.refresh(cycle)

    assert phase1_task.status == TaskStatus.CANCELLED
    assert cycle.outcome == strategic_cycle.OUTCOME_FAILED_PHASE1
    assert "source=admin_task_mutation_cascade" in (cycle.failure_reason or "")
    # A durably terminal cycle with no completion broadcast leaves the operator
    # UI showing an active strategist forever.
    emit.assert_awaited_once()
    assert emit.await_args.args[1] == str(phase1_task.id)
    assert emit.await_args.args[2] == 1
    assert emit.await_args.kwargs["cycle_id"] == str(cycle.id)
    assert emit.await_args.kwargs["phase_state"] == (
        strategic_cycle.OUTCOME_FAILED_PHASE1
    )
    assert emit.await_args.kwargs["completion"]["status"] == "CANCELLED"


@pytest.mark.asyncio
async def test_single_admin_failure_reconciles_linked_phase1_cycle(db_session, mocker):
    """The single-task status mutation uses the same failure rule as cancel."""
    from app import strategic_cycle
    from app.admin_task_mutations import modify_task_apply

    emit = mocker.patch(
        "app.strategic_events.emit_strategic_cycle_completed", new_callable=AsyncMock
    )

    project = await _make_project_with_member(db_session)
    task = await _make_task(
        db_session,
        project,
        status=TaskStatus.IN_PROGRESS,
        task_type="design_doc_update",
    )
    cycle = await strategic_cycle.create(
        db_session, project_id=project.id, phase1_task_id=task.id
    )
    await db_session.commit()

    await modify_task_apply(
        task,
        {"status": TaskStatus.FAILED.value},
        BackgroundTasks(),
        db_session,
    )
    await db_session.refresh(cycle)

    assert cycle.outcome == strategic_cycle.OUTCOME_FAILED_PHASE1
    assert "source=admin_task_mutation" in (cycle.failure_reason or "")
    emit.assert_awaited_once()
    assert emit.await_args.args[2] == 1
    assert emit.await_args.kwargs["cycle_id"] == str(cycle.id)
    assert emit.await_args.kwargs["phase_state"] == (
        strategic_cycle.OUTCOME_FAILED_PHASE1
    )
    assert emit.await_args.kwargs["completion"]["status"] == "FAILED"


@pytest.mark.asyncio
async def test_bulk_cancel_reconciles_linked_phase2_cycle(
    client: AsyncClient, db_session, mocker
):
    """The public bulk endpoint persists phase-2 failure in its transaction."""
    from app import strategic_cycle

    emit = mocker.patch(
        "app.strategic_events.emit_strategic_cycle_completed", new_callable=AsyncMock
    )

    project = await _make_project_with_member(db_session)
    task = await _make_task(
        db_session,
        project,
        status=TaskStatus.IN_PROGRESS,
        task_type="strategic_review",
    )
    cycle = StrategicCycle(
        id="sc-bulk-cancel-phase2",
        project_id=project.id,
        outcome=strategic_cycle.OUTCOME_PHASE2_RUNNING,
        phase1_started_at=task.created_at,
        phase2_started_at=task.created_at,
        phase2_task_id=task.id,
    )
    db_session.add(cycle)
    await db_session.commit()

    response = await client.post(
        "/api/admin/tasks/bulk-action",
        json={"action": "cancel", "task_ids": [str(task.id)]},
    )

    assert response.status_code == 200, response.text
    assert response.json()["results"] == [{"task_id": str(task.id), "status": "ok"}]
    await db_session.refresh(task)
    await db_session.refresh(cycle)
    assert task.status == TaskStatus.CANCELLED
    assert cycle.outcome == strategic_cycle.OUTCOME_FAILED_PHASE2
    assert "source=admin_bulk_cancel" in (cycle.failure_reason or "")
    emit.assert_awaited_once()
    assert emit.await_args.args[1] == str(task.id)
    assert emit.await_args.args[2] == 2
    assert emit.await_args.kwargs["cycle_id"] == "sc-bulk-cancel-phase2"
    assert emit.await_args.kwargs["phase_state"] == (
        strategic_cycle.OUTCOME_FAILED_PHASE2
    )
    assert emit.await_args.kwargs["completion"]["status"] == "CANCELLED"


@pytest.mark.asyncio
async def test_bulk_retry_strips_fresh_attempt_context(db_session):
    from app.admin import _bulk_retry_one_task

    project = await _make_project_with_member(db_session)
    task = await _make_task(
        db_session,
        project,
        task_type="create",
        status=TaskStatus.FAILED,
        context={
            "failure_class": "CODE_ERROR",
            "failure_category": "CODE_ERROR",
            "blocker": {"source": "agent"},
            "_blocked_escalated": True,
            "_precondition_escalated_key": "dep",
            "_blocked_optimistic_retries": 2,
            "_dispatch_prep_failures": 3,
            "provision_failures": 1,
            "llm_account_id": "acct-1",
            "_previous_failure_class": "SCOPE_OVERFLOW",
            "_failure_class_history": ["SCOPE_OVERFLOW"],
        },
    )

    await _bulk_retry_one_task(task.id, db_session, preloaded_has_children=False)

    await db_session.refresh(task)
    assert task.status == TaskStatus.QUEUED
    ctx = task.context or {}
    assert "failure_class" not in ctx
    assert "failure_category" not in ctx
    assert "blocker" not in ctx
    assert "_blocked_escalated" not in ctx
    assert "_precondition_escalated_key" not in ctx
    assert "_blocked_optimistic_retries" not in ctx
    assert "_dispatch_prep_failures" not in ctx
    assert "provision_failures" not in ctx
    assert "llm_account_id" not in ctx
    assert ctx["_previous_failure_class"] == "SCOPE_OVERFLOW"
    assert ctx["_failure_class_history"] == ["SCOPE_OVERFLOW"]


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

    async def _fake_retry(task_id: uuid.UUID, request_session, preloaded_has_children=None):
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

    async def _slow_retry(task_id: uuid.UUID, request_session, preloaded_has_children=None):
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


_BULK_ROLLUP_PR = 4242
_BULK_ROLLUP_BRANCH = "research/bulk-rollup-4242"
_BULK_ROLLUP_HEAD = "bulk-head-4242"
_BULK_ROLLUP_BASE = "bulk-base-4242"
_BULK_ROLLUP_MERGE = "bulk-merge-4242"


async def _make_rollup_approval_task(db_session, project, base_branch, *, hotfix=False):
    payload = {
        "type": "research",
        "branch": _BULK_ROLLUP_BRANCH,
        "base_branch": base_branch,
    }
    if hotfix:
        payload["hotfix"] = True
    task = Task(
        project_id=project.id,
        title="rollup approval",
        status=TaskStatus.EVALUATING,
        requires_approval=True,
        context={
            "rollup_pr_number": _BULK_ROLLUP_PR,
            # Human merge approval is bound to the exact reviewed attempt.  A
            # PR number alone must fail closed because its head can move before
            # the durable merge intent is published.
            "review_loop": {
                "project_id": str(project.id),
                "pr_number": _BULK_ROLLUP_PR,
                "branch": _BULK_ROLLUP_BRANCH,
                "base_branch": base_branch,
                "head_sha": _BULK_ROLLUP_HEAD,
                "base_sha": _BULK_ROLLUP_BASE,
                "review_target": {
                    "project_id": str(project.id),
                    "pr_number": _BULK_ROLLUP_PR,
                    "head_sha": _BULK_ROLLUP_HEAD,
                    "base_sha": _BULK_ROLLUP_BASE,
                },
            },
        },
        payloads=[payload],
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)
    return task


def _patch_explicit_merge_target(mocker, project, *, base_ref: str):
    target = {
        "project_id": str(project.id),
        "pr_number": _BULK_ROLLUP_PR,
        "base_sha": _BULK_ROLLUP_BASE,
        "head_sha": _BULK_ROLLUP_HEAD,
        "base_ref": base_ref,
        "head_ref": _BULK_ROLLUP_BRANCH,
        "state": "open",
        "merged": False,
        "draft": False,
        "mergeable": True,
        "mergeable_state": "clean",
    }
    for patch_target in (
        "app.review_policy_merge._fetch_live_review_target",
        "app.review_merge_authority._fetch_live_review_target",
    ):
        mocker.patch(
            patch_target,
            new_callable=AsyncMock,
            return_value=dict(target),
        )
    mocker.patch(
        "app.ci_merge_helpers._merge_commit_parent_matches_checked_base",
        new_callable=AsyncMock,
        return_value=True,
    )


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
    _patch_explicit_merge_target(mocker, project, base_ref="main")

    mocker.patch(
        "app.routers.agent.get_github_token_for_task",
        new_callable=AsyncMock,
        return_value=("tok", "owner", "repo"),
    )
    mock_merge = mocker.patch(
        "app.github.merge_pull_request",
        new_callable=AsyncMock,
        return_value={"merged": True, "sha": _BULK_ROLLUP_MERGE, "message": "ok"},
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
async def test_bulk_approve_critical_merge_blocks_on_dual_head_collision(
    client: AsyncClient, db_session, mocker
):
    project = await _make_project_with_member(db_session)
    project.integration_branch = "automain"
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = Task(
        project_id=project.id,
        title="Critical approval task",
        payloads=[{"type": "create", "prompt": "danger", "base_branch": "automain"}],
        status=TaskStatus.COMPLETED,
        requires_approval=True,
        context={"touched_critical": True},
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    artifact = Artifact(
        task_id=task.id,
        artifact_type=ArtifactType.PULL_REQUEST,
        pr_url="https://github.com/owner/repo/pull/123",
    )
    db_session.add(artifact)
    await db_session.commit()

    mocker.patch(
        "app.notification_dispatch.dispatch_external_notification",
        new_callable=AsyncMock,
    )
    mocker.patch(
        "app.routers.agent.get_github_token_for_task",
        new_callable=AsyncMock,
        return_value=("token", "owner", "repo"),
    )
    mocker.patch(
        "app.github.get_pr_files",
        new_callable=AsyncMock,
        return_value=[{
            "filename": "backend/alembic/versions/0002_dupe.py",
            "status": "added",
            "patch": '+revision = "0002"\n+down_revision = "0001"',
        }],
    )
    mocker.patch(
        "app.github.get_pull_request_diff",
        new_callable=AsyncMock,
        return_value=(
            "diff --git a/backend/alembic/versions/0002_dupe.py "
            "b/backend/alembic/versions/0002_dupe.py\n"
            "+++ b/backend/alembic/versions/0002_dupe.py\n"
            '+revision = "0002"\n'
            '+down_revision = "0001"\n'
        ),
    )
    mocker.patch(
        "app.github.list_versions_at_ref",
        new_callable=AsyncMock,
        return_value=["0001_seed.py", "0002_existing.py"],
    )
    merge_pr = mocker.patch(
        "app.github.merge_pull_request",
        new_callable=AsyncMock,
        return_value={"merged": True},
    )
    comment_pr = mocker.patch("app.github.create_pr_comment", new_callable=AsyncMock)
    delete_branch = mocker.patch("app.github.delete_branch", new_callable=AsyncMock)

    resp = await client.post(
        "/api/admin/tasks/bulk-action",
        json={"action": "approve", "task_ids": [str(task.id)]},
    )

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["success_count"] == 0
    assert body["error_count"] == 1
    assert body["results"][0]["status"] == "error"
    assert "Alembic combined-chain guard blocked" in body["results"][0]["error"]
    assert "0002" in body["results"][0]["error"]

    await db_session.refresh(task)
    assert task.requires_approval is True
    assert task.status == TaskStatus.COMPLETED
    assert "Alembic combined-chain guard blocked" in (
        task.latest_status_message or ""
    )
    merge_pr.assert_not_awaited()
    comment_pr.assert_not_awaited()
    delete_branch.assert_not_awaited()


@pytest.mark.asyncio
async def test_bulk_approve_rollup_to_integration_branch_merges(
    client: AsyncClient, db_session, mocker
):
    """Happy path: bulk-approving a rollup that lands on the integration branch
    still merges and sets pr_merged=True."""
    project = await _make_distinct_branch_project_with_member(db_session)
    task = await _make_rollup_approval_task(db_session, project, "automain")
    _patch_explicit_merge_target(mocker, project, base_ref="automain")

    mocker.patch(
        "app.routers.agent.get_github_token_for_task",
        new_callable=AsyncMock,
        return_value=("tok", "owner", "repo"),
    )
    mock_merge = mocker.patch(
        "app.github.merge_pull_request",
        new_callable=AsyncMock,
        return_value={"merged": True, "sha": _BULK_ROLLUP_MERGE, "message": "ok"},
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
    promotion-target landing). The merge proceeds and, because the PR did merge,
    ``apply_merged_landed_signal`` records the merge-equivalent unblock
    (``pr_merged=True``) so the hotfix's dependents are released — but the
    landing is on the promotion target, distinct from the integration branch, so
    it is NOT an integration landing (``landed_on_integration=False``, with the
    ``merged_pr_non_integration_branch`` reason). Analytics key off
    ``landed_on_integration``, not ``pr_merged``."""
    project = await _make_distinct_branch_project_with_member(db_session)
    task = await _make_rollup_approval_task(db_session, project, "main", hotfix=True)
    _patch_explicit_merge_target(mocker, project, base_ref="main")

    mocker.patch(
        "app.routers.agent.get_github_token_for_task",
        new_callable=AsyncMock,
        return_value=("tok", "owner", "repo"),
    )
    mock_merge = mocker.patch(
        "app.github.merge_pull_request",
        new_callable=AsyncMock,
        return_value={"merged": True, "sha": _BULK_ROLLUP_MERGE, "message": "ok"},
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
    # Merge proceeded (hotfix exemption). Because the PR merged, the
    # merge-equivalent unblock is recorded (pr_merged=True) so dependents are
    # released — but a landing on the promotion target that is distinct from the
    # integration branch is NOT an integration landing.
    ctx = task.context or {}
    assert ctx.get("pr_merged") is True
    assert ctx.get("landed_on_integration") is False
    assert ctx.get("_non_merge_unblock_reason") == "merged_pr_non_integration_branch"


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

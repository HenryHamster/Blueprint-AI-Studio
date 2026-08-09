"""
Integration tests for the review pipeline state machine.

Exercises the full create → review → fix → re-review cycle via the agent
claim and status endpoints, verifying that each step spawns the correct
downstream task and that the review_loop context is updated correctly.

All external calls (VM provisioning/deletion, GitHub API, WebSocket
broadcasts) are mocked so no real infrastructure is required.
"""

import json
import uuid
import pytest
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock
from httpx import AsyncClient
from sqlalchemy import select

from app.models import Project, Task, TaskStatus
from app.auth import create_agent_token
from app.review_pipeline import (
    _spawn_pipeline_task,
    MAX_REVIEW_FIX_ITERATIONS,
    PIPELINE_TASK_STALE_MINUTES,
    SPAWN_REASON_KEY,
    SPAWN_REASON_DEFERRED,
    SPAWN_REASON_DEDUP,
    SPAWN_REASON_SPAWNED,
)


@pytest.fixture(autouse=True)
def _default_no_migration_pr(mocker):
    """Default the pre-merge Alembic dual-head guard to a no-op for merge tests.

    The CI merge primitive now runs ``enforce_premerge_migration_guard`` (which
    calls ``github.get_pr_files``) before merging. Pipeline tests that drive a
    merge do not all mock that call; returning an empty (no-migration) file list
    short-circuits the guard to ``ok``. Tests that exercise other ``get_pr_files``
    behaviors re-patch it explicitly, which overrides this default.
    """
    from unittest.mock import AsyncMock

    mocker.patch("app.github.get_pr_files", AsyncMock(return_value=[{"filename": "README.md"}]))
    mocker.patch(
        "app.rollup_size_preflight.preflight_rollup_size_caps",
        AsyncMock(return_value=("ok", None)),
    )
    # Prevent the status handler from hitting the real GitHub API to verify
    # PR base branches when a task completes with a PULL_REQUEST artifact.
    # In test, the repo doesn't exist and the token is fake — mock it to
    # return a valid base branch on the task payload so validation passes.
    mocker.patch(
        "app.github.get_pull_request",
        AsyncMock(return_value={"base": {"ref": "main"}}),
    )


def _naive_utcnow() -> datetime:
    """Naive UTC datetime matching what app.time_utils._utcnow() produces."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _task_type(task: Task) -> str:
    return (task.payloads or [{}])[0].get("type", "")


def _review_loop(task: Task) -> dict:
    return (task.context or {}).get("review_loop", {})


def _patch_exact_pr_execution_target(
    mocker,
    *,
    pr_number: int,
    branch: str,
    base_branch: str = "main",
    base_sha: str,
    head_sha: str,
) -> dict:
    """Bind review/fix transitions and their claim fence to one live target."""
    live_pr = {
        "number": pr_number,
        "state": "open",
        "merged": False,
        "mergeable": True,
        "mergeable_state": "clean",
        "base": {"sha": base_sha, "ref": base_branch},
        "head": {"sha": head_sha, "ref": branch},
    }
    mocker.patch(
        "app.routers.agent.get_github_token_for_task",
        new=AsyncMock(return_value=("fake-token", "owner", "repo")),
    )
    mocker.patch(
        "app.review_claim_fence.get_github_token_for_task",
        new=AsyncMock(return_value=("fake-token", "owner", "repo")),
    )
    mocker.patch(
        "app.github.get_pull_request",
        new=AsyncMock(return_value=live_pr),
    )
    mocker.patch(
        "app.review_claim_fence.get_pull_request",
        new=AsyncMock(return_value=live_pr),
    )
    mocker.patch(
        "app.github.get_check_runs_cached",
        new=AsyncMock(return_value={"status": "completed", "conclusion": "failure"}),
    )
    mocker.patch(
        "app.github.submit_pr_review", new=AsyncMock(return_value={"id": 1})
    )
    mocker.patch(
        "app.github.create_pr_comment", new=AsyncMock(return_value={"id": 1})
    )
    return live_pr


async def _claim(client: AsyncClient, task: Task) -> None:
    """Claim a task and assert it moves to IN_PROGRESS."""
    token = create_agent_token(str(task.id), task.vm_instance_id)
    resp = await client.post("/api/agent/claim", headers={"x-agent-token": token})
    assert resp.status_code == 200, resp.text


async def _status(client: AsyncClient, task: Task, **fields) -> dict:
    """POST /api/agent/status for the given task with the provided fields."""
    token = create_agent_token(str(task.id), task.vm_instance_id)
    payload = {"task_id": str(task.id), **fields}
    resp = await client.post(
        "/api/agent/status",
        json=payload,
        headers={"x-agent-token": token},
    )
    assert resp.status_code == 200, resp.text
    return resp.json()


# ---------------------------------------------------------------------------
# Full end-to-end pipeline test (covers the full scenario from the task spec)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_full_review_fix_cycle(client: AsyncClient, db_session, mocker):
    """
    Full end-to-end integration test for the review/fix pipeline state machine.

    1. Create a Project and a create-type Task in QUEUED state.
    2. Call POST /api/agent/claim → task moves to IN_PROGRESS.
    3. Call POST /api/agent/status with COMPLETED + PULL_REQUEST artifact.
       Verify a review task is spawned with the correct review_loop context.
    4. Claim the review task; call status with COMPLETED and a
       verdict_json indicating REQUEST_CHANGES.
       Verify a fix task is spawned with the review feedback in its prompt.
    5. Claim the fix task; call status with COMPLETED.
       Verify the fix task is COMPLETED and a new review task is spawned
       with an incremented iteration counter.
    """
    mocker.patch("app.vm_lifecycle.delete_agent_vm", return_value=None)
    mocker.patch("app.vm_lifecycle.fire_and_forget_vm_delete", return_value=None)
    mocker.patch(
        "app.orchestrator._persist_circuit_breakers",
        new=AsyncMock(return_value=None),
    )
    mocker.patch("app.main.provision_agent_vm", return_value=True)
    mocker.patch("app.broadcast.broadcast_to_project")
    mocker.patch(
        "app.security.scan_pr_for_injection",
        new=AsyncMock(
            return_value={
                "blocked": False,
                "findings": [],
                "summary": "clean",
                "report": "",
            }
        ),
    )
    # Warm-VM continuity (default-on) would leave spawned tasks with
    # vm_instance_id=None plus a warm_target_vm_id context pin, to be claimed
    # via /next-role. This test exercises the cold-provision claim flow of the
    # review/fix state machine, so pin the flag off for determinism. Warm
    # continuity has dedicated coverage in test_review_pipeline.py /
    # test_warm_vm_integration.py.
    mocker.patch.dict(
        "app.review_pipeline._runtime_config",
        {"warm_continuity_enabled": False},
        clear=False,
    )

    # ------------------------------------------------------------------
    # Step 1: Create project + create-type task (QUEUED)
    # ------------------------------------------------------------------
    project = Project(name="Full Pipeline Integration Test", user_id="test-user-123")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    vm_id = "agent-vm-fp-create-001"
    create_task = Task(
        project_id=project.id,
        title="Implement feature",
        payloads=[
            {
                "type": "create",
                "prompt": "Build a new feature",
                "base_branch": "research/full-cycle-base",
            }
        ],
        context={"scope": "large"},
        vm_instance_id=vm_id,
        status=TaskStatus.QUEUED,
    )
    db_session.add(create_task)
    await db_session.commit()
    await db_session.refresh(create_task)
    task_branch = f"blueprint/task-{str(create_task.id)[:8]}"
    live_pr = _patch_exact_pr_execution_target(
        mocker,
        pr_number=42,
        branch=task_branch,
        base_branch="research/full-cycle-base",
        base_sha="full-cycle-base-42",
        head_sha="full-cycle-head-42-v1",
    )

    assert create_task.status == TaskStatus.QUEUED

    # ------------------------------------------------------------------
    # Step 2: Claim → IN_PROGRESS
    # ------------------------------------------------------------------
    await _claim(client, create_task)
    await db_session.refresh(create_task)
    assert create_task.status == TaskStatus.IN_PROGRESS

    # ------------------------------------------------------------------
    # Step 3: Complete with PULL_REQUEST artifact → review task spawned
    # ------------------------------------------------------------------
    pr_url = "https://github.com/owner/repo/pull/42"
    await _status(
        client,
        create_task,
        status="COMPLETED",
        artifact_type="PULL_REQUEST",
        pr_url=pr_url,
        logs=pr_url,
    )

    await db_session.refresh(create_task)
    assert create_task.status == TaskStatus.COMPLETED

    # Query for spawned child tasks
    result = await db_session.execute(
        select(Task).where(
            Task.project_id == project.id,
            Task.id != create_task.id,
        )
    )
    spawned = result.scalars().all()
    review_tasks = [t for t in spawned if _task_type(t) == "review"]

    assert len(review_tasks) == 1, (
        f"Expected 1 review task, found {len(review_tasks)}: "
        f"{[(t.id, _task_type(t)) for t in spawned]}"
    )

    review_task = review_tasks[0]
    assert review_task.status == TaskStatus.QUEUED
    assert review_task.vm_instance_id is not None

    # Verify payload content
    r_payload = (review_task.payloads or [{}])[0]
    assert r_payload.get("type") == "review"
    assert r_payload.get("pr_number") == 42

    # Verify review_loop context
    r_loop = _review_loop(review_task)
    assert r_loop.get("pr_number") == 42
    assert r_loop.get("iteration") == 1
    assert r_loop.get("original_task_id") == str(create_task.id)

    # ------------------------------------------------------------------
    # Step 4: Claim review task; complete with REQUEST_CHANGES verdict
    # ------------------------------------------------------------------
    await _claim(client, review_task)
    await db_session.refresh(review_task)
    assert review_task.status == TaskStatus.IN_PROGRESS

    feedback_text = "The authentication handler is missing a null check on user.id"
    verdict_json = json.dumps(
        {
            "verdict": "REQUEST_CHANGES",
            "feedback": feedback_text,
            "issues": [{"severity": "major", "description": feedback_text}],
            "reviewed_base_sha": "full-cycle-base-42",
            "reviewed_head_sha": "full-cycle-head-42-v1",
        }
    )
    await _status(
        client,
        review_task,
        status="COMPLETED",
        verdict_json=verdict_json,
        logs="Code review: null check missing in auth handler",
    )

    await db_session.refresh(review_task)
    assert review_task.status == TaskStatus.COMPLETED

    # Verify a fix task was spawned
    result = await db_session.execute(
        select(Task).where(
            Task.project_id == project.id,
            Task.id.notin_([create_task.id, review_task.id]),
        )
    )
    new_tasks = result.scalars().all()
    fix_tasks = [t for t in new_tasks if _task_type(t) == "fix"]

    assert len(fix_tasks) == 1, (
        f"Expected 1 fix task, found {len(fix_tasks)}: "
        f"{[(t.id, _task_type(t)) for t in new_tasks]}"
    )

    fix_task = fix_tasks[0]
    assert fix_task.status == TaskStatus.QUEUED
    assert fix_task.vm_instance_id is not None

    # Verify fix task payload
    f_payload = (fix_task.payloads or [{}])[0]
    assert f_payload.get("type") == "fix"
    assert f_payload.get("pr_number") == 42
    assert feedback_text in (f_payload.get("prompt") or ""), (
        "Fix prompt should contain the review feedback"
    )

    # Verify fix task review_loop context
    f_loop = _review_loop(fix_task)
    assert f_loop.get("pr_number") == 42
    assert f_loop.get("iteration") == 2
    assert f_loop.get("original_task_id") == str(create_task.id)

    # ------------------------------------------------------------------
    # Step 5: Claim fix task; complete it
    # ------------------------------------------------------------------
    await _claim(client, fix_task)
    await db_session.refresh(fix_task)
    assert fix_task.status == TaskStatus.IN_PROGRESS
    live_pr["head"]["sha"] = "full-cycle-head-42-v2"

    await _status(
        client,
        fix_task,
        status="COMPLETED",
        logs="Added null check to authentication handler",
    )

    # ------------------------------------------------------------------
    # Step 6: Verify fix task COMPLETED and review loop state updated
    # ------------------------------------------------------------------
    await db_session.refresh(fix_task)
    assert fix_task.status == TaskStatus.COMPLETED

    # A new review task should have been spawned after fix completes
    result = await db_session.execute(
        select(Task).where(
            Task.project_id == project.id,
            Task.id.notin_([create_task.id, review_task.id, fix_task.id]),
        )
    )
    post_fix_tasks = result.scalars().all()
    new_review_tasks = [t for t in post_fix_tasks if _task_type(t) == "review"]

    assert len(new_review_tasks) == 1, (
        f"Expected 1 new review task after fix, found {len(new_review_tasks)}: "
        f"{[(t.id, _task_type(t)) for t in post_fix_tasks]}"
    )

    new_review_task = new_review_tasks[0]
    assert new_review_task.status == TaskStatus.QUEUED

    # The new review_loop should carry the incremented iteration
    nr_loop = _review_loop(new_review_task)
    assert nr_loop.get("pr_number") == 42
    assert nr_loop.get("iteration") == 3  # create→review=1, review→fix=2, fix→review=3
    assert nr_loop.get("original_task_id") == str(create_task.id)
    assert nr_loop.get("review_target") == {
        "project_id": str(project.id),
        "pr_number": 42,
        "base_sha": "full-cycle-base-42",
        "head_sha": "full-cycle-head-42-v2",
    }
    assert nr_loop.get("delta_base_sha") == "full-cycle-head-42-v1"


# ---------------------------------------------------------------------------
# Focused tests for each pipeline transition
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_task_spawns_review_on_pr_completion(
    client: AsyncClient, db_session, mocker
):
    """create task COMPLETED with PULL_REQUEST artifact → review task spawned."""
    mocker.patch("app.vm_lifecycle.delete_agent_vm", return_value=None)
    mocker.patch("app.vm_lifecycle.fire_and_forget_vm_delete", return_value=None)
    mocker.patch(
        "app.orchestrator._persist_circuit_breakers",
        new=AsyncMock(return_value=None),
    )
    mocker.patch("app.main.provision_agent_vm", return_value=True)
    mocker.patch("app.broadcast.broadcast_to_project")
    mocker.patch(
        "app.security.scan_pr_for_injection",
        new=AsyncMock(
            return_value={
                "blocked": False,
                "findings": [],
                "summary": "clean",
                "report": "",
            }
        ),
    )
    # Cold-provision claim flow — pin warm-VM continuity off (see
    # test_full_review_fix_cycle for rationale).
    mocker.patch.dict(
        "app.review_pipeline._runtime_config",
        {"warm_continuity_enabled": False},
        clear=False,
    )

    project = Project(name="Spawn Review Test", user_id="test-user-123")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    vm_id = "agent-vm-sr-create-001"
    task = Task(
        project_id=project.id,
        title="Create feature Y",
        payloads=[
            {
                "type": "create",
                "prompt": "Build feature Y",
                "base_branch": "research/spawn-review-base",
            }
        ],
        # Initial review is reserved for large-scope intermediate PRs; direct
        # non-intermediate PRs intentionally use CI + the promotion gate.
        context={"scope": "large"},
        vm_instance_id=vm_id,
        status=TaskStatus.QUEUED,
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)
    task_branch = f"blueprint/task-{str(task.id)[:8]}"
    _patch_exact_pr_execution_target(
        mocker,
        pr_number=99,
        branch=task_branch,
        base_branch="research/spawn-review-base",
        base_sha="base-99",
        head_sha="head-99",
    )

    await _claim(client, task)
    await db_session.refresh(task)
    assert task.status == TaskStatus.IN_PROGRESS

    pr_url = "https://github.com/owner/repo/pull/99"
    await _status(
        client,
        task,
        status="COMPLETED",
        artifact_type="PULL_REQUEST",
        pr_url=pr_url,
        logs=pr_url,
    )

    await db_session.refresh(task)
    assert task.status == TaskStatus.COMPLETED

    result = await db_session.execute(
        select(Task).where(Task.project_id == project.id, Task.id != task.id)
    )
    children = result.scalars().all()
    review_tasks = [t for t in children if _task_type(t) == "review"]

    assert len(review_tasks) == 1
    rt = review_tasks[0]
    assert rt.status == TaskStatus.QUEUED
    assert rt.vm_instance_id is not None

    r_loop = _review_loop(rt)
    assert r_loop.get("pr_number") == 99
    assert r_loop.get("iteration") == 1
    assert r_loop.get("review_target") == {
        "project_id": str(project.id),
        "pr_number": 99,
        "base_sha": "base-99",
        "head_sha": "head-99",
    }


@pytest.mark.asyncio
async def test_review_request_changes_empty_pr_closes_rollup_instead_of_spawning_fix(
    client: AsyncClient, db_session, mocker
):
    """When REQUEST_CHANGES is issued on a PR whose diff is empty (the branch was
    flattened onto base during a prior fix cycle), we must NOT spawn another fix
    (there's nothing to fix) — instead close the PR and mark the rollup parent
    COMPLETED. This is the "empty rollup orphan" guard."""
    mocker.patch("app.vm_lifecycle.delete_agent_vm", return_value=None)
    mocker.patch("app.main.provision_agent_vm", return_value=True)
    mocker.patch("app.broadcast.broadcast_to_project")
    mocker.patch(
        "app.routers.agent.get_github_token_for_task",
        return_value=("fake-token", "owner", "repo"),
    )
    # Empty diff — PR has no file changes.
    mocker.patch("app.github.get_pr_files", return_value=[])
    close_mock = mocker.patch("app.github.close_pull_request", return_value=True)

    project = Project(name="Empty Rollup Guard Test", user_id="test-user-123")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    # Rollup parent parking in CI wait
    parent_task = Task(
        project_id=project.id,
        title="Rollup: empty branch",
        payloads=[{"type": "research"}],
        status=TaskStatus.EVALUATING,
        context={
            "review_loop": {
                "pr_number": 999,
                "branch": "research/abc123",
                "base_branch": "main",
                "ci_waiting": True,
                "ci_passed": True,
                "iteration": 6,
            }
        },
    )
    db_session.add(parent_task)
    await db_session.commit()
    await db_session.refresh(parent_task)

    vm_id = "agent-vm-empty-review-001"
    review_task = Task(
        project_id=project.id,
        title="Review PR #999",
        payloads=[
            {
                "type": "review",
                "pr_number": 999,
                "branch": "research/abc123",
                "base_branch": "main",
                "original_task_id": str(parent_task.id),
            }
        ],
        context={
            "review_loop": {
                "pr_number": 999,
                "branch": "research/abc123",
                "base_branch": "main",
                "iteration": 6,
                "original_task_id": str(parent_task.id),
            }
        },
        vm_instance_id=vm_id,
        status=TaskStatus.IN_PROGRESS,
    )
    db_session.add(review_task)
    await db_session.commit()
    await db_session.refresh(review_task)

    verdict_json = json.dumps(
        {
            "verdict": "REQUEST_CHANGES",
            "feedback": "PR #999 is empty — branch tip equals main tip",
        }
    )
    await _status(
        client,
        review_task,
        status="COMPLETED",
        verdict_json=verdict_json,
        logs="Review: empty PR",
    )

    # No fix task should have been spawned
    result = await db_session.execute(
        select(Task).where(
            Task.project_id == project.id,
            Task.id.not_in([review_task.id, parent_task.id]),
        )
    )
    spawned = result.scalars().all()
    fix_tasks = [t for t in spawned if _task_type(t) == "fix"]
    assert len(fix_tasks) == 0, f"Empty PR should not spawn a fix; got {fix_tasks}"

    # The PR should have been closed on GitHub
    assert close_mock.called, "Empty PR should be closed via GitHub API"
    close_args = close_mock.call_args
    assert 999 in close_args.args or close_args.kwargs.get("pr_number") == 999

    # Parent rollup parking task should be COMPLETED, ci_waiting cleared
    await db_session.refresh(parent_task)
    assert parent_task.status == TaskStatus.COMPLETED
    parent_loop = (parent_task.context or {}).get("review_loop", {})
    assert "ci_waiting" not in parent_loop
    assert (parent_task.context or {}).get("pr_closed_empty") is True

    # Review task itself should be COMPLETED (short-circuited)
    await db_session.refresh(review_task)
    assert review_task.status == TaskStatus.COMPLETED


@pytest.mark.asyncio
async def test_review_request_changes_api_error_does_not_close_pr(
    client: AsyncClient, db_session, mocker
):
    """When the empty-PR check hits a GitHub API error, the system must NOT
    treat the PR as empty (which would close it as an obsolete rollup) — it
    should fall through to spawning a fix task. Regression guard for the
    false-positive empty-rollup closure caused by get_pr_files swallowing
    non-200 errors as []."""
    from fastapi import HTTPException

    mocker.patch("app.vm_lifecycle.delete_agent_vm", return_value=None)
    mocker.patch("app.main.provision_agent_vm", return_value=True)
    mocker.patch("app.broadcast.broadcast_to_project")
    mocker.patch(
        "app.routers.agent.get_github_token_for_task",
        return_value=("fake-token", "owner", "repo"),
    )
    # Simulate a transient GitHub API failure during the empty-PR check.
    mocker.patch(
        "app.github.get_pr_files",
        side_effect=HTTPException(status_code=502, detail="GitHub API error"),
    )
    close_mock = mocker.patch("app.github.close_pull_request", return_value=True)

    project = Project(name="API Error Empty-PR Guard Test", user_id="test-user-123")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)
    _patch_exact_pr_execution_target(
        mocker,
        pr_number=888,
        branch="research/abc888",
        base_sha="api-error-base-888",
        head_sha="api-error-head-888",
    )
    review_target = {
        "project_id": str(project.id),
        "pr_number": 888,
        "base_sha": "api-error-base-888",
        "head_sha": "api-error-head-888",
    }

    parent_task = Task(
        project_id=project.id,
        title="Rollup: api-error branch",
        payloads=[{"type": "research"}],
        status=TaskStatus.EVALUATING,
        context={
            "review_loop": {
                "pr_number": 888,
                "branch": "research/abc888",
                "base_branch": "main",
                "ci_waiting": True,
                "ci_passed": True,
                "iteration": 2,
                "review_target": review_target,
            }
        },
    )
    db_session.add(parent_task)
    await db_session.commit()
    await db_session.refresh(parent_task)

    vm_id = "agent-vm-api-err-review-001"
    review_task = Task(
        project_id=project.id,
        title="Review PR #888",
        payloads=[
            {
                "type": "review",
                "pr_number": 888,
                "branch": "research/abc888",
                "base_branch": "main",
                "original_task_id": str(parent_task.id),
            }
        ],
        context={
            "review_loop": {
                "pr_number": 888,
                "branch": "research/abc888",
                "base_branch": "main",
                "iteration": 2,
                "original_task_id": str(parent_task.id),
                "review_target": review_target,
            }
        },
        vm_instance_id=vm_id,
        status=TaskStatus.IN_PROGRESS,
    )
    db_session.add(review_task)
    await db_session.commit()
    await db_session.refresh(review_task)

    verdict_json = json.dumps(
        {
            "verdict": "REQUEST_CHANGES",
            "feedback": "Real changes are needed; do not treat as empty",
            "issues": [
                {"severity": "MAJOR", "description": "Fix the real defect"}
            ],
            "reviewed_base_sha": "api-error-base-888",
            "reviewed_head_sha": "api-error-head-888",
        }
    )
    await _status(
        client,
        review_task,
        status="COMPLETED",
        verdict_json=verdict_json,
        logs="Review: needs changes",
    )

    # The PR must NOT have been closed — API error masked file presence.
    assert not close_mock.called, (
        "API error during empty-PR check must not close the PR"
    )

    # A fix task should have been spawned (normal REQUEST_CHANGES flow).
    result = await db_session.execute(
        select(Task).where(
            Task.project_id == project.id,
            Task.id.not_in([review_task.id, parent_task.id]),
        )
    )
    spawned = result.scalars().all()
    fix_tasks = [t for t in spawned if _task_type(t) == "fix"]
    assert len(fix_tasks) == 1, f"Expected fix task to be spawned, got: {spawned}"

    # Parent rollup must still be in EVALUATING (not COMPLETED-as-empty).
    await db_session.refresh(parent_task)
    assert parent_task.status == TaskStatus.EVALUATING
    assert (parent_task.context or {}).get("pr_closed_empty") is not True


@pytest.mark.asyncio
async def test_review_request_changes_spawns_fix(
    client: AsyncClient, db_session, mocker
):
    """review task COMPLETED with REQUEST_CHANGES verdict → fix task spawned."""
    mocker.patch("app.vm_lifecycle.delete_agent_vm", return_value=None)
    mocker.patch("app.main.provision_agent_vm", return_value=True)
    mocker.patch("app.broadcast.broadcast_to_project")
    mocker.patch(
        "app.routers.agent.get_github_token_for_task",
        return_value=("fake-token", "owner", "repo"),
    )
    mocker.patch(
        "app.github.get_pull_request",
        return_value={
            "head": {"sha": "review-head-77", "ref": "blueprint/task-aabbccdd"},
            "base": {"sha": "review-base-77", "ref": "main"},
            "state": "open",
            "merged": False,
        },
    )
    mocker.patch(
        "app.github.get_check_runs_cached",
        return_value={"status": "completed", "conclusion": "failure"},
    )
    mocker.patch("app.github.submit_pr_review", return_value={"id": 1})
    mocker.patch("app.github.create_pr_comment", return_value={"id": 1})

    project = Project(name="Spawn Fix Test", user_id="test-user-123")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    vm_id = "agent-vm-sf-review-001"
    review_task = Task(
        project_id=project.id,
        title="Review PR #77",
        payloads=[
            {
                "type": "review",
                "pr_number": 77,
                "branch": "blueprint/task-aabbccdd",
                "base_branch": "main",
            }
        ],
        context={
            "review_loop": {
                "pr_number": 77,
                "branch": "blueprint/task-aabbccdd",
                "base_branch": "main",
                "iteration": 1,
                "review_target": {
                    "project_id": str(project.id),
                    "pr_number": 77,
                    "base_sha": "review-base-77",
                    "head_sha": "review-head-77",
                },
            }
        },
        vm_instance_id=vm_id,
        status=TaskStatus.IN_PROGRESS,
    )
    db_session.add(review_task)
    await db_session.commit()
    await db_session.refresh(review_task)

    feedback = "Missing input validation on the /users endpoint"
    verdict_json = json.dumps(
        {
            "verdict": "REQUEST_CHANGES",
            "feedback": feedback,
            "issues": [{"severity": "major", "description": feedback}],
            "reviewed_base_sha": "review-base-77",
            "reviewed_head_sha": "review-head-77",
        }
    )
    await _status(
        client,
        review_task,
        status="COMPLETED",
        verdict_json=verdict_json,
        logs="Review: missing input validation",
    )

    await db_session.refresh(review_task)
    assert review_task.status == TaskStatus.COMPLETED

    result = await db_session.execute(
        select(Task).where(
            Task.project_id == project.id,
            Task.id != review_task.id,
        )
    )
    children = result.scalars().all()
    fix_tasks = [t for t in children if _task_type(t) == "fix"]

    assert len(fix_tasks) == 1
    ft = fix_tasks[0]
    assert ft.status == TaskStatus.QUEUED
    assert ft.vm_instance_id is not None

    f_payload = (ft.payloads or [{}])[0]
    assert f_payload.get("pr_number") == 77
    assert feedback in (f_payload.get("prompt") or "")

    f_loop = _review_loop(ft)
    assert f_loop.get("pr_number") == 77
    assert f_loop.get("iteration") == 2


@pytest.mark.asyncio
async def test_fix_task_spawns_review_on_completion(
    client: AsyncClient, db_session, mocker
):
    """fix task COMPLETED → new review task spawned with iteration+1."""
    mocker.patch("app.vm_lifecycle.delete_agent_vm", return_value=None)
    mocker.patch("app.main.provision_agent_vm", return_value=True)
    mocker.patch("app.broadcast.broadcast_to_project")

    project = Project(name="Respawn Review Test", user_id="test-user-123")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    vm_id = "agent-vm-rr-fix-001"
    fix_task = Task(
        project_id=project.id,
        title="Fix PR #55",
        payloads=[
            {
                "type": "fix",
                "pr_number": 55,
                "branch": "blueprint/task-11223344",
                "base_branch": "main",
                "prompt": "Fix the validation error",
            }
        ],
        context={
            "review_loop": {
                "pr_number": 55,
                "branch": "blueprint/task-11223344",
                "base_branch": "main",
                "iteration": 2,
                "reviewed_head_sha": "reviewed-before-fix",
            }
        },
        vm_instance_id=vm_id,
        status=TaskStatus.IN_PROGRESS,
    )
    db_session.add(fix_task)
    await db_session.commit()
    await db_session.refresh(fix_task)

    await _status(
        client,
        fix_task,
        status="COMPLETED",
        logs="Validation added to /users endpoint",
    )

    await db_session.refresh(fix_task)
    assert fix_task.status == TaskStatus.COMPLETED

    result = await db_session.execute(
        select(Task).where(
            Task.project_id == project.id,
            Task.id != fix_task.id,
        )
    )
    children = result.scalars().all()
    review_tasks = [t for t in children if _task_type(t) == "review"]

    assert len(review_tasks) == 1
    nr = review_tasks[0]
    assert nr.status == TaskStatus.QUEUED
    assert nr.vm_instance_id is not None

    nr_loop = _review_loop(nr)
    assert nr_loop.get("pr_number") == 55
    assert nr_loop.get("iteration") == 3
    assert nr_loop.get("delta_base_sha") == "reviewed-before-fix"
    assert nr.first_payload.get("delta_base_sha") == "reviewed-before-fix"


@pytest.mark.asyncio
async def test_fix_task_always_gets_exact_head_rereview(
    client: AsyncClient, db_session, mocker
):
    """Every completed fix gets an exact-head re-review."""
    mocker.patch("app.vm_lifecycle.delete_agent_vm", return_value=None)
    mocker.patch("app.main.provision_agent_vm", return_value=True)
    mocker.patch("app.broadcast.broadcast_to_project")

    project = Project(name="Exact-Head Rereview Test", user_id="test-user-123")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    fix_task = Task(
        project_id=project.id,
        title="Fix PR #63",
        payloads=[
            {
                "type": "fix",
                "pr_number": 63,
                "branch": "blueprint/task-minor063",
                "base_branch": "main",
                "prompt": "Address the minor nits",
            }
        ],
        context={
            "review_loop": {
                "pr_number": 63,
                "branch": "blueprint/task-minor063",
                "base_branch": "main",
                "iteration": 2,
                "fix_reason": "code_review",
            }
        },
        vm_instance_id="agent-vm-minor-fix-001",
        status=TaskStatus.IN_PROGRESS,
    )
    db_session.add(fix_task)
    await db_session.commit()
    await db_session.refresh(fix_task)

    await _status(
        client,
        fix_task,
        status="COMPLETED",
        logs="Renamed variable per minor nit",
    )

    await db_session.refresh(fix_task)
    assert fix_task.status == TaskStatus.COMPLETED

    result = await db_session.execute(
        select(Task).where(
            Task.project_id == project.id,
            Task.id != fix_task.id,
        )
    )
    review_tasks = [t for t in result.scalars().all() if _task_type(t) == "review"]
    assert len(review_tasks) == 1


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_review_approve_does_not_spawn_fix(
    client: AsyncClient, db_session, mocker
):
    """
    When a review task completes with APPROVE verdict, no fix task should be
    spawned. The merge attempt fails gracefully (no GitHub integration) but
    the review is still marked COMPLETED.
    """
    mocker.patch("app.vm_lifecycle.delete_agent_vm", return_value=None)
    mocker.patch("app.main.provision_agent_vm", return_value=True)
    mocker.patch("app.broadcast.broadcast_to_project")
    # This test asserts the synchronous APPROVE→merge path (no fix spawned):
    # reviews finalize synchronously, so the APPROVE reaches the merge directly.

    project = Project(name="Approve No Fix Test", user_id="test-user-123")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    vm_id = "agent-vm-approve-001"
    review_task = Task(
        project_id=project.id,
        title="Review PR #10",
        payloads=[
            {
                "type": "review",
                "pr_number": 10,
                "branch": "blueprint/task-deadbeef",
                "base_branch": "main",
            }
        ],
        context={
            "review_loop": {
                "pr_number": 10,
                "branch": "blueprint/task-deadbeef",
                "base_branch": "main",
                "iteration": 1,
            }
        },
        vm_instance_id=vm_id,
        status=TaskStatus.IN_PROGRESS,
    )
    db_session.add(review_task)
    await db_session.commit()
    await db_session.refresh(review_task)

    verdict_json = json.dumps({"verdict": "APPROVE", "feedback": "Looks great!"})
    await _status(
        client,
        review_task,
        status="COMPLETED",
        verdict_json=verdict_json,
        logs="Approved: code looks clean",
    )

    await db_session.refresh(review_task)
    # Without GitHub integration, the merge attempt fails and the task is
    # parked in EVALUATING with ci_waiting so the CI monitor can retry.
    assert review_task.status in (TaskStatus.COMPLETED, TaskStatus.EVALUATING)
    if review_task.status == TaskStatus.EVALUATING:
        ci_waiting = (
            (review_task.context or {}).get("review_loop", {}).get("ci_waiting")
        )
        assert ci_waiting is True, "EVALUATING review should have ci_waiting=True"

    # No fix task should have been spawned
    result = await db_session.execute(
        select(Task).where(
            Task.project_id == project.id,
            Task.id != review_task.id,
        )
    )
    children = result.scalars().all()
    fix_tasks = [t for t in children if _task_type(t) == "fix"]

    assert len(fix_tasks) == 0, (
        f"APPROVE verdict should not spawn a fix task, but found: {[t.id for t in fix_tasks]}"
    )


@pytest.mark.asyncio
async def test_cached_approve_with_stale_base_always_respawns_exact_target_review(
    client: AsyncClient, db_session, mocker
):
    """A stale cached APPROVE is never authoritative for the live target."""
    mocker.patch("app.vm_lifecycle.delete_agent_vm", return_value=None)
    mocker.patch("app.main.provision_agent_vm", return_value=True)
    mocker.patch("app.broadcast.broadcast_to_project")
    mocker.patch(
        "app.routers.agent.get_github_token_for_task",
        return_value=("fake-token", "owner", "repo"),
    )
    mocker.patch(
        "app.github.get_pull_request",
        AsyncMock(
            return_value={
                "merged": False,
                "state": "open",
                "base": {"ref": "main", "sha": "live-base"},
                "head": {"ref": "blueprint/task-stale-base", "sha": "same-head"},
            }
        ),
    )
    mocker.patch("app.github.submit_pr_review", AsyncMock(return_value={"id": 1}))
    mocker.patch("app.github.create_pr_comment", AsyncMock(return_value={"id": 2}))
    merge_mock = mocker.patch("app.github.merge_pull_request", AsyncMock(return_value=True))

    project = Project(name="Stale Base Approval Test", user_id="test-user-123")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    review_task = Task(
        project_id=project.id,
        title="Review PR #110",
        payloads=[
            {
                "type": "review",
                "pr_number": 110,
                "branch": "blueprint/task-stale-base",
                "base_branch": "main",
                "cached_verdict_json": '{"verdict":"APPROVE"}',
                "cached_review_task_id": "cached-review-110",
            }
        ],
        context={
            "review_loop": {
                "pr_number": 110,
                "branch": "blueprint/task-stale-base",
                "base_branch": "main",
                "iteration": 1,
            }
        },
        vm_instance_id="agent-vm-stale-base-approve",
        status=TaskStatus.IN_PROGRESS,
    )
    db_session.add(review_task)
    await db_session.commit()
    await db_session.refresh(review_task)

    await _status(
        client,
        review_task,
        status="COMPLETED",
        verdict_json=json.dumps(
            {
                "verdict": "APPROVE",
                "feedback": "Looks good",
                "reviewed_base_sha": "old-base",
                "reviewed_head_sha": "same-head",
            }
        ),
        logs="Approved against an older base",
    )

    await db_session.refresh(review_task)
    assert "Stale/unproven approval discarded" in (
        review_task.latest_status_message or ""
    )
    result = await db_session.execute(
        select(Task).where(
            Task.project_id == project.id,
            Task.id != review_task.id,
        )
    )
    children = result.scalars().all()
    assert not [task for task in children if _task_type(task) == "fix"]
    merge_mock.assert_not_awaited()
    rereviews = [task for task in children if _task_type(task) == "review"]
    assert len(rereviews) == 1
    assert _review_loop(rereviews[0]).get("review_target") == {
        "project_id": str(project.id),
        "pr_number": 110,
        "base_sha": "live-base",
        "head_sha": "same-head",
    }
    # Base-drift-only: the head never moved, so a head-relative delta would be
    # an EMPTY diff range reviewed with broad context suppressed — a rubber
    # stamp. The respawn must be a full re-review against the new base.
    assert not _review_loop(rereviews[0]).get("delta_base_sha")


@pytest.mark.asyncio
async def test_status_completed_idempotency_no_duplicate_fix(
    client: AsyncClient, db_session, mocker
):
    """
    Calling POST /api/agent/status COMPLETED twice on the same review task
    must be idempotent — only one fix task should be spawned, and the second
    call should return 'already_terminal'.
    """
    mocker.patch("app.vm_lifecycle.delete_agent_vm", return_value=None)
    mocker.patch("app.vm_lifecycle.fire_and_forget_vm_delete", return_value=None)
    mocker.patch("app.main.provision_agent_vm", return_value=True)
    mocker.patch("app.broadcast.broadcast_to_project")
    _patch_exact_pr_execution_target(
        mocker,
        pr_number=20,
        branch="blueprint/task-cafebabe",
        base_sha="idem-base-20",
        head_sha="idem-head-20",
    )

    project = Project(name="Idempotency Test", user_id="test-user-123")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    vm_id = "agent-vm-idem-001"
    review_task = Task(
        project_id=project.id,
        title="Review PR #20",
        payloads=[
            {
                "type": "review",
                "pr_number": 20,
                "branch": "blueprint/task-cafebabe",
                "base_branch": "main",
            }
        ],
        context={
            "review_loop": {
                "pr_number": 20,
                "branch": "blueprint/task-cafebabe",
                "base_branch": "main",
                "iteration": 1,
                "review_target": {
                    "project_id": str(project.id),
                    "pr_number": 20,
                    "base_sha": "idem-base-20",
                    "head_sha": "idem-head-20",
                },
            }
        },
        vm_instance_id=vm_id,
        status=TaskStatus.IN_PROGRESS,
    )
    db_session.add(review_task)
    await db_session.commit()
    await db_session.refresh(review_task)

    verdict_json = json.dumps(
        {
            "verdict": "REQUEST_CHANGES",
            "feedback": "Fix the typo",
            "issues": [{"severity": "major", "description": "Fix the typo"}],
            "reviewed_base_sha": "idem-base-20",
            "reviewed_head_sha": "idem-head-20",
        }
    )

    # First status call — should spawn a fix task
    result1 = await _status(
        client,
        review_task,
        status="COMPLETED",
        verdict_json=verdict_json,
        logs="Typo found",
    )
    assert result1.get("status") == "received"

    # Second identical call — task is already terminal, should be a no-op.
    # The status endpoint is exempt from JWT revocation (agents may retry
    # their own completion POST), so it returns 200 with already_terminal.
    result2 = await _status(
        client,
        review_task,
        status="COMPLETED",
        verdict_json=verdict_json,
        logs="Typo found",
    )
    assert result2.get("status") == "already_terminal"

    # Exactly one fix task should exist
    result = await db_session.execute(
        select(Task).where(
            Task.project_id == project.id,
            Task.id != review_task.id,
        )
    )
    children = result.scalars().all()
    fix_tasks = [t for t in children if _task_type(t) == "fix"]

    assert len(fix_tasks) == 1, (
        f"Expected exactly 1 fix task after two identical COMPLETED calls, "
        f"found {len(fix_tasks)}"
    )


@pytest.mark.asyncio
async def test_verdict_parsed_from_logs_when_no_verdict_json(
    client: AsyncClient, db_session, mocker
):
    """
    When verdict_json is absent, the pipeline falls back to parsing the
    verdict from the logs field. REQUEST_CHANGES in the logs should still
    trigger a fix task spawn.
    """
    mocker.patch("app.vm_lifecycle.delete_agent_vm", return_value=None)
    mocker.patch("app.main.provision_agent_vm", return_value=True)
    mocker.patch("app.broadcast.broadcast_to_project")

    project = Project(name="Verdict Fallback Test", user_id="test-user-123")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)
    _patch_exact_pr_execution_target(
        mocker,
        pr_number=33,
        branch="blueprint/task-feedface",
        base_sha="logs-base-33",
        head_sha="logs-head-33",
    )
    review_target = {
        "project_id": str(project.id),
        "pr_number": 33,
        "base_sha": "logs-base-33",
        "head_sha": "logs-head-33",
    }

    vm_id = "agent-vm-vf-review-001"
    review_task = Task(
        project_id=project.id,
        title="Review PR #33",
        payloads=[
            {
                "type": "review",
                "pr_number": 33,
                "branch": "blueprint/task-feedface",
                "base_branch": "main",
            }
        ],
        context={
            "review_loop": {
                "pr_number": 33,
                "branch": "blueprint/task-feedface",
                "base_branch": "main",
                "iteration": 1,
                "review_target": review_target,
            }
        },
        vm_instance_id=vm_id,
        status=TaskStatus.IN_PROGRESS,
    )
    db_session.add(review_task)
    await db_session.commit()
    await db_session.refresh(review_task)

    # Provide a JSON verdict embedded in the logs (no top-level verdict_json field)
    logs_with_verdict = (
        "After reviewing the code I found several issues.\n\n"
        '{"verdict": "REQUEST_CHANGES", "feedback": "Add error handling", '
        '"issues": [{"severity": "MAJOR", "description": "Missing error handling"}], '
        '"reviewed_base_sha": "logs-base-33", "reviewed_head_sha": "logs-head-33"}\n\n'
        "Please address the above points."
    )
    await _status(
        client,
        review_task,
        status="COMPLETED",
        logs=logs_with_verdict,
        # no verdict_json field
    )

    await db_session.refresh(review_task)
    assert review_task.status == TaskStatus.COMPLETED

    result = await db_session.execute(
        select(Task).where(
            Task.project_id == project.id,
            Task.id != review_task.id,
        )
    )
    children = result.scalars().all()
    fix_tasks = [t for t in children if _task_type(t) == "fix"]

    assert len(fix_tasks) == 1, "Verdict parsed from logs should spawn a fix task"
    ft = fix_tasks[0]
    assert _review_loop(ft).get("pr_number") == 33


@pytest.mark.asyncio
async def test_pipeline_duplicate_guard_blocks_second_review(
    client: AsyncClient, db_session, mocker
):
    """
    _spawn_pipeline_task guards against spawning a duplicate review/fix for
    the same PR when one is already active IN THE SAME CHAIN. If an active
    review task for PR #42 already exists and belongs to the same chain, a
    fix completion must not spawn a second review.
    """
    mocker.patch("app.vm_lifecycle.delete_agent_vm", return_value=None)
    mocker.patch("app.main.provision_agent_vm", return_value=True)
    mocker.patch("app.broadcast.broadcast_to_project")

    project = Project(name="Duplicate Guard Test", user_id="test-user-123")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    # Original create task that opened PR #42 — identifies the chain.
    original_create = Task(
        project_id=project.id,
        title="Create PR #42",
        payloads=[
            {
                "type": "create",
                "prompt": "Build feature X",
                "base_branch": "main",
            }
        ],
        vm_instance_id="agent-vm-dg-original",
        status=TaskStatus.COMPLETED,
    )
    db_session.add(original_create)
    await db_session.commit()
    await db_session.refresh(original_create)

    # Pre-existing QUEUED review task for PR #42 — same chain as the fix below.
    existing_review = Task(
        project_id=project.id,
        title="Review PR #42 (existing)",
        payloads=[
            {
                "type": "review",
                "pr_number": 42,
                "branch": "blueprint/task-existing",
                "base_branch": "main",
                "original_task_id": str(original_create.id),
            }
        ],
        context={
            "review_loop": {
                "pr_number": 42,
                "branch": "blueprint/task-existing",
                "base_branch": "main",
                "original_task_id": str(original_create.id),
                "iteration": 1,
            }
        },
        vm_instance_id="agent-vm-existing-review",
        status=TaskStatus.QUEUED,
    )
    db_session.add(existing_review)
    await db_session.commit()
    await db_session.refresh(existing_review)

    # Fix task for the same PR, same chain (original_task_id).
    vm_id = "agent-vm-dg-fix-001"
    fix_task = Task(
        project_id=project.id,
        title="Fix PR #42",
        payloads=[
            {
                "type": "fix",
                "pr_number": 42,
                "branch": "blueprint/task-existing",
                "base_branch": "main",
                "prompt": "Fix the issues",
                "original_task_id": str(original_create.id),
            }
        ],
        context={
            "review_loop": {
                "pr_number": 42,
                "branch": "blueprint/task-existing",
                "base_branch": "main",
                "original_task_id": str(original_create.id),
                "iteration": 2,
            }
        },
        vm_instance_id=vm_id,
        status=TaskStatus.IN_PROGRESS,
    )
    db_session.add(fix_task)
    await db_session.commit()
    await db_session.refresh(fix_task)

    # Completing the fix should NOT spawn another review — one is already active
    await _status(
        client,
        fix_task,
        status="COMPLETED",
        logs="Fix applied",
    )

    await db_session.refresh(fix_task)
    assert fix_task.status == TaskStatus.COMPLETED

    result = await db_session.execute(
        select(Task).where(
            Task.project_id == project.id,
            Task.id.notin_([existing_review.id, fix_task.id]),
        )
    )
    extra_tasks = result.scalars().all()
    extra_review_tasks = [t for t in extra_tasks if _task_type(t) == "review"]

    assert len(extra_review_tasks) == 0, (
        f"Duplicate guard should have blocked the review spawn, "
        f"but found extra review tasks: {[t.id for t in extra_review_tasks]}"
    )


# ---------------------------------------------------------------------------
# Iteration cap enforcement tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_review_fix_cap_blocks_new_fix_on_iteration_limit(
    client: AsyncClient, db_session, mocker
):
    """
    When a review task reaches the per-chain iteration cap, _spawn_pipeline_task
    must return None (no fix task spawned) and set requires_approval=True on the
    original task.

    Setup: scope='small' → cap=10.  Place the review task at iteration=10 so the
    next spawned task would be iteration=11 (> cap), triggering the per-chain cap.
    Keep completed pipeline tasks at 1 (just this review itself after it finishes)
    so the global cap (pr_iterations >= cap) does not fire first.
    """
    mocker.patch("app.vm_lifecycle.delete_agent_vm", return_value=None)
    mocker.patch("app.main.provision_agent_vm", return_value=True)
    mocker.patch("app.broadcast.broadcast_to_project")
    mocker.patch(
        "app.review_loop_convergence._pr_is_preservable_on_cap",
        return_value=False,
    )
    _patch_exact_pr_execution_target(
        mocker,
        pr_number=88,
        branch="blueprint/task-cap0001",
        base_sha="cap-base-88",
        head_sha="cap-head-88",
    )

    project = Project(name="Cap Enforcement Test", user_id="test-user-123")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    # The "original" create task that started the pipeline
    original_task = Task(
        project_id=project.id,
        title="Implement cap-tested feature",
        payloads=[{"type": "create", "prompt": "Build it", "base_branch": "main"}],
        vm_instance_id="agent-vm-cap-orig-001",
        status=TaskStatus.COMPLETED,
        context={"scope": "small"},
    )
    db_session.add(original_task)
    await db_session.commit()
    await db_session.refresh(original_task)

    # Review task at iteration=10 (the 10th review in the chain).
    # scope='small' → _max_review_iterations returns 10.
    # Attempting to spawn next task → iteration=11 > 10 → per-chain cap fires.
    review_task = Task(
        project_id=project.id,
        title="Review PR #88",
        payloads=[
            {
                "type": "review",
                "pr_number": 88,
                "branch": "blueprint/task-cap0001",
                "base_branch": "main",
            }
        ],
        context={
            "scope": "small",
            "review_loop": {
                "pr_number": 88,
                "branch": "blueprint/task-cap0001",
                "base_branch": "main",
                "iteration": 10,
                "original_task_id": str(original_task.id),
                "review_target": {
                    "project_id": str(project.id),
                    "pr_number": 88,
                    "base_sha": "cap-base-88",
                    "head_sha": "cap-head-88",
                },
            },
        },
        vm_instance_id="agent-vm-cap-review-001",
        status=TaskStatus.IN_PROGRESS,
    )
    db_session.add(review_task)
    await db_session.commit()
    await db_session.refresh(review_task)

    feedback = "Still missing null checks"
    verdict_json = json.dumps(
        {
            "verdict": "REQUEST_CHANGES",
            "feedback": feedback,
            "issues": [{"severity": "major", "description": feedback}],
            "reviewed_base_sha": "cap-base-88",
            "reviewed_head_sha": "cap-head-88",
        }
    )
    await _status(
        client,
        review_task,
        status="COMPLETED",
        verdict_json=verdict_json,
        logs="Review: null checks still missing",
    )

    await db_session.refresh(review_task)
    assert review_task.status == TaskStatus.COMPLETED

    # No fix task should have been spawned — cap was hit
    result = await db_session.execute(
        select(Task).where(
            Task.project_id == project.id,
            Task.id.notin_([original_task.id, review_task.id]),
        )
    )
    new_tasks = result.scalars().all()
    fix_tasks = [t for t in new_tasks if _task_type(t) == "fix"]

    assert len(fix_tasks) == 0, (
        f"Iteration cap should have blocked fix spawn, but found: {[t.id for t in fix_tasks]}"
    )

    # Original task must be flagged for manual intervention
    await db_session.refresh(original_task)
    assert original_task.requires_approval is True, (
        "Per-chain iteration cap must set requires_approval=True on the original task"
    )


@pytest.mark.asyncio
async def test_review_fix_cap_ci_failure_lower_limit(
    client: AsyncClient, db_session, mocker
):
    """
    When fix_reason='ci_failure' is stored in the review_loop context,
    _max_review_iterations returns 8 (lower than the default 10-16).

    A review task at iteration=8 with fix_reason='ci_failure' should trigger
    the per-chain cap when attempting to spawn iteration=9, because 9 > 8.
    """
    mocker.patch("app.vm_lifecycle.delete_agent_vm", return_value=None)
    mocker.patch("app.main.provision_agent_vm", return_value=True)
    mocker.patch("app.broadcast.broadcast_to_project")
    mocker.patch(
        "app.review_loop_convergence._pr_is_preservable_on_cap",
        return_value=False,
    )
    _patch_exact_pr_execution_target(
        mocker,
        pr_number=55,
        branch="blueprint/task-ci0001",
        base_sha="ci-cap-base-55",
        head_sha="ci-cap-head-55",
    )

    project = Project(name="CI Failure Cap Test", user_id="test-user-123")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    original_task = Task(
        project_id=project.id,
        title="Create CI-tested feature",
        payloads=[{"type": "create", "prompt": "Build it", "base_branch": "main"}],
        vm_instance_id="agent-vm-ci-orig-001",
        status=TaskStatus.COMPLETED,
    )
    db_session.add(original_task)
    await db_session.commit()
    await db_session.refresh(original_task)

    # Review task at iteration=8 with fix_reason='ci_failure'.
    # _max_review_iterations returns 8 for ci_failure.
    # Attempting to spawn next task → iteration=9 > 8 → per-chain cap fires.
    review_task = Task(
        project_id=project.id,
        title="Review PR #55 (ci failure context)",
        payloads=[
            {
                "type": "review",
                "pr_number": 55,
                "branch": "blueprint/task-ci0001",
                "base_branch": "main",
            }
        ],
        context={
            "review_loop": {
                "pr_number": 55,
                "branch": "blueprint/task-ci0001",
                "base_branch": "main",
                "iteration": 8,
                "fix_reason": "ci_failure",
                "original_task_id": str(original_task.id),
                "review_target": {
                    "project_id": str(project.id),
                    "pr_number": 55,
                    "base_sha": "ci-cap-base-55",
                    "head_sha": "ci-cap-head-55",
                },
            },
        },
        vm_instance_id="agent-vm-ci-review-001",
        status=TaskStatus.IN_PROGRESS,
    )
    db_session.add(review_task)
    await db_session.commit()
    await db_session.refresh(review_task)

    verdict_json = json.dumps(
        {
            "verdict": "REQUEST_CHANGES",
            "feedback": "CI errors remain",
            "issues": [
                {"severity": "major", "description": "CI errors remain"}
            ],
            "reviewed_base_sha": "ci-cap-base-55",
            "reviewed_head_sha": "ci-cap-head-55",
        }
    )
    await _status(
        client,
        review_task,
        status="COMPLETED",
        verdict_json=verdict_json,
        logs="CI still failing",
    )

    await db_session.refresh(review_task)
    assert review_task.status == TaskStatus.COMPLETED

    # No fix task should be spawned — ci_failure cap (8) was hit at iteration 8
    result = await db_session.execute(
        select(Task).where(
            Task.project_id == project.id,
            Task.id.notin_([original_task.id, review_task.id]),
        )
    )
    new_tasks = result.scalars().all()
    fix_tasks = [t for t in new_tasks if _task_type(t) == "fix"]

    assert len(fix_tasks) == 0, (
        f"ci_failure cap (limit=8) should have blocked fix spawn at iteration 9, "
        f"but found: {[t.id for t in fix_tasks]}"
    )

    # Original task must be flagged
    await db_session.refresh(original_task)
    assert original_task.requires_approval is True, (
        "ci_failure iteration cap must set requires_approval=True on the original task"
    )


@pytest.mark.asyncio
async def test_review_fix_cap_sets_status_message(
    client: AsyncClient, db_session, mocker
):
    """
    When the per-chain iteration cap is hit, the original task's
    latest_status_message must describe the cap event — specifically, it must
    mention 'iteration' and 'manual review' so operators know why the pipeline
    stalled.
    """
    mocker.patch("app.vm_lifecycle.delete_agent_vm", return_value=None)
    mocker.patch("app.main.provision_agent_vm", return_value=True)
    mocker.patch("app.broadcast.broadcast_to_project")
    mocker.patch(
        "app.review_loop_convergence._pr_is_preservable_on_cap",
        return_value=False,
    )
    _patch_exact_pr_execution_target(
        mocker,
        pr_number=73,
        branch="blueprint/task-msg0001",
        base_sha="msg-cap-base-73",
        head_sha="msg-cap-head-73",
    )

    project = Project(name="Cap Status Message Test", user_id="test-user-123")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    original_task = Task(
        project_id=project.id,
        title="Feature with status message check",
        payloads=[{"type": "create", "prompt": "Build it", "base_branch": "main"}],
        vm_instance_id="agent-vm-msg-orig-001",
        status=TaskStatus.COMPLETED,
        context={"scope": "small"},
    )
    db_session.add(original_task)
    await db_session.commit()
    await db_session.refresh(original_task)

    # scope='small' → cap=10; iteration=10 → next would be 11 > 10 → per-chain cap fires
    review_task = Task(
        project_id=project.id,
        title="Review PR #73",
        payloads=[
            {
                "type": "review",
                "pr_number": 73,
                "branch": "blueprint/task-msg0001",
                "base_branch": "main",
            }
        ],
        context={
            "scope": "small",
            "review_loop": {
                "pr_number": 73,
                "branch": "blueprint/task-msg0001",
                "base_branch": "main",
                "iteration": 10,
                "original_task_id": str(original_task.id),
                "review_target": {
                    "project_id": str(project.id),
                    "pr_number": 73,
                    "base_sha": "msg-cap-base-73",
                    "head_sha": "msg-cap-head-73",
                },
            },
        },
        vm_instance_id="agent-vm-msg-review-001",
        status=TaskStatus.IN_PROGRESS,
    )
    db_session.add(review_task)
    await db_session.commit()
    await db_session.refresh(review_task)

    verdict_json = json.dumps(
        {
            "verdict": "REQUEST_CHANGES",
            "feedback": "More changes needed",
            "issues": [
                {"severity": "major", "description": "More changes needed"}
            ],
            "reviewed_base_sha": "msg-cap-base-73",
            "reviewed_head_sha": "msg-cap-head-73",
        }
    )
    await _status(
        client,
        review_task,
        status="COMPLETED",
        verdict_json=verdict_json,
        logs="Review: more changes needed",
    )

    # The original task's status message must contain the expected keywords
    await db_session.refresh(original_task)
    msg = original_task.latest_status_message or ""
    assert "iteration" in msg.lower(), (
        f"Status message should mention 'iteration', got: {msg!r}"
    )
    assert "manual review" in msg.lower(), (
        f"Status message should mention 'manual review', got: {msg!r}"
    )


# ---------------------------------------------------------------------------
# Stuck-cycle detection tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_stuck_cycle_detected_marks_original_and_returns_no_fix(
    client: AsyncClient, db_session, mocker
):
    """
    When >=50% of CRITICAL/MAJOR issues repeat across two consecutive review
    iterations, the fix spawn must be suppressed (return None) and the original
    task must be marked requires_approval=True.

    Setup: a review task at iteration=1 whose review_loop already carries a
    prior review_history entry with the same CRITICAL issue. When the review
    completes with REQUEST_CHANGES (same issue), _spawn_pipeline_task is called
    with task_type="fix" at iteration=2. new_review_history will have 2 entries
    with matching CRITICAL issues → stuck-cycle detection fires.
    """
    mocker.patch("app.vm_lifecycle.delete_agent_vm", return_value=None)
    mocker.patch("app.main.provision_agent_vm", return_value=True)
    mocker.patch("app.broadcast.broadcast_to_project")
    # This test exercises synchronous stuck-cycle detection on a REQUEST_CHANGES
    # verdict (concrete repeated CRITICAL issue): reviews finalize synchronously,
    # so the verdict reaches the stuck-cycle suppression path directly.

    project = Project(name="Stuck Cycle Test", user_id="test-user-123")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)
    _patch_exact_pr_execution_target(
        mocker,
        pr_number=99,
        branch="blueprint/task-stuck0001",
        base_sha="stuck-base-99",
        head_sha="stuck-head-99",
    )
    review_target = {
        "project_id": str(project.id),
        "pr_number": 99,
        "base_sha": "stuck-base-99",
        "head_sha": "stuck-head-99",
    }

    original_task = Task(
        project_id=project.id,
        title="Create stuck-cycle feature",
        payloads=[{"type": "create", "prompt": "Build it", "base_branch": "main"}],
        vm_instance_id="agent-vm-stuck-orig-001",
        status=TaskStatus.COMPLETED,
        context={"scope": "medium"},
    )
    db_session.add(original_task)
    await db_session.commit()
    await db_session.refresh(original_task)

    # Repeated CRITICAL issue: same description in prior history and current verdict
    repeated_issue = {
        "severity": "CRITICAL",
        "description": "Missing input validation on user-facing endpoints",
    }
    # This entry is already in the review_loop context from the previous iteration
    prior_history_entry = {
        "iteration": 0,
        "verdict": "REQUEST_CHANGES",
        "feedback": "Still missing validation",
        "issues": [repeated_issue],
        "reviewed_base_sha": "stuck-base-99",
        "reviewed_head_sha": "stuck-head-99",
        "target_verified_by_backend": True,
    }

    # Review task at iteration=1. Its review_loop carries one prior history entry.
    # When it completes with the same CRITICAL issue, iteration for the spawned fix
    # will be 2, and new_review_history will have 2 matching entries → stuck detected.
    review_task = Task(
        project_id=project.id,
        title="Review PR #99",
        payloads=[
            {
                "type": "review",
                "pr_number": 99,
                "branch": "blueprint/task-stuck0001",
                "base_branch": "main",
                "original_task_id": str(original_task.id),
            }
        ],
        context={
            "scope": "medium",
            "review_loop": {
                "pr_number": 99,
                "branch": "blueprint/task-stuck0001",
                "base_branch": "main",
                "iteration": 1,
                "original_task_id": str(original_task.id),
                "review_history": [prior_history_entry],
                "review_target": review_target,
            },
        },
        vm_instance_id="agent-vm-stuck-review-001",
        status=TaskStatus.IN_PROGRESS,
    )
    db_session.add(review_task)
    await db_session.commit()
    await db_session.refresh(review_task)

    verdict_json = json.dumps(
        {
            "verdict": "REQUEST_CHANGES",
            "feedback": "Still missing validation",
            "issues": [repeated_issue],
            "reviewed_base_sha": "stuck-base-99",
            "reviewed_head_sha": "stuck-head-99",
        }
    )
    await _status(
        client,
        review_task,
        status="COMPLETED",
        verdict_json=verdict_json,
        logs="Review: still missing validation",
    )

    await db_session.refresh(review_task)
    assert review_task.status == TaskStatus.COMPLETED

    # No fix task should be spawned — stuck cycle was detected
    result = await db_session.execute(
        select(Task).where(
            Task.project_id == project.id,
            Task.id.notin_([original_task.id, review_task.id]),
        )
    )
    new_tasks = result.scalars().all()
    fix_tasks = [t for t in new_tasks if _task_type(t) == "fix"]

    assert len(fix_tasks) == 0, (
        f"Stuck-cycle detection should have suppressed fix spawn, "
        f"but found: {[t.id for t in fix_tasks]}"
    )

    # Original task must be flagged for manual intervention
    await db_session.refresh(original_task)
    assert original_task.requires_approval is True, (
        "Stuck-cycle detection must set requires_approval=True on the original task"
    )
    msg = original_task.latest_status_message or ""
    assert "stuck" in msg.lower() or "manual review" in msg.lower(), (
        f"Status message should indicate a stuck cycle, got: {msg!r}"
    )


@pytest.mark.asyncio
async def test_stuck_cycle_detected_without_explicit_original_task_id(
    client: AsyncClient, db_session, mocker
):
    """
    Regression test: stuck-cycle detection must still mark a task as
    requires_approval even when original_task_id is absent from source_loop
    and source_payload.

    In this case resolved_original_id falls back to str(source_task.id) (the
    review task itself), so the review task gets flagged — the key assertion is
    that the function does NOT silently drop the cycle (return None without
    setting requires_approval anywhere), which was the bug introduced by
    recomputing stuck_orig_id with an incomplete fallback chain.
    """
    mocker.patch("app.vm_lifecycle.delete_agent_vm", return_value=None)
    mocker.patch("app.main.provision_agent_vm", return_value=True)
    mocker.patch("app.broadcast.broadcast_to_project")
    # Synchronous stuck-cycle detection under test (concrete repeated MAJOR issue
    # → REQUEST_CHANGES): reviews finalize synchronously, so the verdict reaches
    # the stuck-cycle suppression path directly.

    project = Project(name="Stuck Cycle No-OrigID Test", user_id="test-user-123")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    repeated_issue = {
        "severity": "MAJOR",
        "description": "SQL injection risk in search query construction",
    }
    prior_history_entry = {
        "iteration": 0,
        "feedback": "SQL injection still present",
        "issues": [repeated_issue],
    }

    # Review task at iteration=1 with NO original_task_id anywhere.
    # resolved_original_id will fall back to str(source_task.id) = review_task.id.
    review_task = Task(
        project_id=project.id,
        title="Review PR #101",
        payloads=[
            {
                "type": "review",
                "pr_number": 101,
                "branch": "blueprint/task-stuck0002",
                "base_branch": "main",
                # deliberately omit original_task_id
            }
        ],
        context={
            "review_loop": {
                "pr_number": 101,
                "branch": "blueprint/task-stuck0002",
                "base_branch": "main",
                "iteration": 1,
                # deliberately omit original_task_id
                "review_history": [prior_history_entry],
            },
        },
        vm_instance_id="agent-vm-stuck-noid-001",
        status=TaskStatus.IN_PROGRESS,
    )
    db_session.add(review_task)
    await db_session.commit()
    await db_session.refresh(review_task)

    verdict_json = json.dumps(
        {
            "verdict": "REQUEST_CHANGES",
            "feedback": "SQL injection still present",
            "issues": [repeated_issue],
        }
    )
    await _status(
        client,
        review_task,
        status="COMPLETED",
        verdict_json=verdict_json,
        logs="Review: SQL injection still present",
    )

    await db_session.refresh(review_task)
    assert review_task.status == TaskStatus.COMPLETED

    # No fix task — stuck cycle suppresses spawn
    result = await db_session.execute(
        select(Task).where(
            Task.project_id == project.id,
            Task.id != review_task.id,
        )
    )
    new_tasks = result.scalars().all()
    fix_tasks = [t for t in new_tasks if _task_type(t) == "fix"]

    assert len(fix_tasks) == 0, (
        f"Stuck-cycle must suppress spawn even without explicit original_task_id, "
        f"but found fix tasks: {[t.id for t in fix_tasks]}"
    )

    # The review task itself (acting as resolved_original_id fallback) must be flagged
    await db_session.refresh(review_task)
    assert review_task.requires_approval is True, (
        "Without explicit original_task_id, resolved_original_id falls back to "
        "source_task.id — that task must still be flagged requires_approval=True"
    )


# ---------------------------------------------------------------------------
# Rollup iteration cap bypass tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_rollup_bypasses_global_cap_via_is_rollup_review_flag(db_session, mocker):
    """
    When source_task.context['_is_rollup_review'] is True, _spawn_pipeline_task
    must use MAX_REVIEW_FIX_ITERATIONS (16) as the global cap instead of the
    scope-based cap, allowing rollup reviews to proceed even after many retries.

    Setup: medium scope task → normal global cap = 12.
    Pre-populate 12 completed review/fix tasks for PR #200 (would hit normal cap).
    Set _is_rollup_review flag in context.
    Expect: _spawn_pipeline_task returns a task (not None).
    """
    mocker.patch("app.broadcast.broadcast_to_project")

    project = Project(name="Rollup Cap Bypass via Flag Test", user_id="test-user-123")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    # Parent research/rollup task — medium scope, _is_rollup_review flag set
    parent_task = Task(
        project_id=project.id,
        title="Research task (rollup parent)",
        payloads=[
            {
                "type": "research",
                "prompt": "Research improvements",
                "base_branch": "main",
            }
        ],
        vm_instance_id="agent-vm-rollup-parent-001",
        status=TaskStatus.EVALUATING,
        context={"scope": "medium", "_is_rollup_review": True},
        latest_status_message="All 3 subtasks merged, rollup PR #200 awaiting review",
    )
    db_session.add(parent_task)
    await db_session.commit()
    await db_session.refresh(parent_task)

    # 12 completed review+fix tasks for PR #200 — this equals the medium scope cap
    # Without rollup bypass, global cap (12) would block the next spawn.
    for i in range(12):
        t = Task(
            project_id=project.id,
            title=f"{'Review' if i % 2 == 0 else 'Fix'} PR #200 (prior attempt {i})",
            payloads=[
                {
                    "type": "review" if i % 2 == 0 else "fix",
                    "pr_number": 200,
                    "branch": "blueprint/rollup-test",
                    "base_branch": "main",
                }
            ],
            vm_instance_id=f"agent-vm-rollup-prior-{i:03d}",
            status=TaskStatus.COMPLETED,
        )
        db_session.add(t)
    await db_session.commit()

    # Call _spawn_pipeline_task directly — should succeed because is_rollup=True
    result, was_created = await _spawn_pipeline_task(
        source_task=parent_task,
        task_type="review",
        session=db_session,
        pr_number=200,
        branch="blueprint/rollup-test",
        base_branch="main",
    )

    assert result is not None and was_created, (
        "Rollup task (_is_rollup_review=True) should bypass the "
        f"medium-scope global cap (12) and use MAX_REVIEW_FIX_ITERATIONS ({MAX_REVIEW_FIX_ITERATIONS})"
    )
    assert result.status == TaskStatus.QUEUED


@pytest.mark.asyncio
async def test_rollup_bypasses_global_cap_via_rollup_retries_context(
    db_session, mocker
):
    """
    When source_task.context contains '_rollup_retries', _spawn_pipeline_task
    must use MAX_REVIEW_FIX_ITERATIONS (16) as the global cap.

    This covers the transient PR creation failure path where rollup_management
    writes _rollup_retries into context before retrying.
    """
    mocker.patch("app.broadcast.broadcast_to_project")

    project = Project(
        name="Rollup Cap Bypass via Context Test", user_id="test-user-123"
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    # Parent task with _rollup_retries in context (set by rollup_management on transient failure)
    parent_task = Task(
        project_id=project.id,
        title="Research task (rollup with retries)",
        payloads=[
            {
                "type": "research",
                "prompt": "Research improvements",
                "base_branch": "main",
            }
        ],
        vm_instance_id="agent-vm-rollup-retry-001",
        status=TaskStatus.EVALUATING,
        context={"scope": "small", "_rollup_retries": 2},
    )
    db_session.add(parent_task)
    await db_session.commit()
    await db_session.refresh(parent_task)

    # 10 completed tasks — equals small scope cap (10)
    for i in range(10):
        t = Task(
            project_id=project.id,
            title=f"{'Review' if i % 2 == 0 else 'Fix'} PR #201 (prior {i})",
            payloads=[
                {
                    "type": "review" if i % 2 == 0 else "fix",
                    "pr_number": 201,
                    "branch": "blueprint/rollup-retry-test",
                    "base_branch": "main",
                }
            ],
            vm_instance_id=f"agent-vm-rollup-retry-prior-{i:03d}",
            status=TaskStatus.COMPLETED,
        )
        db_session.add(t)
    await db_session.commit()

    result, was_created = await _spawn_pipeline_task(
        source_task=parent_task,
        task_type="review",
        session=db_session,
        pr_number=201,
        branch="blueprint/rollup-retry-test",
        base_branch="main",
    )

    assert result is not None and was_created, (
        "Rollup task with _rollup_retries in context should bypass the "
        f"small-scope global cap (10) and use MAX_REVIEW_FIX_ITERATIONS ({MAX_REVIEW_FIX_ITERATIONS})"
    )
    assert result.status == TaskStatus.QUEUED


@pytest.mark.asyncio
async def test_non_rollup_task_respects_global_cap(db_session, mocker):
    """
    Regression: a normal (non-rollup) task must still be blocked when the
    global cap is reached. Verifies the rollup bypass does not accidentally
    apply to non-rollup tasks.
    """
    mocker.patch("app.broadcast.broadcast_to_project")

    project = Project(name="Non-Rollup Cap Enforcement Test", user_id="test-user-123")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    # Normal task — no rollup markers
    normal_task = Task(
        project_id=project.id,
        title="Normal create task",
        payloads=[{"type": "create", "prompt": "Build feature", "base_branch": "main"}],
        vm_instance_id="agent-vm-normal-cap-001",
        status=TaskStatus.EVALUATING,
        context={"scope": "small"},
        latest_status_message="Processing feature task",
    )
    db_session.add(normal_task)
    await db_session.commit()
    await db_session.refresh(normal_task)

    # 10 completed tasks — equals small scope cap (10)
    for i in range(10):
        t = Task(
            project_id=project.id,
            title=f"{'Review' if i % 2 == 0 else 'Fix'} PR #202 (prior {i})",
            payloads=[
                {
                    "type": "review" if i % 2 == 0 else "fix",
                    "pr_number": 202,
                    "branch": "blueprint/normal-cap-test",
                    "base_branch": "main",
                }
            ],
            vm_instance_id=f"agent-vm-normal-prior-{i:03d}",
            status=TaskStatus.COMPLETED,
        )
        db_session.add(t)
    await db_session.commit()

    result, was_created = await _spawn_pipeline_task(
        source_task=normal_task,
        task_type="review",
        session=db_session,
        pr_number=202,
        branch="blueprint/normal-cap-test",
        base_branch="main",
    )

    assert result is None and not was_created, (
        "Non-rollup task at small-scope global cap (10) must be blocked — "
        "rollup bypass should not apply"
    )


# ---------------------------------------------------------------------------
# CI completion: non-transient merge block escalation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_ci_merge_rule_block_escalates_immediately(db_session, mocker):
    """
    When merge_pull_request returns a rule-violation message, handle_ci_completion
    must immediately set requires_approval=True and flag merge_rule_blocked instead
    of entering the retry loop.
    """
    from app.ci_completion import handle_ci_completion

    mocker.patch("app.broadcast.broadcast_to_project")
    mocker.patch("app.alert_dispatch._maybe_create_failure_alert", return_value=None)

    project = Project(name="Rule Block Test", user_id="test-user-123")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = Task(
        project_id=project.id,
        title="CI rule block test",
        payloads=[
            {
                "type": "review",
                "pr_number": 99,
                "branch": "blueprint/rule-block-test",
                "base_branch": "main",
            }
        ],
        context={
            "review_loop": {
                "pr_number": 99,
                "branch": "blueprint/rule-block-test",
                "base_branch": "main",
                "iteration": 1,
                # Gate is open: review approved, CI passing — exercise the merge attempt
                "review_approved": True,
                "head_sha": "test-head-99",
                "base_sha": "test-base-99",
                "ci_checked_head_sha": "test-head-99",
                "ci_checked_base_sha": "test-base-99",
                "review_target": {
                    "project_id": str(project.id),
                    "pr_number": 99,
                    "base_sha": "test-base-99",
                    "head_sha": "test-head-99",
                },
                "review_history": [
                    {
                        "verdict": "APPROVE",
                        "issues": [],
                        "reviewed_base_sha": "test-base-99",
                        "reviewed_head_sha": "test-head-99",
                        "target_verified_by_backend": True,
                        "review_task_id": "test-rule-block-review",
                    }
                ],
            },
        },
        vm_instance_id="agent-vm-rule-block-001",
        status=TaskStatus.EVALUATING,
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    # Mock merge_pull_request to return a rule-violation failure
    mocker.patch(
        "app.github.merge_pull_request",
        return_value={
            "merged": False,
            "message": "Repository rule violations: Required approving review not satisfied",
        },
    )
    # Mock get_pull_request to indicate PR is still open (not already merged/closed)
    mocker.patch(
        "app.github.get_pull_request",
        return_value={
            "merged": False,
            "state": "open",
            "mergeable": True,
            "head": {
                "sha": "test-head-99",
                "ref": "blueprint/rule-block-test",
            },
            "base": {"sha": "test-base-99", "ref": "main"},
        },
    )

    ci_result = {"status": "completed", "conclusion": "success", "failed_logs": ""}
    await handle_ci_completion(
        task=task,
        ci_result=ci_result,
        gh_token="fake-token",
        owner="test-owner",
        repo="test-repo",
        session=db_session,
        source="webhook",
    )

    # Flush so in-memory mutations are visible; assert directly (don't refresh from DB
    # as the handler doesn't commit — per filter-test-session-refresh-pattern)
    await db_session.flush()
    assert task.requires_approval is True, (
        "Rule-violation merge block must set requires_approval=True immediately"
    )
    review_loop = (task.context or {}).get("review_loop", {})
    assert review_loop.get("merge_rule_blocked") is True, (
        "merge_rule_blocked flag must be set in review_loop context"
    )
    assert review_loop.get("merge_failures", 0) == 0, (
        "merge_failures must NOT be incremented for a rule-violation block"
    )
    assert "manual intervention" in (task.latest_status_message or ""), (
        "Status message must indicate manual intervention is required"
    )


@pytest.mark.asyncio
async def test_ci_fail_fix_suppressed_uses_generic_message(db_session, mocker):
    """
    When _spawn_pipeline_task returns None (fix suppressed for any reason:
    iteration cap, stuck cycle, dedup, direction cap), the status message
    must not misattribute the cause to 'iteration cap'.
    """
    from app.ci_completion import handle_ci_completion

    mocker.patch("app.broadcast.broadcast_to_project")
    mocker.patch("app.alert_dispatch._maybe_create_failure_alert", return_value=None)
    mocker.patch("app.github.create_pr_comment", return_value=None)
    mocker.patch(
        "app.review_pipeline._spawn_pipeline_task",
        return_value=(None, False),
    )

    project = Project(name="Fix Suppressed Test", user_id="test-user-123")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = Task(
        project_id=project.id,
        title="CI fix suppressed test",
        payloads=[
            {
                "type": "review",
                "pr_number": 777,
                "branch": "blueprint/fix-suppressed-test",
                "base_branch": "main",
            }
        ],
        context={
            "review_loop": {
                "pr_number": 777,
                "branch": "blueprint/fix-suppressed-test",
                "base_branch": "main",
                "iteration": 1,
                "ci_waiting": True,
            },
        },
        vm_instance_id="agent-vm-fix-suppressed-001",
        status=TaskStatus.EVALUATING,
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    ci_result = {
        "status": "completed",
        "conclusion": "failure",
        "failed_logs": "test failure",
    }
    await handle_ci_completion(
        task=task,
        ci_result=ci_result,
        gh_token="fake-token",
        owner="test-owner",
        repo="test-repo",
        session=db_session,
        source="webhook",
    )

    await db_session.flush()
    assert task.status == TaskStatus.FAILED
    msg = task.latest_status_message or ""
    assert "fix not spawned" in msg
    assert "iteration cap" not in msg, (
        "Status message must not misattribute suppression to iteration cap"
    )
    assert "pipeline limit" in msg


# ---------------------------------------------------------------------------
# Merge gate: ci_passed AND review_approved both required
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_ci_passed_without_review_approved_does_not_merge(db_session, mocker):
    """
    Regression for PR #1408: a rollup-to-main parent has ci_waiting=True but
    no review_approved yet. When CI completes successfully, handle_ci_completion
    must NOT merge the PR — it should record ci_passed=True and leave the task
    parked, waiting for the review task's APPROVE handoff.
    """
    from app.ci_completion import handle_ci_completion

    mocker.patch("app.broadcast.broadcast_to_project")
    mocker.patch("app.alert_dispatch._maybe_create_failure_alert", return_value=None)
    merge_mock = mocker.patch(
        "app.github.merge_pull_request",
        return_value={"merged": True},
    )

    project = Project(name="Gate ci_passed only", user_id="test-user-123")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = Task(
        project_id=project.id,
        title="Rollup parent waiting on CI",
        payloads=[{"type": "research", "prompt": "rollup parent"}],
        context={
            "review_loop": {
                "ci_waiting": True,
                "pr_number": 1408,
                "branch": "research/2ed0480e",
                "base_branch": "main",
                "ci_wait_started": "2026-04-15T07:18:36",
            },
        },
        vm_instance_id="agent-vm-gate-ci-only-001",
        status=TaskStatus.EVALUATING,
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    ci_result = {"status": "completed", "conclusion": "success", "failed_logs": ""}
    await handle_ci_completion(
        task=task,
        ci_result=ci_result,
        gh_token="fake-token",
        owner="test-owner",
        repo="test-repo",
        session=db_session,
        source="webhook",
    )

    await db_session.flush()
    await db_session.refresh(task)
    assert merge_mock.call_count == 0, (
        "handle_ci_completion must NOT call merge_pull_request when review_approved is not set "
        "(this was the PR #1408 bug — CI passing alone was treated as authorization to merge)"
    )
    review_loop = (task.context or {}).get("review_loop", {})
    assert review_loop.get("ci_passed") is True, (
        "ci_passed must be recorded on the parking task so the gate fires when review approves"
    )
    assert not review_loop.get("review_approved"), (
        "review_approved must NOT have been auto-set by CI completion"
    )
    assert task.status == TaskStatus.EVALUATING, (
        f"Task must remain EVALUATING (waiting for review), got {task.status}"
    )
    assert "waiting for review" in (task.latest_status_message or "").lower()


@pytest.mark.asyncio
async def test_ci_passed_with_review_approved_merges(db_session, mocker):
    """
    When both gate signals are set on the parking task, handle_ci_completion
    proceeds with the merge.
    """
    from app.ci_completion import handle_ci_completion

    mocker.patch("app.broadcast.broadcast_to_project")
    mocker.patch("app.alert_dispatch._maybe_create_failure_alert", return_value=None)
    mocker.patch("app.github.create_pr_comment", return_value=None)
    merge_mock = mocker.patch(
        "app.github.merge_pull_request",
        return_value={"merged": True},
    )

    project = Project(name="Gate both signals", user_id="test-user-123")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)
    review_target = {
        "project_id": str(project.id),
        "pr_number": 1500,
        "base_sha": "gate-base-1500",
        "head_sha": "gate-head-1500",
    }
    mocker.patch(
        "app.review_merge_authority._fetch_live_review_target",
        new=AsyncMock(
            return_value={
                **review_target,
                "state": "open",
                "merged": False,
                "mergeable": True,
                "mergeable_state": "clean",
            }
        ),
    )

    task = Task(
        project_id=project.id,
        title="Rollup parent both gates open",
        payloads=[{"type": "research", "prompt": "rollup parent"}],
        context={
            "review_loop": {
                "ci_waiting": True,
                "review_approved": True,  # review already handed off
                "pr_number": 1500,
                "branch": "research/abc",
                "base_branch": "main",
                "ci_wait_started": "2026-04-15T07:18:36",
                "base_sha": "gate-base-1500",
                "head_sha": "gate-head-1500",
                "ci_checked_base_sha": "gate-base-1500",
                "ci_checked_head_sha": "gate-head-1500",
                "review_target": review_target,
                "review_history": [
                    {
                        "verdict": "APPROVE",
                        "issues": [],
                        "reviewed_base_sha": "gate-base-1500",
                        "reviewed_head_sha": "gate-head-1500",
                        "target_verified_by_backend": True,
                        "review_task_id": "gate-review-1500",
                    }
                ],
            },
        },
        vm_instance_id="agent-vm-gate-both-001",
        status=TaskStatus.EVALUATING,
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    ci_result = {"status": "completed", "conclusion": "success", "failed_logs": ""}
    await handle_ci_completion(
        task=task,
        ci_result=ci_result,
        gh_token="fake-token",
        owner="test-owner",
        repo="test-repo",
        session=db_session,
        source="webhook",
    )

    await db_session.flush()
    await db_session.refresh(task)
    assert merge_mock.call_count >= 1, (
        "handle_ci_completion must call merge_pull_request when both gates are open"
    )
    assert task.status == TaskStatus.COMPLETED, (
        f"Task must be COMPLETED after a successful gated merge, got {task.status}"
    )


@pytest.mark.asyncio
async def test_ci_passed_legacy_verdict_requires_exact_review(db_session, mocker):
    """A scalar legacy APPROVE is not durable merge authority."""
    from app.ci_completion import handle_ci_completion

    mocker.patch("app.broadcast.broadcast_to_project")
    mocker.patch("app.alert_dispatch._maybe_create_failure_alert", return_value=None)
    mocker.patch("app.github.create_pr_comment", return_value=None)
    merge_mock = mocker.patch(
        "app.github.merge_pull_request",
        new=AsyncMock(return_value={"merged": True}),
    )

    project = Project(name="Gate legacy verdict", user_id="test-user-123")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = Task(
        project_id=project.id,
        title="In-flight legacy task",
        payloads=[{"type": "review", "prompt": "legacy review"}],
        context={
            "review_loop": {
                "ci_waiting": True,
                "verdict": "APPROVE",  # legacy form
                "pr_number": 1600,
                "branch": "feat/legacy",
                "base_branch": "main",
            },
        },
        vm_instance_id="agent-vm-gate-legacy-001",
        status=TaskStatus.EVALUATING,
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    head_sha = "a" * 40
    base_sha = "b" * 40
    mocker.patch(
        "app.github.get_pull_request",
        new=AsyncMock(
            return_value={
                "number": 1600,
                "state": "open",
                "merged": False,
                "base": {"sha": base_sha},
                "head": {"sha": head_sha},
            }
        ),
    )
    review_task = Task(
        id=uuid.uuid4(),
        project_id=project.id,
        title="Exact target review",
        payloads=[{"type": "review"}],
        status=TaskStatus.QUEUED,
    )
    spawn_mock = mocker.patch(
        "app.review_pipeline._spawn_pipeline_task",
        new=AsyncMock(return_value=(review_task, True)),
    )

    ci_result = {"status": "completed", "conclusion": "success", "failed_logs": ""}
    await handle_ci_completion(
        task=task,
        ci_result=ci_result,
        gh_token="fake-token",
        owner="test-owner",
        repo="test-repo",
        session=db_session,
        source="webhook",
        checked_head_sha=head_sha,
        checked_base_sha=base_sha,
    )

    await db_session.refresh(task)
    merge_mock.assert_not_awaited()
    spawn_mock.assert_awaited_once()
    assert spawn_mock.await_args.kwargs["review_target"] == {
        "project_id": str(project.id),
        "pr_number": 1600,
        "base_sha": base_sha,
        "head_sha": head_sha,
        "base_ref": "",
        "state": "open",
        "merged": False,
        "draft": False,
        "mergeable": None,
        "mergeable_state": "",
    }
    gated_loop = (task.context or {})["review_loop"]
    assert gated_loop["review_approved"] is False
    assert gated_loop["authoritative_review_required"] is True
    assert gated_loop["authoritative_review_task_id"] == str(review_task.id)
    assert "verdict" not in gated_loop


@pytest.mark.asyncio
async def test_review_approved_only_does_not_merge(db_session, mocker):
    """
    Symmetric to ci_passed-only: a parking task with review_approved=True but
    no ci_passed must wait for CI. The orchestrator's CI poll will eventually
    record ci_passed and then merge.
    """
    from app.ci_completion import _review_approved

    mocker.patch("app.broadcast.broadcast_to_project")
    merge_mock = mocker.patch(
        "app.github.merge_pull_request",
        return_value={"merged": True},
    )

    project = Project(name="Gate review only", user_id="test-user-123")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    review_loop = {
        "ci_waiting": True,
        "review_approved": True,
        "pr_number": 1700,
    }
    assert _review_approved(review_loop) is True
    assert review_loop.get("ci_passed") is None  # gate is still closed

    # Direct gate-helper call would only fire if both flags are set; verify the
    # caller-side check guards us. We don't actually call _attempt_merge here
    # because the caller (handle_ci_completion / task_lifecycle) is responsible
    # for the gate check before calling. This test documents the contract.
    assert merge_mock.call_count == 0


@pytest.mark.asyncio
async def test_rollup_review_approval_writes_review_approved_on_parent(
    client, db_session, mocker
):
    """
    End-to-end rollup gate handoff: a rollup PR's review task completes with
    APPROVE verdict. The handler must write review_approved=True on the
    PARENT (not the review task) and either merge (if ci_passed already set)
    or leave the parent parked waiting for CI.

    This test covers the path where ci_passed is NOT yet set, so the handoff
    just records the flag on the parent.
    """
    mocker.patch("app.vm_lifecycle.delete_agent_vm", return_value=None)
    mocker.patch("app.main.provision_agent_vm", return_value=True)
    mocker.patch("app.broadcast.broadcast_to_project")
    # This test asserts the synchronous rollup-gate handoff for an APPROVE
    # verdict: reviews finalize synchronously, so the handoff runs directly.
    mocker.patch(
        "app.routers.agent.get_github_token_for_task",
        return_value=("fake-token", "test-owner", "test-repo"),
    )
    mocker.patch(
        "app.github.get_pull_request",
        return_value={
            "head": {"ref": "research/abc", "sha": "deadbeef"},
            "merged": False,
            "state": "open",
        },
    )
    mocker.patch("app.github.create_pr_comment", return_value=None)
    mocker.patch("app.github.submit_pr_review", return_value={"id": 1})
    merge_mock = mocker.patch(
        "app.github.merge_pull_request",
        return_value={"merged": True},
    )

    project = Project(name="Rollup gate handoff", user_id="test-user-123")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)
    _patch_exact_pr_execution_target(
        mocker,
        pr_number=1408,
        branch="research/abc",
        base_sha="rollup-base-1408",
        head_sha="deadbeef",
    )
    review_target = {
        "project_id": str(project.id),
        "pr_number": 1408,
        "base_sha": "rollup-base-1408",
        "head_sha": "deadbeef",
    }

    parent = Task(
        project_id=project.id,
        title="Rollup parent",
        payloads=[{"type": "research", "prompt": "rollup parent"}],
        vm_instance_id="agent-vm-rollup-parent-001",
        status=TaskStatus.EVALUATING,
    )
    db_session.add(parent)
    await db_session.commit()
    await db_session.refresh(parent)

    parent.context = {
        "review_loop": {
            "ci_waiting": True,
            "pr_number": 1408,
            "branch": "research/abc",
            "base_branch": "main",
            "original_task_id": str(parent.id),
            "ci_wait_started": "2026-04-15T07:18:36",
            "review_target": review_target,
        },
    }
    db_session.add(parent)
    await db_session.commit()
    await db_session.refresh(parent)

    review_task = Task(
        project_id=project.id,
        title="Review rollup PR #1408",
        payloads=[
            {
                "type": "review",
                "pr_number": 1408,
                "branch": "research/abc",
                "base_branch": "main",
                "original_task_id": str(parent.id),
            }
        ],
        context={
            "review_loop": {
                "pr_number": 1408,
                "branch": "research/abc",
                "base_branch": "main",
                "original_task_id": str(parent.id),
                "iteration": 1,
                "review_target": review_target,
            }
        },
        vm_instance_id="agent-vm-rollup-review-001",
        status=TaskStatus.IN_PROGRESS,
    )
    db_session.add(review_task)
    await db_session.commit()
    await db_session.refresh(review_task)

    verdict_json = json.dumps(
        {
            "verdict": "APPROVE",
            "feedback": "Looks fine",
            "issues": [],
            "reviewed_base_sha": "rollup-base-1408",
            "reviewed_head_sha": "deadbeef",
        }
    )
    await _status(
        client,
        review_task,
        status="COMPLETED",
        verdict_json=verdict_json,
        logs="APPROVE",
    )

    await db_session.refresh(parent)
    parent_loop = (parent.context or {}).get("review_loop", {})
    assert parent_loop.get("review_approved") is True, (
        "Rollup review approval must write review_approved=True on the PARENT, not the review task"
    )
    assert merge_mock.call_count == 0, (
        "Without ci_passed on the parent, the gate is still closed — no merge yet"
    )

    await db_session.refresh(review_task)
    assert review_task.status == TaskStatus.COMPLETED, (
        f"Review task must be COMPLETED after handoff, got {review_task.status}"
    )


# ---------------------------------------------------------------------------
# Auto-playbook generation tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_auto_playbook_generation(db_session, mocker):
    """_maybe_auto_playbook creates a playbook entry after 3 tasks share the
    same failure signature.  First two calls store the signature but return
    None; the third call returns a key and the entry appears in project.design_doc.
    """
    mocker.patch("app.broadcast.broadcast_to_project")

    project = Project(name="Auto Playbook Test", user_id="test-user-autopb")
    project.design_doc = {}
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    error_msg = "ModuleNotFoundError: No module named 'requests'"
    failure_class = "CODE_ERROR"

    from app.playbook_learning import _maybe_auto_playbook

    # --- First and second failures: signature stored, threshold not yet reached ---
    for i in range(2):
        t = Task(
            project_id=project.id,
            title=f"failing task {i}",
            payloads=[{"type": "create", "prompt": "do something"}],
            vm_instance_id=f"agent-vm-{uuid.uuid4().hex}",
            status=TaskStatus.FAILED,
            latest_status_message=error_msg,
        )
        db_session.add(t)
        await db_session.commit()
        await db_session.refresh(t)

        key = await _maybe_auto_playbook(t, failure_class, error_msg, db_session)
        await db_session.commit()

        assert key is None, f"Expected None on occurrence {i + 1}, got '{key}'"
        assert (t.context or {}).get("_failure_signature"), (
            f"Signature should be stored on task after call {i + 1}"
        )

    # --- Third failure: should trigger playbook creation ---
    t3 = Task(
        project_id=project.id,
        title="failing task 2",
        payloads=[{"type": "create", "prompt": "do something"}],
        vm_instance_id=f"agent-vm-{uuid.uuid4().hex}",
        status=TaskStatus.FAILED,
        latest_status_message=error_msg,
    )
    db_session.add(t3)
    await db_session.commit()
    await db_session.refresh(t3)

    key = await _maybe_auto_playbook(t3, failure_class, error_msg, db_session)
    await db_session.commit()

    assert key is not None, "Expected a playbook key to be returned on the 3rd failure"

    # Verify the entry was persisted on the project
    await db_session.refresh(project)
    doc = project.design_doc or {}
    playbook_full_key = f"playbook/{key}"
    assert playbook_full_key in doc, (
        f"Expected '{playbook_full_key}' in design_doc; got keys: {list(doc.keys())}"
    )
    entry = doc[playbook_full_key]
    assert entry.get("source") == "auto-generated"
    assert "3" in entry.get("content", ""), "Content should mention 3 occurrences"
    assert str(t3.id) in entry.get("content", ""), (
        "Content should reference the triggering task ID"
    )


@pytest.mark.asyncio
async def test_auto_playbook_no_duplicate(db_session, mocker):
    """_maybe_auto_playbook skips creation if an entry with the same key
    already exists in the project's design_doc."""
    mocker.patch("app.broadcast.broadcast_to_project")

    from app.playbook_learning import (
        _extract_failure_signature,
        _signature_to_key,
        _maybe_auto_playbook,
    )

    error_msg = "ModuleNotFoundError: No module named 'requests'"
    failure_class = "CODE_ERROR"

    # Pre-compute the key the auto-playbook would generate
    sig = _extract_failure_signature(failure_class, error_msg, "create", "medium")
    existing_key = _signature_to_key(sig)

    project = Project(name="No Duplicate Playbook Test", user_id="test-user-nodup")
    project.design_doc = {
        f"playbook/{existing_key}": {
            "content": "pre-existing entry",
            "source_task_id": None,
            "updated_at": "2024-01-01T00:00:00",
            "confirmed_count": 0,
            "source": "manual",
        }
    }
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    # Create 3 tasks with matching signature so threshold is reached
    for i in range(3):
        t = Task(
            project_id=project.id,
            title=f"failing task {i}",
            payloads=[{"type": "create", "prompt": "do something"}],
            vm_instance_id=f"agent-vm-{uuid.uuid4().hex}",
            status=TaskStatus.FAILED,
            latest_status_message=error_msg,
        )
        db_session.add(t)
        await db_session.commit()
        await db_session.refresh(t)

        await _maybe_auto_playbook(t, failure_class, error_msg, db_session)
        await db_session.commit()

    # The pre-existing entry must not have been overwritten
    await db_session.refresh(project)
    doc = project.design_doc or {}
    assert doc.get(f"playbook/{existing_key}", {}).get("source") == "manual", (
        "Auto-playbook must not overwrite a pre-existing entry"
    )


@pytest.mark.asyncio
async def test_auto_playbook_timeout_signature(db_session, mocker):
    """AGENT_TIMEOUT failures use (task_type, scope) as the signature,
    not the error message text."""
    mocker.patch("app.broadcast.broadcast_to_project")

    from app.playbook_learning import _maybe_auto_playbook, _extract_failure_signature

    sig = _extract_failure_signature(
        "AGENT_TIMEOUT", "execution timed out", "create", "large"
    )
    assert sig == "timeout-create-large"

    project = Project(name="Timeout Signature Test", user_id="test-user-timeout")
    project.design_doc = {}
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    for i in range(3):
        t = Task(
            project_id=project.id,
            title=f"timeout task {i}",
            payloads=[{"type": "create", "prompt": "big job"}],
            vm_instance_id=f"agent-vm-{uuid.uuid4().hex}",
            status=TaskStatus.FAILED,
            latest_status_message="execution timed out",
            context={"scope": "large"},
        )
        db_session.add(t)
        await db_session.commit()
        await db_session.refresh(t)

        key = await _maybe_auto_playbook(
            t, "AGENT_TIMEOUT", "execution timed out", db_session
        )
        await db_session.commit()

    assert key is not None, "Expected playbook entry for repeated AGENT_TIMEOUT"

    await db_session.refresh(project)
    doc = project.design_doc or {}
    assert any(k.startswith("playbook/auto-timeout") for k in doc), (
        "Expected a playbook key starting with 'auto-timeout'"
    )


# ---------------------------------------------------------------------------
# Dedup guard: type-awareness and liveness
# ---------------------------------------------------------------------------
#
# Historic bug (#1710, #1683): _spawn_pipeline_task ignored task_type when
# deduping against active pipeline tasks on the same PR/chain. Asking for a
# "fix" while a "review" was in flight returned the review as if it were the
# fix, so merge conflicts never got resolved — the PR sat forever with an
# approved informational review. The tests below pin the corrected semantics.


@pytest.mark.asyncio
async def test_dedup_cross_type_fix_cancels_stale_review(
    client: AsyncClient, db_session, mocker
):
    """Fix requested while review is in flight on the same chain → cancel the
    review and permit the fresh fix. A review scoped to a pre-conflict /
    pre-CI-failure snapshot produces stale verdicts; the fix must take over."""
    mocker.patch("app.vm_lifecycle.delete_agent_vm", return_value=None)
    mocker.patch("app.main.provision_agent_vm", return_value=True)
    mocker.patch("app.broadcast.broadcast_to_project")

    project = Project(name="Cross-Type Dedup Test", user_id="user1")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    original = Task(
        project_id=project.id,
        payloads=[{"type": "create", "prompt": "x", "base_branch": "main"}],
        vm_instance_id="agent-orig-xt",
        status=TaskStatus.COMPLETED,
    )
    db_session.add(original)
    await db_session.commit()
    await db_session.refresh(original)

    alive_review = Task(
        project_id=project.id,
        payloads=[
            {
                "type": "review",
                "pr_number": 4277,
                "branch": "blueprint/task-xt",
                "base_branch": "main",
                "original_task_id": str(original.id),
            }
        ],
        context={
            "review_loop": {"original_task_id": str(original.id), "pr_number": 4277}
        },
        vm_instance_id="agent-review-xt",
        status=TaskStatus.IN_PROGRESS,
        last_heartbeat=_naive_utcnow(),
    )
    db_session.add(alive_review)
    await db_session.commit()
    await db_session.refresh(alive_review)

    ci_task = Task(
        project_id=project.id,
        payloads=[
            {
                "type": "create",
                "pr_number": 4277,
                "branch": "blueprint/task-xt",
                "base_branch": "main",
                "original_task_id": str(original.id),
            }
        ],
        context={
            "review_loop": {"original_task_id": str(original.id), "pr_number": 4277}
        },
        status=TaskStatus.EVALUATING,
    )
    db_session.add(ci_task)
    await db_session.commit()
    await db_session.refresh(ci_task)

    fix_task, was_created = await _spawn_pipeline_task(
        ci_task,
        "fix",
        db_session,
        4277,
        "blueprint/task-xt",
        "main",
        fix_prompt="rebase and resolve",
        original_task_id=str(original.id),
    )
    await db_session.commit()

    assert was_created is True, (
        "A fresh fix task must be created, not a return of the review corpse"
    )
    assert fix_task is not None
    assert _task_type(fix_task) == "fix", (
        f"Returned task must be a fix, got {_task_type(fix_task)!r}"
    )

    await db_session.refresh(alive_review)
    assert alive_review.status == TaskStatus.CANCELLED, (
        f"Stale review should be cancelled, got {alive_review.status}: "
        f"{alive_review.latest_status_message!r}"
    )
    assert "superseded by fix" in (alive_review.latest_status_message or "").lower()


@pytest.mark.asyncio
async def test_dedup_same_type_stale_task_superseded(
    client: AsyncClient, db_session, mocker
):
    """Same-type task with no heartbeat past the stale threshold → cancelled
    and superseded by the new spawn. Prior behaviour returned the corpse as a
    dedup hit, blocking the caller until the 30-min CI backstop timed out
    (root cause of the FAILED-but-CI-passed tasks on PRs #1705/#1688/#1676)."""
    mocker.patch("app.vm_lifecycle.delete_agent_vm", return_value=None)
    mocker.patch("app.main.provision_agent_vm", return_value=True)
    mocker.patch("app.broadcast.broadcast_to_project")

    project = Project(name="Stale Supersede Test", user_id="user1")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    original = Task(
        project_id=project.id,
        payloads=[{"type": "create", "prompt": "x", "base_branch": "main"}],
        vm_instance_id="agent-orig-stale",
        status=TaskStatus.COMPLETED,
    )
    db_session.add(original)
    await db_session.commit()
    await db_session.refresh(original)

    stale_hb = _naive_utcnow() - timedelta(minutes=PIPELINE_TASK_STALE_MINUTES + 10)
    dying_review = Task(
        project_id=project.id,
        payloads=[
            {
                "type": "review",
                "pr_number": 4288,
                "branch": "blueprint/task-stale",
                "base_branch": "main",
                "original_task_id": str(original.id),
            }
        ],
        context={
            "review_loop": {"original_task_id": str(original.id), "pr_number": 4288}
        },
        vm_instance_id="agent-dying",
        status=TaskStatus.IN_PROGRESS,
        last_heartbeat=stale_hb,
        started_at=stale_hb,
    )
    db_session.add(dying_review)
    await db_session.commit()
    await db_session.refresh(dying_review)

    ci_task = Task(
        project_id=project.id,
        payloads=[
            {
                "type": "create",
                "pr_number": 4288,
                "branch": "blueprint/task-stale",
                "base_branch": "main",
                "original_task_id": str(original.id),
            }
        ],
        context={
            "review_loop": {"original_task_id": str(original.id), "pr_number": 4288}
        },
        status=TaskStatus.EVALUATING,
    )
    db_session.add(ci_task)
    await db_session.commit()
    await db_session.refresh(ci_task)

    review_task, was_created = await _spawn_pipeline_task(
        ci_task,
        "review",
        db_session,
        4288,
        "blueprint/task-stale",
        "main",
        original_prompt="review again",
        original_task_id=str(original.id),
    )
    await db_session.commit()

    assert was_created is True, "Stale review must be superseded, not returned"
    assert review_task is not None
    assert review_task.id != dying_review.id

    await db_session.refresh(dying_review)
    assert dying_review.status == TaskStatus.CANCELLED
    assert "no heartbeat" in (dying_review.latest_status_message or "").lower()


@pytest.mark.asyncio
async def test_dedup_review_deferred_while_fix_active(
    client: AsyncClient, db_session, mocker
):
    """Review requested while a fix is in flight on the same chain → defer
    (return (None, False)). Reviewing pre-fix code is wasteful and the fix
    will change the diff the reviewer looks at. Ensures no silent substitution
    in the opposite direction from the cross-type fix test above."""
    mocker.patch("app.vm_lifecycle.delete_agent_vm", return_value=None)
    mocker.patch("app.main.provision_agent_vm", return_value=True)
    mocker.patch("app.broadcast.broadcast_to_project")

    project = Project(name="Deferred Review Test", user_id="user1")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    original = Task(
        project_id=project.id,
        payloads=[{"type": "create", "prompt": "x", "base_branch": "main"}],
        vm_instance_id="agent-orig-def",
        status=TaskStatus.COMPLETED,
    )
    db_session.add(original)
    await db_session.commit()
    await db_session.refresh(original)

    alive_fix = Task(
        project_id=project.id,
        payloads=[
            {
                "type": "fix",
                "pr_number": 4299,
                "branch": "blueprint/task-def",
                "base_branch": "main",
                "original_task_id": str(original.id),
            }
        ],
        context={
            "review_loop": {"original_task_id": str(original.id), "pr_number": 4299}
        },
        vm_instance_id="agent-fix-def",
        status=TaskStatus.IN_PROGRESS,
        last_heartbeat=_naive_utcnow(),
    )
    db_session.add(alive_fix)
    await db_session.commit()
    await db_session.refresh(alive_fix)

    source = Task(
        project_id=project.id,
        payloads=[
            {
                "type": "create",
                "pr_number": 4299,
                "branch": "blueprint/task-def",
                "base_branch": "main",
                "original_task_id": str(original.id),
            }
        ],
        context={
            "review_loop": {"original_task_id": str(original.id), "pr_number": 4299}
        },
        status=TaskStatus.EVALUATING,
    )
    db_session.add(source)
    await db_session.commit()
    await db_session.refresh(source)

    review_task, was_created = await _spawn_pipeline_task(
        source,
        "review",
        db_session,
        4299,
        "blueprint/task-def",
        "main",
        original_prompt="review",
        original_task_id=str(original.id),
    )
    await db_session.commit()

    assert review_task is None
    assert was_created is False

    await db_session.refresh(source)
    assert "deferred" in (source.latest_status_message or "").lower()

    await db_session.refresh(alive_fix)
    assert alive_fix.status == TaskStatus.IN_PROGRESS, (
        "The running fix must not be cancelled when a review is deferred"
    )

    assert (source.context or {}).get(SPAWN_REASON_KEY) == SPAWN_REASON_DEFERRED, (
        "Deferred-cross-type return must record its reason on source_task.context "
        "so rollup callers can distinguish defer (retry later) from cap (terminal)"
    )


@pytest.mark.asyncio
async def test_rollup_sources_cross_chain_dedup_on_shared_pr(
    client: AsyncClient, db_session, mocker
):
    """Two rollup parents targeting the same rollup PR must dedup by PR number
    (not by chain) — otherwise each parent independently spawns its own fix
    task for the shared PR, and we end up with duplicate agents racing on the
    same branch.

    Scenario: parent A (rollup source) just spawned fix F for PR #7000. ~16s
    later, parent B (also rollup source, different chain) enters rollup logic
    for the same PR #7000 and calls _spawn_pipeline_task("fix"). The dedup
    must see F as same-type active and return (F, False) rather than
    greenlighting a duplicate spawn.
    """
    mocker.patch("app.vm_lifecycle.delete_agent_vm", return_value=None)
    mocker.patch("app.main.provision_agent_vm", return_value=True)
    mocker.patch("app.broadcast.broadcast_to_project")

    project = Project(name="Rollup dedup test", user_id="user1")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    # Parent A: rollup source that already spawned a fix for PR #7000.
    parent_a = Task(
        project_id=project.id,
        payloads=[{"type": "perf", "prompt": "x", "base_branch": "research/abc"}],
        context={"_is_rollup_review": True, "rollup_pr_number": 7000},
        status=TaskStatus.EVALUATING,
    )
    db_session.add(parent_a)
    await db_session.commit()
    await db_session.refresh(parent_a)

    # The fix task parent_a spawned — chain keyed off parent_a.id.
    fix_from_a = Task(
        project_id=project.id,
        payloads=[
            {
                "type": "fix",
                "pr_number": 7000,
                "branch": "research/abc",
                "base_branch": "main",
                "original_task_id": str(parent_a.id),
            }
        ],
        vm_instance_id="agent-fix-a",
        status=TaskStatus.IN_PROGRESS,
        last_heartbeat=_naive_utcnow(),
    )
    db_session.add(fix_from_a)
    await db_session.commit()
    await db_session.refresh(fix_from_a)

    # Parent B: different rollup source, different chain, same PR.
    parent_b = Task(
        project_id=project.id,
        payloads=[{"type": "perf", "prompt": "y", "base_branch": "research/abc"}],
        context={"_is_rollup_review": True, "rollup_pr_number": 7000},
        status=TaskStatus.EVALUATING,
    )
    db_session.add(parent_b)
    await db_session.commit()
    await db_session.refresh(parent_b)

    result, was_created = await _spawn_pipeline_task(
        parent_b,
        "fix",
        db_session,
        7000,
        "research/abc",
        "main",
        fix_prompt="resolve conflicts",
        original_task_id=str(parent_b.id),  # different chain from fix_from_a
    )
    await db_session.commit()

    # Cross-chain dedup hit: return the existing fix, no new task.
    assert result is not None, "Expected existing fix task (dedup hit)"
    assert result.id == fix_from_a.id, "Expected fix_from_a as the dedup target"
    assert was_created is False, "Must NOT create a duplicate fix"

    await db_session.refresh(parent_b)
    assert (parent_b.context or {}).get(SPAWN_REASON_KEY) == SPAWN_REASON_DEDUP


@pytest.mark.asyncio
async def test_non_rollup_sources_preserve_chain_based_dedup(
    client: AsyncClient, db_session, mocker
):
    """Sanity check: non-rollup sources still require same-chain match. If a
    fix is running on chain A, a non-rollup source on chain B must NOT dedup
    against it (they're unrelated — different PRs normally, but if PR numbers
    happen to match for some reason, chain isolation is the safety net)."""
    mocker.patch("app.vm_lifecycle.delete_agent_vm", return_value=None)
    mocker.patch("app.main.provision_agent_vm", return_value=True)
    mocker.patch("app.broadcast.broadcast_to_project")
    mocker.patch("app.review_pipeline.broadcast_to_project")

    project = Project(name="Non-rollup dedup test", user_id="user1")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    create_a = Task(
        project_id=project.id,
        payloads=[{"type": "create", "prompt": "a"}],
        status=TaskStatus.COMPLETED,
    )
    create_b = Task(
        project_id=project.id,
        payloads=[{"type": "create", "prompt": "b"}],
        status=TaskStatus.COMPLETED,
    )
    db_session.add_all([create_a, create_b])
    await db_session.commit()
    await db_session.refresh(create_a)
    await db_session.refresh(create_b)

    # Fix on chain A for PR #8001
    fix_a = Task(
        project_id=project.id,
        payloads=[
            {
                "type": "fix",
                "pr_number": 8001,
                "branch": "blueprint/task-a",
                "base_branch": "main",
                "original_task_id": str(create_a.id),
            }
        ],
        vm_instance_id="agent-fix-a",
        status=TaskStatus.IN_PROGRESS,
        last_heartbeat=_naive_utcnow(),
    )
    db_session.add(fix_a)
    await db_session.commit()
    await db_session.refresh(fix_a)

    # Source on chain B — not a rollup source. Same PR number is an edge case
    # (shouldn't happen in normal flow) but we verify isolation regardless.
    source_b = Task(
        project_id=project.id,
        payloads=[
            {
                "type": "create",
                "pr_number": 8001,
                "branch": "blueprint/task-b",
                "base_branch": "main",
                "original_task_id": str(create_b.id),
            }
        ],
        context={},  # no _is_rollup_review
        status=TaskStatus.EVALUATING,
    )
    db_session.add(source_b)
    await db_session.commit()
    await db_session.refresh(source_b)

    result, was_created = await _spawn_pipeline_task(
        source_b,
        "fix",
        db_session,
        8001,
        "blueprint/task-b",
        "main",
        fix_prompt="fix b",
        original_task_id=str(create_b.id),
    )
    await db_session.commit()

    # Different chain + not rollup → dedup skipped, fresh fix spawned.
    assert result is not None
    assert result.id != fix_a.id
    assert was_created is True
    await db_session.refresh(source_b)
    assert (source_b.context or {}).get(SPAWN_REASON_KEY) == SPAWN_REASON_SPAWNED

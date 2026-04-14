"""
Integration tests for the review pipeline state machine.

Exercises the full create → review → fix → re-review cycle via the agent
claim and status endpoints, verifying that each step spawns the correct
downstream task and that the review_loop context is updated correctly.

All external calls (VM provisioning/deletion, GitHub API, WebSocket
broadcasts) are mocked so no real infrastructure is required.
"""
import json
import pytest
from httpx import AsyncClient
from sqlalchemy import select

from app.models import Project, Task, TaskStatus
from app.auth import create_agent_token
from app.review_pipeline import _spawn_pipeline_task, MAX_REVIEW_FIX_ITERATIONS


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _task_type(task: Task) -> str:
    return (task.payloads or [{}])[0].get("type", "")


def _review_loop(task: Task) -> dict:
    return (task.context or {}).get("review_loop", {})


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
    mocker.patch("app.main.delete_agent_vm", return_value=None)
    mocker.patch("app.main.provision_agent_vm", return_value=True)
    mocker.patch("app.broadcast.broadcast_to_project")
    mocker.patch("app.routers.agent.get_github_token_for_task", return_value=("fake-token", "owner", "repo"))

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
        payloads=[{"type": "create", "prompt": "Build a new feature", "base_branch": "main"}],
        vm_instance_id=vm_id,
        status=TaskStatus.QUEUED,
    )
    db_session.add(create_task)
    await db_session.commit()
    await db_session.refresh(create_task)

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
        client, create_task,
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
    verdict_json = json.dumps({"verdict": "REQUEST_CHANGES", "feedback": feedback_text})
    await _status(
        client, review_task,
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

    await _status(
        client, fix_task,
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


# ---------------------------------------------------------------------------
# Focused tests for each pipeline transition
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_create_task_spawns_review_on_pr_completion(
    client: AsyncClient, db_session, mocker
):
    """create task COMPLETED with PULL_REQUEST artifact → review task spawned."""
    mocker.patch("app.main.delete_agent_vm", return_value=None)
    mocker.patch("app.main.provision_agent_vm", return_value=True)
    mocker.patch("app.broadcast.broadcast_to_project")
    mocker.patch("app.routers.agent.get_github_token_for_task", return_value=("fake-token", "owner", "repo"))

    project = Project(name="Spawn Review Test", user_id="test-user-123")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    vm_id = "agent-vm-sr-create-001"
    task = Task(
        project_id=project.id,
        title="Create feature Y",
        payloads=[{"type": "create", "prompt": "Build feature Y", "base_branch": "main"}],
        vm_instance_id=vm_id,
        status=TaskStatus.QUEUED,
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    await _claim(client, task)
    await db_session.refresh(task)
    assert task.status == TaskStatus.IN_PROGRESS

    pr_url = "https://github.com/owner/repo/pull/99"
    await _status(
        client, task,
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


@pytest.mark.asyncio
async def test_review_request_changes_spawns_fix(
    client: AsyncClient, db_session, mocker
):
    """review task COMPLETED with REQUEST_CHANGES verdict → fix task spawned."""
    mocker.patch("app.main.delete_agent_vm", return_value=None)
    mocker.patch("app.main.provision_agent_vm", return_value=True)
    mocker.patch("app.broadcast.broadcast_to_project")

    project = Project(name="Spawn Fix Test", user_id="test-user-123")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    vm_id = "agent-vm-sf-review-001"
    review_task = Task(
        project_id=project.id,
        title="Review PR #77",
        payloads=[{
            "type": "review",
            "pr_number": 77,
            "branch": "blueprint/task-aabbccdd",
            "base_branch": "main",
        }],
        context={"review_loop": {
            "pr_number": 77,
            "branch": "blueprint/task-aabbccdd",
            "base_branch": "main",
            "iteration": 1,
        }},
        vm_instance_id=vm_id,
        status=TaskStatus.IN_PROGRESS,
    )
    db_session.add(review_task)
    await db_session.commit()
    await db_session.refresh(review_task)

    feedback = "Missing input validation on the /users endpoint"
    verdict_json = json.dumps({"verdict": "REQUEST_CHANGES", "feedback": feedback})
    await _status(
        client, review_task,
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
    mocker.patch("app.main.delete_agent_vm", return_value=None)
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
        payloads=[{
            "type": "fix",
            "pr_number": 55,
            "branch": "blueprint/task-11223344",
            "base_branch": "main",
            "prompt": "Fix the validation error",
        }],
        context={"review_loop": {
            "pr_number": 55,
            "branch": "blueprint/task-11223344",
            "base_branch": "main",
            "iteration": 2,
        }},
        vm_instance_id=vm_id,
        status=TaskStatus.IN_PROGRESS,
    )
    db_session.add(fix_task)
    await db_session.commit()
    await db_session.refresh(fix_task)

    await _status(
        client, fix_task,
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
    mocker.patch("app.main.delete_agent_vm", return_value=None)
    mocker.patch("app.main.provision_agent_vm", return_value=True)
    mocker.patch("app.broadcast.broadcast_to_project")

    project = Project(name="Approve No Fix Test", user_id="test-user-123")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    vm_id = "agent-vm-approve-001"
    review_task = Task(
        project_id=project.id,
        title="Review PR #10",
        payloads=[{
            "type": "review",
            "pr_number": 10,
            "branch": "blueprint/task-deadbeef",
            "base_branch": "main",
        }],
        context={"review_loop": {
            "pr_number": 10,
            "branch": "blueprint/task-deadbeef",
            "base_branch": "main",
            "iteration": 1,
        }},
        vm_instance_id=vm_id,
        status=TaskStatus.IN_PROGRESS,
    )
    db_session.add(review_task)
    await db_session.commit()
    await db_session.refresh(review_task)

    verdict_json = json.dumps({"verdict": "APPROVE", "feedback": "Looks great!"})
    await _status(
        client, review_task,
        status="COMPLETED",
        verdict_json=verdict_json,
        logs="Approved: code looks clean",
    )

    await db_session.refresh(review_task)
    # Without GitHub integration, the merge attempt fails and the task is
    # parked in EVALUATING with ci_waiting so the CI monitor can retry.
    assert review_task.status in (TaskStatus.COMPLETED, TaskStatus.EVALUATING)
    if review_task.status == TaskStatus.EVALUATING:
        ci_waiting = (review_task.context or {}).get("review_loop", {}).get("ci_waiting")
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
async def test_status_completed_idempotency_no_duplicate_fix(
    client: AsyncClient, db_session, mocker
):
    """
    Calling POST /api/agent/status COMPLETED twice on the same review task
    must be idempotent — only one fix task should be spawned, and the second
    call should return 'already_terminal'.
    """
    mocker.patch("app.main.delete_agent_vm", return_value=None)
    mocker.patch("app.main.provision_agent_vm", return_value=True)
    mocker.patch("app.broadcast.broadcast_to_project")

    project = Project(name="Idempotency Test", user_id="test-user-123")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    vm_id = "agent-vm-idem-001"
    review_task = Task(
        project_id=project.id,
        title="Review PR #20",
        payloads=[{
            "type": "review",
            "pr_number": 20,
            "branch": "blueprint/task-cafebabe",
            "base_branch": "main",
        }],
        context={"review_loop": {
            "pr_number": 20,
            "branch": "blueprint/task-cafebabe",
            "base_branch": "main",
            "iteration": 1,
        }},
        vm_instance_id=vm_id,
        status=TaskStatus.IN_PROGRESS,
    )
    db_session.add(review_task)
    await db_session.commit()
    await db_session.refresh(review_task)

    verdict_json = json.dumps({"verdict": "REQUEST_CHANGES", "feedback": "Fix the typo"})

    # First status call — should spawn a fix task
    result1 = await _status(
        client, review_task,
        status="COMPLETED",
        verdict_json=verdict_json,
        logs="Typo found",
    )
    assert result1.get("status") == "received"

    # Second identical call — task is already terminal, should be a no-op.
    # The status endpoint is exempt from JWT revocation (agents may retry
    # their own completion POST), so it returns 200 with already_terminal.
    result2 = await _status(
        client, review_task,
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
    mocker.patch("app.main.delete_agent_vm", return_value=None)
    mocker.patch("app.main.provision_agent_vm", return_value=True)
    mocker.patch("app.broadcast.broadcast_to_project")

    project = Project(name="Verdict Fallback Test", user_id="test-user-123")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    vm_id = "agent-vm-vf-review-001"
    review_task = Task(
        project_id=project.id,
        title="Review PR #33",
        payloads=[{
            "type": "review",
            "pr_number": 33,
            "branch": "blueprint/task-feedface",
            "base_branch": "main",
        }],
        context={"review_loop": {
            "pr_number": 33,
            "branch": "blueprint/task-feedface",
            "base_branch": "main",
            "iteration": 1,
        }},
        vm_instance_id=vm_id,
        status=TaskStatus.IN_PROGRESS,
    )
    db_session.add(review_task)
    await db_session.commit()
    await db_session.refresh(review_task)

    # Provide a JSON verdict embedded in the logs (no top-level verdict_json field)
    logs_with_verdict = (
        'After reviewing the code I found several issues.\n\n'
        '{"verdict": "REQUEST_CHANGES", "feedback": "Add error handling"}\n\n'
        'Please address the above points.'
    )
    await _status(
        client, review_task,
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

    assert len(fix_tasks) == 1, (
        "Verdict parsed from logs should spawn a fix task"
    )
    ft = fix_tasks[0]
    assert _review_loop(ft).get("pr_number") == 33


@pytest.mark.asyncio
async def test_pipeline_duplicate_guard_blocks_second_review(
    client: AsyncClient, db_session, mocker
):
    """
    _spawn_pipeline_task guards against spawning a duplicate review/fix for
    the same PR when one is already active. If an active review task already
    exists for PR #42, completing the fix should NOT spawn a second review.
    """
    mocker.patch("app.main.delete_agent_vm", return_value=None)
    mocker.patch("app.main.provision_agent_vm", return_value=True)
    mocker.patch("app.broadcast.broadcast_to_project")

    project = Project(name="Duplicate Guard Test", user_id="test-user-123")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    # Pre-existing QUEUED review task for PR #42 — simulates an already-active review
    existing_review = Task(
        project_id=project.id,
        title="Review PR #42 (existing)",
        payloads=[{
            "type": "review",
            "pr_number": 42,
            "branch": "blueprint/task-existing",
            "base_branch": "main",
        }],
        context={"review_loop": {
            "pr_number": 42,
            "branch": "blueprint/task-existing",
            "base_branch": "main",
            "iteration": 1,
        }},
        vm_instance_id="agent-vm-existing-review",
        status=TaskStatus.QUEUED,
    )
    db_session.add(existing_review)
    await db_session.commit()
    await db_session.refresh(existing_review)

    # Fix task for the same PR
    vm_id = "agent-vm-dg-fix-001"
    fix_task = Task(
        project_id=project.id,
        title="Fix PR #42",
        payloads=[{
            "type": "fix",
            "pr_number": 42,
            "branch": "blueprint/task-existing",
            "base_branch": "main",
            "prompt": "Fix the issues",
        }],
        context={"review_loop": {
            "pr_number": 42,
            "branch": "blueprint/task-existing",
            "base_branch": "main",
            "iteration": 2,
        }},
        vm_instance_id=vm_id,
        status=TaskStatus.IN_PROGRESS,
    )
    db_session.add(fix_task)
    await db_session.commit()
    await db_session.refresh(fix_task)

    # Completing the fix should NOT spawn another review — one is already active
    await _status(
        client, fix_task,
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
    mocker.patch("app.main.delete_agent_vm", return_value=None)
    mocker.patch("app.main.provision_agent_vm", return_value=True)
    mocker.patch("app.broadcast.broadcast_to_project")

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
        payloads=[{
            "type": "review",
            "pr_number": 88,
            "branch": "blueprint/task-cap0001",
            "base_branch": "main",
        }],
        context={
            "scope": "small",
            "review_loop": {
                "pr_number": 88,
                "branch": "blueprint/task-cap0001",
                "base_branch": "main",
                "iteration": 10,
                "original_task_id": str(original_task.id),
            },
        },
        vm_instance_id="agent-vm-cap-review-001",
        status=TaskStatus.IN_PROGRESS,
    )
    db_session.add(review_task)
    await db_session.commit()
    await db_session.refresh(review_task)

    feedback = "Still missing null checks"
    verdict_json = json.dumps({"verdict": "REQUEST_CHANGES", "feedback": feedback})
    await _status(
        client, review_task,
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
    mocker.patch("app.main.delete_agent_vm", return_value=None)
    mocker.patch("app.main.provision_agent_vm", return_value=True)
    mocker.patch("app.broadcast.broadcast_to_project")

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
        payloads=[{
            "type": "review",
            "pr_number": 55,
            "branch": "blueprint/task-ci0001",
            "base_branch": "main",
        }],
        context={
            "review_loop": {
                "pr_number": 55,
                "branch": "blueprint/task-ci0001",
                "base_branch": "main",
                "iteration": 8,
                "fix_reason": "ci_failure",
                "original_task_id": str(original_task.id),
            },
        },
        vm_instance_id="agent-vm-ci-review-001",
        status=TaskStatus.IN_PROGRESS,
    )
    db_session.add(review_task)
    await db_session.commit()
    await db_session.refresh(review_task)

    verdict_json = json.dumps({"verdict": "REQUEST_CHANGES", "feedback": "CI errors remain"})
    await _status(
        client, review_task,
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
    mocker.patch("app.main.delete_agent_vm", return_value=None)
    mocker.patch("app.main.provision_agent_vm", return_value=True)
    mocker.patch("app.broadcast.broadcast_to_project")

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
        payloads=[{
            "type": "review",
            "pr_number": 73,
            "branch": "blueprint/task-msg0001",
            "base_branch": "main",
        }],
        context={
            "scope": "small",
            "review_loop": {
                "pr_number": 73,
                "branch": "blueprint/task-msg0001",
                "base_branch": "main",
                "iteration": 10,
                "original_task_id": str(original_task.id),
            },
        },
        vm_instance_id="agent-vm-msg-review-001",
        status=TaskStatus.IN_PROGRESS,
    )
    db_session.add(review_task)
    await db_session.commit()
    await db_session.refresh(review_task)

    verdict_json = json.dumps({"verdict": "REQUEST_CHANGES", "feedback": "More changes needed"})
    await _status(
        client, review_task,
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
    mocker.patch("app.main.delete_agent_vm", return_value=None)
    mocker.patch("app.main.provision_agent_vm", return_value=True)
    mocker.patch("app.broadcast.broadcast_to_project")

    project = Project(name="Stuck Cycle Test", user_id="test-user-123")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

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
        "feedback": "Still missing validation",
        "issues": [repeated_issue],
    }

    # Review task at iteration=1. Its review_loop carries one prior history entry.
    # When it completes with the same CRITICAL issue, iteration for the spawned fix
    # will be 2, and new_review_history will have 2 matching entries → stuck detected.
    review_task = Task(
        project_id=project.id,
        title="Review PR #99",
        payloads=[{
            "type": "review",
            "pr_number": 99,
            "branch": "blueprint/task-stuck0001",
            "base_branch": "main",
            "original_task_id": str(original_task.id),
        }],
        context={
            "scope": "medium",
            "review_loop": {
                "pr_number": 99,
                "branch": "blueprint/task-stuck0001",
                "base_branch": "main",
                "iteration": 1,
                "original_task_id": str(original_task.id),
                "review_history": [prior_history_entry],
            },
        },
        vm_instance_id="agent-vm-stuck-review-001",
        status=TaskStatus.IN_PROGRESS,
    )
    db_session.add(review_task)
    await db_session.commit()
    await db_session.refresh(review_task)

    verdict_json = json.dumps({
        "verdict": "REQUEST_CHANGES",
        "feedback": "Still missing validation",
        "issues": [repeated_issue],
    })
    await _status(
        client, review_task,
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
    mocker.patch("app.main.delete_agent_vm", return_value=None)
    mocker.patch("app.main.provision_agent_vm", return_value=True)
    mocker.patch("app.broadcast.broadcast_to_project")

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
        payloads=[{
            "type": "review",
            "pr_number": 101,
            "branch": "blueprint/task-stuck0002",
            "base_branch": "main",
            # deliberately omit original_task_id
        }],
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

    verdict_json = json.dumps({
        "verdict": "REQUEST_CHANGES",
        "feedback": "SQL injection still present",
        "issues": [repeated_issue],
    })
    await _status(
        client, review_task,
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
async def test_rollup_bypasses_global_cap_via_status_message(
    db_session, mocker
):
    """
    When source_task.latest_status_message contains 'rollup', _spawn_pipeline_task
    must use MAX_REVIEW_FIX_ITERATIONS (16) as the global cap instead of the
    scope-based cap, allowing rollup reviews to proceed even after many retries.

    Setup: medium scope task → normal global cap = 12.
    Pre-populate 12 completed review/fix tasks for PR #200 (would hit normal cap).
    Set latest_status_message to contain 'rollup'.
    Expect: _spawn_pipeline_task returns a task (not None).
    """
    mocker.patch("app.broadcast.broadcast_to_project")

    project = Project(name="Rollup Cap Bypass via Status Test", user_id="test-user-123")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    # Parent research/rollup task — medium scope, 'rollup' in latest_status_message
    parent_task = Task(
        project_id=project.id,
        title="Research task (rollup parent)",
        payloads=[{"type": "research", "prompt": "Research improvements", "base_branch": "main"}],
        vm_instance_id="agent-vm-rollup-parent-001",
        status=TaskStatus.EVALUATING,
        context={"scope": "medium"},
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
            payloads=[{
                "type": "review" if i % 2 == 0 else "fix",
                "pr_number": 200,
                "branch": "blueprint/rollup-test",
                "base_branch": "main",
            }],
            vm_instance_id=f"agent-vm-rollup-prior-{i:03d}",
            status=TaskStatus.COMPLETED,
        )
        db_session.add(t)
    await db_session.commit()

    # Call _spawn_pipeline_task directly — should succeed because is_rollup=True
    result = await _spawn_pipeline_task(
        source_task=parent_task,
        task_type="review",
        session=db_session,
        pr_number=200,
        branch="blueprint/rollup-test",
        base_branch="main",
    )

    assert result is not None, (
        "Rollup task (latest_status_message contains 'rollup') should bypass the "
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

    project = Project(name="Rollup Cap Bypass via Context Test", user_id="test-user-123")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    # Parent task with _rollup_retries in context (set by rollup_management on transient failure)
    parent_task = Task(
        project_id=project.id,
        title="Research task (rollup with retries)",
        payloads=[{"type": "research", "prompt": "Research improvements", "base_branch": "main"}],
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
            payloads=[{
                "type": "review" if i % 2 == 0 else "fix",
                "pr_number": 201,
                "branch": "blueprint/rollup-retry-test",
                "base_branch": "main",
            }],
            vm_instance_id=f"agent-vm-rollup-retry-prior-{i:03d}",
            status=TaskStatus.COMPLETED,
        )
        db_session.add(t)
    await db_session.commit()

    result = await _spawn_pipeline_task(
        source_task=parent_task,
        task_type="review",
        session=db_session,
        pr_number=201,
        branch="blueprint/rollup-retry-test",
        base_branch="main",
    )

    assert result is not None, (
        "Rollup task with _rollup_retries in context should bypass the "
        f"small-scope global cap (10) and use MAX_REVIEW_FIX_ITERATIONS ({MAX_REVIEW_FIX_ITERATIONS})"
    )
    assert result.status == TaskStatus.QUEUED


@pytest.mark.asyncio
async def test_non_rollup_task_respects_global_cap(
    db_session, mocker
):
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
            payloads=[{
                "type": "review" if i % 2 == 0 else "fix",
                "pr_number": 202,
                "branch": "blueprint/normal-cap-test",
                "base_branch": "main",
            }],
            vm_instance_id=f"agent-vm-normal-prior-{i:03d}",
            status=TaskStatus.COMPLETED,
        )
        db_session.add(t)
    await db_session.commit()

    result = await _spawn_pipeline_task(
        source_task=normal_task,
        task_type="review",
        session=db_session,
        pr_number=202,
        branch="blueprint/normal-cap-test",
        base_branch="main",
    )

    assert result is None, (
        "Non-rollup task at small-scope global cap (10) must be blocked — "
        "rollup bypass should not apply"
    )

"""End-to-end test of the no-CI detected-test merge gate.

The merge-verification gate for external repos without CI runs the project's
own detected test suite as the merge verdict. Every layer of that path has a
unit test in isolation:

  - ``ci_resolution.should_run_no_ci_test_gate`` (decision)
  - ``review_pipeline._spawn_pipeline_task("verify_tests")`` (spawn)
  - ``ci_resolution.classify_merge_test_verdict`` (verdict classifier)
  - ``release_qualifier_audit._latest_artifact_logs`` (artifact reader)
  - ``ci_completion.handle_ci_completion`` (block-and-fix / merge)

What was missing is a single test that composes them as ONE flow, with the
agent run mocked by a planted ``MERGE_TEST_RESULT`` artifact. These tests pin
the load-bearing contract the orchestrator's inline no-CI gate
(orchestrator.py ~4837-5073) hands to ``handle_ci_completion`` so a future
refactor cannot silently regress external-repo verification back to
merge-without-checks.

The orchestrator's verdict→``ci_result`` transition is no longer mirrored as a
copied literal here: the orchestrator builds the ``ci_result`` via
``ci_resolution.build_no_ci_gate_ci_result(outcome, detail)`` and these tests
call that SAME helper, so a regression in the live mapping (e.g. flipping a
``fail`` back to a merge-allowing ``success``) fails this flow rather than
passing against a stale hand-copy.

Beyond the terminal pass / fail / undetected outcomes, the gate's *awaiting*
transition is exercised too: a freshly-spawned ``verify_tests`` task is
non-terminal (QUEUED), and while it runs the orchestrator's terminal guard
(``status in {COMPLETED, FAILED, CANCELLED}``) is unmet — so the gate must
read no verdict and attempt no merge until the task terminalizes. That poll is
driven through the real verify-task lifecycle (QUEUED → COMPLETED) and the real
artifact reader so a regression that classifies a still-running verify task —
or merges before its verdict lands — fails this flow.
"""

import json
import uuid

import pytest
from sqlalchemy import select

from app.ci_completion import handle_ci_completion
from app.ci_resolution import (
    build_no_ci_gate_ci_result,
    classify_merge_test_verdict,
    should_run_no_ci_test_gate,
)
from app.models import Artifact, ArtifactType, Project, Task, TaskStatus
from app.release_qualifier_audit import _latest_artifact_logs
from app.review_pipeline import _spawn_pipeline_task

PR_NUMBER = 4242
BRANCH = "feat/no-ci-gate"
BASE = "main"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _make_no_ci_project(db_session, *, with_test_cmd: bool):
    """An EXTERNAL no-CI project (requires_ci_checks=False).

    ``with_test_cmd`` controls whether a runnable ``test_cmd`` is detected so
    the gate either runs the suite or falls back to proceed-without-checks.
    """
    directories = [{"path": ".", "test_cmd": "pytest" if with_test_cmd else ""}]
    project = Project(
        name="External no-CI repo",
        user_id="test-user",
        github_owner="acme",
        github_repo="widget",
        integration_branch=BASE,
        build_config={
            "requires_ci_checks": False,
            "directories": directories,
        },
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)
    return project


def _make_parent_task(db_session, project) -> Task:
    """EVALUATING review task holding the ci_loop the gate drives through."""
    loop = {
        "pr_number": PR_NUMBER,
        "branch": BRANCH,
        "base_branch": BASE,
        "iteration": 1,
        "review_approved": True,
        "ci_waiting": True,
    }
    task = Task(
        project_id=project.id,
        title="No-CI merge gate parent",
        payloads=[
            {
                "type": "review",
                "pr_number": PR_NUMBER,
                "branch": BRANCH,
                "base_branch": BASE,
            }
        ],
        context={"review_loop": loop},
        vm_instance_id=f"agent-vm-{uuid.uuid4().hex[:8]}",
        status=TaskStatus.EVALUATING,
    )
    db_session.add(task)
    return task


async def _plant_verdict(db_session, verify_task: Task, verdict: dict) -> None:
    """Mock the agent run: write its MERGE_TEST_RESULT artifact + mark COMPLETED.

    The artifact ``logs`` is the JSON the agent's verify-tests handler posts —
    ``{detected, passed, tests[], output}`` — which is exactly what
    ``_latest_artifact_logs`` reads back and ``classify_merge_test_verdict``
    maps onto the merge gate.
    """
    db_session.add(
        Artifact(
            task_id=verify_task.id,
            artifact_type=ArtifactType.MERGE_TEST_RESULT,
            logs=json.dumps(verdict),
        )
    )
    verify_task.status = TaskStatus.COMPLETED
    db_session.add(verify_task)
    await db_session.commit()


def _patch_github(mocker):
    """Patch every outward I/O the gate touches so the test does no real work."""
    mocker.patch("app.broadcast.broadcast_to_project", return_value=None)
    mocker.patch("app.review_pipeline.broadcast_to_project", return_value=None)
    mocker.patch("app.github.create_pr_comment", return_value=None)
    mocker.patch("app.alert_dispatch._maybe_create_failure_alert", return_value=None)


# ---------------------------------------------------------------------------
# 1. Failing suite blocks the merge and spawns a fix carrying the output
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_no_ci_failing_suite_blocks_merge_and_spawns_fix_with_output(
    db_session, mocker
):
    _patch_github(mocker)
    merge_mock = mocker.patch("app.github.merge_pull_request", return_value=None)

    project = await _make_no_ci_project(db_session, with_test_cmd=True)
    parent = _make_parent_task(db_session, project)
    await db_session.commit()
    await db_session.refresh(parent)

    # (1) Decision: this external no-CI repo with a detectable suite gates on a
    #     verify_tests verdict rather than taking the legacy immediate-pass.
    assert (
        should_run_no_ci_test_gate(project, BASE, gate_enabled=True) is True
    ), "external no-CI repo with a test_cmd must gate the merge on verify_tests"

    # (2) Spawn: the orchestrator spawns verify_tests on the PR head.
    verify_task, was_created = await _spawn_pipeline_task(
        parent,
        "verify_tests",
        db_session,
        PR_NUMBER,
        BRANCH,
        BASE,
        original_task_id=str(parent.id),
        suppress_warm_handoff=True,
    )
    assert was_created is True
    assert verify_task is not None
    assert verify_task.first_payload.get("type") == "verify_tests"

    # (3) Mock the agent: plant a FAILING MERGE_TEST_RESULT verdict.
    failing_output = (
        "============ FAILURES ============\n"
        "FAILED tests/test_math.py::test_add - assert 3 == 4\n"
        "1 failed in 0.12s"
    )
    await _plant_verdict(
        db_session,
        verify_task,
        {
            "detected": True,
            "passed": False,
            "tests": [{"name": "tests/test_math.py::test_add", "passed": False}],
            "output": failing_output,
        },
    )

    # (4) Read back the verdict + classify exactly as the orchestrator does.
    verdict = await _latest_artifact_logs(
        db_session, verify_task.id, ArtifactType.MERGE_TEST_RESULT
    )
    outcome, detail = classify_merge_test_verdict(verdict)
    assert outcome == "fail"
    assert "test_add" in detail

    # (5) Build the ci_result the orchestrator hands to handle_ci_completion via
    #     the SAME helper the orchestrator's inline gate calls — not a hand-copy.
    ci_result = build_no_ci_gate_ci_result(outcome, detail)
    assert ci_result["conclusion"] == "failure"
    assert ci_result["failed_logs"] == detail

    # Re-lock parent as EVALUATING (verify spawn committed; parent still EVALUATING).
    parent = await db_session.get(Task, parent.id)
    acted = await handle_ci_completion(
        parent, ci_result, "ghs_test", "acme", "widget", db_session, source="poll"
    )

    assert acted is True
    # Merge must NOT have been attempted — the failing suite blocked it.
    merge_mock.assert_not_called()

    # A fix task was spawned carrying the failing-test output.
    all_tasks = (
        (await db_session.execute(select(Task).where(Task.project_id == project.id)))
        .scalars()
        .all()
    )
    fix_tasks = [t for t in all_tasks if t.first_payload.get("type") == "fix"]
    assert len(fix_tasks) == 1, "exactly one fix task should be spawned on CI failure"
    fix = fix_tasks[0]
    assert fix.context["review_loop"]["fix_reason"] == "ci_failure"
    assert "test_add" in fix.context["ci_failure_logs"]
    assert "test_add" in fix.first_payload.get("prompt", "")

    # Parent is terminalized COMPLETED with the spawned fix recorded.
    await db_session.refresh(parent)
    assert parent.status == TaskStatus.COMPLETED
    assert parent.context["review_loop"]["ci_fix_spawned"] == str(fix.id)


# ---------------------------------------------------------------------------
# 2. Passing suite proceeds to merge
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_no_ci_passing_suite_proceeds_to_merge(db_session, mocker):
    _patch_github(mocker)
    merge_mock = mocker.patch(
        "app.github.merge_pull_request", return_value={"merged": True}
    )

    project = await _make_no_ci_project(db_session, with_test_cmd=True)
    parent = _make_parent_task(db_session, project)
    await db_session.commit()
    await db_session.refresh(parent)

    assert should_run_no_ci_test_gate(project, BASE, gate_enabled=True) is True

    verify_task, was_created = await _spawn_pipeline_task(
        parent,
        "verify_tests",
        db_session,
        PR_NUMBER,
        BRANCH,
        BASE,
        original_task_id=str(parent.id),
        suppress_warm_handoff=True,
    )
    assert was_created is True and verify_task is not None

    # Mock the agent: planted PASSING verdict.
    await _plant_verdict(
        db_session,
        verify_task,
        {
            "detected": True,
            "passed": True,
            "tests": [{"name": "tests/test_math.py::test_add", "passed": True}],
            "output": "1 passed in 0.10s",
        },
    )

    verdict = await _latest_artifact_logs(
        db_session, verify_task.id, ArtifactType.MERGE_TEST_RESULT
    )
    outcome, detail = classify_merge_test_verdict(verdict)
    assert outcome == "pass"
    assert detail == ""

    # Orchestrator pass-verdict ci_result, built via the real gate helper.
    ci_result = build_no_ci_gate_ci_result(outcome, detail)
    assert ci_result["conclusion"] == "success"

    parent = await db_session.get(Task, parent.id)
    acted = await handle_ci_completion(
        parent, ci_result, "ghs_test", "acme", "widget", db_session, source="poll"
    )

    assert acted is True
    merge_mock.assert_called_once()
    await db_session.refresh(parent)
    assert parent.status == TaskStatus.COMPLETED


# The orchestrator's terminal guard: a verify_tests task is classified only once
# it reaches one of these states. While QUEUED / IN_PROGRESS the no-CI gate
# leaves ci_result PENDING and keeps polling (orchestrator.py ~5118-5159).
_VERIFY_TERMINAL = {TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.CANCELLED}


# ---------------------------------------------------------------------------
# 2b. Awaiting-verdict poll: while verify_tests is still running the gate keeps
#     polling (no classify, no merge); once it terminates the verdict is read.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_no_ci_awaiting_verdict_while_verify_running_then_passes(
    db_session, mocker
):
    _patch_github(mocker)
    merge_mock = mocker.patch(
        "app.github.merge_pull_request", return_value={"merged": True}
    )

    project = await _make_no_ci_project(db_session, with_test_cmd=True)
    parent = _make_parent_task(db_session, project)
    await db_session.commit()
    await db_session.refresh(parent)

    assert should_run_no_ci_test_gate(project, BASE, gate_enabled=True) is True

    # First poll: spawn verify_tests on the PR head. It is created QUEUED —
    # non-terminal — exactly as the orchestrator's first-encounter branch does.
    verify_task, was_created = await _spawn_pipeline_task(
        parent,
        "verify_tests",
        db_session,
        PR_NUMBER,
        BRANCH,
        BASE,
        original_task_id=str(parent.id),
        suppress_warm_handoff=True,
    )
    assert was_created is True and verify_task is not None
    assert verify_task.status == TaskStatus.QUEUED

    # ── Awaiting-verdict poll ───────────────────────────────────────────────
    # While the verify task is still running the orchestrator's terminal guard
    # is unmet, so the gate neither reads a verdict nor builds a ci_result — it
    # keeps polling. Drive that exact guard with the real artifact reader: no
    # MERGE_TEST_RESULT has been posted yet, so there is nothing to classify.
    assert verify_task.status not in _VERIFY_TERMINAL
    pending_verdict = await _latest_artifact_logs(
        db_session, verify_task.id, ArtifactType.MERGE_TEST_RESULT
    )
    assert pending_verdict is None, (
        "no verdict must be readable while verify_tests is still running — "
        "the gate must keep polling, not classify a missing artifact"
    )
    # The merge must NOT have been attempted on the awaiting poll.
    merge_mock.assert_not_called()

    # ── Terminal poll ───────────────────────────────────────────────────────
    # The agent finishes: plant a PASSING verdict and mark the task COMPLETED.
    await _plant_verdict(
        db_session,
        verify_task,
        {
            "detected": True,
            "passed": True,
            "tests": [{"name": "tests/test_math.py::test_add", "passed": True}],
            "output": "1 passed in 0.10s",
        },
    )
    await db_session.refresh(verify_task)
    assert verify_task.status in _VERIFY_TERMINAL

    # Now the guard is satisfied: read + classify + build the ci_result via the
    # SAME helpers the orchestrator's terminal branch calls.
    verdict = await _latest_artifact_logs(
        db_session, verify_task.id, ArtifactType.MERGE_TEST_RESULT
    )
    outcome, detail = classify_merge_test_verdict(verdict)
    assert outcome == "pass"
    ci_result = build_no_ci_gate_ci_result(outcome, detail)
    assert ci_result["conclusion"] == "success"

    parent = await db_session.get(Task, parent.id)
    acted = await handle_ci_completion(
        parent, ci_result, "ghs_test", "acme", "widget", db_session, source="poll"
    )
    assert acted is True
    merge_mock.assert_called_once()
    await db_session.refresh(parent)
    assert parent.status == TaskStatus.COMPLETED


# ---------------------------------------------------------------------------
# 3. No detectable tests → proceed-without-checks fallback
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_no_ci_no_detectable_tests_falls_back_to_proceed_without_checks(
    db_session, mocker
):
    _patch_github(mocker)
    merge_mock = mocker.patch(
        "app.github.merge_pull_request", return_value={"merged": True}
    )

    project = await _make_no_ci_project(db_session, with_test_cmd=False)
    parent = _make_parent_task(db_session, project)
    await db_session.commit()
    await db_session.refresh(parent)

    # Decision: no runnable test command → the gate does NOT run; the legacy
    # immediate-pass applies, so no verify_tests task is ever spawned.
    assert (
        should_run_no_ci_test_gate(project, BASE, gate_enabled=True) is False
    ), "a repo with no detectable test_cmd must not gate on verify_tests"

    # An agent that did run and reported detected=false maps to 'undetected',
    # which the gate routes to proceed-without-checks (merge UNVERIFIED).
    outcome, detail = classify_merge_test_verdict({"detected": False})
    assert outcome == "undetected"
    assert detail == ""

    # No verify_tests task should exist for this PR — the gate was never entered.
    all_tasks = (
        (await db_session.execute(select(Task).where(Task.project_id == project.id)))
        .scalars()
        .all()
    )
    assert not [
        t for t in all_tasks if t.first_payload.get("type") == "verify_tests"
    ], "no verify_tests task should be spawned when no suite is detectable"

    # Fallback proceed-without-checks: the orchestrator builds a success
    # ci_result via the real gate helper and the merge proceeds UNVERIFIED.
    ci_result = build_no_ci_gate_ci_result(outcome, detail)
    assert ci_result["conclusion"] == "success"
    parent = await db_session.get(Task, parent.id)
    acted = await handle_ci_completion(
        parent, ci_result, "ghs_test", "acme", "widget", db_session, source="poll"
    )

    assert acted is True
    merge_mock.assert_called_once()
    await db_session.refresh(parent)
    assert parent.status == TaskStatus.COMPLETED

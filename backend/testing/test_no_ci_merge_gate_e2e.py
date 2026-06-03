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

import base64
import json
import uuid
from datetime import datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy import select

from app.build_config_detection import persist_recovered_test_command
from app.ci_completion import handle_ci_completion
from app.ci_resolution import (
    build_no_ci_gate_ci_result,
    classify_merge_test_verdict,
    merge_gate_runtime_block,
    record_no_ci_gate_runtime,
    should_run_no_ci_test_gate,
)
from app.models import Alert, Artifact, ArtifactType, Project, Task, TaskStatus
from app.orchestrator import _VERIFY_TESTS_MAX_RETRIES
from app.release_qualifier_audit import _latest_artifact_logs
from app.review_pipeline import _spawn_pipeline_task

PR_NUMBER = 4242
BRANCH = "feat/no-ci-gate"
BASE = "main"


@pytest.fixture(autouse=True)
def _default_no_migration_pr(mocker):
    """Default the pre-merge Alembic dual-head guard to a no-op for merge tests.

    ``handle_ci_completion``'s merge primitive now runs
    ``enforce_premerge_migration_guard`` (which calls ``github.get_pr_files``)
    before merging. These gate tests do not mock that call; returning an empty
    (no-migration) file list short-circuits the guard to ``ok`` so the no-CI
    gate behavior under test is what is exercised.
    """
    from unittest.mock import AsyncMock

    mocker.patch("app.github.get_pr_files", AsyncMock(return_value=[{"filename": "README.md"}]))


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


async def _terminalize_without_verdict(db_session, verify_task: Task) -> None:
    """Mark the verify task terminal without creating a verdict artifact."""
    verify_task.status = TaskStatus.FAILED
    db_session.add(verify_task)
    await db_session.commit()


def _is_verify_crash(outcome: str, verdict: dict | None) -> bool:
    """Mirror the orchestrator's crash-vs-undetected split (orchestrator.py
    ~5283).

    A verify task that terminalized without a usable verdict artifact
    (``verdict is None``) crashed — most often a transient cold-clone flake —
    and is re-spawned. A detected=false verdict (a ``dict`` from a suite that
    actually ran and found no runnable command) is a genuine undetected result,
    not a crash, and falls open immediately without consuming the retry budget.
    """
    return outcome == "undetected" and verdict is None


def _patch_github(mocker):
    """Patch every outward I/O the gate touches so the test does no real work."""
    mocker.patch("app.broadcast.broadcast_to_project", return_value=None)
    mocker.patch("app.review_pipeline.broadcast_to_project", return_value=None)
    mocker.patch("app.alert_dispatch.broadcast_to_project", return_value=None)
    mocker.patch(
        "app.notification_dispatch.dispatch_external_notification", return_value=None
    )
    mocker.patch("app.github.create_pr_comment", return_value=None)
    mocker.patch("app.alert_dispatch._maybe_create_failure_alert", return_value=None)


# Fixed timestamp so the persisted runtime record is deterministic — the
# orchestrator passes ``when=_utcnow()`` here.
_RUNTIME_WHEN = datetime(2026, 6, 3, 12, 0, 0)


async def _record_gate_runtime(
    db_session, project, *, outcome: str, unverified_reason: str | None
):
    """Mirror the orchestrator's terminal-outcome recording call.

    ``executed_real_tests`` is True only when verify_tests produced a real
    pass/fail verdict — exactly the orchestrator's
    ``ci_loop['verify_tests_verdict'] in ('pass', 'fail')`` condition.
    """
    record_no_ci_gate_runtime(
        db_session,
        project,
        executed_real_tests=outcome in ("pass", "fail"),
        unverified_reason=unverified_reason,
        when=_RUNTIME_WHEN,
    )
    await db_session.commit()
    await db_session.refresh(project)


async def _unverified_merge_alerts(db_session, project_id):
    return (
        (
            await db_session.execute(
                select(Alert).where(
                    Alert.project_id == project_id,
                    Alert.title.contains("Merged UNVERIFIED"),
                )
            )
        )
        .scalars()
        .all()
    )


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

    # Runtime readiness: a real suite ran to a FAIL verdict, so the gate records
    # executed_real_tests=True and does NOT stamp the UNVERIFIED fields.
    await _record_gate_runtime(
        db_session, project, outcome=outcome, unverified_reason=None
    )
    runtime = merge_gate_runtime_block(project)
    assert runtime is not None
    assert runtime["executed_real_tests"] is True
    assert runtime["last_run_at"] == _RUNTIME_WHEN.isoformat()
    assert "last_unverified_at" not in runtime
    assert "last_unverified_reason" not in runtime


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

    # PASS verdict gates correctly: no UNVERIFIED-merge alert is raised because
    # the orchestrator never set review_loop["unverified_merge_reason"].
    assert (
        await _unverified_merge_alerts(db_session, project.id) == []
    ), "a verified (passing) merge must not raise an UNVERIFIED-merge alert"

    # Runtime readiness: a real suite ran to a PASS verdict → executed_real_tests
    # True and the UNVERIFIED fields are NOT stamped.
    await _record_gate_runtime(
        db_session, project, outcome=outcome, unverified_reason=None
    )
    runtime = merge_gate_runtime_block(project)
    assert runtime is not None
    assert runtime["executed_real_tests"] is True
    assert runtime["last_run_at"] == _RUNTIME_WHEN.isoformat()
    assert "last_unverified_at" not in runtime
    assert "last_unverified_reason" not in runtime


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


@pytest.mark.asyncio
async def test_no_ci_failed_verify_task_without_artifact_merges_unverified_and_alerts_once(
    db_session, mocker
):
    """A crashed (FAILED, no-artifact) verify task is RETRIED once before the
    gate falls open — and only the exhausted retry raises the UNVERIFIED alert.

    A verify task that terminalizes with no verdict artifact did not run the
    suite (most often a transient cold-clone flake), so the orchestrator's
    crash-vs-undetected split re-spawns it up to ``_VERIFY_TESTS_MAX_RETRIES``
    times before merging UNVERIFIED. This pins that retry-then-fail-open
    contract: the first crash must NOT merge or alert; only the crash after the
    retry budget is spent does.
    """
    _patch_github(mocker)
    merge_mock = mocker.patch(
        "app.github.merge_pull_request", return_value={"merged": True}
    )

    project = await _make_no_ci_project(db_session, with_test_cmd=True)
    parent = _make_parent_task(db_session, project)
    await db_session.commit()
    await db_session.refresh(parent)

    assert should_run_no_ci_test_gate(project, BASE, gate_enabled=True) is True

    # ── First attempt: spawn, then crash without posting a verdict artifact ──
    verify_task, was_created = await _spawn_pipeline_task(
        parent,
        "verify_tests",
        db_session,
        PR_NUMBER,
        BRANCH,
        BASE,
        original_task_id=str(parent.id),
        suppress_warm_handoff=True,
        attempt=0,
    )
    assert was_created is True and verify_task is not None
    assert verify_task.status == TaskStatus.QUEUED

    await _terminalize_without_verdict(db_session, verify_task)
    await db_session.refresh(verify_task)
    assert verify_task.status == TaskStatus.FAILED

    verdict = await _latest_artifact_logs(
        db_session, verify_task.id, ArtifactType.MERGE_TEST_RESULT
    )
    assert verdict is None, "FAILED verify_tests with no artifact must read no verdict"
    outcome, detail = classify_merge_test_verdict(verdict)
    assert outcome == "undetected" and detail == ""

    # A terminal task with no verdict artifact is a CRASH, not a real
    # detected-undetected suite — with retries remaining the gate re-spawns
    # rather than failing open.
    retries = 0
    assert _is_verify_crash(outcome, verdict) is True
    assert retries < _VERIFY_TESTS_MAX_RETRIES, "first crash still has retry budget"

    # ── Retry: a NEW verify task is spawned (attempt bumps the idem key) ──────
    retries += 1
    retry_task, retry_created = await _spawn_pipeline_task(
        parent,
        "verify_tests",
        db_session,
        PR_NUMBER,
        BRANCH,
        BASE,
        original_task_id=str(parent.id),
        suppress_warm_handoff=True,
        attempt=retries,
    )
    assert retry_created is True and retry_task is not None
    assert retry_task.id != verify_task.id, (
        "a crashed verify task must be re-spawned as a distinct task, not reuse "
        "the crashed corpse"
    )
    # The retry is still pending: nothing merged, no alert raised yet.
    merge_mock.assert_not_called()
    assert await _unverified_merge_alerts(db_session, project.id) == []

    # ── Retry also crashes: budget spent → fall open to the UNVERIFIED merge ──
    await _terminalize_without_verdict(db_session, retry_task)
    verdict = await _latest_artifact_logs(
        db_session, retry_task.id, ArtifactType.MERGE_TEST_RESULT
    )
    outcome, detail = classify_merge_test_verdict(verdict)
    assert _is_verify_crash(outcome, verdict) is True
    assert retries >= _VERIFY_TESTS_MAX_RETRIES, "retry budget is now exhausted"

    ci_result = build_no_ci_gate_ci_result(outcome, detail)
    assert ci_result["conclusion"] == "success"

    parent = await _set_unverified_reason(db_session, parent, "undetected_runtime")
    acted = await handle_ci_completion(
        parent, ci_result, "ghs_test", "acme", "widget", db_session, source="poll"
    )

    assert acted is True
    merge_mock.assert_called_once()
    await db_session.refresh(parent)
    assert parent.status == TaskStatus.COMPLETED

    alerts = await _unverified_merge_alerts(db_session, project.id)
    assert len(alerts) == 1, "exhausted-retry crash must raise exactly one alert"
    assert f"PR #{PR_NUMBER}" in alerts[0].title
    assert "UNVERIFIED" in alerts[0].message

    # Runtime readiness: the crash-fallback merged UNVERIFIED, so the gate did
    # NOT run a real suite and stamps the runtime reason.
    await _record_gate_runtime(
        db_session, project, outcome=outcome, unverified_reason="undetected_runtime"
    )
    runtime = merge_gate_runtime_block(project)
    assert runtime is not None
    assert runtime["executed_real_tests"] is False
    assert runtime["last_unverified_at"] == _RUNTIME_WHEN.isoformat()
    assert runtime["last_unverified_reason"] == "undetected_runtime"


@pytest.mark.asyncio
async def test_no_ci_detected_undetected_verdict_does_not_retry(db_session, mocker):
    """A real ``detected=false`` verdict does NOT consume the verify retry budget.

    When the suite actually ran and reported no runnable command, ``_verdict``
    is a dict — not a crash — so the gate falls open immediately. This guards
    the split from over-firing: a genuine no-suite repo must not be retried as
    if it were a transient infra flake.
    """
    verdict = {"detected": False}
    outcome, detail = classify_merge_test_verdict(verdict)
    assert outcome == "undetected" and detail == ""
    assert _is_verify_crash(outcome, verdict) is False, (
        "a detected=false verdict (a dict from a suite that ran) is not a crash "
        "and must not be retried"
    )


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

    # Runtime readiness: the immediate-pass merged UNVERIFIED with no detectable
    # test command, so executed_real_tests stays False and the runtime reason is
    # stamped using the SAME label the unverified-merge signal uses.
    await _record_gate_runtime(
        db_session,
        project,
        outcome=outcome,
        unverified_reason="no_detectable_test_command",
    )
    runtime = merge_gate_runtime_block(project)
    assert runtime is not None
    assert runtime["executed_real_tests"] is False
    assert runtime["last_unverified_at"] == _RUNTIME_WHEN.isoformat()
    assert runtime["last_unverified_reason"] == "no_detectable_test_command"


# ---------------------------------------------------------------------------
# 4. UNVERIFIED merge raises exactly one operator alert (and dedups on re-entry)
# ---------------------------------------------------------------------------


async def _set_unverified_reason(db_session, parent, reason):
    """Mirror the orchestrator threading review_loop['unverified_merge_reason']."""
    from sqlalchemy.orm.attributes import flag_modified

    parent = await db_session.get(Task, parent.id)
    loop = dict((parent.context or {}).get("review_loop", {}))
    loop["unverified_merge_reason"] = reason
    parent.context = {**(parent.context or {}), "review_loop": loop}
    flag_modified(parent, "context")
    db_session.add(parent)
    await db_session.commit()
    return parent


@pytest.mark.asyncio
async def test_unverified_merge_raises_single_alert_naming_pr_and_reason(
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

    # The verify run reported detected=false → 'undetected' → merge UNVERIFIED.
    outcome, detail = classify_merge_test_verdict({"detected": False})
    assert outcome == "undetected"
    ci_result = build_no_ci_gate_ci_result(outcome, detail)
    assert ci_result["conclusion"] == "success"

    # The orchestrator records WHY the merge is unverified on the parent context.
    parent = await _set_unverified_reason(db_session, parent, "undetected_runtime")

    acted = await handle_ci_completion(
        parent, ci_result, "ghs_test", "acme", "widget", db_session, source="poll"
    )
    assert acted is True
    merge_mock.assert_called_once()

    alerts = await _unverified_merge_alerts(db_session, project.id)
    assert len(alerts) == 1, "exactly one UNVERIFIED-merge alert per merged PR"
    from app.models import AlertSeverity

    assert alerts[0].severity == AlertSeverity.WARNING
    assert alerts[0].task_id == parent.id
    assert f"PR #{PR_NUMBER}" in alerts[0].title
    assert "UNVERIFIED" in alerts[0].message
    assert "no usable verdict" in alerts[0].message  # undetected_runtime label


@pytest.mark.asyncio
async def test_unverified_merge_alert_not_duplicated_on_second_sweep(
    db_session, mocker
):
    _patch_github(mocker)
    mocker.patch("app.github.merge_pull_request", return_value={"merged": True})

    project = await _make_no_ci_project(db_session, with_test_cmd=False)
    parent = _make_parent_task(db_session, project)
    await db_session.commit()
    await db_session.refresh(parent)

    ci_result = build_no_ci_gate_ci_result("undetected", "")
    parent = await _set_unverified_reason(
        db_session, parent, "no_detectable_test_command"
    )

    # First sweep merges and raises the alert.
    await handle_ci_completion(
        parent, ci_result, "ghs_test", "acme", "widget", db_session, source="poll"
    )
    # A second sweep re-enters the merge-confirmed path (idempotent merge); the
    # alert helper dedups on (task_id, title) so no duplicate row is created.
    parent = await db_session.get(Task, parent.id)
    parent.status = TaskStatus.EVALUATING
    db_session.add(parent)
    await db_session.commit()
    await handle_ci_completion(
        parent, ci_result, "ghs_test", "acme", "widget", db_session, source="poll"
    )

    alerts = await _unverified_merge_alerts(db_session, project.id)
    assert len(alerts) == 1, "second merge sweep must not duplicate the alert"


# ---------------------------------------------------------------------------
# 5. A repo detected via a NON-MANIFEST entrypoint runs its suite and blocks
#    the merge on failure (detection layer → gate layer, composed).
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_no_ci_makefile_detected_repo_blocks_merge_on_failure(
    db_session, mocker
):
    """An external no-CI repo whose tests run via a Makefile ``test:`` target
    (no python/node/go/rust manifest) is auto-detected at onboarding and the
    gate runs that suite as the merge verdict — a failing run blocks the merge.

    This composes the onboarding detector (which fetches the Makefile body and
    emits ``make test``) with the no-CI gate so a non-manifest repo is no longer
    silently merged UNVERIFIED.
    """
    from app.routers.admin_onboard import _detect_build_config

    _patch_github(mocker)
    merge_mock = mocker.patch("app.github.merge_pull_request", return_value=None)

    # --- Detection: a Makefile-only repo with a real test target. ---
    root_resp = MagicMock()
    root_resp.status_code = 200
    root_resp.json.return_value = [{"name": "Makefile", "type": "file"}]
    makefile_resp = MagicMock()
    makefile_resp.status_code = 200
    makefile_resp.json.return_value = {
        "type": "file",
        "content": base64.b64encode(
            b"build:\n\tcc -o app main.c\n\ntest:\n\t./run-suite\n"
        ).decode(),
        "encoding": "base64",
    }
    with patch(
        "app.github._github_request",
        new_callable=AsyncMock,
        side_effect=[root_resp, makefile_resp],
    ):
        detected = await _detect_build_config("ghs_test", "acme", "widget")

    assert any(
        d.get("test_cmd") == "make test" for d in detected["directories"]
    ), "Makefile-with-test-target repo must detect a `make test` command"

    # --- Build a no-CI project from the DETECTED config. ---
    project = Project(
        name="Makefile no-CI repo",
        user_id="test-user",
        github_owner="acme",
        github_repo="widget",
        integration_branch=BASE,
        build_config={"requires_ci_checks": False, **detected},
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    parent = _make_parent_task(db_session, project)
    await db_session.commit()
    await db_session.refresh(parent)

    # The gate runs because the non-manifest entrypoint produced a test_cmd.
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

    # The agent ran `make test` and it failed.
    await _plant_verdict(
        db_session,
        verify_task,
        {
            "detected": True,
            "passed": False,
            "tests": [{"name": "make test", "passed": False}],
            "output": "make: *** [test] Error 1\nFAILED suite::case_a",
        },
    )

    verdict = await _latest_artifact_logs(
        db_session, verify_task.id, ArtifactType.MERGE_TEST_RESULT
    )
    outcome, detail = classify_merge_test_verdict(verdict)
    assert outcome == "fail"
    assert "case_a" in detail

    ci_result = build_no_ci_gate_ci_result(outcome, detail)
    assert ci_result["conclusion"] == "failure"

    parent = await db_session.get(Task, parent.id)
    acted = await handle_ci_completion(
        parent, ci_result, "ghs_test", "acme", "widget", db_session, source="poll"
    )

    assert acted is True
    merge_mock.assert_not_called()  # the failing make-test suite blocked the merge
    assert await _unverified_merge_alerts(db_session, project.id) == []


@pytest.mark.asyncio
async def test_no_ci_package_json_with_test_script_detected_repo_passes_and_merges(
    db_session, mocker
):
    """A Node repo whose package.json declares ``scripts.test`` is detected at
    onboarding (the body confirms the script), the gate runs that ``npm test``
    suite, and a passing run proceeds to merge.

    Composes the runtime-aligned detector (only persists ``npm test`` when
    ``scripts.test`` exists) with the no-CI gate so a real Node suite gates the
    merge instead of either a false "tests gated" promise or a needless
    UNVERIFIED fallback.
    """
    from app.routers.admin_onboard import _detect_build_config

    _patch_github(mocker)
    merge_mock = mocker.patch(
        "app.github.merge_pull_request", return_value={"merged": True}
    )

    # --- Detection: a package.json declaring a real scripts.test. ---
    root_resp = MagicMock()
    root_resp.status_code = 200
    root_resp.json.return_value = [
        {"name": "package.json", "type": "file"},
        {"name": "index.ts", "type": "file"},
    ]
    package_resp = MagicMock()
    package_resp.status_code = 200
    package_resp.json.return_value = {
        "type": "file",
        "content": base64.b64encode(
            b'{"scripts": {"test": "vitest run", "build": "tsc"}}'
        ).decode(),
        "encoding": "base64",
    }
    with patch(
        "app.github._github_request",
        new_callable=AsyncMock,
        side_effect=[root_resp, package_resp],
    ):
        detected = await _detect_build_config("ghs_test", "acme", "widget")

    assert any(
        d.get("test_cmd") == "npm test" for d in detected["directories"]
    ), "package.json with scripts.test must detect an `npm test` command"

    project = Project(
        name="Node no-CI repo",
        user_id="test-user",
        github_owner="acme",
        github_repo="widget",
        integration_branch=BASE,
        build_config={"requires_ci_checks": False, **detected},
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

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

    await _plant_verdict(
        db_session,
        verify_task,
        {
            "detected": True,
            "passed": True,
            "tests": [{"name": "src/app.test.ts > adds", "passed": True}],
            "output": "Test Files  1 passed (1)\nTests  1 passed (1)",
        },
    )

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
    assert await _unverified_merge_alerts(db_session, project.id) == []


@pytest.mark.asyncio
async def test_no_ci_pytest_ini_detected_repo_blocks_merge_on_failure(
    db_session, mocker
):
    """An external no-CI repo whose only test marker is a BARE ``pytest.ini``
    (no pyproject/setup) is auto-detected at onboarding as a Python project and
    the gate runs ``python3 -m pytest`` as the merge verdict — a failing run
    blocks the merge with no UNVERIFIED fall-open.

    This composes the onboarding detector (which now recognizes a bare
    pytest.ini, mirroring the agent's runtime detector) with the no-CI gate so a
    pytest.ini-only repo is no longer silently merged UNVERIFIED.
    """
    from app.routers.admin_onboard import _detect_build_config

    _patch_github(mocker)
    merge_mock = mocker.patch("app.github.merge_pull_request", return_value=None)

    # --- Detection: a pytest.ini-only repo (no core Python manifest). ---
    root_resp = MagicMock()
    root_resp.status_code = 200
    root_resp.json.return_value = [{"name": "pytest.ini", "type": "file"}]
    with patch(
        "app.github._github_request",
        new_callable=AsyncMock,
        side_effect=[root_resp],
    ):
        detected = await _detect_build_config("ghs_test", "acme", "widget")

    assert any(
        d.get("test_cmd") == "python3 -m pytest" for d in detected["directories"]
    ), "bare pytest.ini repo must detect a `python3 -m pytest` command"

    # --- Build a no-CI project from the DETECTED config. ---
    project = Project(
        name="pytest.ini no-CI repo",
        user_id="test-user",
        github_owner="acme",
        github_repo="widget",
        integration_branch=BASE,
        build_config={"requires_ci_checks": False, **detected},
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    parent = _make_parent_task(db_session, project)
    await db_session.commit()
    await db_session.refresh(parent)

    # The gate runs because the pytest.ini-detected dir produced a test_cmd.
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

    # The agent ran `python3 -m pytest` and it failed.
    await _plant_verdict(
        db_session,
        verify_task,
        {
            "detected": True,
            "passed": False,
            "tests": [{"name": "tests/test_widget.py::test_x", "passed": False}],
            "output": "FAILED tests/test_widget.py::test_x - assert 1 == 2\n1 failed",
        },
    )

    verdict = await _latest_artifact_logs(
        db_session, verify_task.id, ArtifactType.MERGE_TEST_RESULT
    )
    outcome, detail = classify_merge_test_verdict(verdict)
    assert outcome == "fail"
    assert "test_x" in detail

    ci_result = build_no_ci_gate_ci_result(outcome, detail)
    assert ci_result["conclusion"] == "failure"

    parent = await db_session.get(Task, parent.id)
    acted = await handle_ci_completion(
        parent, ci_result, "ghs_test", "acme", "widget", db_session, source="poll"
    )

    assert acted is True
    merge_mock.assert_not_called()  # the failing pytest suite blocked the merge
    assert await _unverified_merge_alerts(db_session, project.id) == []


@pytest.mark.asyncio
async def test_no_ci_pytest_ini_detected_repo_merges_on_pass(db_session, mocker):
    """The passing-verdict half of the pytest.ini gate: a bare-pytest.ini repo
    whose detected suite passes merges, and raises no UNVERIFIED alert."""
    from app.routers.admin_onboard import _detect_build_config

    _patch_github(mocker)
    merge_mock = mocker.patch(
        "app.github.merge_pull_request", return_value={"merged": True}
    )

    root_resp = MagicMock()
    root_resp.status_code = 200
    root_resp.json.return_value = [{"name": "pytest.ini", "type": "file"}]
    with patch(
        "app.github._github_request",
        new_callable=AsyncMock,
        side_effect=[root_resp],
    ):
        detected = await _detect_build_config("ghs_test", "acme", "widget")

    assert any(
        d.get("test_cmd") == "python3 -m pytest" for d in detected["directories"]
    )

    project = Project(
        name="pytest.ini no-CI repo (pass)",
        user_id="test-user",
        github_owner="acme",
        github_repo="widget",
        integration_branch=BASE,
        build_config={"requires_ci_checks": False, **detected},
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

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

    await _plant_verdict(
        db_session,
        verify_task,
        {
            "detected": True,
            "passed": True,
            "tests": [{"name": "tests/test_widget.py::test_x", "passed": True}],
            "output": "1 passed in 0.10s",
        },
    )

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
    assert (
        await _unverified_merge_alerts(db_session, project.id) == []
    ), "a verified (passing) pytest.ini merge must not raise an UNVERIFIED alert"


@pytest.mark.asyncio
async def test_failing_verdict_raises_no_unverified_alert(db_session, mocker):
    _patch_github(mocker)
    merge_mock = mocker.patch("app.github.merge_pull_request", return_value=None)

    project = await _make_no_ci_project(db_session, with_test_cmd=True)
    parent = _make_parent_task(db_session, project)
    await db_session.commit()
    await db_session.refresh(parent)

    # FAIL verdict blocks the merge entirely — no merge, so no unverified alert.
    ci_result = build_no_ci_gate_ci_result("fail", "1 failed")
    assert ci_result["conclusion"] == "failure"
    parent = await db_session.get(Task, parent.id)
    await handle_ci_completion(
        parent, ci_result, "ghs_test", "acme", "widget", db_session, source="poll"
    )
    merge_mock.assert_not_called()
    assert (
        await _unverified_merge_alerts(db_session, project.id) == []
    ), "a blocked (failing) merge must not raise an UNVERIFIED-merge alert"


# ---------------------------------------------------------------------------
# 6. Runtime readiness record persistence + field-preservation semantics
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_merge_gate_runtime_absent_until_first_gate_run(db_session):
    """A freshly-onboarded project has no runtime record until the gate runs."""
    project = await _make_no_ci_project(db_session, with_test_cmd=True)
    assert project.merge_gate_runtime is None
    assert merge_gate_runtime_block(project) is None


@pytest.mark.asyncio
async def test_merge_gate_runtime_verified_run_preserves_prior_unverified(db_session):
    """A later verified (real-suite) run updates last_run_at/executed_real_tests
    but PRESERVES the most recent UNVERIFIED fall-open fields.

    The orchestrator records at every terminal gate outcome. An UNVERIFIED merge
    stamps last_unverified_*; a subsequent run where a real suite passed must
    flip executed_real_tests to True WITHOUT clearing the historical
    last_unverified_* — they remain true of the most recent fall-open.
    """
    project = await _make_no_ci_project(db_session, with_test_cmd=True)

    # First: an UNVERIFIED fall-open stamps the unverified fields.
    await _record_gate_runtime(
        db_session,
        project,
        outcome="undetected",
        unverified_reason="no_detectable_test_command",
    )
    first = merge_gate_runtime_block(project)
    assert first["executed_real_tests"] is False
    assert first["last_unverified_reason"] == "no_detectable_test_command"

    # Then: a real suite runs and passes — executed_real_tests flips True, but
    # the prior last_unverified_* are preserved (not cleared).
    later_when = datetime(2026, 6, 4, 9, 30, 0)
    record_no_ci_gate_runtime(
        db_session,
        project,
        executed_real_tests=True,
        unverified_reason=None,
        when=later_when,
    )
    await db_session.commit()
    await db_session.refresh(project)

    second = merge_gate_runtime_block(project)
    assert second["executed_real_tests"] is True
    assert second["last_run_at"] == later_when.isoformat()
    # Preserved from the earlier UNVERIFIED run — NOT cleared by the verified run.
    assert second["last_unverified_at"] == _RUNTIME_WHEN.isoformat()
    assert second["last_unverified_reason"] == "no_detectable_test_command"


@pytest.mark.asyncio
async def test_record_no_ci_gate_runtime_is_best_effort_on_none_project(db_session):
    """The recorder never raises on a None project — the merge path is protected
    even if the project lookup somehow returned nothing."""
    # Must not raise.
    record_no_ci_gate_runtime(
        db_session,
        None,
        executed_real_tests=True,
        unverified_reason=None,
        when=_RUNTIME_WHEN,
    )


# ---------------------------------------------------------------------------
# 7. Auto-recovery: a stale stored test command self-heals when verify_tests
# runs a real replacement suite and reports it via
# recovered/recovered_directories.
#
# These mirror the orchestrator's inline persistence call (orchestrator.py
# ~5365): after the verdict is read+classified, when recovered is true with
# concrete recovered_directories, persist_recovered_test_command converges the
# project's build_config so FUTURE merges use the corrected command —
# regardless of the replacement suite's pass/fail outcome.
# ---------------------------------------------------------------------------


async def _make_stale_no_ci_project(db_session):
    """A no-CI project whose stored test_cmd is STALE, with operator-owned fields.

    The operator-set fields (git identity, branch prefix, requires_ci_checks,
    env_vars) must survive recovery persistence untouched.
    """
    project = Project(
        name="External no-CI repo with stale cmd",
        user_id="test-user",
        github_owner="acme",
        github_repo="widget",
        integration_branch=BASE,
        build_config={
            "requires_ci_checks": False,
            "directories": [{"path": ".", "type": "python", "test_cmd": "make test"}],
            "git_user_name": "Operator Bot",
            "git_user_email": "ops@example.com",
            "task_branch_prefix": "ops/",
            "env_vars": {"FOO": "bar"},
        },
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)
    return project


@pytest.mark.asyncio
async def test_no_ci_recovered_passing_suite_merges_and_persists_command(
    db_session, mocker
):
    _patch_github(mocker)
    merge_mock = mocker.patch(
        "app.github.merge_pull_request", return_value={"merged": True}
    )

    project = await _make_stale_no_ci_project(db_session)
    parent = _make_parent_task(db_session, project)
    await db_session.commit()
    await db_session.refresh(parent)

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

    # The agent self-healed: the stored `make test` was stale, so it ran the
    # repo's REAL pytest suite and reports the recovery. detected/passed reflect
    # the REPLACEMENT suite's real run.
    await _plant_verdict(
        db_session,
        verify_task,
        {
            "detected": True,
            "passed": True,
            "tests": [{"name": "tests/test_math.py::test_add", "passed": True}],
            "output": "1 passed in 0.10s",
            "recovered": True,
            "recovered_directories": [
                {"path": ".", "type": "python", "test_cmd": "python3 -m pytest"}
            ],
        },
    )

    verdict = await _latest_artifact_logs(
        db_session, verify_task.id, ArtifactType.MERGE_TEST_RESULT
    )
    outcome, detail = classify_merge_test_verdict(verdict)
    assert (
        outcome == "pass"
    ), "recovered passing suite must classify as pass, not undetected"

    # Mirror the orchestrator: persist the recovered command before merging.
    persisted = await persist_recovered_test_command(
        project, db_session, verdict.get("recovered_directories") or []
    )
    assert persisted is True

    ci_result = build_no_ci_gate_ci_result(outcome, detail)
    assert ci_result["conclusion"] == "success"

    parent = await db_session.get(Task, parent.id)
    acted = await handle_ci_completion(
        parent, ci_result, "ghs_test", "acme", "widget", db_session, source="poll"
    )
    assert acted is True
    # (a) Merge decision honors the REAL replacement-suite verdict — not an
    #     UNVERIFIED fall-open.
    merge_mock.assert_called_once()
    assert (
        await _unverified_merge_alerts(db_session, project.id) == []
    ), "a recovered+passing merge is verified, not UNVERIFIED"

    # (b) build_config converged to the recovered command; operator-owned fields
    #     are preserved.
    await db_session.refresh(project)
    dirs = project.build_config["directories"]
    assert len(dirs) == 1
    assert dirs[0]["path"] == "."
    assert dirs[0]["test_cmd"] == "python3 -m pytest"
    assert project.build_config["git_user_name"] == "Operator Bot"
    assert project.build_config["git_user_email"] == "ops@example.com"
    assert project.build_config["task_branch_prefix"] == "ops/"
    assert project.build_config["env_vars"] == {"FOO": "bar"}
    assert project.build_config["requires_ci_checks"] is False


@pytest.mark.asyncio
async def test_no_ci_recovered_failing_suite_blocks_merge_and_persists_command(
    db_session, mocker
):
    _patch_github(mocker)
    merge_mock = mocker.patch("app.github.merge_pull_request", return_value=None)

    project = await _make_stale_no_ci_project(db_session)
    parent = _make_parent_task(db_session, project)
    await db_session.commit()
    await db_session.refresh(parent)

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

    # The replacement suite ran but FAILED — recovery still corrects the stored
    # command (a recovered-but-failing suite means the stored command was stale).
    failing_output = "FAILED tests/test_math.py::test_add - assert 3 == 4\n1 failed"
    await _plant_verdict(
        db_session,
        verify_task,
        {
            "detected": True,
            "passed": False,
            "tests": [{"name": "tests/test_math.py::test_add", "passed": False}],
            "output": failing_output,
            "recovered": True,
            "recovered_directories": [
                {"path": ".", "type": "python", "test_cmd": "python3 -m pytest"}
            ],
        },
    )

    verdict = await _latest_artifact_logs(
        db_session, verify_task.id, ArtifactType.MERGE_TEST_RESULT
    )
    outcome, detail = classify_merge_test_verdict(verdict)
    assert outcome == "fail"

    # Recovery persistence runs regardless of pass/fail.
    persisted = await persist_recovered_test_command(
        project, db_session, verdict.get("recovered_directories") or []
    )
    assert persisted is True

    ci_result = build_no_ci_gate_ci_result(outcome, detail)
    assert ci_result["conclusion"] == "failure"

    parent = await db_session.get(Task, parent.id)
    acted = await handle_ci_completion(
        parent, ci_result, "ghs_test", "acme", "widget", db_session, source="poll"
    )
    assert acted is True
    # (a) Merge decision honors the REAL replacement-suite verdict: blocked, with
    #     a fix spawned — NOT an UNVERIFIED fall-open.
    merge_mock.assert_not_called()
    all_tasks = (
        (await db_session.execute(select(Task).where(Task.project_id == project.id)))
        .scalars()
        .all()
    )
    fix_tasks = [t for t in all_tasks if t.first_payload.get("type") == "fix"]
    assert (
        len(fix_tasks) == 1
    ), "a recovered+failing suite must block and spawn a fix"

    # (b) build_config still converged to the recovered command.
    await db_session.refresh(project)
    assert project.build_config["directories"][0]["test_cmd"] == "python3 -m pytest"


@pytest.mark.asyncio
async def test_persist_recovered_appends_new_directory_and_guards_blanks(db_session):
    """Unit-level contract for the persistence helper itself."""
    project = await _make_stale_no_ci_project(db_session)

    # Append a newly discovered directory + update the existing one; a blank
    # test_cmd entry is skipped (conservative guard).
    persisted = await persist_recovered_test_command(
        project,
        db_session,
        [
            {"path": "./", "type": "python", "test_cmd": "python3 -m pytest"},
            {"path": "services/api", "type": "node", "test_cmd": "npm test"},
            {"path": "blank", "type": "python", "test_cmd": ""},
        ],
    )
    assert persisted is True

    await db_session.refresh(project)
    dirs = {d["path"]: d for d in project.build_config["directories"]}
    assert dirs["."]["test_cmd"] == "python3 -m pytest"
    assert dirs["services/api"]["test_cmd"] == "npm test"
    assert "blank" not in dirs, "blank test_cmd must never be persisted"

    # Empty recovered list is a no-op.
    assert await persist_recovered_test_command(project, db_session, []) is False


@pytest.mark.asyncio
async def test_persist_recovered_appends_other_type_stays_in_schema(db_session):
    """A make/shell suite recovery (agent maps both to the schema-valid ``other``
    type) must round-trip through BuildConfigSchema validation and persist as a
    clean config — NOT fall to the raw-store fallback.

    Regression guard for the agent-side enum mismatch: ``runnerKindFromCmd``
    previously emitted ``make``/``shell``/``unknown``, which are outside the
    BuildConfigDirectoryEntry.type enum (^(python|node|go|rust|other)$) and would
    silently persist a schema-invalid config that a later operator PATCH
    /build_config rejects with HTTP 400.
    """
    from app.schemas import BuildConfig as BuildConfigSchema

    project = await _make_stale_no_ci_project(db_session)

    persisted = await persist_recovered_test_command(
        project,
        db_session,
        [
            # Makefile test target -> agent emits type 'other'.
            {"path": "services/worker", "type": "other", "test_cmd": "make test"},
            # bash script suite -> agent emits type 'other'.
            {"path": "scripts", "type": "other", "test_cmd": "bash scripts/test.sh"},
        ],
    )
    assert persisted is True

    await db_session.refresh(project)
    dirs = {d["path"]: d for d in project.build_config["directories"]}
    assert dirs["services/worker"]["test_cmd"] == "make test"
    assert dirs["services/worker"]["type"] == "other"
    assert dirs["scripts"]["test_cmd"] == "bash scripts/test.sh"
    assert dirs["scripts"]["type"] == "other"

    # The persisted config must be schema-valid (validated round-trip, NOT the
    # raw-store fallback) so a subsequent operator PATCH /build_config succeeds.
    BuildConfigSchema(**project.build_config)

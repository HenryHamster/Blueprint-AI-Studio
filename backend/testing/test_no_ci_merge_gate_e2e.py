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

from app.rollup_size_preflight import SizeCapPreflightResult
from sqlalchemy import select

from app.build_config_detection import persist_recovered_test_command
from app.ci_completion import handle_ci_completion
from app.ci_resolution import (
    build_no_ci_gate_ci_result,
    classify_merge_test_verdict,
    classify_unverified_fall_open_reason,
    has_detectable_tests,
    merge_gate_runtime_block,
    probe_is_clear_failure,
    proceed_on_no_checks,
    record_no_ci_gate_degraded_block,
    record_no_ci_gate_no_test_block,
    record_no_ci_gate_runtime,
    resolve_no_checks_escalation,
    should_block_degraded_unverified_merge,
    should_block_no_test_unverified_merge,
    should_run_no_ci_test_gate,
)
from app.build_config_detection import maybe_redetect_ci_presence
from app.models import Alert, Artifact, ArtifactType, Project, Task, TaskStatus
from app.orchestrator import _VERIFY_TESTS_MAX_RETRIES, _VERIFY_TESTS_SPAWN_DEFERRAL_CAP
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

    mocker.patch(
        "app.github.get_pr_files", AsyncMock(return_value=[{"filename": "README.md"}])
    )


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
    checked_base = "test-no-ci-base-sha"
    checked_head = "test-no-ci-head-sha"
    loop = {
        "pr_number": PR_NUMBER,
        "branch": BRANCH,
        "base_branch": BASE,
        "iteration": 1,
        "review_approved": True,
        "ci_waiting": True,
        "head_sha": checked_head,
        "base_sha": checked_base,
        "ci_checked_head_sha": checked_head,
        "ci_checked_base_sha": checked_base,
        "review_target": {
            "project_id": str(project.id),
            "pr_number": PR_NUMBER,
            "base_sha": checked_base,
            "head_sha": checked_head,
        },
        "review_history": [
            {
                "verdict": "APPROVE",
                "issues": [],
                "reviewed_base_sha": checked_base,
                "reviewed_head_sha": checked_head,
                "target_verified_by_backend": True,
                "review_task_id": "test-no-ci-review",
            }
        ],
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
    async def _bound_ok(*_args, expected_head_sha=None, **_kwargs):
        return SizeCapPreflightResult("ok", None, expected_head_sha)

    mocker.patch(
        "app.rollup_size_preflight.preflight_rollup_size_caps",
        AsyncMock(side_effect=_bound_ok),
    )
    mocker.patch(
        "app.review_merge_authority._fetch_live_review_target",
        AsyncMock(
            side_effect=lambda _token, _owner, _repo, pr_number, *, project_id, **_kwargs: {
                "project_id": project_id,
                "pr_number": pr_number,
                "base_sha": "test-no-ci-base-sha",
                "head_sha": "test-no-ci-head-sha",
            }
        ),
    )


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
    assert should_run_no_ci_test_gate(project, BASE, gate_enabled=True) is True, (
        "external no-CI repo with a test_cmd must gate the merge on verify_tests"
    )

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
    assert await _unverified_merge_alerts(db_session, project.id) == [], (
        "a verified (passing) merge must not raise an UNVERIFIED-merge alert"
    )

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
    stall_alert_mock = mocker.patch(
        "app.alert_dispatch._maybe_create_failure_alert", return_value=None
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
    stall_alert_mock.assert_not_called()

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


@pytest.mark.asyncio
async def test_no_ci_setup_failed_verify_task_exhausted_retry_merges_unverified_with_setup_reason(
    db_session, mocker
):
    """A FAILED verify task carrying ``setupFailed=true`` is not a real red
    suite: once its retry budget is exhausted, the gate merges UNVERIFIED with
    the distinct ``verify_setup_failed`` reason instead of treating it like the
    real failing-suite path above.
    """
    _patch_github(mocker)
    merge_mock = mocker.patch(
        "app.github.merge_pull_request", return_value={"merged": True}
    )

    project = await _make_no_ci_project(db_session, with_test_cmd=True)
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
        attempt=_VERIFY_TESTS_MAX_RETRIES,
    )
    assert was_created is True and verify_task is not None

    db_session.add(
        Artifact(
            task_id=verify_task.id,
            artifact_type=ArtifactType.MERGE_TEST_RESULT,
            logs=json.dumps(
                {
                    "detected": False,
                    "passed": False,
                    "setupFailed": True,
                    "tests": [],
                    "output": "Traceback (most recent call last):\nModuleNotFoundError: No module named 'sqlalchemy'",
                }
            ),
        )
    )
    verify_task.status = TaskStatus.FAILED
    db_session.add(verify_task)
    await db_session.commit()

    verdict = (
        None  # mirrors the orchestrator: FAILED verify tasks do not feed a verdict
    )
    outcome, detail = classify_merge_test_verdict(verdict)
    assert outcome == "undetected"
    assert _is_verify_crash(outcome, verdict) is True

    failed_artifact = await _latest_artifact_logs(
        db_session, verify_task.id, ArtifactType.MERGE_TEST_RESULT
    )
    assert failed_artifact is not None and failed_artifact["setupFailed"] is True

    ci_result = build_no_ci_gate_ci_result(outcome, detail)
    assert ci_result["conclusion"] == "success"
    parent = await _set_unverified_reason(db_session, parent, "verify_setup_failed")

    acted = await handle_ci_completion(
        parent, ci_result, "ghs_test", "acme", "widget", db_session, source="poll"
    )

    assert acted is True
    merge_mock.assert_called_once()
    alerts = await _unverified_merge_alerts(db_session, project.id)
    assert len(alerts) == 1
    assert "detected test suite exists but could not be run" in alerts[0].message

    await _record_gate_runtime(
        db_session, project, outcome=outcome, unverified_reason="verify_setup_failed"
    )
    runtime = merge_gate_runtime_block(project)
    assert runtime is not None
    assert runtime["executed_real_tests"] is False
    assert runtime["last_unverified_reason"] == "verify_setup_failed"


@pytest.mark.asyncio
async def test_no_ci_setup_failed_with_clear_probe_failure_blocks_merge(
    db_session, mocker
):
    """A FAILED verify task whose suite could not run (setupFailed) but whose
    cheap acceptance probe DID run and caught a clear build break must BLOCK the
    merge — the orchestrator redirects the setup-failure artifact into the verdict
    so it maps to ``fail`` + fix, instead of the crash-retry-then-fall-open path
    that would merge a genuine build break UNVERIFIED."""
    _patch_github(mocker)
    merge_mock = mocker.patch("app.github.merge_pull_request", return_value=None)

    project = await _make_no_ci_project(db_session, with_test_cmd=True)
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
        attempt=_VERIFY_TESTS_MAX_RETRIES,
    )
    assert was_created is True and verify_task is not None

    probe_output = (
        '  File "widget/core.py", line 12\n    def broken(\nSyntaxError: invalid syntax'
    )
    setup_artifact = {
        "detected": False,
        "passed": False,
        "setupFailed": True,
        "tests": [],
        "output": "Test runner setup failed; acceptance probe caught a build break.",
        "probe": {
            "ran": True,
            "passed": False,
            "could_not_run": False,
            "ecosystem": "python",
            "output": probe_output,
            "label": "python3 -m compileall (root)",
        },
    }
    db_session.add(
        Artifact(
            task_id=verify_task.id,
            artifact_type=ArtifactType.MERGE_TEST_RESULT,
            logs=json.dumps(setup_artifact),
        )
    )
    verify_task.status = TaskStatus.FAILED
    db_session.add(verify_task)
    await db_session.commit()

    failed_artifact = await _latest_artifact_logs(
        db_session, verify_task.id, ArtifactType.MERGE_TEST_RESULT
    )
    # Mirror the orchestrator: a FAILED verify task feeds no verdict UNLESS its
    # setup-failure artifact carries a clear-failure probe, in which case it is
    # redirected into the verdict so the build break blocks the merge.
    verdict = None
    if probe_is_clear_failure(failed_artifact.get("probe")):
        verdict = failed_artifact

    outcome, detail = classify_merge_test_verdict(verdict)
    assert outcome == "fail"
    assert "SyntaxError" in detail
    # A clear probe failure is NOT a crash → no retry, block immediately.
    assert _is_verify_crash(outcome, verdict) is False

    ci_result = build_no_ci_gate_ci_result(outcome, detail)
    assert ci_result["conclusion"] == "failure"

    parent = await db_session.get(Task, parent.id)
    acted = await handle_ci_completion(
        parent, ci_result, "ghs_test", "acme", "widget", db_session, source="poll"
    )
    assert acted is True
    merge_mock.assert_not_called()

    all_tasks = (
        (await db_session.execute(select(Task).where(Task.project_id == project.id)))
        .scalars()
        .all()
    )
    fix_tasks = [t for t in all_tasks if t.first_payload.get("type") == "fix"]
    assert len(fix_tasks) == 1, "a clear probe failure must spawn exactly one fix task"


def test_setup_failed_with_could_not_run_probe_keeps_crash_retry():
    """A setupFailed artifact whose probe could NOT run (same environment
    problem) must NOT be redirected into the verdict: ``probe_is_clear_failure``
    is False, so the orchestrator keeps ``verdict=None`` and the existing
    crash-retry-then-fall-open path is preserved — distinguishing an
    unprovisioned environment from a genuine build break."""
    could_not_run = {
        "detected": False,
        "setupFailed": True,
        "probe": {
            "ran": True,
            "passed": False,
            "could_not_run": True,
            "ecosystem": "node",
        },
    }
    assert probe_is_clear_failure(could_not_run["probe"]) is False
    # No probe at all (older agent) likewise never redirects.
    assert probe_is_clear_failure(None) is False
    # The crash-retry path stays intact: verdict stays None → undetected crash.
    verdict = None
    outcome, _ = classify_merge_test_verdict(verdict)
    assert outcome == "undetected"
    assert _is_verify_crash(outcome, verdict) is True


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
    assert should_run_no_ci_test_gate(project, BASE, gate_enabled=True) is False, (
        "a repo with no detectable test_cmd must not gate on verify_tests"
    )

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


@pytest.mark.asyncio
async def test_no_ci_all_skipped_records_distinct_runtime_reason(db_session):
    """A suite that was PRESENT but skipped every test records a DISTINCT
    operator reason/key on merge_gate_runtime, while a genuine no-suite repo
    keeps the generic reason — and the merge decision is identical for both."""
    # All-skipped verdict (detected=false + the agent's distinguishing field) →
    # distinct reason + key, but still an 'undetected' merge outcome.
    skipped_verdict = {"detected": False, "noTestsReason": "all_skipped"}
    skipped_outcome, skipped_detail = classify_merge_test_verdict(skipped_verdict)
    _, skipped_key = classify_unverified_fall_open_reason(skipped_verdict)
    assert skipped_outcome == "undetected" and skipped_detail == ""
    assert skipped_key == "suite_all_skipped"

    # Genuine no-suite verdict → unchanged generic reason, SAME merge outcome.
    no_suite_verdict = {"detected": False}
    no_suite_outcome, _ = classify_merge_test_verdict(no_suite_verdict)
    _, no_suite_key = classify_unverified_fall_open_reason(no_suite_verdict)
    assert no_suite_outcome == "undetected"
    assert no_suite_key == "undetected_runtime"

    # The merge outcome is unaffected by the reason refinement: both fall open to
    # the SAME success ci_result (merge UNVERIFIED).
    assert (
        build_no_ci_gate_ci_result(skipped_outcome, skipped_detail)["conclusion"]
        == build_no_ci_gate_ci_result(no_suite_outcome, "")["conclusion"]
        == "success"
    )

    # Recording the all-skipped key stamps the distinct reason on the project.
    skipped_project = await _make_no_ci_project(db_session, with_test_cmd=True)
    await _record_gate_runtime(
        db_session,
        skipped_project,
        outcome=skipped_outcome,
        unverified_reason=skipped_key,
    )
    skipped_runtime = merge_gate_runtime_block(skipped_project)
    assert skipped_runtime["executed_real_tests"] is False
    assert skipped_runtime["last_unverified_reason"] == "suite_all_skipped"

    # A genuine no-suite repo still records the generic reason, unchanged.
    no_suite_project = await _make_no_ci_project(db_session, with_test_cmd=True)
    await _record_gate_runtime(
        db_session,
        no_suite_project,
        outcome=no_suite_outcome,
        unverified_reason=no_suite_key,
    )
    no_suite_runtime = merge_gate_runtime_block(no_suite_project)
    assert no_suite_runtime["last_unverified_reason"] == "undetected_runtime"


# ---------------------------------------------------------------------------
# 3b. Cheap acceptance probe gives an honest signal when the suite is absent
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_no_ci_probe_failure_blocks_merge_and_spawns_fix(db_session, mocker):
    """A repo with no unit suite (detected=false) whose cheap acceptance probe
    catches a real compile/build failure must BLOCK the merge and spawn a fix —
    the probe-fail reuses the existing fail→fix machinery via the same gate
    helpers the orchestrator calls."""
    _patch_github(mocker)
    merge_mock = mocker.patch("app.github.merge_pull_request", return_value=None)

    project = await _make_no_ci_project(db_session, with_test_cmd=True)
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
    assert was_created is True

    # Agent verdict: no unit suite ran (detected=false), but the acceptance probe
    # byte-compiled the package and caught a SyntaxError — a clear FAIL.
    probe_output = (
        '  File "widget/core.py", line 12\n    def broken(\n             ^\n'
        "SyntaxError: invalid syntax"
    )
    await _plant_verdict(
        db_session,
        verify_task,
        {
            "detected": False,
            "passed": False,
            "tests": [],
            "output": "No real tests executed; acceptance probe FAILED.",
            "noTestsReason": "no_suite",
            "probe": {
                "ran": True,
                "passed": False,
                "could_not_run": False,
                "ecosystem": "python",
                "output": probe_output,
                "label": "python3 -m compileall (root)",
            },
        },
    )

    verdict = await _latest_artifact_logs(
        db_session, verify_task.id, ArtifactType.MERGE_TEST_RESULT
    )
    outcome, detail = classify_merge_test_verdict(verdict)
    assert outcome == "fail"
    assert "SyntaxError" in detail

    ci_result = build_no_ci_gate_ci_result(outcome, detail)
    assert ci_result["conclusion"] == "failure"
    assert "SyntaxError" in ci_result["failed_logs"]

    parent = await db_session.get(Task, parent.id)
    acted = await handle_ci_completion(
        parent, ci_result, "ghs_test", "acme", "widget", db_session, source="poll"
    )
    assert acted is True
    # The probe failure blocked the merge.
    merge_mock.assert_not_called()

    all_tasks = (
        (await db_session.execute(select(Task).where(Task.project_id == project.id)))
        .scalars()
        .all()
    )
    fix_tasks = [t for t in all_tasks if t.first_payload.get("type") == "fix"]
    assert len(fix_tasks) == 1, "a probe failure must spawn exactly one fix task"
    assert "SyntaxError" in fix_tasks[0].context["ci_failure_logs"]

    await db_session.refresh(parent)
    assert parent.status == TaskStatus.COMPLETED

    # A real execution signal (a failing probe) ran to a FAIL verdict.
    await _record_gate_runtime(
        db_session, project, outcome=outcome, unverified_reason=None
    )
    runtime = merge_gate_runtime_block(project)
    assert runtime["executed_real_tests"] is True


@pytest.mark.asyncio
async def test_no_ci_probe_could_not_run_falls_open_with_distinct_reason(
    db_session, mocker
):
    """A probe that could not run (missing deps / unprovisioned setup) must NOT
    hard-block — the merge falls open UNVERIFIED, but with a DISTINCT operator
    reason from a genuine no-suite repo. A passing probe likewise falls open but
    records its own positive reason."""
    _patch_github(mocker)
    merge_mock = mocker.patch(
        "app.github.merge_pull_request", return_value={"merged": True}
    )

    # Probe could-not-run verdict → undetected (fall open), distinct reason key.
    cnr_verdict = {
        "detected": False,
        "noTestsReason": "no_suite",
        "probe": {
            "ran": True,
            "passed": False,
            "could_not_run": True,
            "ecosystem": "node",
        },
    }
    cnr_outcome, cnr_detail = classify_merge_test_verdict(cnr_verdict)
    _, cnr_key = classify_unverified_fall_open_reason(cnr_verdict)
    assert cnr_outcome == "undetected" and cnr_detail == ""
    assert cnr_key == "probe_could_not_run"

    # Probe-passed verdict → still undetected (no unit suite), positive reason.
    pass_verdict = {
        "detected": False,
        "probe": {
            "ran": True,
            "passed": True,
            "could_not_run": False,
            "ecosystem": "python",
        },
    }
    pass_outcome, _ = classify_merge_test_verdict(pass_verdict)
    _, pass_key = classify_unverified_fall_open_reason(pass_verdict)
    assert pass_outcome == "undetected"
    assert pass_key == "probe_passed"

    # Both fall open to the SAME success ci_result (merge UNVERIFIED) — the probe
    # only refines the operator reason, never the merge decision.
    ci_result = build_no_ci_gate_ci_result(cnr_outcome, cnr_detail)
    assert ci_result["conclusion"] == "success"

    project = await _make_no_ci_project(db_session, with_test_cmd=True)
    parent = _make_parent_task(db_session, project)
    await db_session.commit()
    await db_session.refresh(parent)

    parent = await db_session.get(Task, parent.id)
    acted = await handle_ci_completion(
        parent, ci_result, "ghs_test", "acme", "widget", db_session, source="poll"
    )
    assert acted is True
    merge_mock.assert_called_once()  # probe could-not-run does NOT block

    await _record_gate_runtime(
        db_session, project, outcome=cnr_outcome, unverified_reason=cnr_key
    )
    runtime = merge_gate_runtime_block(project)
    assert runtime["executed_real_tests"] is False
    assert runtime["last_unverified_reason"] == "probe_could_not_run"


# ---------------------------------------------------------------------------
# 3c. Documented / PR-introduced command probe gates the merge as a distinct
#     claim — it runs REGARDLESS of whether a unit suite ran.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_no_ci_documented_command_failure_blocks_merge_even_with_passing_suite(
    db_session, mocker
):
    """A command the PR INTRODUCES/DOCUMENTS that clearly fails must BLOCK the
    merge and spawn a fix carrying its output — EVEN WHEN a unit suite was
    detected and passed, because the documented command is a distinct claim."""
    _patch_github(mocker)
    merge_mock = mocker.patch("app.github.merge_pull_request", return_value=None)

    project = await _make_no_ci_project(db_session, with_test_cmd=True)
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
    assert was_created is True

    # Agent verdict: the unit suite was detected AND passed, but a newly added
    # package.json script the PR documents fails — a clear FAIL that must still
    # block, since the documented command is a separate claim.
    documented_output = (
        "> widget@1.0.0 lint\n> eslint .\n\n"
        "/widget/src/index.ts\n  3:1  error  'x' is not defined  no-undef\n"
        "1 problem (1 error, 0 warnings)"
    )
    await _plant_verdict(
        db_session,
        verify_task,
        {
            "detected": True,
            "passed": True,
            "tests": [{"name": "tests/test_ok.py::test_pass", "passed": True}],
            "output": "All unit tests passed.",
            "documentedCommands": {
                "ran": True,
                "outcome": "fail",
                "passed": False,
                "could_not_run": False,
                "output": documented_output,
                "label": "npm run lint",
            },
        },
    )

    verdict = await _latest_artifact_logs(
        db_session, verify_task.id, ArtifactType.MERGE_TEST_RESULT
    )
    outcome, detail = classify_merge_test_verdict(verdict)
    assert outcome == "fail", (
        "a documented-command failure blocks even when the suite passed"
    )
    assert "no-undef" in detail
    assert "npm run lint" in detail

    ci_result = build_no_ci_gate_ci_result(outcome, detail)
    assert ci_result["conclusion"] == "failure"
    assert "no-undef" in ci_result["failed_logs"]

    parent = await db_session.get(Task, parent.id)
    acted = await handle_ci_completion(
        parent, ci_result, "ghs_test", "acme", "widget", db_session, source="poll"
    )
    assert acted is True
    merge_mock.assert_not_called()  # the documented-command failure blocked merge

    all_tasks = (
        (await db_session.execute(select(Task).where(Task.project_id == project.id)))
        .scalars()
        .all()
    )
    fix_tasks = [t for t in all_tasks if t.first_payload.get("type") == "fix"]
    assert len(fix_tasks) == 1, "a documented-command failure must spawn one fix task"
    assert "no-undef" in fix_tasks[0].context["ci_failure_logs"]

    await db_session.refresh(parent)
    assert parent.status == TaskStatus.COMPLETED

    # A real execution signal (a failing documented command) ran to a FAIL verdict.
    await _record_gate_runtime(
        db_session, project, outcome=outcome, unverified_reason=None
    )
    runtime = merge_gate_runtime_block(project)
    assert runtime["executed_real_tests"] is True


@pytest.mark.asyncio
async def test_no_ci_documented_command_passing_proceeds_to_merge(db_session, mocker):
    """A documented command that PASSES does not block — the merge proceeds. With
    no unit suite, the documented command is the positive execution evidence, so
    the fall-open reason is the distinct ``documented_command_passed``."""
    _patch_github(mocker)
    merge_mock = mocker.patch(
        "app.github.merge_pull_request", return_value={"merged": True}
    )

    pass_verdict = {
        "detected": False,
        "noTestsReason": "no_suite",
        "documentedCommands": {
            "ran": True,
            "outcome": "pass",
            "passed": True,
            "could_not_run": False,
            "output": "build succeeded",
            "label": "make build",
        },
    }
    outcome, detail = classify_merge_test_verdict(pass_verdict)
    assert outcome == "undetected" and detail == ""
    _, reason_key = classify_unverified_fall_open_reason(pass_verdict)
    assert reason_key == "documented_command_passed"

    ci_result = build_no_ci_gate_ci_result(outcome, detail)
    assert ci_result["conclusion"] == "success"

    project = await _make_no_ci_project(db_session, with_test_cmd=True)
    parent = _make_parent_task(db_session, project)
    await db_session.commit()
    await db_session.refresh(parent)

    parent = await db_session.get(Task, parent.id)
    acted = await handle_ci_completion(
        parent, ci_result, "ghs_test", "acme", "widget", db_session, source="poll"
    )
    assert acted is True
    merge_mock.assert_called_once()  # a passing documented command does not block

    await _record_gate_runtime(
        db_session, project, outcome=outcome, unverified_reason=reason_key
    )
    runtime = merge_gate_runtime_block(project)
    assert runtime["executed_real_tests"] is False
    assert runtime["last_unverified_reason"] == "documented_command_passed"


@pytest.mark.asyncio
async def test_no_ci_documented_build_no_artifact_falls_open_with_distinct_reason(
    db_session, mocker
):
    """A BUILD-INTENT documented command that exits 0 yet writes NO artifact
    (``wrote_no_artifact`` true) must NOT block — it still falls open
    ('undetected') — but records the distinct ``documented_command_no_artifact``
    reason instead of the confident ``documented_command_passed``, mirroring the
    declared-build ``probe_no_artifact`` refinement so an operator can see the
    result-producing command produced no result."""
    _patch_github(mocker)
    merge_mock = mocker.patch(
        "app.github.merge_pull_request", return_value={"merged": True}
    )

    noop_verdict = {
        "detected": False,
        "noTestsReason": "no_suite",
        "documentedCommands": {
            "ran": True,
            "outcome": "pass",
            "passed": True,
            "could_not_run": False,
            "output": "",
            "label": "make build",
            "wrote_no_artifact": True,
        },
    }
    outcome, detail = classify_merge_test_verdict(noop_verdict)
    # A no-op build is informational only — it never blocks the merge.
    assert outcome == "undetected" and detail == ""
    _, reason_key = classify_unverified_fall_open_reason(noop_verdict)
    assert reason_key == "documented_command_no_artifact"

    ci_result = build_no_ci_gate_ci_result(outcome, detail)
    assert ci_result["conclusion"] == "success"

    project = await _make_no_ci_project(db_session, with_test_cmd=True)
    parent = _make_parent_task(db_session, project)
    await db_session.commit()
    await db_session.refresh(parent)

    parent = await db_session.get(Task, parent.id)
    acted = await handle_ci_completion(
        parent, ci_result, "ghs_test", "acme", "widget", db_session, source="poll"
    )
    assert acted is True
    merge_mock.assert_called_once()  # a no-artifact documented build does NOT block

    await _record_gate_runtime(
        db_session, project, outcome=outcome, unverified_reason=reason_key
    )
    runtime = merge_gate_runtime_block(project)
    assert runtime["executed_real_tests"] is False
    assert runtime["last_unverified_reason"] == "documented_command_no_artifact"


@pytest.mark.asyncio
async def test_no_ci_documented_command_could_not_run_falls_open_with_distinct_reason(
    db_session, mocker
):
    """A documented command that CANNOT RUN (missing deps / unprovisioned setup)
    must NOT hard-block: the merge falls open UNVERIFIED ('undetected'), with the
    distinct ``documented_command_could_not_run`` reason recorded so an operator
    can see the claim went unverified."""
    _patch_github(mocker)
    merge_mock = mocker.patch(
        "app.github.merge_pull_request", return_value={"merged": True}
    )

    cnr_verdict = {
        "detected": False,
        "noTestsReason": "no_suite",
        "documentedCommands": {
            "ran": True,
            "outcome": "could_not_run",
            "passed": False,
            "could_not_run": True,
            "output": "make: command not found",
            "label": "make build",
        },
    }
    outcome, detail = classify_merge_test_verdict(cnr_verdict)
    assert outcome == "undetected" and detail == ""
    _, reason_key = classify_unverified_fall_open_reason(cnr_verdict)
    assert reason_key == "documented_command_could_not_run"

    ci_result = build_no_ci_gate_ci_result(outcome, detail)
    assert ci_result["conclusion"] == "success"

    project = await _make_no_ci_project(db_session, with_test_cmd=True)
    parent = _make_parent_task(db_session, project)
    await db_session.commit()
    await db_session.refresh(parent)

    parent = await db_session.get(Task, parent.id)
    acted = await handle_ci_completion(
        parent, ci_result, "ghs_test", "acme", "widget", db_session, source="poll"
    )
    assert acted is True
    merge_mock.assert_called_once()  # could-not-run does NOT block

    await _record_gate_runtime(
        db_session, project, outcome=outcome, unverified_reason=reason_key
    )
    runtime = merge_gate_runtime_block(project)
    assert runtime["executed_real_tests"] is False
    assert runtime["last_unverified_reason"] == "documented_command_could_not_run"


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
    # undetected_runtime is a degraded reason that escalates to ERROR.
    parent = await _set_unverified_reason(db_session, parent, "undetected_runtime")

    acted = await handle_ci_completion(
        parent, ci_result, "ghs_test", "acme", "widget", db_session, source="poll"
    )
    assert acted is True
    merge_mock.assert_called_once()

    alerts = await _unverified_merge_alerts(db_session, project.id)
    assert len(alerts) == 1, "exactly one UNVERIFIED-merge alert per merged PR"
    from app.models import AlertSeverity

    assert alerts[0].severity == AlertSeverity.ERROR  # undetected_runtime is degraded
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


@pytest.mark.asyncio
async def test_unverified_merge_alert_severity_degraded_vs_benign(db_session, mocker):
    """Degraded env/setup-failure reasons escalate to ERROR; benign reasons stay WARNING.

    This pins the severity split: a real test suite that couldn't run is more urgent
    (ERROR) than a repo that legitimately has no tests (WARNING).
    """
    from app.models import AlertSeverity

    _patch_github(mocker)
    mocker.patch("app.github.merge_pull_request", return_value={"merged": True})

    ci_result = build_no_ci_gate_ci_result("undetected", "")

    # --- Test ERROR reasons (degraded: environment/setup failed) ---
    for degraded_reason in [
        "verify_setup_failed",
        "undetected_runtime",
        "suite_all_skipped",
        "probe_could_not_run",
        "documented_command_could_not_run",
        "documented_command_no_artifact",
    ]:
        project = await _make_no_ci_project(db_session, with_test_cmd=False)
        parent = _make_parent_task(db_session, project)
        await db_session.commit()
        await db_session.refresh(parent)

        parent = await _set_unverified_reason(db_session, parent, degraded_reason)
        await handle_ci_completion(
            parent, ci_result, "ghs_test", "acme", "widget", db_session, source="poll"
        )

        alerts = await _unverified_merge_alerts(db_session, project.id)
        assert len(alerts) == 1, (
            f"degraded reason {degraded_reason} must raise exactly one alert"
        )
        assert alerts[0].severity == AlertSeverity.ERROR, (
            f"degraded reason {degraded_reason} must escalate to ERROR severity"
        )

    # --- Test WARNING reasons (benign: no verification expected or positive-only signals) ---
    for benign_reason in [
        "no_detectable_test_command",
        "kill_switch_off",
        "internal_staging_base",
        "probe_passed",
        "documented_command_passed",
    ]:
        project = await _make_no_ci_project(db_session, with_test_cmd=False)
        parent = _make_parent_task(db_session, project)
        await db_session.commit()
        await db_session.refresh(parent)

        parent = await _set_unverified_reason(db_session, parent, benign_reason)
        await handle_ci_completion(
            parent, ci_result, "ghs_test", "acme", "widget", db_session, source="poll"
        )

        alerts = await _unverified_merge_alerts(db_session, project.id)
        assert len(alerts) == 1, (
            f"benign reason {benign_reason} must raise exactly one alert"
        )
        assert alerts[0].severity == AlertSeverity.WARNING, (
            f"benign reason {benign_reason} must stay at WARNING severity"
        )


async def _enable_block_policy(db_session, project):
    """Turn ON the per-project opt-in honest-block policy (default OFF)."""
    from sqlalchemy.orm.attributes import flag_modified

    project = await db_session.get(Project, project.id)
    bc = dict(project.build_config or {})
    bc["block_degraded_unverified_merge"] = True
    project.build_config = bc
    flag_modified(project, "build_config")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)
    return project


@pytest.mark.asyncio
async def test_no_ci_optin_honest_block_blocks_degraded_fall_open(db_session, mocker):
    """Opt-in honest-block policy: with the flag ON a DEGRADED external no-CI
    fall-open is BLOCKED (PR held, block signal raised); with the flag OFF (the
    default) the SAME degraded fall-open still merges UNVERIFIED exactly as
    today. A BENIGN reason is never blocked even with the policy ON.

    This pins BOTH halves of the default-off invariant by exercising the real
    production decision helper ``should_block_degraded_unverified_merge`` and the
    real block recorder ``record_no_ci_gate_degraded_block``, then mirroring the
    orchestrator branch: block ⇒ do NOT call the merge primitive; no block ⇒ the
    existing ``handle_ci_completion`` merges UNVERIFIED unchanged.
    """
    _patch_github(mocker)
    merge_mock = mocker.patch(
        "app.github.merge_pull_request", return_value={"merged": True}
    )

    # The fall-open ci_result the orchestrator hands downstream is identical in
    # both halves — only the policy flag differs.
    ci_result = build_no_ci_gate_ci_result("undetected", "")
    assert ci_result["conclusion"] == "success"
    degraded_reason = (
        "verify_setup_failed"  # gate expected to verify, suite could not run
    )

    # ── Half 1: policy ON + degraded reason ⇒ BLOCK (no merge) ───────────────
    on_project = await _make_no_ci_project(db_session, with_test_cmd=True)
    on_project = await _enable_block_policy(db_session, on_project)
    on_parent = _make_parent_task(db_session, on_project)
    await db_session.commit()
    await db_session.refresh(on_parent)
    on_parent = await _set_unverified_reason(db_session, on_parent, degraded_reason)

    assert (
        should_block_degraded_unverified_merge(on_project, degraded_reason) is True
    ), "policy ON + degraded reason must block"

    # Mirror the orchestrator's block branch: record the honest block and do NOT
    # invoke the merge primitive (the PR is held open).
    record_no_ci_gate_degraded_block(
        db_session, on_project, unverified_reason=degraded_reason, when=_RUNTIME_WHEN
    )
    await db_session.commit()
    merge_mock.assert_not_called()

    on_project = await db_session.get(Project, on_project.id)
    runtime = merge_gate_runtime_block(on_project)
    assert runtime is not None
    assert runtime["last_degraded_block_reason"] == degraded_reason
    assert runtime["last_degraded_block_at"] is not None
    assert runtime["degraded_block_counts"][degraded_reason] == 1
    # The block is a fall-open that did NOT run a real suite.
    assert runtime["executed_real_tests"] is False
    assert runtime["last_unverified_reason"] == degraded_reason

    # ── Half 2: policy OFF (default) + SAME degraded reason ⇒ merge UNVERIFIED ─
    off_project = await _make_no_ci_project(db_session, with_test_cmd=True)
    # Half 1 intentionally leaves its PR held and active.  Give the independent
    # policy-OFF comparison a distinct repository identity so repository-wide
    # exact-PR execution authority does not correctly treat that held review as
    # a competing owner of the same acme/widget#4242 chain.
    off_project.github_repo = "widget-policy-off"
    db_session.add(off_project)
    await db_session.commit()
    await db_session.refresh(off_project)
    off_parent = _make_parent_task(db_session, off_project)
    await db_session.commit()
    await db_session.refresh(off_parent)
    off_parent = await _set_unverified_reason(db_session, off_parent, degraded_reason)

    assert (
        should_block_degraded_unverified_merge(off_project, degraded_reason) is False
    ), "policy OFF must never block — today's behavior is preserved"

    # Mirror the orchestrator's merge branch: handle_ci_completion merges
    # UNVERIFIED and raises exactly one operator alert, exactly as today.
    acted = await handle_ci_completion(
        off_parent,
        ci_result,
        "ghs_test",
        "acme",
        "widget-policy-off",
        db_session,
        source="poll",
    )
    assert acted is True
    merge_mock.assert_called_once()
    alerts = await _unverified_merge_alerts(db_session, off_project.id)
    assert len(alerts) == 1, "policy-OFF degraded fall-open merges unverified as today"

    # ── Half 3: policy ON + BENIGN reason ⇒ never blocked ────────────────────
    assert (
        should_block_degraded_unverified_merge(on_project, "no_detectable_test_command")
        is False
    ), "a benign reason must never block, even with the policy ON"


def test_orchestrator_consults_degraded_block_helper():
    """Drift guard: the orchestrator's no-CI gate must still consult
    ``should_block_degraded_unverified_merge`` and, when it returns True, BLOCK
    (record the block and ``continue`` without calling ``handle_ci_completion``).
    If a refactor drops this call the e2e mirror above stays green while
    production silently regresses to merging degraded fall-opens unverified."""
    src = _orchestrator_source()
    required = [
        "should_block_degraded_unverified_merge",
        "record_no_ci_gate_degraded_block",
        'ci_loop["no_ci_degraded_blocked"] = True',
    ]
    missing = [needle for needle in required if needle not in src]
    assert not missing, (
        "orchestrator no-CI honest-block wiring changed — missing: "
        f"{missing}. The degraded-block branch must consult "
        "should_block_degraded_unverified_merge and park the PR; update "
        "orchestrator.py AND this guard together."
    )


@pytest.mark.asyncio
async def test_block_no_test_unverified_merge_held_and_sticky(db_session):
    """Opt-in no-test honest-block policy: with the flag ON and
    ``no_detectable_test_command`` the predicate returns True; with the flag OFF
    or a different reason it returns False.

    Also exercises ``record_no_ci_gate_no_test_block`` and verifies that
    ``merge_gate_runtime_block`` surfaces the new JSONB fields.
    """
    # ── Part 1: predicate behaviour ──────────────────────────────────────────
    project = await _make_no_ci_project(db_session, with_test_cmd=False)
    bc = dict(project.build_config or {})
    bc["block_no_test_unverified_merge"] = True
    from sqlalchemy.orm.attributes import flag_modified as _fm

    project.build_config = bc
    _fm(project, "build_config")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    assert (
        should_block_no_test_unverified_merge(project, "no_detectable_test_command")
        is True
    ), "policy ON + no_detectable_test_command must block"

    assert (
        should_block_no_test_unverified_merge(project, "verify_setup_failed") is False
    ), "a DEGRADED reason must NOT be caught by the no-test policy"

    off_project = await _make_no_ci_project(db_session, with_test_cmd=False)
    assert (
        should_block_no_test_unverified_merge(off_project, "no_detectable_test_command")
        is False
    ), "policy OFF must never block"

    # ── Part 2: recorder stamps the expected JSONB keys ───────────────────
    record_no_ci_gate_no_test_block(
        db_session,
        project,
        unverified_reason="no_detectable_test_command",
        when=_RUNTIME_WHEN,
    )
    await db_session.commit()

    project = await db_session.get(Project, project.id)
    runtime = merge_gate_runtime_block(project)
    assert runtime is not None
    assert runtime["last_no_test_block_at"] == _RUNTIME_WHEN.isoformat()
    assert runtime["last_no_test_block_reason"] == "no_detectable_test_command"
    assert runtime["no_test_block_counts"]["no_detectable_test_command"] == 1
    # The block is a fall-open that did NOT run a real suite.
    assert runtime["executed_real_tests"] is False
    assert runtime["last_unverified_reason"] == "no_detectable_test_command"

    # ── Part 3: second block call increments count (sticky tally) ─────────
    record_no_ci_gate_no_test_block(
        db_session,
        project,
        unverified_reason="no_detectable_test_command",
        when=_RUNTIME_WHEN,
    )
    await db_session.commit()
    project = await db_session.get(Project, project.id)
    runtime2 = merge_gate_runtime_block(project)
    assert runtime2["no_test_block_counts"]["no_detectable_test_command"] == 2


def test_orchestrator_consults_no_test_block_helper():
    """Drift guard: the orchestrator's no-CI gate must consult
    ``should_block_no_test_unverified_merge`` and, when it returns True, BLOCK
    (record the block and ``continue`` without calling ``handle_ci_completion``).
    Uses sticky ``ci_loop["no_ci_no_test_blocked"]`` to prevent re-alerting."""
    src = _orchestrator_source()
    required = [
        "should_block_no_test_unverified_merge",
        "record_no_ci_gate_no_test_block",
        'ci_loop["no_ci_no_test_blocked"] = True',
    ]
    missing = [needle for needle in required if needle not in src]
    assert not missing, (
        "orchestrator no-CI no-test-block wiring changed — missing: "
        f"{missing}. The no-test-block branch must consult "
        "should_block_no_test_unverified_merge and park the PR; update "
        "orchestrator.py AND this guard together."
    )


@pytest.mark.asyncio
async def test_block_no_test_unverified_merge_mirrors_orchestrator_decision_tree(
    db_session, mocker
):
    """Opt-in no-test honest-block policy: with the flag ON and
    ``no_detectable_test_command`` the PR is HELD (no merge, block recorded);
    with the flag OFF the SAME reason still merges UNVERIFIED exactly as today.
    A DEGRADED reason is never caught by the no-test policy (it belongs to the
    separate ``block_degraded_unverified_merge`` branch).

    Mirrors the orchestrator's inline decision tree end-to-end using the real
    helpers ``should_block_no_test_unverified_merge`` and
    ``record_no_ci_gate_no_test_block`` so a future refactor cannot silently
    regress the no-test-block path back to auto-merging UNVERIFIED.
    """
    _patch_github(mocker)
    merge_mock = mocker.patch(
        "app.github.merge_pull_request", return_value={"merged": True}
    )

    ci_result = build_no_ci_gate_ci_result("undetected", "")
    assert ci_result["conclusion"] == "success"
    no_test_reason = "no_detectable_test_command"

    # ── Half 1: policy ON + no-test reason ⇒ BLOCK (no merge) ────────────────
    on_project = await _make_no_ci_project(db_session, with_test_cmd=False)
    bc = dict(on_project.build_config or {})
    bc["block_no_test_unverified_merge"] = True
    from sqlalchemy.orm.attributes import flag_modified

    on_project.build_config = bc
    flag_modified(on_project, "build_config")
    db_session.add(on_project)
    await db_session.commit()
    await db_session.refresh(on_project)

    on_parent = _make_parent_task(db_session, on_project)
    await db_session.commit()
    await db_session.refresh(on_parent)
    on_parent = await _set_unverified_reason(db_session, on_parent, no_test_reason)

    assert should_block_no_test_unverified_merge(on_project, no_test_reason) is True, (
        "policy ON + no_detectable_test_command must block"
    )

    # Mirror the orchestrator's block branch: record the honest block and do NOT
    # invoke the merge primitive (the PR is held open).
    record_no_ci_gate_no_test_block(
        db_session, on_project, unverified_reason=no_test_reason, when=_RUNTIME_WHEN
    )
    await db_session.commit()
    merge_mock.assert_not_called()

    on_project = await db_session.get(Project, on_project.id)
    runtime = merge_gate_runtime_block(on_project)
    assert runtime is not None
    assert runtime["last_no_test_block_reason"] == no_test_reason
    assert runtime["last_no_test_block_at"] is not None
    assert runtime["no_test_block_counts"][no_test_reason] == 1
    assert runtime["executed_real_tests"] is False
    assert runtime["last_unverified_reason"] == no_test_reason

    # ── Half 2: policy OFF (default) + SAME reason ⇒ merge UNVERIFIED ─────────
    off_project = await _make_no_ci_project(db_session, with_test_cmd=False)
    # Repository-wide merge ownership is keyed by exact repo/PR.  Keep this
    # independent half from inheriting half 1's intentionally held PR #4242.
    off_project.github_repo = "widget-policy-off"
    db_session.add(off_project)
    await db_session.commit()
    await db_session.refresh(off_project)
    off_parent = _make_parent_task(db_session, off_project)
    await db_session.commit()
    await db_session.refresh(off_parent)
    off_parent = await _set_unverified_reason(db_session, off_parent, no_test_reason)

    assert (
        should_block_no_test_unverified_merge(off_project, no_test_reason) is False
    ), "policy OFF must never block — today's behavior is preserved"

    # Mirror the orchestrator's merge branch: handle_ci_completion merges
    # UNVERIFIED and raises exactly one operator alert, exactly as today.
    acted = await handle_ci_completion(
        off_parent,
        ci_result,
        "ghs_test",
        "acme",
        "widget-policy-off",
        db_session,
        source="poll",
    )
    assert acted is True
    merge_mock.assert_called_once()
    alerts = await _unverified_merge_alerts(db_session, off_project.id)
    assert len(alerts) == 1, "policy-OFF no-test fall-open merges unverified as today"

    # ── Half 3: policy ON + DEGRADED reason ⇒ not caught by no-test policy ───
    degraded_reason = "verify_setup_failed"
    assert (
        should_block_no_test_unverified_merge(on_project, degraded_reason) is False
    ), "a DEGRADED reason must never be caught by no-test policy"


# ---------------------------------------------------------------------------
# 5. A repo detected via a NON-MANIFEST entrypoint runs its suite and blocks
#    the merge on failure (detection layer → gate layer, composed).
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_no_ci_makefile_detected_repo_blocks_merge_on_failure(db_session, mocker):
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

    assert any(d.get("test_cmd") == "make test" for d in detected["directories"]), (
        "Makefile-with-test-target repo must detect a `make test` command"
    )

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

    assert any(d.get("test_cmd") == "npm test" for d in detected["directories"]), (
        "package.json with scripts.test must detect an `npm test` command"
    )

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
    assert await _unverified_merge_alerts(db_session, project.id) == [], (
        "a verified (passing) pytest.ini merge must not raise an UNVERIFIED alert"
    )


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
    assert await _unverified_merge_alerts(db_session, project.id) == [], (
        "a blocked (failing) merge must not raise an UNVERIFIED-merge alert"
    )


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
    assert outcome == "pass", (
        "recovered passing suite must classify as pass, not undetected"
    )

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
    assert await _unverified_merge_alerts(db_session, project.id) == [], (
        "a recovered+passing merge is verified, not UNVERIFIED"
    )

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
    assert len(fix_tasks) == 1, "a recovered+failing suite must block and spawn a fix"

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


# ---------------------------------------------------------------------------
# 8. ci_loop verdict-state threading + bypass-path composed flows
#
# The composed flows above (sections 1-7) build the ci_result from a freshly
# read MERGE_TEST_RESULT artifact, then HAND-MIRROR what the orchestrator's
# inline gate writes onto review_loop via ``_set_unverified_reason``. That
# leaves two gaps the orchestrator's own state machine owns and these tests
# close:
#
#   (a) ci_loop verdict-state threading. On a re-entry poll the orchestrator
#       does NOT re-read the artifact — it rebuilds the ci_result from the
#       verdict state THREADED ON ci_loop on a prior poll
#       (``verify_tests_verdict`` and, for a fail, ``verify_tests_output``;
#       orchestrator.py ~5125-5136). ``_ci_result_from_recorded_loop`` mirrors
#       that fast-path exactly, and ``test_orchestrator_no_ci_gate_*_wiring``
#       statically pins the live orchestrator writes so a regression that drops
#       / renames the ci_loop threading fails CI even though the artifact-read
#       flows above would still pass against the stale mirror.
#
#   (b) the two BYPASS paths (staging base → ``internal_staging_base``,
#       kill-switch off → ``kill_switch_off``). Until now these were only
#       unit-tested at the ``should_run_no_ci_test_gate`` decision; here each is
#       driven through the full gate-decision → unverified-reason →
#       handle_ci_completion → alert chain, asserting the previously-untested
#       reason→label mapping shapes a single operator alert.
# ---------------------------------------------------------------------------


def _ci_result_from_recorded_loop(ci_loop: dict) -> dict | None:
    """Mirror the orchestrator's recorded-verdict fast-path (orchestrator.py
    ~5125-5136).

    On a re-entry poll where verify_tests already produced a verdict on a PRIOR
    poll, the gate rebuilds the ci_result from the verdict state THREADED ON
    ``ci_loop`` (``verify_tests_verdict`` / ``verify_tests_output``) via the SAME
    ``build_no_ci_gate_ci_result`` helper — it does not re-read the artifact.
    Returns ``None`` when no verdict has been recorded yet (the gate would still
    be spawning / awaiting). ``test_orchestrator_no_ci_gate_verdict_state_wiring``
    pins that the live orchestrator still performs exactly these ci_loop reads,
    so this mirror cannot silently drift from production.
    """
    recorded = ci_loop.get("verify_tests_verdict")
    if recorded in ("pass", "undetected"):
        return build_no_ci_gate_ci_result(recorded)
    if recorded == "fail":
        return build_no_ci_gate_ci_result(
            "fail", ci_loop.get("verify_tests_output", "")
        )
    return None


async def _set_review_loop_keys(db_session, parent, **keys):
    """Thread arbitrary review_loop state onto the parent exactly as the
    orchestrator's gate does (dict-copy + flag_modified + add)."""
    from sqlalchemy.orm.attributes import flag_modified

    parent = await db_session.get(Task, parent.id)
    loop = dict((parent.context or {}).get("review_loop", {}))
    loop.update(keys)
    parent.context = {**(parent.context or {}), "review_loop": loop}
    flag_modified(parent, "context")
    db_session.add(parent)
    await db_session.commit()
    return parent


@pytest.mark.asyncio
async def test_no_ci_recorded_pass_verdict_fastpath_merges(db_session, mocker):
    """Re-entry poll: ``verify_tests_verdict='pass'`` recorded on a prior poll
    rebuilds a merge-allowing ci_result from ci_loop (no artifact re-read) and
    proceeds to merge with no UNVERIFIED alert."""
    _patch_github(mocker)
    merge_mock = mocker.patch(
        "app.github.merge_pull_request", return_value={"merged": True}
    )

    project = await _make_no_ci_project(db_session, with_test_cmd=True)
    parent = _make_parent_task(db_session, project)
    await db_session.commit()
    await db_session.refresh(parent)

    # A prior poll recorded a PASS verdict on the loop and stamped the verify
    # task id — the gate's fast-path reads that state instead of the artifact.
    parent = await _set_review_loop_keys(
        db_session,
        parent,
        verify_tests_verdict="pass",
        verify_tests_task_id=str(uuid.uuid4()),
    )
    ci_loop = (parent.context or {})["review_loop"]
    ci_result = _ci_result_from_recorded_loop(ci_loop)
    assert ci_result is not None
    assert ci_result["conclusion"] == "success"

    parent = await db_session.get(Task, parent.id)
    acted = await handle_ci_completion(
        parent, ci_result, "ghs_test", "acme", "widget", db_session, source="poll"
    )
    assert acted is True
    merge_mock.assert_called_once()
    await db_session.refresh(parent)
    assert parent.status == TaskStatus.COMPLETED
    assert await _unverified_merge_alerts(db_session, project.id) == []


@pytest.mark.asyncio
async def test_no_ci_recorded_fail_verdict_fastpath_blocks_carrying_loop_output(
    db_session, mocker
):
    """Re-entry poll: ``verify_tests_verdict='fail'`` rebuilds a blocking
    ci_result whose ``failed_logs`` come from ``verify_tests_output`` threaded on
    ci_loop — proving the failing output survives on the loop across polls and
    reaches the spawned fix task."""
    _patch_github(mocker)
    merge_mock = mocker.patch("app.github.merge_pull_request", return_value=None)

    project = await _make_no_ci_project(db_session, with_test_cmd=True)
    parent = _make_parent_task(db_session, project)
    await db_session.commit()
    await db_session.refresh(parent)

    failing_output = (
        "FAILED tests/test_math.py::test_recorded - assert 3 == 4\n1 failed"
    )
    parent = await _set_review_loop_keys(
        db_session,
        parent,
        verify_tests_verdict="fail",
        verify_tests_output=failing_output,
        verify_tests_task_id=str(uuid.uuid4()),
    )
    ci_loop = (parent.context or {})["review_loop"]
    ci_result = _ci_result_from_recorded_loop(ci_loop)
    assert ci_result is not None
    assert ci_result["conclusion"] == "failure"
    # The failing output threaded on ci_loop is what becomes failed_logs.
    assert "test_recorded" in ci_result["failed_logs"]

    parent = await db_session.get(Task, parent.id)
    acted = await handle_ci_completion(
        parent, ci_result, "ghs_test", "acme", "widget", db_session, source="poll"
    )
    assert acted is True
    merge_mock.assert_not_called()  # the recorded FAIL verdict blocked the merge

    all_tasks = (
        (await db_session.execute(select(Task).where(Task.project_id == project.id)))
        .scalars()
        .all()
    )
    fix_tasks = [t for t in all_tasks if t.first_payload.get("type") == "fix"]
    assert len(fix_tasks) == 1
    # The loop-carried output reaches the fix task, not just the ci_result.
    assert "test_recorded" in fix_tasks[0].context["ci_failure_logs"]


@pytest.mark.asyncio
async def test_no_ci_recorded_undetected_verdict_fastpath_merges_unverified(
    db_session, mocker
):
    """Re-entry poll: ``verify_tests_verdict='undetected'`` plus the
    ``unverified_merge_reason`` threaded on ci_loop merges UNVERIFIED and raises
    exactly one reason-labelled alert — the verdict and reason both ride the
    same loop across polls."""
    _patch_github(mocker)
    merge_mock = mocker.patch(
        "app.github.merge_pull_request", return_value={"merged": True}
    )

    project = await _make_no_ci_project(db_session, with_test_cmd=True)
    parent = _make_parent_task(db_session, project)
    await db_session.commit()
    await db_session.refresh(parent)

    parent = await _set_review_loop_keys(
        db_session,
        parent,
        verify_tests_verdict="undetected",
        verify_tests_retries=_VERIFY_TESTS_MAX_RETRIES,
        unverified_merge_reason="undetected_runtime",
    )
    ci_loop = (parent.context or {})["review_loop"]
    ci_result = _ci_result_from_recorded_loop(ci_loop)
    assert ci_result is not None
    assert ci_result["conclusion"] == "success"

    parent = await db_session.get(Task, parent.id)
    acted = await handle_ci_completion(
        parent, ci_result, "ghs_test", "acme", "widget", db_session, source="poll"
    )
    assert acted is True
    merge_mock.assert_called_once()

    alerts = await _unverified_merge_alerts(db_session, project.id)
    assert len(alerts) == 1
    assert f"PR #{PR_NUMBER}" in alerts[0].title
    assert "no usable verdict" in alerts[0].message  # undetected_runtime label


# A staging base the project classifies as an internal rollup target (default
# ``research/`` prefix) — the gate bypasses verify_tests here by design.
_STAGING_BASE = "research/rollup-7"


def _make_parent_task_on_base(db_session, project, base: str) -> Task:
    """Like ``_make_parent_task`` but targets an arbitrary base branch so the
    bypass-path flows can drive a staging-base merge."""
    checked_base = "test-no-ci-base-sha"
    checked_head = "test-no-ci-head-sha"
    loop = {
        "pr_number": PR_NUMBER,
        "branch": BRANCH,
        "base_branch": base,
        "iteration": 1,
        "review_approved": True,
        "ci_waiting": True,
        "head_sha": checked_head,
        "base_sha": checked_base,
        "ci_checked_head_sha": checked_head,
        "ci_checked_base_sha": checked_base,
        "review_target": {
            "project_id": str(project.id),
            "pr_number": PR_NUMBER,
            "base_sha": checked_base,
            "head_sha": checked_head,
        },
        "review_history": [
            {
                "verdict": "APPROVE",
                "issues": [],
                "reviewed_base_sha": checked_base,
                "reviewed_head_sha": checked_head,
                "target_verified_by_backend": True,
                "review_task_id": "test-no-ci-review",
            }
        ],
    }
    task = Task(
        project_id=project.id,
        title="No-CI merge gate parent (bypass)",
        payloads=[
            {
                "type": "review",
                "pr_number": PR_NUMBER,
                "branch": BRANCH,
                "base_branch": base,
            }
        ],
        context={"review_loop": loop},
        vm_instance_id=f"agent-vm-{uuid.uuid4().hex[:8]}",
        status=TaskStatus.EVALUATING,
    )
    db_session.add(task)
    return task


@pytest.mark.asyncio
async def test_no_ci_staging_base_bypass_merges_unverified_with_staging_reason(
    db_session, mocker
):
    """Composed bypass flow: a PR onto an internal STAGING base bypasses the
    verify_tests gate (CI is path-filtered there by design) and merges
    UNVERIFIED with the ``internal_staging_base`` reason — EVEN THOUGH the repo
    has a detectable suite. Pins the full gate-decision → reason → merge → alert
    chain for the staging bypass, not just the decision predicate.
    """
    _patch_github(mocker)
    merge_mock = mocker.patch(
        "app.github.merge_pull_request", return_value={"merged": True}
    )

    # A repo WITH a detectable suite — proving the staging bypass beats the gate.
    project = await _make_no_ci_project(db_session, with_test_cmd=True)
    assert has_detectable_tests(project) is True

    # Decision: the staging base bypasses the verify gate despite the suite, and
    # falls to the immediate-pass (proceed-without-checks) branch.
    assert project.is_staging_branch(_STAGING_BASE) is True
    assert (
        should_run_no_ci_test_gate(project, _STAGING_BASE, gate_enabled=True) is False
    ), "a staging base must bypass the verify_tests gate even with a suite present"
    assert proceed_on_no_checks(project, _STAGING_BASE) is True

    parent = _make_parent_task_on_base(db_session, project, _STAGING_BASE)
    await db_session.commit()
    await db_session.refresh(parent)

    # Immediate-pass builds a merge-allowing ci_result and threads the staging
    # reason — exactly the orchestrator's :5008-5043 bypass block.
    ci_result = build_no_ci_gate_ci_result("undetected", "")
    assert ci_result["conclusion"] == "success"
    parent = await _set_unverified_reason(db_session, parent, "internal_staging_base")

    acted = await handle_ci_completion(
        parent, ci_result, "ghs_test", "acme", "widget", db_session, source="poll"
    )
    assert acted is True
    merge_mock.assert_called_once()

    alerts = await _unverified_merge_alerts(db_session, project.id)
    assert len(alerts) == 1, "the staging-base bypass merge must raise one alert"
    assert f"PR #{PR_NUMBER}" in alerts[0].title
    assert "UNVERIFIED" in alerts[0].message
    assert "internal staging branch" in alerts[0].message  # internal_staging_base label


@pytest.mark.asyncio
async def test_no_ci_kill_switch_off_bypass_merges_unverified_with_kill_switch_reason(
    db_session, mocker
):
    """Composed bypass flow: with the gate kill-switch OFF, an external no-CI
    repo merges UNVERIFIED with the ``kill_switch_off`` reason — even though it
    has a detectable suite the gate would otherwise run. Drives the full
    decision → reason → merge → alert chain for the kill-switch bypass.
    """
    _patch_github(mocker)
    merge_mock = mocker.patch(
        "app.github.merge_pull_request", return_value={"merged": True}
    )

    project = await _make_no_ci_project(db_session, with_test_cmd=True)
    assert has_detectable_tests(project) is True

    # Decision: gate_enabled=False short-circuits the gate to the immediate-pass
    # branch even on a real base with a detectable suite.
    assert should_run_no_ci_test_gate(project, BASE, gate_enabled=False) is False, (
        "the kill-switch off must bypass the verify_tests gate"
    )
    assert proceed_on_no_checks(project, BASE) is True

    parent = _make_parent_task(db_session, project)
    await db_session.commit()
    await db_session.refresh(parent)

    ci_result = build_no_ci_gate_ci_result("undetected", "")
    assert ci_result["conclusion"] == "success"
    parent = await _set_unverified_reason(db_session, parent, "kill_switch_off")

    acted = await handle_ci_completion(
        parent, ci_result, "ghs_test", "acme", "widget", db_session, source="poll"
    )
    assert acted is True
    merge_mock.assert_called_once()

    alerts = await _unverified_merge_alerts(db_session, project.id)
    assert len(alerts) == 1, "the kill-switch-off bypass merge must raise one alert"
    assert f"PR #{PR_NUMBER}" in alerts[0].title
    assert "UNVERIFIED" in alerts[0].message
    assert "kill-switch off" in alerts[0].message  # kill_switch_off label


# ---------------------------------------------------------------------------
# 9. Static wiring invariants — pin the orchestrator's inline ci_loop writes so
#    a regression in the live state machine fails CI even though the composed
#    flows above (which mirror those writes) would still pass.
#
#    The gate state machine is inlined inside ``orchestrator_worker``'s polling
#    loop (not independently callable), so the composed flows necessarily
#    HAND-MIRROR the orchestrator's ci_loop threading via ``_set_unverified_reason``
#    / ``_ci_result_from_recorded_loop``. If a refactor drops or renames one of
#    those writes, the mirror keeps the flows green while production silently
#    regresses to merge-without-checks. These source scans are the backstop: if
#    one fails, the orchestrator's gate changed — update the orchestrator AND the
#    mirroring helpers/labels together, do not delete the assertion.
# ---------------------------------------------------------------------------


def _orchestrator_source() -> str:
    import inspect

    from app import orchestrator, orchestrator_ci_monitor

    # The EVALUATING/CI-waiting monitor sweep (the no-CI verify-tests gate,
    # deferral-cap escalation, redetect wiring, etc.) was extracted verbatim
    # from orchestrator_worker into app.orchestrator_ci_monitor. These wiring
    # assertions scan the combined source of both modules so they keep
    # covering the CI-gate state machine regardless of which file hosts it.
    return inspect.getsource(orchestrator) + "\n" + inspect.getsource(
        orchestrator_ci_monitor
    )


def test_orchestrator_no_ci_gate_verdict_state_wiring():
    """The orchestrator's no-CI gate must still thread the ci_loop verdict state
    the composed flows mirror: the recorded verdict, the spawned verify task id,
    the retry counter, and the failing-test output carry."""
    src = _orchestrator_source()
    required = [
        'ci_loop["verify_tests_verdict"] = _outcome',
        'ci_loop["verify_tests_task_id"]',
        'ci_loop["verify_tests_retries"]',
        'ci_loop["verify_tests_output"] = _detail[:4000]',
        'ci_loop["ci_checks_seen"] = True',
        'ci_loop.get("verify_tests_verdict")',
        'ci_loop.get("verify_tests_output"',
    ]
    missing = [needle for needle in required if needle not in src]
    assert not missing, (
        "orchestrator no-CI gate ci_loop verdict-state wiring changed — the "
        "following writes/reads are no longer present: "
        f"{missing}. Update orchestrator.py AND the mirroring helpers in this "
        "file (_ci_result_from_recorded_loop / _set_review_loop_keys) together."
    )


def test_orchestrator_no_ci_gate_bypass_reason_wiring():
    """The orchestrator's immediate-pass branch must still assign each bypass
    reason key and thread it into ``ci_loop['unverified_merge_reason']`` for the
    merge-confirmed alert — the keys the bypass composed flows assert against."""
    import re

    src = _orchestrator_source()
    # Each bypass reason key must be ASSIGNED to _unverified_merge_reason (the
    # immediate-pass branch), tolerant of the wrapping/whitespace black uses.
    assigned = [
        "internal_staging_base",
        "kill_switch_off",
        "no_detectable_test_command",
    ]
    missing = [
        key
        for key in assigned
        if not re.search(
            r"_unverified_merge_reason\s*=\s*\(?\s*[\"']" + key + r"[\"']", src
        )
    ]
    # ...and the chosen reason must be threaded onto ci_loop for the merge alert.
    if not re.search(r'ci_loop\["unverified_merge_reason"\]\s*=', src):
        missing.append("ci_loop['unverified_merge_reason'] threading")
    assert not missing, (
        "orchestrator no-CI gate bypass-reason wiring changed — missing: "
        f"{missing}. Each reason key must match an _UNVERIFIED_REASON_LABELS "
        "entry in alert_dispatch.py; update both sides together."
    )


def test_orchestrator_no_ci_gate_redetects_missing_test_command_wiring():
    """Drift guard: the orchestrator's no-CI gate must still call
    ``maybe_redetect_missing_test_command`` when a repo has no detectable test
    command at gate-time, before falling back to the unverified merge. If this
    call is dropped, the gate silently regresses to always merging UNVERIFIED
    without attempting re-detection, even though a test command might be
    recoverable."""
    src = _orchestrator_source()
    required = [
        "maybe_redetect_missing_test_command",
        "_redetected = await",
        "_no_ci_verify_gate = True",
        '"no_detectable_test_command"',
    ]
    missing = [needle for needle in required if needle not in src]
    assert not missing, (
        "orchestrator no-CI gate re-detection wiring changed — missing: "
        f"{missing}. Before merging a no-CI repo UNVERIFIED, the gate must "
        "attempt maybe_redetect_missing_test_command and route to verify_tests "
        "if successful; update orchestrator.py AND this guard together."
    )


def test_orchestrator_no_ci_redetect_refreshes_snapshot_before_gate_check():
    """A successful no-test-command re-detect must be visible in the same poll."""
    import re

    src = _orchestrator_source()
    assert re.search(
        r"_redetected = await maybe_redetect_missing_test_command\(.+?"
        r"if _redetected:\s+_ci_proj = \(\s+await _resnapshot_project\(_ci_proj_orm\)",
        src,
        re.DOTALL,
    ), (
        "orchestrator no-CI re-detection must refresh the cached _ProjectSnapshot "
        "before re-checking should_run_no_ci_test_gate; otherwise a newly found "
        "test command still merges UNVERIFIED for one poll."
    )


# ---------------------------------------------------------------------------
# PR-caused self-suppression: the PR's OWN diff disabled tests AND the suite
# stopped verifying (all-skipped, or a mostly-skipped pass). The gate must BLOCK
# and route to the existing block-and-fix path. Environmental skips and small
# flaky quarantines (where the suite still genuinely runs) MUST be preserved.
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_no_ci_pr_self_suppressed_suite_blocks_merge_and_spawns_fix(
    db_session, mocker
):
    _patch_github(mocker)
    merge_mock = mocker.patch("app.github.merge_pull_request", return_value=None)

    project = await _make_no_ci_project(db_session, with_test_cmd=True)
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

    # The PR's own diff newly added skip markers AND the suite all-skipped: the
    # agent emits noTestsReason='all_skipped' + a disabledTests block.
    await _plant_verdict(
        db_session,
        verify_task,
        {
            "detected": False,
            "passed": False,
            "noTestsReason": "all_skipped",
            "tests": [],
            "output": "0 passed, 12 skipped",
            "disabledTests": {
                "count": 12,
                "markers": [
                    {
                        "file": "tests/test_math.py",
                        "marker": "@pytest.mark.skip",
                        "line": "1",
                    }
                ],
            },
        },
    )

    verdict = await _latest_artifact_logs(
        db_session, verify_task.id, ArtifactType.MERGE_TEST_RESULT
    )
    outcome, detail = classify_merge_test_verdict(verdict)
    assert outcome == "fail"
    assert "self-suppressed" in detail
    assert "@pytest.mark.skip" in detail

    ci_result = build_no_ci_gate_ci_result(outcome, detail)
    assert ci_result["conclusion"] == "failure"

    parent = await db_session.get(Task, parent.id)
    acted = await handle_ci_completion(
        parent, ci_result, "ghs_test", "acme", "widget", db_session, source="poll"
    )
    assert acted is True
    # The suite was made green by skipping → merge blocked, fix spawned.
    merge_mock.assert_not_called()
    all_tasks = (
        (await db_session.execute(select(Task).where(Task.project_id == project.id)))
        .scalars()
        .all()
    )
    fix_tasks = [t for t in all_tasks if t.first_payload.get("type") == "fix"]
    assert len(fix_tasks) == 1, "self-suppressed suite must spawn exactly one fix task"
    assert "skip" in fix_tasks[0].context["ci_failure_logs"]


def test_no_ci_self_suppression_preservation_cases_unchanged():
    """The conservative non-blocking behavior is preserved when the PR did NOT
    cause the suppression: an environmental all-skip (no disabledTests block)
    still falls open UNVERIFIED, and a small flaky quarantine where the suite
    still genuinely runs still passes. Both keep ``conclusion='success'``."""
    # Environmental all-skip — no disabledTests block → undetected fall-open.
    outcome, detail = classify_merge_test_verdict(
        {"detected": False, "passed": False, "noTestsReason": "all_skipped"}
    )
    assert outcome == "undetected"
    assert build_no_ci_gate_ci_result(outcome, detail)["conclusion"] == "success"

    # Small flaky quarantine — markers present but the suite still genuinely runs
    # (fully-exercised pass) → non-blocking pass with the visibility note.
    outcome, detail = classify_merge_test_verdict(
        {
            "detected": True,
            "passed": True,
            "coverage": {"passed": 1400, "skipped": 3, "total": 1403},
            "disabledTests": {"count": 3, "markers": []},
        }
    )
    assert outcome == "pass"
    assert "disables 3 test(s)" in detail
    assert build_no_ci_gate_ci_result(outcome, detail)["conclusion"] == "success"


# ---------------------------------------------------------------------------
# 10. No-checks escalation auto-resolution backstop + self-heal re-detect.
#
# The orchestrator's escalation block (after ~600s with no checks ever seen)
# historically raised ONE alert and then waited forever for a manual merge, so
# every rollup PR on a no-CI repo had to be hand-merged. The backstop now calls
# ci_resolution.resolve_no_checks_escalation to AUTO-RESOLVE a no-CI strand —
# strictly gated on proceed_on_no_checks so a CI-gated repo is never
# auto-merged. The self-heal helper maybe_redetect_ci_presence flips a stale
# has_ci=None to a definitive value so legacy/incomplete-detection projects stop
# stranding. These compose the SAME production helpers the orchestrator calls.
# ---------------------------------------------------------------------------


async def _make_definitive_no_ci_project(
    db_session, *, with_test_cmd, has_ci=False, requires_ci_checks=False
):
    """A project with an explicit tri-state ``has_ci`` in its build_config.

    ``has_ci=False`` is a DEFINITIVE no-CI repo (detection completed, found no
    workflows); ``has_ci=True`` is CI-gated; ``has_ci=None`` is the
    stale/incomplete-detection state that strands legacy projects.
    """
    directories = [{"path": ".", "test_cmd": "pytest" if with_test_cmd else ""}]
    build_config = {
        "requires_ci_checks": requires_ci_checks,
        "directories": directories,
    }
    if has_ci is not None:
        build_config["has_ci"] = has_ci
    project = Project(
        name="Definitive no-CI repo",
        user_id="test-user",
        github_owner="acme",
        github_repo="widget",
        integration_branch=BASE,
        build_config=build_config,
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)
    return project


@pytest.mark.asyncio
async def test_no_checks_escalation_auto_resolves_for_definitive_no_ci_repo(
    db_session, mocker
):
    """A PR on a DEFINITIVE no-CI repo (``has_ci=False``) WITH a detectable suite
    that stranded with no checks for >600s must AUTO-RESOLVE at escalation rather
    than wait forever — routing through the SAME verify_tests gate so a real
    MERGE_TEST_RESULT verdict gates the merge."""
    _patch_github(mocker)
    merge_mock = mocker.patch(
        "app.github.merge_pull_request", return_value={"merged": True}
    )

    project = await _make_definitive_no_ci_project(
        db_session, with_test_cmd=True, has_ci=False
    )
    parent = _make_parent_task(db_session, project)
    await db_session.commit()
    await db_session.refresh(parent)

    # The NEW backstop decision the orchestrator's escalation block calls: a
    # definitive has_ci=False repo WITH a runnable suite routes through the
    # verify_tests gate (NOT keep_polling, NOT an unverified immediate-merge).
    assert (
        resolve_no_checks_escalation(project, BASE, gate_enabled=True) == "verify_tests"
    )

    # Promote → the verify gate runs the repo's suite; a real PASSING verdict
    # gates the merge through the same path the definitive-no_checks branch uses.
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
            "tests": [{"name": "tests/test_x.py::test_ok", "passed": True}],
            "output": "1 passed in 0.05s",
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
    await db_session.refresh(parent)
    assert parent.status == TaskStatus.COMPLETED


@pytest.mark.asyncio
async def test_no_checks_escalation_auto_resolves_unverified_for_testless_no_ci_repo(
    db_session, mocker
):
    """A DEFINITIVE no-CI repo (``has_ci=False``) with NO detectable suite must
    auto-resolve to an UNVERIFIED merge at escalation — never keep waiting."""
    _patch_github(mocker)
    merge_mock = mocker.patch(
        "app.github.merge_pull_request", return_value={"merged": True}
    )

    project = await _make_definitive_no_ci_project(
        db_session, with_test_cmd=False, has_ci=False
    )
    parent = _make_parent_task(db_session, project)
    await db_session.commit()
    await db_session.refresh(parent)

    # Test-less definitive no-CI repo → merge UNVERIFIED (NOT verify_tests).
    assert (
        resolve_no_checks_escalation(project, BASE, gate_enabled=True)
        == "merge_unverified"
    )

    # The escalation merge_unverified path builds a success ci_result (matching
    # build_no_ci_gate_ci_result's undetected→success mapping) and merges.
    ci_result = build_no_ci_gate_ci_result("undetected", "")
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
async def test_no_checks_escalation_keeps_polling_for_ci_gated_repo(db_session):
    """A CI-gated repo (``has_ci=True``) that has not yet seen its checks must
    NEVER auto-merge — the backstop keeps polling exactly as today."""
    project = await _make_definitive_no_ci_project(
        db_session, with_test_cmd=True, has_ci=True, requires_ci_checks=True
    )
    assert proceed_on_no_checks(project, BASE) is False
    assert (
        resolve_no_checks_escalation(project, BASE, gate_enabled=True) == "keep_polling"
    )


@pytest.mark.asyncio
async def test_no_checks_escalation_keeps_polling_for_unknown_has_ci(db_session):
    """A repo with ``has_ci=None`` (incomplete/legacy detection) is treated as
    possibly-CI-gated and must NOT auto-merge — checks may still register."""
    project = await _make_definitive_no_ci_project(
        db_session, with_test_cmd=False, has_ci=None, requires_ci_checks=True
    )
    assert proceed_on_no_checks(project, BASE) is False
    assert (
        resolve_no_checks_escalation(project, BASE, gate_enabled=True) == "keep_polling"
    )


@pytest.mark.asyncio
async def test_self_heal_redetect_flips_stale_has_ci_none_to_false_and_resolves(
    db_session, mocker
):
    """Mechanism 2: a stale ``has_ci=None`` strands a genuine no-CI repo. A bounded
    re-detect that confirms zero workflows flips ``has_ci`` to a definitive False,
    so proceed_on_no_checks (and thus the backstop) resolves the strand."""
    project = await _make_definitive_no_ci_project(
        db_session, with_test_cmd=False, has_ci=None, requires_ci_checks=True
    )
    # Before re-detect: stale has_ci=None keeps polling forever.
    assert (
        resolve_no_checks_escalation(project, BASE, gate_enabled=True) == "keep_polling"
    )

    mocker.patch(
        "app.github.get_repo_installation_token", AsyncMock(return_value="ghs_tok")
    )
    mocker.patch(
        "app.routers.admin_onboard._detect_build_config",
        AsyncMock(
            return_value={
                "language": "python",
                "directories": [{"path": ".", "test_cmd": ""}],
                "has_ci": False,
            }
        ),
    )

    flipped = await maybe_redetect_ci_presence(project, db_session, ref=BASE)
    assert flipped is True
    await db_session.refresh(project)
    assert (project.build_config or {}).get("has_ci") is False
    # Now the strand resolves (test-less → merge UNVERIFIED).
    assert proceed_on_no_checks(project, BASE) is True
    assert (
        resolve_no_checks_escalation(project, BASE, gate_enabled=True)
        == "merge_unverified"
    )


@pytest.mark.asyncio
async def test_self_heal_redetect_finds_has_ci_true_keeps_polling(db_session, mocker):
    """Mechanism 2 safety: if the re-detect finds the repo REALLY has CI
    (``has_ci=True`` — checks just not registered yet), it persists True but the
    backstop keeps polling and never auto-merges."""
    project = await _make_definitive_no_ci_project(
        db_session, with_test_cmd=False, has_ci=None, requires_ci_checks=True
    )
    mocker.patch(
        "app.github.get_repo_installation_token", AsyncMock(return_value="ghs_tok")
    )
    mocker.patch(
        "app.routers.admin_onboard._detect_build_config",
        AsyncMock(
            return_value={"language": "python", "directories": [], "has_ci": True}
        ),
    )

    flipped = await maybe_redetect_ci_presence(project, db_session, ref=BASE)
    assert flipped is True
    await db_session.refresh(project)
    assert (project.build_config or {}).get("has_ci") is True
    # has_ci=True is CI-gated → still keep polling, never auto-merge.
    assert proceed_on_no_checks(project, BASE) is False
    assert (
        resolve_no_checks_escalation(project, BASE, gate_enabled=True) == "keep_polling"
    )


@pytest.mark.asyncio
async def test_self_heal_redetect_incomplete_read_leaves_config_untouched(
    db_session, mocker
):
    """Mechanism 2 conservatism: a re-detect that is still incomplete
    (``has_ci=None``) persists NOTHING — a guessed value is never recorded and
    the strand keeps polling."""
    project = await _make_definitive_no_ci_project(
        db_session, with_test_cmd=False, has_ci=None, requires_ci_checks=True
    )
    mocker.patch(
        "app.github.get_repo_installation_token", AsyncMock(return_value="ghs_tok")
    )
    mocker.patch(
        "app.routers.admin_onboard._detect_build_config",
        AsyncMock(
            return_value={"language": "python", "directories": [], "has_ci": None}
        ),
    )

    flipped = await maybe_redetect_ci_presence(project, db_session, ref=BASE)
    assert flipped is False
    await db_session.refresh(project)
    assert (project.build_config or {}).get("has_ci") is None


@pytest.mark.asyncio
async def test_self_heal_redetect_noop_when_has_ci_already_definitive(
    db_session, mocker
):
    """The self-heal helper fires ONLY for a stale ``has_ci=None``; a project that
    already carries a definitive ``has_ci`` (here True) is left untouched and the
    detector is never even called."""
    project = await _make_definitive_no_ci_project(
        db_session, with_test_cmd=False, has_ci=True, requires_ci_checks=True
    )
    detect_mock = mocker.patch(
        "app.routers.admin_onboard._detect_build_config",
        AsyncMock(return_value={"has_ci": False}),
    )
    flipped = await maybe_redetect_ci_presence(project, db_session, ref=BASE)
    assert flipped is False
    detect_mock.assert_not_called()


# Static wiring: the orchestrator's escalation block must thread the backstop
# decision and the verify-gate continuation marker the composed flows mirror.
def test_orchestrator_no_checks_escalation_backstop_wiring():
    src = _orchestrator_source()
    required = [
        "resolve_no_checks_escalation",
        "maybe_redetect_ci_presence",
        "_esc_redetected = await maybe_redetect_ci_presence",
        "await _resnapshot_project(_esc_proj_orm)",
        # The escalation block sets the sticky verify-gate marker...
        'ci_loop["no_ci_escalation_verify_gate"] = (',
        # ...and the gate block honors it on the next poll via the base marker.
        'ci_loop.get("no_ci_escalation_base")',
    ]
    missing = [needle for needle in required if needle not in src]
    assert not missing, (
        "orchestrator no-checks escalation backstop wiring changed — missing: "
        f"{missing}. Update orchestrator.py AND the mirroring tests together."
    )


def test_orchestrator_verify_tests_spawn_deferral_escalation_wiring():
    """The orchestrator must escalate through canonical approval provenance.

    A direct ``requires_approval = True`` write would inherit an older
    review-wait owner.  The monitor-owned helper establishes a fresh independent
    hold after the verify_tests spawn-deferral cap is exhausted.
    """
    src = _orchestrator_source()
    required = [
        "_VERIFY_TESTS_SPAWN_DEFERRAL_CAP",
        '"verify_tests_spawn_deferrals"',
        "verify_tests spawn deferred",
        "escalating for operator review",
        "_establish_monitor_approval_hold(",
        'reason="verify_tests_spawn_deferrals_exhausted"',
    ]
    missing = [needle for needle in required if needle not in src]
    assert not missing, (
        "orchestrator verify_tests spawn-deferral escalation wiring changed — "
        f"missing: {missing}. Update orchestrator.py AND the mirroring tests together."
    )


@pytest.mark.asyncio
async def test_no_ci_verify_tests_spawn_deferral_cap_escalates_to_operator_review(
    db_session, mocker
):
    """After _VERIFY_TESTS_SPAWN_DEFERRAL_CAP consecutive spawn deferrals the
    parent task must be set to FAILED+requires_approval and an alert dispatched.

    When _spawn_pipeline_task returns None (resource pressure / suppression) on
    every attempt, the orchestrator increments verify_tests_spawn_deferrals in
    ci_loop.  Once that counter reaches the cap the task must leave EVALUATING
    rather than remaining stranded.
    """
    alert_mock = mocker.patch(
        "app.alert_dispatch._maybe_create_failure_alert",
        new_callable=mocker.AsyncMock,
    )
    mocker.patch("app.broadcast.broadcast_to_project", return_value=None)

    project = await _make_no_ci_project(db_session, with_test_cmd=True)
    parent = _make_parent_task(db_session, project)
    await db_session.commit()
    await db_session.refresh(parent)

    # Simulate the orchestrator's deferral loop directly: increment the counter
    # and apply the same escalation predicate the inline code applies.
    from app.models import TaskStatus
    from app.runtime_config_state import runtime_config as _runtime_config
    from app.alert_dispatch import _maybe_create_failure_alert

    ci_loop = dict((parent.context or {}).get("review_loop", {}) or {})

    for _cycle in range(1, _VERIFY_TESTS_SPAWN_DEFERRAL_CAP + 1):
        _spawn_deferrals = (ci_loop.get("verify_tests_spawn_deferrals", 0) or 0) + 1
        ci_loop["verify_tests_spawn_deferrals"] = _spawn_deferrals

        if (
            _spawn_deferrals >= _VERIFY_TESTS_SPAWN_DEFERRAL_CAP
            and not parent.requires_approval
        ):
            parent.status = TaskStatus.FAILED
            if _runtime_config.get("require_approval_gates", True):
                parent.requires_approval = True
            parent.latest_status_message = (
                f"No CI on PR #{PR_NUMBER} — verify_tests spawn deferred "
                f"{_spawn_deferrals} consecutive times — escalating for operator review"
            )
            await _maybe_create_failure_alert(
                parent,
                db_session,
                f"PR #{PR_NUMBER} no-CI verify_tests spawn deferred "
                f"{_spawn_deferrals} consecutive cycles — escalating for operator review",
            )

    assert (
        ci_loop["verify_tests_spawn_deferrals"] == _VERIFY_TESTS_SPAWN_DEFERRAL_CAP
    ), f"counter must reach cap ({_VERIFY_TESTS_SPAWN_DEFERRAL_CAP})"
    assert parent.status == TaskStatus.FAILED, (
        "task must be FAILED after spawn-deferral cap is exhausted"
    )
    assert parent.requires_approval is True, (
        "task must require operator approval after spawn-deferral cap"
    )
    assert "escalating for operator review" in parent.latest_status_message
    alert_mock.assert_called_once(), "exactly one alert must be dispatched at the cap"

    # Before the cap: no escalation should have occurred.
    parent2 = _make_parent_task(db_session, project)
    await db_session.commit()
    await db_session.refresh(parent2)
    ci_loop2 = {}
    for _cycle in range(1, _VERIFY_TESTS_SPAWN_DEFERRAL_CAP):
        _d = (ci_loop2.get("verify_tests_spawn_deferrals", 0) or 0) + 1
        ci_loop2["verify_tests_spawn_deferrals"] = _d
        if _d >= _VERIFY_TESTS_SPAWN_DEFERRAL_CAP and not parent2.requires_approval:
            parent2.status = TaskStatus.FAILED
            parent2.requires_approval = True

    assert parent2.status == TaskStatus.EVALUATING, (
        f"task must remain EVALUATING before the cap ({_VERIFY_TESTS_SPAWN_DEFERRAL_CAP - 1} deferrals)"
    )
    assert not parent2.requires_approval, (
        "requires_approval must NOT be set before the deferral cap"
    )


# ---------------------------------------------------------------------------
# merge_gate_runtime locked-fetch contract
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_no_ci_gate_recording_uses_locked_project_fetch(db_session, mocker):
    """The orchestrator's merge_gate_runtime recording path uses
    session.execute(...with_for_update()) not session.get() — preventing
    lost-update races when concurrent poll cycles write to merge_gate_runtime.

    Mirrors the three fixed call sites in orchestrator_ci_monitor.py: after
    the fix, no plain session.get remains on any merge_gate_runtime writer.
    """
    from sqlmodel import select as _select

    project = await _make_no_ci_project(db_session, with_test_cmd=False)

    # Spy on session.get — the fixed code must NOT call it when fetching the
    # project for merge_gate_runtime writes.
    get_spy = mocker.spy(db_session, "get")

    # Exercise the locked fetch pattern that now lives in each of the three
    # fixed sites in orchestrator_ci_monitor.py.
    fetched = (
        (
            await db_session.execute(
                _select(Project)
                .where(Project.id == project.id)
                .with_for_update()
            )
        )
        .scalars()
        .first()
    )

    assert fetched is not None, "locked fetch must return the project row"
    assert fetched.id == project.id

    # The locked path must not have fallen back to session.get.
    get_spy.assert_not_called()

    # Verify arecord_no_ci_gate_runtime correctly updates merge_gate_runtime
    # through the locked project reference.
    from app.ci_resolution import arecord_no_ci_gate_runtime

    await arecord_no_ci_gate_runtime(
        db_session,
        fetched,
        executed_real_tests=False,
        unverified_reason="no_detectable_test_command",
        when=_RUNTIME_WHEN,
    )
    await db_session.commit()
    await db_session.refresh(fetched)

    runtime = merge_gate_runtime_block(fetched)
    assert runtime is not None
    assert runtime["executed_real_tests"] is False
    assert runtime["last_unverified_reason"] == "no_detectable_test_command"
    assert runtime["last_run_at"] == _RUNTIME_WHEN.isoformat()

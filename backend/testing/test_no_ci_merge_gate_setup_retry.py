"""End-to-end coverage for the transient verify-time setup-failure retry path.

The no-CI merge gate distinguishes two terminal-without-pass outcomes that both
classify as ``"undetected"`` from ``classify_merge_test_verdict``:

  - A genuine ``detected=false`` verdict (a suite ran, found no runnable
    command). ``_verdict`` is a dict → NOT a crash → falls open immediately.
  - A verify task that could not run an EXISTING suite because setup/runner
    infrastructure failed. The agent's verify-tests handler now posts status
    ``FAILED`` (with a ``setupFailed=true`` MERGE_TEST_RESULT artifact) instead
    of ``COMPLETED detected=false``, so the orchestrator's crash-vs-undetected
    split treats it like a crash: ``_verdict`` is None (only a COMPLETED task is
    read for a verdict) → re-spawn up to ``_VERIFY_TESTS_MAX_RETRIES`` times
    before falling open. The exhausted-retry UNVERIFIED merge is labeled with
    the distinct ``"verify_setup_failed"`` reason for operators.

This file pins that contract end-to-end without touching the sibling-owned
``test_no_ci_merge_gate_e2e.py``. It mirrors the orchestrator's terminal-poll
logic (verdict read only on COMPLETED; setupFailed peek on FAILED) the same way
that file mirrors ``_is_verify_crash``.
"""

import json
import uuid

import pytest
from sqlalchemy import select

from app.alert_dispatch import _UNVERIFIED_REASON_LABELS
from app.ci_completion import handle_ci_completion
from app.ci_resolution import (
    build_no_ci_gate_ci_result,
    classify_merge_test_verdict,
    resolve_no_checks_escalation,
    should_run_no_ci_test_gate,
)
from app.models import Alert, Artifact, ArtifactType, Project, Task, TaskStatus
from app.orchestrator import _VERIFY_TESTS_MAX_RETRIES
from app.release_qualifier_audit import _latest_artifact_logs
from app.review_pipeline import _spawn_pipeline_task

PR_NUMBER = 5151
BRANCH = "feat/setup-flake"
BASE = "main"
BASE_SHA = "base-5151"
HEAD_SHA = "head-5151"


@pytest.fixture(autouse=True)
def _default_no_migration_pr(mocker):
    from unittest.mock import AsyncMock

    mocker.patch(
        "app.github.get_pr_files",
        AsyncMock(return_value=[{"filename": "README.md"}]),
    )


async def _make_no_ci_project(db_session) -> Project:
    project = Project(
        name="External no-CI repo",
        user_id="test-user",
        github_owner="acme",
        github_repo="widget",
        integration_branch=BASE,
        build_config={
            "requires_ci_checks": False,
            "directories": [{"path": ".", "test_cmd": "pytest"}],
        },
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)
    return project


def _make_parent_task(db_session, project) -> Task:
    review_target = {
        "project_id": str(project.id),
        "pr_number": PR_NUMBER,
        "base_sha": BASE_SHA,
        "head_sha": HEAD_SHA,
    }
    loop = {
        "pr_number": PR_NUMBER,
        "branch": BRANCH,
        "base_branch": BASE,
        "iteration": 1,
        "review_approved": True,
        "ci_waiting": True,
        "base_sha": BASE_SHA,
        "head_sha": HEAD_SHA,
        "ci_checked_base_sha": BASE_SHA,
        "ci_checked_head_sha": HEAD_SHA,
        "review_target": review_target,
        "review_history": [
            {
                "verdict": "APPROVE",
                "issues": [],
                "reviewed_base_sha": BASE_SHA,
                "reviewed_head_sha": HEAD_SHA,
                "target_verified_by_backend": True,
                "review_task_id": "test-no-ci-setup-review",
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


async def _terminalize_with_setup_failed(db_session, verify_task: Task) -> None:
    """Mark the verify task FAILED with a setupFailed MERGE_TEST_RESULT artifact.

    Mirrors the agent verify-tests handler's persistent-setup-failure exit: it
    posts status FAILED but still writes the diagnostic artifact carrying
    ``setupFailed=true`` so the orchestrator can label the eventual UNVERIFIED
    merge precisely.
    """
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
                    "output": "npm ci failed: registry timeout",
                }
            ),
        )
    )
    verify_task.status = TaskStatus.FAILED
    db_session.add(verify_task)
    await db_session.commit()


def _read_terminal_verdict(verify_task: Task, artifact: dict | None):
    """Mirror the orchestrator terminal-poll verdict read (orchestrator.py ~5266).

    Only a COMPLETED verify task carries a usable verdict; a FAILED one is
    treated as a crash, so ``_verdict`` stays None even when a diagnostic
    artifact exists.
    """
    return artifact if verify_task.status == TaskStatus.COMPLETED else None


def _verify_setup_failed(verify_task: Task, artifact: dict | None) -> bool:
    """Mirror the orchestrator setup-failure peek (orchestrator.py ~5290)."""
    return bool(
        verify_task.status == TaskStatus.FAILED
        and isinstance(artifact, dict)
        and artifact.get("setupFailed")
    )


def _is_verify_crash(outcome: str, verdict: dict | None) -> bool:
    return outcome == "undetected" and verdict is None


def _patch_github(mocker, project_id):
    from unittest.mock import AsyncMock

    mocker.patch("app.broadcast.broadcast_to_project", return_value=None)
    mocker.patch("app.review_pipeline.broadcast_to_project", return_value=None)
    mocker.patch("app.alert_dispatch.broadcast_to_project", return_value=None)
    mocker.patch(
        "app.notification_dispatch.dispatch_external_notification", return_value=None
    )
    mocker.patch("app.github.create_pr_comment", return_value=None)
    mocker.patch("app.alert_dispatch._maybe_create_failure_alert", return_value=None)
    mocker.patch(
        "app.rollup_size_preflight.preflight_rollup_size_caps",
        AsyncMock(return_value=("ok", None)),
    )
    mocker.patch(
        "app.review_merge_authority._fetch_live_review_target",
        AsyncMock(
            return_value={
                "project_id": str(project_id),
                "pr_number": PR_NUMBER,
                "base_sha": BASE_SHA,
                "head_sha": HEAD_SHA,
            }
        ),
    )


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


async def _set_unverified_reason(db_session, parent, reason):
    from sqlalchemy.orm.attributes import flag_modified

    parent = await db_session.get(Task, parent.id)
    loop = dict((parent.context or {}).get("review_loop", {}))
    loop["unverified_merge_reason"] = reason
    parent.context = {**(parent.context or {}), "review_loop": loop}
    flag_modified(parent, "context")
    db_session.add(parent)
    await db_session.commit()
    return parent


# ---------------------------------------------------------------------------
# 1. A FAILED-with-setupFailed verify task is retried, then (budget spent) falls
#    open UNVERIFIED with the distinct verify_setup_failed reason.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_setup_failed_verify_task_retries_then_merges_unverified_with_setup_reason(
    db_session, mocker
):
    merge_mock = mocker.patch(
        "app.github.merge_pull_request", return_value={"merged": True}
    )

    project = await _make_no_ci_project(db_session)
    _patch_github(mocker, project.id)
    parent = _make_parent_task(db_session, project)
    await db_session.commit()
    await db_session.refresh(parent)

    assert should_run_no_ci_test_gate(project, BASE, gate_enabled=True) is True

    # ── First attempt: spawn, then FAIL with a setupFailed artifact. ──────────
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

    await _terminalize_with_setup_failed(db_session, verify_task)
    await db_session.refresh(verify_task)
    assert verify_task.status == TaskStatus.FAILED

    artifact = await _latest_artifact_logs(
        db_session, verify_task.id, ArtifactType.MERGE_TEST_RESULT
    )
    # The setupFailed marker is present on the artifact even though the verdict
    # read (COMPLETED-only) yields None.
    assert _verify_setup_failed(verify_task, artifact) is True
    verdict = _read_terminal_verdict(verify_task, artifact)
    assert verdict is None
    outcome, _ = classify_merge_test_verdict(verdict)
    assert outcome == "undetected"

    # A setup-failure crash still has retry budget → re-spawn, no merge/alert.
    retries = 0
    assert _is_verify_crash(outcome, verdict) is True
    assert retries < _VERIFY_TESTS_MAX_RETRIES

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
    assert retry_task.id != verify_task.id
    merge_mock.assert_not_called()
    assert await _unverified_merge_alerts(db_session, project.id) == []

    # ── Retry also fails setup: budget spent → fall open with the setup reason ─
    await _terminalize_with_setup_failed(db_session, retry_task)
    artifact = await _latest_artifact_logs(
        db_session, retry_task.id, ArtifactType.MERGE_TEST_RESULT
    )
    verdict = _read_terminal_verdict(retry_task, artifact)
    outcome, detail = classify_merge_test_verdict(verdict)
    assert _is_verify_crash(outcome, verdict) is True
    assert retries >= _VERIFY_TESTS_MAX_RETRIES

    # Exhausted setup-failure crash → distinct reason key (vs undetected_runtime).
    reason = (
        "verify_setup_failed"
        if _verify_setup_failed(retry_task, artifact)
        else "undetected_runtime"
    )
    assert reason == "verify_setup_failed"

    ci_result = build_no_ci_gate_ci_result(outcome, detail)
    assert ci_result["conclusion"] == "success"

    parent = await _set_unverified_reason(db_session, parent, reason)
    acted = await handle_ci_completion(
        parent, ci_result, "ghs_test", "acme", "widget", db_session, source="poll"
    )
    assert acted is True
    merge_mock.assert_called_once()
    await db_session.refresh(parent)
    assert parent.status == TaskStatus.COMPLETED

    alerts = await _unverified_merge_alerts(db_session, project.id)
    assert len(alerts) == 1
    assert f"PR #{PR_NUMBER}" in alerts[0].title
    # The operator-facing message uses the setup-failure label, not the generic
    # "no usable verdict" / "no runnable test command" wording.
    assert _UNVERIFIED_REASON_LABELS["verify_setup_failed"] in alerts[0].message
    assert "could not be run" in alerts[0].message


# ---------------------------------------------------------------------------
# 2. A genuine detected=false verdict still falls open immediately (no retry,
#    no setup-failed reason) — forward progress preserved.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_genuine_detected_false_still_falls_open_without_setup_reason(
    db_session, mocker
):
    project = await _make_no_ci_project(db_session)

    # A COMPLETED detected=false verdict (suite ran, no runnable command) is a
    # dict — NOT a crash — so it must not be retried nor labeled setup-failed.
    fake_completed = Task(
        project_id=project.id,
        title="verify",
        payloads=[{"type": "verify_tests"}],
        status=TaskStatus.COMPLETED,
    )
    artifact = {"detected": False}
    assert _verify_setup_failed(fake_completed, artifact) is False
    verdict = _read_terminal_verdict(fake_completed, artifact)
    assert verdict == {"detected": False}
    outcome, _ = classify_merge_test_verdict(verdict)
    assert outcome == "undetected"
    assert _is_verify_crash(outcome, verdict) is False


# ---------------------------------------------------------------------------
# 3. The no-checks escalation backstop routes a DEFINITIVE no-CI repo (has_ci=
#    False) with a detectable suite into the SAME verify_tests gate — so this
#    file's setup-failure retry path applies whether the gate was reached via the
#    definitive-no_checks branch OR via the escalation backstop.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_escalation_backstop_routes_definitive_no_ci_repo_into_verify_gate(
    db_session,
):
    project = await _make_no_ci_project(db_session)
    # Make the no-CI signal DEFINITIVE (detection found zero workflows).
    project.build_config = {**project.build_config, "has_ci": False}
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    # The escalation backstop promotes this strand to the verify_tests gate —
    # the same entry point this file's retry machinery guards. A setup failure
    # then retries and (budget spent) falls open with verify_setup_failed,
    # exactly as the definitive-no_checks branch does.
    assert (
        resolve_no_checks_escalation(project, BASE, gate_enabled=True) == "verify_tests"
    )
    assert should_run_no_ci_test_gate(project, BASE, gate_enabled=True) is True

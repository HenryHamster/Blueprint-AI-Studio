"""Capability-epoch regressions for deterministic local-runner declines."""

import uuid
from datetime import timedelta

import pytest

from app.models import LocalRunner, Project, Task, TaskStatus, _utcnow
from app.blocked_retry_authority import execution_capability_retry_receipt
from app.execution_capability_requirements import (
    CAPABILITY_BACKEND_VERIFICATION,
    persist_learned_execution_capabilities,
)
from app.orchestrator_runner_dispatch import (
    _load_active_runners_cache,
    _select_eligible_runner,
)
from app.runner_decline import (
    DECLINE_EXCLUSION_SECONDS,
    decline_exclusion_is_advisory,
    decline_record_for_runner,
    recently_declined_runner_ids,
    record_deterministic_runner_decline,
    record_runner_decline,
)
from app.runner_eligibility import RunnerDispatchSnapshot


def _verification_proof(*, ready: bool = True) -> dict:
    exit_code = 0 if ready else 1
    return {
        "version": 1,
        "ready": ready,
        "boundary": {"class": "agent", "backend": "bwrap"},
        "probes": {
            "ruff": {"ok": True, "exit_code": 0},
            "docker_postgres": {"ok": ready, "exit_code": exit_code},
        },
    }


def _runner(
    runner_id: uuid.UUID,
    project_id: uuid.UUID,
    *,
    source_sha: str,
    agent_source_sha: str,
    ready: bool = True,
) -> RunnerDispatchSnapshot:
    return RunnerDispatchSnapshot(
        id=runner_id,
        project_id=project_id,
        capabilities={
            "task_types": "all",
            "agent_available": True,
            "project_verification": _verification_proof(ready=ready),
        },
        source_sha=source_sha,
        agent_source_sha=agent_source_sha,
    )


def _task(project_id: uuid.UUID) -> Task:
    return Task(
        project_id=project_id,
        payloads=[{"type": "refactor"}],
        status=TaskStatus.QUEUED,
        context={},
    )


def _select(task: Task, runner: RunnerDispatchSnapshot):
    return _select_eligible_runner(
        {task.project_id: [runner]},
        task.project_id,
        "refactor",
        None,
        {},
        dispatch_seed=task.id.int,
        declined_runner_ids=recently_declined_runner_ids(task),
        requires_project_verification=True,
    )


def test_deterministic_decline_retries_same_runner_once_per_changed_source_epoch():
    project_id = uuid.uuid4()
    runner_id = uuid.uuid4()
    task = _task(project_id)
    old = _runner(
        runner_id,
        project_id,
        source_sha="a" * 40,
        agent_source_sha="a" * 40,
    )
    record_runner_decline(
        task,
        str(runner_id),
        reason="execution_capability_unavailable",
        same_runner_retryable=False,
        runner=old,
    )

    assert _select(task, old) is None
    updated = old._replace(agent_source_sha="b" * 40)
    assert _select(task, updated) is updated
    assert decline_record_for_runner(task, str(runner_id), runner=updated) is None

    # If that new epoch produces the same deterministic failure, recording it
    # replaces the old epoch and fences the runner again until another change.
    record_runner_decline(
        task,
        str(runner_id),
        reason="execution_capability_unavailable",
        same_runner_retryable=False,
        runner=updated,
    )
    assert _select(task, updated) is None


def test_changed_functional_proof_recovers_stable_runner_id_and_source_sha():
    project_id = uuid.uuid4()
    runner_id = uuid.uuid4()
    task = _task(project_id)
    failed = _runner(
        runner_id,
        project_id,
        source_sha="c" * 40,
        agent_source_sha="d" * 40,
        ready=False,
    )
    record_runner_decline(
        task,
        str(runner_id),
        reason="postgres probe failed",
        same_runner_retryable=False,
        runner=failed,
    )

    recovered = failed._replace(
        capabilities={
            **failed.capabilities,
            "project_verification": _verification_proof(ready=True),
        }
    )
    assert _select(task, recovered) is recovered


def test_unversioned_deterministic_decline_remains_fail_closed():
    project_id = uuid.uuid4()
    runner_id = uuid.uuid4()
    task = _task(project_id)
    task.context = {
        "declined_by": {
            str(runner_id): {
                "count": 1,
                "same_runner_retryable": False,
                "reason": "legacy deterministic mismatch",
            }
        }
    }
    capable = _runner(
        runner_id,
        project_id,
        source_sha="e" * 40,
        agent_source_sha="f" * 40,
    )
    assert _select(task, capable) is None


@pytest.mark.asyncio
async def test_capability_retry_receipt_applies_browser_contract(mocker):
    """Retry authority must select a runner that actual dispatch can use."""

    project_id = uuid.uuid4()
    browser_incapable = _runner(
        uuid.uuid4(),
        project_id,
        source_sha="1" * 40,
        agent_source_sha="2" * 40,
    )
    browser_capable = _runner(
        uuid.uuid4(),
        project_id,
        source_sha="3" * 40,
        agent_source_sha="4" * 40,
    )._replace(
        capabilities={
            **browser_incapable.capabilities,
            "isolation_backends": {
                "docker_usable": True,
                "playwright_chromium_usable": True,
            },
        }
    )
    task = _task(project_id)
    task.payloads = [{"type": "refactor", "requires_browser": True}]
    task.context, _ = persist_learned_execution_capabilities(
        {"preferred_runner_target_id": str(browser_incapable.id)},
        {CAPABILITY_BACKEND_VERIFICATION},
        observed_at=_utcnow(),
    )
    mocker.patch(
        "app.orchestrator_runner_dispatch._load_active_runners_cache",
        return_value={project_id: [browser_incapable, browser_capable]},
    )
    session = mocker.Mock()
    session.get = mocker.AsyncMock(return_value=None)

    receipt = await execution_capability_retry_receipt(task, session)

    assert receipt is not None
    assert receipt["runner_id"] == str(browser_capable.id)


def test_explicit_empty_runner_epoch_recovers_when_proof_first_appears():
    project_id = uuid.uuid4()
    runner_id = uuid.uuid4()
    task = _task(project_id)
    legacy_runner = RunnerDispatchSnapshot(
        id=runner_id,
        project_id=project_id,
        capabilities={"task_types": "all", "agent_available": True},
    )
    record_runner_decline(
        task,
        str(runner_id),
        reason="project verification unavailable",
        same_runner_retryable=False,
        runner=legacy_runner,
    )
    assert task.context["declined_by"][str(runner_id)]["capability_epoch"] == {
        "runner_source_sha": None,
        "runner_agent_source_sha": None,
        "project_verification_fingerprint": None,
    }

    proof_ready = legacy_runner._replace(
        capabilities={
            **legacy_runner.capabilities,
            "project_verification": _verification_proof(),
        }
    )
    assert _select(task, proof_ready) is proof_ready


def test_stale_deterministic_epoch_does_not_poison_strategic_transient_waiver():
    project_id = uuid.uuid4()
    stale_id = uuid.uuid4()
    transient_id = uuid.uuid4()
    task = Task(
        project_id=project_id,
        payloads=[{"type": "design_doc_update"}],
        status=TaskStatus.QUEUED,
        context={},
    )
    stale_old = RunnerDispatchSnapshot(
        id=stale_id,
        project_id=project_id,
        capabilities={"task_types": ["compute_experiment"]},
        source_sha="a" * 40,
    )
    stale_current = stale_old._replace(source_sha="b" * 40)
    transient = RunnerDispatchSnapshot(
        id=transient_id,
        project_id=project_id,
        capabilities={"task_types": "all"},
    )
    record_runner_decline(
        task,
        str(stale_id),
        reason="old deterministic mismatch",
        same_runner_retryable=False,
        runner=stale_old,
    )
    record_runner_decline(
        task,
        str(transient_id),
        reason="one-off spawn failure",
    )
    exclusions = recently_declined_runner_ids(task)

    # The task-only legacy predicate cannot see epochs and remains conservative.
    assert decline_exclusion_is_advisory(task, "design_doc_update") is False
    chosen = _select_eligible_runner(
        {project_id: [stale_current, transient]},
        project_id,
        "design_doc_update",
        None,
        {},
        dispatch_seed=task.id.int,
        declined_runner_ids=exclusions,
        declined_exclusion_advisory=False,
    )
    assert chosen is transient


def test_epoch_advisory_preserves_fleet_wide_transient_decline_bound():
    project_id = uuid.uuid4()
    retired_id = uuid.uuid4()
    current_id = uuid.uuid4()
    task = Task(
        project_id=project_id,
        payloads=[{"type": "design_doc_update"}],
        status=TaskStatus.QUEUED,
        context={
            "declined_by": {
                str(retired_id): {
                    "count": 3,
                    "at": (
                        _utcnow()
                        - timedelta(seconds=DECLINE_EXCLUSION_SECONDS + 1)
                    ).isoformat(),
                    "reason": "expired transient declines on retired runner",
                    "same_runner_retryable": True,
                }
            }
        },
    )
    current = RunnerDispatchSnapshot(
        id=current_id,
        project_id=project_id,
        capabilities={"task_types": "all"},
    )
    record_runner_decline(
        task,
        str(current_id),
        reason="current transient decline",
    )
    exclusions = recently_declined_runner_ids(task)
    assert exclusions == {str(current_id)}
    assert decline_exclusion_is_advisory(task, "design_doc_update") is False

    assert (
        _select_eligible_runner(
            {project_id: [current]},
            project_id,
            "design_doc_update",
            None,
            {},
            dispatch_seed=task.id.int,
            declined_runner_ids=exclusions,
            declined_exclusion_advisory=False,
        )
        is None
    )


@pytest.mark.asyncio
async def test_server_records_epoch_and_dispatch_snapshot_carries_it(db_session):
    project = Project(name="Decline epoch", user_id="epoch-user")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    runner = LocalRunner(
        project_id=project.id,
        token_hash="1" * 64,
        hostname="epoch-runner",
        source_sha="1" * 40,
        agent_source_sha="2" * 40,
        last_seen_at=_utcnow(),
        capabilities={
            "task_types": "all",
            "agent_available": True,
            "project_verification": _verification_proof(),
        },
    )
    task = _task(project.id)
    task.context, _ = persist_learned_execution_capabilities(
        {"blocker": {"source": "agent"}},
        {CAPABILITY_BACKEND_VERIFICATION},
        observed_at=_utcnow(),
    )
    db_session.add(runner)
    db_session.add(task)
    await db_session.commit()

    await record_deterministic_runner_decline(
        db_session,
        task,
        str(runner.id),
        reason="execution_capability_unavailable",
        hostname=runner.hostname,
    )
    epoch = task.context["declined_by"][str(runner.id)]["capability_epoch"]
    assert epoch["runner_source_sha"] == "1" * 40
    assert epoch["runner_agent_source_sha"] == "2" * 40
    assert len(epoch["project_verification_fingerprint"]) == 16
    assert await execution_capability_retry_receipt(task, db_session) is None

    cache = await _load_active_runners_cache(db_session)
    snapshot = next(item for item in cache[project.id] if item.id == runner.id)
    assert snapshot.source_sha == "1" * 40
    assert snapshot.agent_source_sha == "2" * 40

    runner.agent_source_sha = "3" * 40
    await db_session.commit()
    first_recovery = await execution_capability_retry_receipt(task, db_session)
    assert first_recovery is not None
    assert first_recovery["runner_agent_source_sha"] == "3" * 40
    assert (
        first_recovery["proof_fingerprint"]
        == epoch["project_verification_fingerprint"]
    )

    await record_deterministic_runner_decline(
        db_session,
        task,
        str(runner.id),
        reason="execution_capability_unavailable again",
        hostname=runner.hostname,
    )
    assert await execution_capability_retry_receipt(task, db_session) is None
    runner.agent_source_sha = "4" * 40
    await db_session.commit()
    second_recovery = await execution_capability_retry_receipt(task, db_session)
    assert second_recovery is not None
    assert second_recovery["runner_agent_source_sha"] == "4" * 40
    assert second_recovery != first_recovery

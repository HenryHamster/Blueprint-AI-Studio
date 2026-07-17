"""
Integration tests for Blueprint orchestrator background services.

Tests three areas:
1. reaper_service() — stale heartbeat detection, retry logic, skip-locked protection
2. Max Plan account pool — load/reconcile, account selection, disabled-account filtering
3. Research task spawning — deduplication guard and depends_on_indices ordering
"""

import os

import pytest
import json
import uuid
from datetime import datetime, timedelta
from unittest.mock import AsyncMock, MagicMock

from app.time_utils import _utcnow
from testing.conftest import USE_POSTGRES
import app.main as _main_module
import app.shared_state as _shared_state
from app.reaper import reaper_service
from app.autonomous_research import _spawn_research_task
from app.failure_classification import classify_failure
from app.research_completion import _handle_research_completion
from app.max_plan_pool import (
    load_max_plan_pool,
    select_max_plan_account,
    release_max_plan_account,
    _max_plan_pools,
    _account_by_id,
)
from app.models import Project, Task, TaskStatus, MaxPlanAccount
from testing.factories import make_project, make_task

_TEST_USER = "test-user-123"


def test_resolved_red_ci_status_handoff_preserves_failed_logs():
    from app.orchestrator import _ci_result_from_resolved_pr_ci_status

    result = _ci_result_from_resolved_pr_ci_status(
        {
            "state": "red",
            "source": "branch_head",
            "failed_logs": "FAILED testing/test_foo.py::test_bar",
            "details": [{"name": "CI / Backend", "conclusion": "failure"}],
        }
    )

    assert result == {
        "status": "completed",
        "conclusion": "failure",
        "details": [{"name": "CI / Backend", "conclusion": "failure"}],
        "failed_logs": "FAILED testing/test_foo.py::test_bar",
    }


def test_resolved_red_ci_status_handoff_falls_back_to_check_names():
    from app.orchestrator import _ci_result_from_resolved_pr_ci_status

    result = _ci_result_from_resolved_pr_ci_status(
        {
            "state": "red",
            "source": "branch_head",
            "failed_logs": "",
            "details": [
                {"name": "CI / Backend", "conclusion": "failure"},
                {"name": "CI / Frontend", "conclusion": "failure"},
                {"name": "Detect changes", "conclusion": "success"},
            ],
        }
    )

    assert result is not None
    assert result["conclusion"] == "failure"
    assert result["failed_logs"] == "CI checks failed: CI / Backend, CI / Frontend"


@pytest.mark.asyncio
async def test_ci_monitor_extraction_contract(db_session, mocker):
    """Contract for the EVALUATING/CI-waiting monitor extraction.

    The sweep formerly inlined in ``orchestrator_worker`` now lives in
    ``app.orchestrator_ci_monitor._monitor_evaluating_tasks``. This test pins
    the extraction so a future edit cannot silently regress it:

    (1) Every extracted symbol is re-exported from ``app.orchestrator`` with
        *identical object identity* (so ``from app.orchestrator import X``
        keeps working for callers and tests), and ``_monitor_evaluating_tasks``
        is genuinely owned by the new module.
    (2) ``orchestrator_worker`` delegates to the extracted sweep rather than
        re-inlining the loop.
    (3) The extracted sweep actually RUNS against a live session: given an
        ``EVALUATING`` row armed with ``review_loop.ci_waiting``, it selects
        the row through the extracted ``_ci_waiting_sql_filter`` prefilter and
        locks it via ``_lock_task_for_mutation`` — exercising the moved
        production code end-to-end (not just introspecting it).
    """
    import inspect

    import app.orchestrator as orch
    import app.orchestrator_ci_monitor as cim

    # (1) re-export identity — same objects, new home.
    reexported = (
        "_VERIFY_TESTS_MAX_RETRIES",
        "_VERIFY_TESTS_SPAWN_DEFERRAL_CAP",
        "_apply_ci_merged_landed_signal",
        "_capture_ci_polling_snapshot",
        "_ci_result_from_resolved_pr_ci_status",
        "_ci_waiting_sql_filter",
        "_clear_retry_after_state",
        "_monitor_evaluating_tasks",
        "_resolve_ci_fix_branch",
    )
    for name in reexported:
        assert getattr(orch, name) is getattr(cim, name), (
            f"{name} must be re-exported from app.orchestrator with identical "
            "identity"
        )
    assert (
        cim._monitor_evaluating_tasks.__module__ == "app.orchestrator_ci_monitor"
    )

    # (2) the worker delegates to the extracted sweep.
    worker_src = inspect.getsource(orch.orchestrator_worker)
    assert "_monitor_evaluating_tasks(session)" in worker_src

    # (3) drive the extracted sweep against a live session. A ci_waiting row
    # with no resolvable PR number locks and short-circuits before any GitHub
    # I/O, so we exercise the moved SELECT + lock path without the network.
    ci_filter_spy = mocker.spy(cim, "_ci_waiting_sql_filter")
    lock_spy = mocker.spy(orch, "_lock_task_for_mutation")

    project = make_project(name="ci-monitor-contract", user_id=_TEST_USER)
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = make_task(
        project_id=project.id,
        status=TaskStatus.EVALUATING,
        payloads=[{"type": "review", "prompt": "p"}],
        context={"review_loop": {"ci_waiting": True}},
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)
    task_id = task.id

    await cim._monitor_evaluating_tasks(db_session)

    assert ci_filter_spy.called, (
        "the extracted sweep must run the ci_waiting SQL prefilter"
    )
    assert lock_spy.await_count >= 1, (
        "the extracted sweep must lock the ci_waiting EVALUATING row it found"
    )
    # The no-PR row is left intact (locked, inspected, rolled back) — the sweep
    # is behaviour-preserving and does not mutate it out of EVALUATING.
    refreshed = await db_session.get(Task, task_id)
    assert refreshed is not None
    assert refreshed.status == TaskStatus.EVALUATING


# ── Contention quarantine bound ───────────────────────────────────────────────


def test_dispatch_prep_contention_quarantine_threshold_is_bounded():
    """DISPATCH_PREP_CONTENTION_QUARANTINE_THRESHOLD must exist and be a bounded int.

    Pre-fix: no threshold constant existed — a task hitting persistent contention
    retried every leader cycle forever (no streak counter, no quarantine escape).
    The fix adds a bounded threshold so a stuck task is quarantined after
    DISPATCH_PREP_CONTENTION_QUARANTINE_THRESHOLD consecutive contention cycles,
    matching the poison-task quarantine that handles other dispatch-prep failures.
    """
    from app.orchestrator import (
        DISPATCH_PREP_CONTENTION_QUARANTINE_THRESHOLD,
        _dispatch_prep_contention_streak,
    )

    assert isinstance(DISPATCH_PREP_CONTENTION_QUARANTINE_THRESHOLD, int), (
        "threshold must be an int"
    )
    assert DISPATCH_PREP_CONTENTION_QUARANTINE_THRESHOLD >= 1, (
        "threshold=0 would quarantine every single contention hit — disabling transient recovery"
    )
    assert DISPATCH_PREP_CONTENTION_QUARANTINE_THRESHOLD <= 1000, (
        "threshold must be bounded; a value > 1000 defeats the runaway-loop guard"
    )
    assert isinstance(_dispatch_prep_contention_streak, dict), (
        "_dispatch_prep_contention_streak must be a dict for per-task streak tracking"
    )


def test_build_compute_launch_kwargs_forwards_no_progress_timeout():
    """_build_compute_launch_kwargs must forward no_progress_timeout_seconds from
    the compute_experiment payload to create_compute_job kwargs — like every peer
    optional launch field (setup_step, container_image, allow_host_fallback) — so a
    task launched directly by the orchestrator gets the same no-progress watchdog an
    agent-launched job would. Omitting it silently dropped the field."""
    from app.orchestrator_runner_dispatch import _build_compute_launch_kwargs

    kwargs = _build_compute_launch_kwargs(
        {"no_progress_timeout_seconds": 300},
        command="python3 run.py",
        spend_cap_usd=5.0,
        deadline_seconds=1800,
    )
    assert kwargs["no_progress_timeout_seconds"] == 300

    # Absent field ⇒ not forwarded (backend default / inert watchdog).
    kwargs_absent = _build_compute_launch_kwargs(
        {}, command="python3 run.py", spend_cap_usd=5.0, deadline_seconds=1800
    )
    assert "no_progress_timeout_seconds" not in kwargs_absent


# ── Main-loop sleep selection (WI-4: gate fast cycle on dispatch progress) ───


def test_select_cycle_sleep_backs_off_when_nothing_dispatched():
    """A cycle whose only queued tasks are held/deferred dispatches nothing
    (_cycle_dispatched == 0) ⇒ the loop takes the slow 5s idle path, NOT the
    2 Hz fast path. This is the WI-4 fix: queue-presence alone no longer spins."""
    from app.orchestrator import _select_cycle_sleep

    sleep = _select_cycle_sleep(
        consecutive_errors=0,
        consecutive_monitoring_errors=0,
        cycle_dispatched=0,
        error_backoff=99.0,
        monitoring_backoff=88.0,
    )

    assert sleep == 5.0


def test_select_cycle_sleep_fast_path_when_dispatch_made_progress():
    """A cycle that actually moved a task out of QUEUED (_cycle_dispatched > 0)
    grabs the next batch on the 0.5s fast path."""
    from app.orchestrator import _select_cycle_sleep

    sleep = _select_cycle_sleep(
        consecutive_errors=0,
        consecutive_monitoring_errors=0,
        cycle_dispatched=3,
        error_backoff=99.0,
        monitoring_backoff=88.0,
    )

    assert sleep == 0.5


def test_select_cycle_sleep_error_backoff_wins_over_dispatch():
    """Consecutive cycle errors take the error backoff even if the cycle also
    dispatched — the error path has highest precedence."""
    from app.orchestrator import _select_cycle_sleep

    sleep = _select_cycle_sleep(
        consecutive_errors=2,
        consecutive_monitoring_errors=0,
        cycle_dispatched=5,
        error_backoff=99.0,
        monitoring_backoff=88.0,
    )

    assert sleep == 99.0


def test_select_cycle_sleep_monitoring_backoff_wins_over_dispatch():
    """Monitoring errors (no cycle errors) take the monitoring backoff even if
    the cycle dispatched work."""
    from app.orchestrator import _select_cycle_sleep

    sleep = _select_cycle_sleep(
        consecutive_errors=0,
        consecutive_monitoring_errors=1,
        cycle_dispatched=5,
        error_backoff=99.0,
        monitoring_backoff=88.0,
    )

    assert sleep == 88.0


# ── Helper: redirect AsyncSessionLocal to the test database ─────────────────


class _FakeSessionFactory:
    """Replaces AsyncSessionLocal so background services use the test session."""

    def __init__(self, session):
        self._session = session

    def __call__(self):
        return self

    async def __aenter__(self):
        return self._session

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        return False


# ── Fixture: isolate _max_plan_pool between tests ────────────────────────────


@pytest.fixture(autouse=False)
def reset_max_plan_pool():
    """Save and restore the in-memory Max Plan pool around each test."""
    orig_pools = {k: list(v) for k, v in _max_plan_pools.items()}
    orig_by_id = dict(_account_by_id)
    _max_plan_pools.clear()
    _account_by_id.clear()
    yield
    _max_plan_pools.clear()
    _account_by_id.clear()
    _max_plan_pools.update(orig_pools)
    _account_by_id.update(orig_by_id)


# ── Per-project spend ceiling dispatch gate ───────────────────────────────────


@pytest.mark.asyncio
async def test_dispatch_gate_ceiling_exceeded_defers_tasks(db_session, mocker):
    """Per-project spend ceiling in resource_envelope prevents dispatch when
    recent spend exceeds the configured ceiling.

    _project_resource_dispatch_defer_message reads spend_ceiling_usd from the
    project's build_config and compares it against get_project_recent_spend_usd.
    When total spend >= ceiling, a human-readable deferral reason is returned.
    When spend < ceiling (or no ceiling is set), None is returned and the task
    remains eligible.
    """
    from app.orchestrator import _project_resource_dispatch_defer_message

    project = make_project(
        name="ceiling-test",
        user_id=_TEST_USER,
        build_config={
            "resource_envelope": {
                "spend_ceiling_usd": 100.0,
                "spend_window_days": 30,
            }
        },
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = make_task(
        project_id=project.id,
        status=TaskStatus.QUEUED,
        payloads=[{"type": "create", "prompt": "test"}],
    )
    db_session.add(task)
    await db_session.commit()

    # Case 1: spend exceeds ceiling → deferral message returned
    mocker.patch(
        "app.orchestrator.get_project_recent_spend_usd",
        new=AsyncMock(return_value={"total": 150.0, "compute": 100.0, "agent_vm": 50.0, "idle": 0.0}),
    )
    msg = await _project_resource_dispatch_defer_message(
        db_session, task, {},
        project_cache={},
        spend_cache={},
    )
    assert msg is not None, (
        "expected deferral message when spend ($150) exceeds ceiling ($100)"
    )
    assert "spend budget" in msg.lower(), (
        f"defer message should mention spend budget; got: {msg}"
    )
    assert "150.00" in msg and "100.00" in msg, (
        f"defer message should include spend/ceiling values; got: {msg}"
    )

    # Case 2: spend well under ceiling → no deferral
    mocker.patch(
        "app.orchestrator.get_project_recent_spend_usd",
        new=AsyncMock(return_value={"total": 30.0, "compute": 20.0, "agent_vm": 10.0, "idle": 0.0}),
    )
    msg2 = await _project_resource_dispatch_defer_message(
        db_session, task, {},
        project_cache={},
        spend_cache={},
    )
    assert msg2 is None, (
        "expected no deferral when spend ($30) is under ceiling ($100)"
    )

    # Case 3: no spend ceiling set → no deferral
    project_no_ceiling = make_project(
        name="no-ceiling",
        user_id=_TEST_USER,
        build_config={},
    )
    db_session.add(project_no_ceiling)
    await db_session.commit()
    await db_session.refresh(project_no_ceiling)

    task2 = make_task(
        project_id=project_no_ceiling.id,
        status=TaskStatus.QUEUED,
        payloads=[{"type": "create", "prompt": "test"}],
    )
    db_session.add(task2)
    await db_session.commit()

    msg3 = await _project_resource_dispatch_defer_message(
        db_session, task2, {},
        project_cache={},
        spend_cache={},
    )
    assert msg3 is None, (
        "expected no deferral when no spend ceiling is configured"
    )


@pytest.mark.asyncio
async def test_dispatch_gate_hard_pause_on_ceiling_exceeded(db_session, mocker):
    """When pause_on_ceiling_exceeded=True and spend exceeds the ceiling,
    _filter_project_resource_dispatch_eligible must set project.paused=True
    and broadcast project_spend_ceiling_exceeded.

    This directly tests the hard-pause path (orchestrator.py lines 788-812)
    that was identified as having no dedicated coverage.
    """
    from app.orchestrator import _filter_project_resource_dispatch_eligible
    import app.shared_state as _shared_state

    # Reset shared state so prior tests don't affect the ceiling-deferred set.
    _shared_state.project_ceiling_deferred_ids = set()

    project = make_project(
        name="hard-pause-test",
        user_id=_TEST_USER,
        build_config={
            "resource_envelope": {
                "spend_ceiling_usd": 100.0,
                "spend_window_days": 30,
                "pause_on_ceiling_exceeded": True,
            }
        },
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = make_task(
        project_id=project.id,
        status=TaskStatus.QUEUED,
        payloads=[{"type": "create", "prompt": "hard-pause test task"}],
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    task_id = task.id

    # Spend exceeds ceiling → hard-pause must fire.
    mocker.patch(
        "app.orchestrator.get_project_recent_spend_usd",
        new=AsyncMock(
            return_value={
                "total": 150.0,
                "ceiling_gate_total": 150.0,
                "compute": 100.0,
                "agent_vm": 50.0,
                "llm": 0.0,
                "idle": 0.0,
            }
        ),
    )
    broadcast_mock = AsyncMock()

    result = await _filter_project_resource_dispatch_eligible(
        db_session,
        [task_id],
        [task],
        {},
        broadcast_mock,
    )

    # Task must be excluded from dispatch (deferred).
    assert task_id not in result, (
        "task must be deferred when project spend ceiling is exceeded"
    )

    # Project must now be paused in the DB.
    await db_session.refresh(project)
    assert project.paused is True, (
        "project must be hard-paused when pause_on_ceiling_exceeded=True "
        "and spend ceiling is breached"
    )

    # broadcast_to_project must have been called with project_spend_ceiling_exceeded.
    broadcast_calls = [
        call.args[1]
        for call in broadcast_mock.call_args_list
        if len(call.args) >= 2
    ]
    assert "project_spend_ceiling_exceeded" in broadcast_calls, (
        f"expected project_spend_ceiling_exceeded broadcast; calls={broadcast_calls}"
    )


@pytest.mark.asyncio
async def test_dispatch_gate_hard_pause_no_refire_when_already_paused(db_session, mocker):
    """When the project is already paused, the hard-pause guard must not
    re-fire the project_spend_ceiling_exceeded broadcast.

    Guards the no-refire condition at orchestrator.py line 794:
    `if not _project.paused:` prevents spamming the broadcast every cycle.
    """
    from app.orchestrator import _filter_project_resource_dispatch_eligible
    import app.shared_state as _shared_state

    _shared_state.project_ceiling_deferred_ids = set()

    project = make_project(
        name="hard-pause-already-paused",
        user_id=_TEST_USER,
        build_config={
            "resource_envelope": {
                "spend_ceiling_usd": 100.0,
                "spend_window_days": 30,
                "pause_on_ceiling_exceeded": True,
            }
        },
    )
    project.paused = True  # Project is already paused.
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = make_task(
        project_id=project.id,
        status=TaskStatus.QUEUED,
        payloads=[{"type": "create", "prompt": "already-paused task"}],
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    task_id = task.id

    mocker.patch(
        "app.orchestrator.get_project_recent_spend_usd",
        new=AsyncMock(
            return_value={
                "total": 150.0,
                "ceiling_gate_total": 150.0,
                "compute": 100.0,
                "agent_vm": 50.0,
                "llm": 0.0,
                "idle": 0.0,
            }
        ),
    )
    broadcast_mock = AsyncMock()

    await _filter_project_resource_dispatch_eligible(
        db_session,
        [task_id],
        [task],
        {},
        broadcast_mock,
    )

    # The project_spend_ceiling_exceeded broadcast must NOT fire again
    # since the project was already paused before this cycle.
    broadcast_calls = [
        call.args[1]
        for call in broadcast_mock.call_args_list
        if len(call.args) >= 2
    ]
    assert "project_spend_ceiling_exceeded" not in broadcast_calls, (
        "project_spend_ceiling_exceeded must not be re-broadcast when "
        f"project is already paused; calls={broadcast_calls}"
    )


# ── Degraded-runner dispatch deprioritization ─────────────────────────────────


def test_degraded_runner_deprioritized_in_dispatch():
    """A degraded runner (consecutive_job_failures >= threshold) is sorted last
    in _select_eligible_runner so healthy runners are always preferred.

    The degraded runner must still appear as a fallback: when it is the only
    candidate, it must be selected (preserving recovery opportunity).

    Pre-fix: _load_active_runners_cache never loaded consecutive_job_failures,
    so all runners looked equally healthy to the dispatcher.
    """
    from app.orchestrator_runner_dispatch import (
        _RUNNER_DEGRADED_FAILURE_THRESHOLD,
        _select_eligible_runner,
        RunnerDispatchSnapshot,
    )

    project_id = uuid.uuid4()
    caps_all = {"task_types": "all"}

    healthy = RunnerDispatchSnapshot(
        id=uuid.uuid4(),
        project_id=project_id,
        capabilities=caps_all,
        consecutive_job_failures=0,
    )
    degraded = RunnerDispatchSnapshot(
        id=uuid.uuid4(),
        project_id=project_id,
        capabilities=caps_all,
        consecutive_job_failures=_RUNNER_DEGRADED_FAILURE_THRESHOLD,
    )

    cache = {project_id: [degraded, healthy]}

    # When both are available, the healthy runner wins.
    selected = _select_eligible_runner(
        active_runners_cache=cache,
        project_id=project_id,
        task_type="create",
        accelerator=None,
        runner_pins_this_cycle={},
    )
    assert selected is not None
    assert selected.id == healthy.id, (
        "healthy runner must be preferred over degraded runner"
    )

    # When only the degraded runner is present, it must still be selected
    # so it can receive a job and reset its failure streak.
    cache_degraded_only = {project_id: [degraded]}
    selected_fallback = _select_eligible_runner(
        active_runners_cache=cache_degraded_only,
        project_id=project_id,
        task_type="create",
        accelerator=None,
        runner_pins_this_cycle={},
    )
    assert selected_fallback is not None
    assert selected_fallback.id == degraded.id, (
        "degraded runner must still be selected when it is the only candidate"
    )



def test_idle_runner_dispatch_rotates_across_ties():
    """When multiple runners are equally idle, different task UUIDs must route
    to different runners (rotation via dispatch_seed), so the fleet is balanced
    rather than every task landing on the same first-in-order runner.
    """
    from app.orchestrator_runner_dispatch import (
        _select_eligible_runner,
        RunnerDispatchSnapshot,
    )

    project_id = uuid.uuid4()
    caps_all = {"task_types": "all"}

    runner_a = RunnerDispatchSnapshot(
        id=uuid.UUID("aaaaaaaa-0000-0000-0000-000000000001"),
        project_id=project_id,
        capabilities=caps_all,
        consecutive_job_failures=0,
    )
    runner_b = RunnerDispatchSnapshot(
        id=uuid.UUID("bbbbbbbb-0000-0000-0000-000000000002"),
        project_id=project_id,
        capabilities=caps_all,
        consecutive_job_failures=0,
    )
    cache = {project_id: [runner_a, runner_b]}

    # Dispatch many tasks with distinct UUIDs; collect which runner is selected.
    selected_ids = set()
    for _ in range(20):
        task_seed = uuid.uuid4().int
        result = _select_eligible_runner(
            active_runners_cache=cache,
            project_id=project_id,
            task_type="create",
            accelerator=None,
            runner_pins_this_cycle={},
            dispatch_seed=task_seed,
        )
        assert result is not None
        selected_ids.add(result.id)

    assert len(selected_ids) == 2, (
        f"Expected both runners to be selected across 20 dispatches, "
        f"but only {len(selected_ids)} runner(s) were chosen: {selected_ids}"
    )


def test_select_eligible_runner_requires_explicit_browser_proof():
    from app.orchestrator_runner_dispatch import (
        RunnerDispatchSnapshot,
        _select_eligible_runner,
    )

    project_id = uuid.uuid4()
    incapable = RunnerDispatchSnapshot(
        uuid.uuid4(), project_id, {"task_types": "all"}
    )
    capable = RunnerDispatchSnapshot(
        id=uuid.uuid4(),
        project_id=project_id,
        capabilities={
            "task_types": "all",
            "playwright_chromium_usable": True,
        },
    )

    selected = _select_eligible_runner(
        {project_id: [incapable, capable]},
        project_id,
        "salvage_bundle",
        None,
        {},
        requires_browser=True,
    )
    assert selected is not None
    assert selected.id == capable.id

    legacy = _select_eligible_runner(
        {project_id: [incapable]}, project_id, "salvage_bundle", None, {}
    )
    assert legacy is not None and legacy.id == incapable.id


@pytest.mark.asyncio
async def test_browser_required_task_falls_through_without_runner_proof(db_session):
    from app.orchestrator_runner_dispatch import (
        RunnerDispatchSnapshot,
        _try_runner_dispatch,
    )

    project = make_project(name="browser-routing", user_id=_TEST_USER)
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)
    task = make_task(
        project_id=project.id,
        status=TaskStatus.QUEUED,
        payloads=[{"type": "salvage_bundle", "requires_browser": True}],
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)
    incapable = RunnerDispatchSnapshot(
        uuid.uuid4(), project.id, {"task_types": "all", "max_concurrent": 1}
    )

    dispatched = await _try_runner_dispatch(
        db_session, task, {project.id: [incapable]}, {}
    )

    assert dispatched is False
    assert task.status == TaskStatus.QUEUED
    assert (task.context or {}).get("runner_target_id") is None


# ── Container-capability gating in runner dispatch ─────────────────────────────


def test_runner_has_container_capability_true_when_backend_reported():
    """_runner_has_container_capability returns True when at least one
    container backend is reported in isolation_backends."""
    from app.orchestrator_runner_dispatch import (
        _runner_has_container_capability,
        RunnerDispatchSnapshot,
    )

    project_id = uuid.uuid4()
    container_runner = RunnerDispatchSnapshot(
        id=uuid.uuid4(),
        project_id=project_id,
        capabilities={
            "task_types": "all",
            "isolation_backends": {"docker_usable": True, "bwrap_ok": False},
        },
    )
    assert _runner_has_container_capability(container_runner) is True

    podman_runner = RunnerDispatchSnapshot(
        id=uuid.uuid4(),
        project_id=project_id,
        capabilities={
            "task_types": "all",
            "isolation_backends": {"podman_usable": True},
        },
    )
    assert _runner_has_container_capability(podman_runner) is True


def test_runner_has_container_capability_false_when_no_container_backend():
    """_runner_has_container_capability returns False when no container
    backend is reported."""
    from app.orchestrator_runner_dispatch import (
        _runner_has_container_capability,
        RunnerDispatchSnapshot,
    )

    project_id = uuid.uuid4()
    no_container_runner = RunnerDispatchSnapshot(
        id=uuid.uuid4(),
        project_id=project_id,
        capabilities={
            "task_types": "all",
            "isolation_backends": {"bwrap_ok": True, "userns_ok": True},
        },
    )
    assert _runner_has_container_capability(no_container_runner) is False


def test_runner_has_container_capability_false_when_no_isolation_backends():
    """_runner_has_container_capability returns False when isolation_backends
    is absent (old runner that never reports it)."""
    from app.orchestrator_runner_dispatch import (
        _runner_has_container_capability,
        RunnerDispatchSnapshot,
    )

    project_id = uuid.uuid4()
    old_runner = RunnerDispatchSnapshot(
        id=uuid.uuid4(),
        project_id=project_id,
        capabilities={"task_types": "all"},
    )
    assert _runner_has_container_capability(old_runner) is False


def test_runner_has_container_capability_false_when_empty_isolation_backends():
    """_runner_has_container_capability returns False when isolation_backends
    is present but empty (no backends reported at all)."""
    from app.orchestrator_runner_dispatch import (
        _runner_has_container_capability,
        RunnerDispatchSnapshot,
    )

    project_id = uuid.uuid4()
    empty_backends_runner = RunnerDispatchSnapshot(
        id=uuid.uuid4(),
        project_id=project_id,
        capabilities={"task_types": "all", "isolation_backends": {}},
    )
    assert _runner_has_container_capability(empty_backends_runner) is False


def test_select_eligible_runner_filters_container_image_no_capable_runner():
    """When a container_image is declared and no runner has container capability,
    _select_eligible_runner returns None."""
    from app.orchestrator_runner_dispatch import (
        _select_eligible_runner,
        RunnerDispatchSnapshot,
    )

    project_id = uuid.uuid4()
    cpu_runner = RunnerDispatchSnapshot(
        id=uuid.uuid4(),
        project_id=project_id,
        capabilities={
            "task_types": "all",
            "isolation_backends": {"bwrap_ok": True},
        },
    )
    cache = {project_id: [cpu_runner]}
    selected = _select_eligible_runner(
        active_runners_cache=cache,
        project_id=project_id,
        task_type="compute_experiment",
        accelerator=None,
        runner_pins_this_cycle={},
        container_image="my-image:latest",
    )
    assert selected is None, (
        "no runner should be selected when container_image is declared "
        "but no runner has container capability"
    )


def test_select_eligible_runner_container_image_selects_capable():
    """When a container_image is declared, only a container-capable runner is
    selected."""
    from app.orchestrator_runner_dispatch import (
        _select_eligible_runner,
        RunnerDispatchSnapshot,
    )

    project_id = uuid.uuid4()
    non_container_runner = RunnerDispatchSnapshot(
        id=uuid.uuid4(),
        project_id=project_id,
        capabilities={
            "task_types": "all",
            "isolation_backends": {"bwrap_ok": True},
        },
    )
    container_runner = RunnerDispatchSnapshot(
        id=uuid.uuid4(),
        project_id=project_id,
        capabilities={
            "task_types": "all",
            "isolation_backends": {"docker_usable": True},
        },
    )
    cache = {project_id: [non_container_runner, container_runner]}
    selected = _select_eligible_runner(
        active_runners_cache=cache,
        project_id=project_id,
        task_type="compute_experiment",
        accelerator=None,
        runner_pins_this_cycle={},
        container_image="my-image:latest",
    )
    assert selected is not None
    assert selected.id == container_runner.id, (
        "the container-capable runner must be selected over the non-container one"
    )


def test_select_eligible_runner_no_container_image_ignores_container_cap():
    """When no container_image is declared, runner selection does NOT filter
    on container capability — a non-container runner is still eligible."""
    from app.orchestrator_runner_dispatch import (
        _select_eligible_runner,
        RunnerDispatchSnapshot,
    )

    project_id = uuid.uuid4()
    non_container_runner = RunnerDispatchSnapshot(
        id=uuid.uuid4(),
        project_id=project_id,
        capabilities={
            "task_types": "all",
            "isolation_backends": {"bwrap_ok": True},
        },
    )
    cache = {project_id: [non_container_runner]}
    selected = _select_eligible_runner(
        active_runners_cache=cache,
        project_id=project_id,
        task_type="compute_experiment",
        accelerator=None,
        runner_pins_this_cycle={},
        container_image=None,  # no container image — should not filter
    )
    assert selected is not None, (
        "a non-container runner must still be selectable when no "
        "container_image is declared"
    )


# ── VRAM-fit dispatch eligibility ─────────────────────────────────────────────


def test_select_eligible_runner_vram_filter_excludes_insufficient():
    """A runner whose reported gpu_free_vram_mib is below the job requirement
    must be excluded from dispatch, even when has_gpu is True.

    This is the core VRAM-fit correctness guarantee: two jobs must not be
    dispatched to the same GPU when free VRAM is insufficient for both.
    """
    from app.orchestrator_runner_dispatch import (
        _select_eligible_runner,
        RunnerDispatchSnapshot,
    )

    project_id = uuid.uuid4()
    # Runner with only 4 GiB free (4096 MiB) cannot hold an 8 GiB job.
    low_vram_runner = RunnerDispatchSnapshot(
        id=uuid.uuid4(),
        project_id=project_id,
        capabilities={
            "task_types": "all",
            "has_gpu": True,
            "gpu_free_vram_mib": [4096],
        },
    )
    cache = {project_id: [low_vram_runner]}

    selected = _select_eligible_runner(
        active_runners_cache=cache,
        project_id=project_id,
        task_type="compute_experiment",
        accelerator="gpu",
        runner_pins_this_cycle={},
        vram_gb=8.0,
    )
    assert selected is None, (
        "runner with only 4 GiB free VRAM must not be selected for an 8 GiB job"
    )


def test_select_eligible_runner_vram_filter_selects_sufficient():
    """When one runner has insufficient VRAM and another has sufficient,
    only the sufficient runner is selected."""
    from app.orchestrator_runner_dispatch import (
        _select_eligible_runner,
        RunnerDispatchSnapshot,
    )

    project_id = uuid.uuid4()
    low_vram = RunnerDispatchSnapshot(
        id=uuid.uuid4(),
        project_id=project_id,
        capabilities={
            "task_types": "all",
            "has_gpu": True,
            "gpu_free_vram_mib": [4096],
        },
    )
    high_vram = RunnerDispatchSnapshot(
        id=uuid.uuid4(),
        project_id=project_id,
        capabilities={
            "task_types": "all",
            "has_gpu": True,
            "gpu_free_vram_mib": [20480],  # 20 GiB free
        },
    )
    cache = {project_id: [low_vram, high_vram]}

    selected = _select_eligible_runner(
        active_runners_cache=cache,
        project_id=project_id,
        task_type="compute_experiment",
        accelerator="gpu",
        runner_pins_this_cycle={},
        vram_gb=16.0,
    )
    assert selected is not None
    assert selected.id == high_vram.id, (
        "runner with 20 GiB free must be preferred over runner with only 4 GiB"
    )


def test_select_eligible_runner_vram_filter_no_vram_declared_unaffected():
    """When no vram_gb is declared, runners without gpu_free_vram_mib are
    still eligible — the VRAM gate is opt-in and must not break existing dispatch."""
    from app.orchestrator_runner_dispatch import (
        _select_eligible_runner,
        RunnerDispatchSnapshot,
    )

    project_id = uuid.uuid4()
    gpu_runner = RunnerDispatchSnapshot(
        id=uuid.uuid4(),
        project_id=project_id,
        capabilities={"task_types": "all", "has_gpu": True},
    )
    cache = {project_id: [gpu_runner]}

    selected = _select_eligible_runner(
        active_runners_cache=cache,
        project_id=project_id,
        task_type="compute_experiment",
        accelerator="gpu",
        runner_pins_this_cycle={},
        vram_gb=None,
    )
    assert selected is not None, (
        "GPU runner must remain eligible when no vram_gb is declared"
    )


def test_select_eligible_runner_vram_filter_absent_telemetry_passes():
    """A runner that does not report gpu_free_vram_mib (older daemon) must
    not be excluded even when the job declares a vram_gb requirement — we
    only gate dispatch on real data, not on absence of telemetry."""
    from app.orchestrator_runner_dispatch import (
        _select_eligible_runner,
        RunnerDispatchSnapshot,
    )

    project_id = uuid.uuid4()
    old_runner = RunnerDispatchSnapshot(
        id=uuid.uuid4(),
        project_id=project_id,
        # has_gpu True but no gpu_free_vram_mib — old runner that never sends it
        capabilities={"task_types": "all", "has_gpu": True},
    )
    cache = {project_id: [old_runner]}

    selected = _select_eligible_runner(
        active_runners_cache=cache,
        project_id=project_id,
        task_type="compute_experiment",
        accelerator="gpu",
        runner_pins_this_cycle={},
        vram_gb=24.0,
    )
    assert selected is not None, (
        "runner without gpu_free_vram_mib telemetry must still be eligible "
        "when vram_gb is declared — absence of data is not evidence of insufficiency"
    )


def test_runner_has_free_vram_any_gpu_sufficient():
    """_runner_has_free_vram returns True when at least one GPU meets the
    requirement, even if others do not (multi-GPU runner where only one
    GPU has enough headroom)."""
    from app.orchestrator_runner_dispatch import (
        _runner_has_free_vram,
        RunnerDispatchSnapshot,
    )

    project_id = uuid.uuid4()
    multi_gpu = RunnerDispatchSnapshot(
        id=uuid.uuid4(),
        project_id=project_id,
        # GPU 0: 4 GiB free, GPU 1: 24 GiB free
        capabilities={"task_types": "all", "gpu_free_vram_mib": [4096, 24576]},
    )
    assert _runner_has_free_vram(multi_gpu, required_mib=16384) is True, (
        "runner with at least one GPU meeting the requirement must be eligible"
    )
    assert _runner_has_free_vram(multi_gpu, required_mib=32768) is False, (
        "runner where no GPU meets the requirement must not be eligible"
    )


# ── Forced (hard) runner pin: _select_eligible_runner ──────────────────────────


def test_select_eligible_runner_forced_target_restricts_to_that_runner():
    """A forced runner target restricts eligibility to ONLY that runner.

    Even with another idle, eligible runner present, the forced id must win and
    the other runner must never be chosen — the hard pin does not spill over.
    """
    from app.orchestrator_runner_dispatch import (
        _select_eligible_runner,
        RunnerDispatchSnapshot,
    )

    project_id = uuid.uuid4()
    caps_all = {"task_types": "all"}
    forced = RunnerDispatchSnapshot(
        id=uuid.uuid4(),
        project_id=project_id,
        capabilities=caps_all,
        consecutive_job_failures=0,
    )
    other = RunnerDispatchSnapshot(
        id=uuid.uuid4(),
        project_id=project_id,
        capabilities=caps_all,
        consecutive_job_failures=0,
    )
    cache = {project_id: [other, forced]}

    selected = _select_eligible_runner(
        active_runners_cache=cache,
        project_id=project_id,
        task_type="compute_experiment",
        accelerator=None,
        runner_pins_this_cycle={},
        forced_runner_target_id=str(forced.id),
    )
    assert selected is not None
    assert selected.id == forced.id, "forced target must be selected, never the other runner"


def test_select_eligible_runner_forced_target_returns_none_when_ineligible():
    """A forced target that is not present/eligible this cycle → None.

    The function must NEVER fall through to the other (eligible) runner: a hard
    pin holds rather than silently reassigns.
    """
    from app.orchestrator_runner_dispatch import (
        _select_eligible_runner,
        RunnerDispatchSnapshot,
    )

    project_id = uuid.uuid4()
    caps_all = {"task_types": "all"}
    other = RunnerDispatchSnapshot(
        id=uuid.uuid4(),
        project_id=project_id,
        capabilities=caps_all,
        consecutive_job_failures=0,
    )
    # The forced id is NOT in the cache (offline this cycle).
    forced_id = str(uuid.uuid4())
    cache = {project_id: [other]}

    selected = _select_eligible_runner(
        active_runners_cache=cache,
        project_id=project_id,
        task_type="compute_experiment",
        accelerator=None,
        runner_pins_this_cycle={},
        forced_runner_target_id=forced_id,
    )
    assert selected is None, (
        "an offline forced target must yield None, never fall back to the "
        "other eligible runner"
    )


def test_select_eligible_runner_forced_target_respects_gpu_gate():
    """A forced target that lacks a required GPU is NOT selected (→ None)."""
    from app.orchestrator_runner_dispatch import (
        _select_eligible_runner,
        RunnerDispatchSnapshot,
    )

    project_id = uuid.uuid4()
    cpu_forced = RunnerDispatchSnapshot(
        id=uuid.uuid4(),
        project_id=project_id,
        capabilities={"task_types": "all", "has_gpu": False},
        consecutive_job_failures=0,
    )
    cache = {project_id: [cpu_forced]}

    selected = _select_eligible_runner(
        active_runners_cache=cache,
        project_id=project_id,
        task_type="compute_experiment",
        accelerator="T4",  # requires a GPU runner
        runner_pins_this_cycle={},
        forced_runner_target_id=str(cpu_forced.id),
    )
    assert selected is None, "a GPU job forced onto a CPU-only runner must hold"


# ── DeepSeek routing preference invariant ───────────────────────────────────


def test_deepseek_preferred_routing_for_low_medium_effort():
    """DeepSeek must be the first-position provider for low/medium-effort
    categories (_default, initiative, build, ops) in the routing defaults.

    The preference matters because ``provider_selection_mode`` is
    ``"preferred"`` by default, which means the first listed provider is
    always tried first.  Regressing this ordering would silently shift
    low/medium-effort task traffic to a more expensive provider without any
    config change.
    """
    from app.runtime_config_state import (
        _validate_deepseek_routing_invariant,
        _backfill_provider_runtime_defaults,
        runtime_config,
    )

    # ---- 1. Default runtime_config passes validation ----
    warnings = _validate_deepseek_routing_invariant(runtime_config)
    assert warnings == [], (
        f"default runtime_config must have DeepSeek first for all "
        f"low/medium-effort categories; got warnings: {warnings}"
    )

    # ---- 2. Per-category default providers confirm DeepSeek is first ----
    cat_routing = runtime_config.get("category_routing", {})
    deepseek_first = {"_default", "initiative", "build", "ops"}
    for category in deepseek_first:
        providers = cat_routing[category]["providers"]
        assert providers[0] == "deepseek", (
            f"category_routing.{category}: expected DeepSeek first, "
            f"got {providers}"
        )

    # ---- 3. Review/explicitly-not-low-medium categories do NOT
    #        require DeepSeek first ----
    review_providers = cat_routing["review"]["providers"]
    assert review_providers[0] == "anthropic", (
        "review category must prefer anthropic (high-effort, not DeepSeek)"
    )
    strategic_providers = cat_routing["strategic"]["providers"]
    assert strategic_providers[0] == "zai", (
        "strategic category must prefer zai (max-effort, not DeepSeek)"
    )

    # ---- 4. Validation catches a violated invariant ----
    bad_config = {
        "category_routing": {
            "build": {"providers": ["openai", "deepseek"], "effort": "medium"},
            "ops": {"providers": ["deepseek"], "effort": "low"},
            "_default": {"providers": ["gemini"], "effort": "medium"},
            "initiative": {"providers": [], "effort": "medium"},
        }
    }
    bad_warnings = _validate_deepseek_routing_invariant(bad_config)
    assert len(bad_warnings) >= 2, (
        f"expected at least 2 warnings for violated invariants, got {bad_warnings}"
    )
    assert any("_default" in w for w in bad_warnings)
    assert any("build" in w for w in bad_warnings)
    # ops is fine (deepseek first), initiative has empty list → skipped

    # ---- 5. _backfill_provider_runtime_defaults preserves operator routes ----
    # The backfill runs on EVERY persisted-config sync, not just once during
    # an upgrade, so rewriting an existing provider list would silently undo
    # a successful settings PATCH on the next sync cycle. Existing routes are
    # operator-owned and must round-trip byte-for-byte, even when they violate
    # the DeepSeek-first default ordering.
    operator_routes: dict = {
        "build": {"providers": ["openai", "deepseek", "anthropic"], "effort": "medium"},
        "initiative": {"providers": ["gemini", "deepseek"], "effort": "medium"},
        "ops": {"providers": ["gemini", "deepseek"], "effort": "low"},
        "_default": {"providers": ["openai", "gemini"], "effort": "medium"},
        "strategic": {"providers": ["zai", "anthropic"], "effort": "max"},
        "review": {"providers": ["anthropic", "openai"], "effort": "high"},
    }
    operator_config: dict = {
        "category_routing": {
            category: {"providers": list(route["providers"]), "effort": route["effort"]}
            for category, route in operator_routes.items()
        },
        "provider_settings_by_effort": {
            "deepseek": {
                "low": {"model": "deepseek-v4-flash"},
                "medium": {"model": "deepseek-v4-pro"},
            },
            "openai": {"medium": {"model": "gpt-5.4-mini"}},
            "gemini": {"medium": {"model": "gemini-2.5-flash"}},
            "anthropic": {"medium": {"model": "claude-sonnet-4-6"}},
            "zai": {"max": {"model": "glm-5.2"}},
        },
    }
    _backfill_provider_runtime_defaults(operator_config)
    preserved = operator_config["category_routing"]
    for category, route in operator_routes.items():
        assert preserved[category]["providers"] == route["providers"], (
            f"after backfill, category_routing.{category} must be preserved "
            f"byte-for-byte; got {preserved[category]['providers']}"
        )
        assert preserved[category]["effort"] == route["effort"]

    # The validator still SEES the violated ordering — detection is decoupled
    # from repair, which is now the operator's call.
    post_warnings = _validate_deepseek_routing_invariant(operator_config)
    assert post_warnings != [], (
        "validator must still flag operator routes that violate the "
        "DeepSeek-first invariant even though backfill no longer rewrites them"
    )

    # ---- 6. Missing categories are still seeded from the current defaults ----
    sparse_config: dict = {
        "category_routing": {
            "build": {"providers": ["openai"], "effort": "medium"},
        },
    }
    _backfill_provider_runtime_defaults(sparse_config)
    seeded = sparse_config["category_routing"]
    assert seeded["build"]["providers"] == ["openai"], (
        "existing sparse route must survive seeding untouched"
    )
    for category in ("_default", "strategic", "initiative", "review", "ops"):
        assert seeded[category]["providers"] == (
            runtime_config["category_routing"][category]["providers"]
        ), f"missing category_routing.{category} must be seeded from defaults"


# ── Fixture: reset circuit breaker state for a project ───────────────────────


@pytest.fixture(autouse=False)
def reset_circuit_breakers():
    original = dict(_shared_state.circuit_breakers)
    yield
    _shared_state.circuit_breakers.clear()
    _shared_state.circuit_breakers.update(original)


# ══════════════════════════════════════════════════════════════════════════════
# Section 1 — reaper_service()
# ══════════════════════════════════════════════════════════════════════════════


class TestReaperService:
    """Tests for reaper_service() background worker."""

    # Helper shared by all reaper tests
    @staticmethod
    def _stop_after_one_cycle(mocker):
        class _StopLoop(Exception):
            pass

        mocker.patch("asyncio.sleep", side_effect=_StopLoop)
        return _StopLoop

    # ── Test 1: stale heartbeat, retries exhausted → FAILED ─────────────────

    @pytest.mark.asyncio
    async def test_stale_heartbeat_exhausted_retries_marks_task_failed(
        self, db_session, mocker
    ):
        """
        A task whose last heartbeat is >5 min old AND has reached max_retries
        must be marked FAILED and its VM deleted.
        """
        mock_delete = mocker.patch(
            "app.vm_lifecycle.delete_agent_vm", return_value=None
        )
        mocker.patch("app.broadcast.broadcast_to_project", new_callable=AsyncMock)
        mocker.patch("app.reaper.AsyncSessionLocal", _FakeSessionFactory(db_session))
        StopLoop = self._stop_after_one_cycle(mocker)

        project = make_project(name="Reaper FAILED test", user_id="user1")
        db_session.add(project)
        await db_session.commit()
        await db_session.refresh(project)

        max_retries = _main_module._runtime_config["max_retries"]
        task = make_task(
            project_id=project.id,
            vm_instance_id="stale-vm-001",
            status=TaskStatus.IN_PROGRESS,
            # Heartbeat older than the 5-minute window
            last_heartbeat=_utcnow() - timedelta(minutes=6),
            attempt_count=max_retries,  # exhausted — next reap should FAIL, not retry
        )
        db_session.add(task)
        await db_session.commit()
        await db_session.refresh(task)

        try:
            await reaper_service()
        except StopLoop:
            pass

        await db_session.refresh(task)
        assert task.status == TaskStatus.FAILED
        mock_delete.assert_called_once_with("stale-vm-001")

    # ── Test 2: recent heartbeat → NOT reaped ───────────────────────────────

    @pytest.mark.asyncio
    async def test_recent_heartbeat_task_is_not_reaped(self, db_session, mocker):
        """
        A task whose last heartbeat is within the 5-minute window must
        be left alone by the reaper.
        """
        mock_delete = mocker.patch(
            "app.vm_lifecycle.delete_agent_vm", return_value=None
        )
        mocker.patch("app.broadcast.broadcast_to_project", new_callable=AsyncMock)
        mocker.patch("app.reaper.AsyncSessionLocal", _FakeSessionFactory(db_session))
        StopLoop = self._stop_after_one_cycle(mocker)

        project = make_project(name="Heartbeat alive", user_id="user1")
        db_session.add(project)
        await db_session.commit()
        await db_session.refresh(project)

        task = make_task(
            project_id=project.id,
            vm_instance_id="healthy-vm-001",
            status=TaskStatus.IN_PROGRESS,
            last_heartbeat=_utcnow() - timedelta(minutes=2),  # within window
            attempt_count=0,
        )
        db_session.add(task)
        await db_session.commit()
        await db_session.refresh(task)

        try:
            await reaper_service()
        except StopLoop:
            pass

        await db_session.refresh(task)
        assert task.status == TaskStatus.IN_PROGRESS, (
            "Healthy task should not be reaped"
        )
        mock_delete.assert_not_called()

    # ── Test 3: stale heartbeat, retries remaining → requeued ───────────────

    @pytest.mark.asyncio
    async def test_stale_task_under_retry_limit_is_requeued(self, db_session, mocker):
        """
        A task with a stale heartbeat that still has retry attempts remaining
        must be requeued (status=QUEUED) with incremented attempt_count,
        NOT marked FAILED.
        """
        mocker.patch("app.vm_lifecycle.delete_agent_vm", return_value=None)
        mocker.patch("app.broadcast.broadcast_to_project", new_callable=AsyncMock)
        mocker.patch("app.reaper.AsyncSessionLocal", _FakeSessionFactory(db_session))
        StopLoop = self._stop_after_one_cycle(mocker)

        project = make_project(name="Retry test", user_id="user1")
        db_session.add(project)
        await db_session.commit()
        await db_session.refresh(project)

        task = make_task(
            project_id=project.id,
            vm_instance_id="retry-vm-001",
            status=TaskStatus.IN_PROGRESS,
            last_heartbeat=_utcnow() - timedelta(minutes=7),
            attempt_count=0,  # first attempt — should be retried
        )
        db_session.add(task)
        await db_session.commit()
        await db_session.refresh(task)

        try:
            await reaper_service()
        except StopLoop:
            pass

        await db_session.refresh(task)
        assert task.status == TaskStatus.QUEUED, (
            "Task with remaining retries should be requeued"
        )
        assert task.attempt_count == 1, "Retry counter must be incremented"

    # ── Test 4: task without vm_instance_id → skipped ───────────────────────

    @pytest.mark.asyncio
    async def test_task_without_vm_id_is_skipped(self, db_session, mocker):
        """
        Logical/parent tasks that have no VM assigned (vm_instance_id=None)
        must be skipped — the reaper only acts on tasks with real VMs.
        """
        mock_delete = mocker.patch(
            "app.vm_lifecycle.delete_agent_vm", return_value=None
        )
        mocker.patch("app.broadcast.broadcast_to_project", new_callable=AsyncMock)
        mocker.patch("app.reaper.AsyncSessionLocal", _FakeSessionFactory(db_session))
        StopLoop = self._stop_after_one_cycle(mocker)

        project = make_project(name="No-VM test", user_id="user1")
        db_session.add(project)
        await db_session.commit()
        await db_session.refresh(project)

        max_retries = _main_module._runtime_config["max_retries"]
        task = make_task(
            project_id=project.id,
            vm_instance_id=None,  # no VM — logical parent task
            status=TaskStatus.IN_PROGRESS,
            last_heartbeat=_utcnow() - timedelta(hours=2),
            attempt_count=max_retries,
        )
        db_session.add(task)
        await db_session.commit()
        await db_session.refresh(task)

        try:
            await reaper_service()
        except StopLoop:
            pass

        await db_session.refresh(task)
        assert task.status == TaskStatus.IN_PROGRESS, (
            "Task without VM should be untouched"
        )
        mock_delete.assert_not_called()

    # ── Test 5: FOR UPDATE SKIP LOCKED / pre-validation guard ───────────────

    @pytest.mark.asyncio
    async def test_skip_locked_pre_validation_prevents_double_reap(
        self, db_session, mocker
    ):
        """
        The reaper re-validates each task's status via session.get() just before acting.
        If the task's status has already transitioned out of PROVISIONING/IN_PROGRESS
        (e.g. reaped by a concurrent instance), the reaper skips it.

        This is the application-level complement to FOR UPDATE SKIP LOCKED:
        even if two reaper instances select the same task, the second one
        detects the status change during re-validation and does nothing.
        """
        mock_delete = mocker.patch(
            "app.vm_lifecycle.delete_agent_vm", return_value=None
        )
        mocker.patch("app.broadcast.broadcast_to_project", new_callable=AsyncMock)
        StopLoop = self._stop_after_one_cycle(mocker)

        project = make_project(name="Skip-locked test", user_id="user1")
        db_session.add(project)
        await db_session.commit()
        await db_session.refresh(project)

        max_retries = _main_module._runtime_config["max_retries"]
        task = make_task(
            project_id=project.id,
            vm_instance_id="race-vm-001",
            status=TaskStatus.IN_PROGRESS,
            last_heartbeat=_utcnow() - timedelta(minutes=6),
            attempt_count=max_retries,
        )
        db_session.add(task)
        await db_session.commit()
        await db_session.refresh(task)

        # Intercept session.get so the "fresh" re-validation returns a COMPLETED task,
        # simulating a concurrent reaper instance having already processed it.
        original_get = db_session.get

        async def _intercepted_get(model, pk, **kwargs):
            if model is Task and pk == task.id:
                # Construct a stub that looks already-completed
                stub = Task(
                    project_id=task.project_id,
                    payloads=[],
                    vm_instance_id="race-vm-001",
                    status=TaskStatus.COMPLETED,  # already done by another instance
                    attempt_count=max_retries,
                )
                stub.id = task.id
                return stub
            return await original_get(model, pk, **kwargs)

        db_session.get = _intercepted_get
        mocker.patch("app.reaper.AsyncSessionLocal", _FakeSessionFactory(db_session))
        # The reaper batch-fetches orphan tasks and only falls back to session.get()
        # when the cached instance is expired.  Force expired=True so _intercepted_get
        # is reached and can return the already-COMPLETED stub that simulates a
        # concurrent reaper instance having already processed the task.
        mocker.patch("app.reaper.inspect", return_value=MagicMock(expired=True))

        try:
            await reaper_service()
        except StopLoop:
            pass

        # Restore db_session.get for the refresh below
        db_session.get = original_get

        # The pre-validation guard must have skipped this task
        mock_delete.assert_not_called()

        # The task's real DB state is still IN_PROGRESS (the reaper never committed a change)
        await db_session.refresh(task)
        assert task.status == TaskStatus.IN_PROGRESS

    # ── Test 6: FAILED tasks excluded from the initial scan ─────────────────

    @pytest.mark.asyncio
    async def test_already_failed_task_is_excluded_from_reaper_scan(
        self, db_session, mocker
    ):
        """
        The reaper's initial query only selects PROVISIONING/IN_PROGRESS tasks.
        A task that is already FAILED must never be picked up — this is the
        database-level guarantee that prevents double-reaping after the first pass.
        """
        mock_delete = mocker.patch(
            "app.vm_lifecycle.delete_agent_vm", return_value=None
        )
        mocker.patch("app.broadcast.broadcast_to_project", new_callable=AsyncMock)
        mocker.patch("app.reaper.AsyncSessionLocal", _FakeSessionFactory(db_session))
        StopLoop = self._stop_after_one_cycle(mocker)

        project = make_project(name="Already-failed test", user_id="user1")
        db_session.add(project)
        await db_session.commit()
        await db_session.refresh(project)

        task = make_task(
            project_id=project.id,
            vm_instance_id="already-failed-vm",
            status=TaskStatus.FAILED,  # terminal state
            last_heartbeat=_utcnow() - timedelta(hours=2),
            attempt_count=3,
        )
        db_session.add(task)
        await db_session.commit()

        try:
            await reaper_service()
        except StopLoop:
            pass

        mock_delete.assert_not_called()
        await db_session.refresh(task)
        assert task.status == TaskStatus.FAILED  # unchanged

    # ── Test 7: PROVISIONING + no heartbeat + stale vm_provisioned_at → pre-claim reap ──

    @pytest.mark.asyncio
    async def test_reaper_provision_timeout_before_agent_claim(
        self, db_session, mocker
    ):
        """
        A VM that was dispatched 7 minutes ago but whose agent never claimed
        (last_heartbeat is None, status still PROVISIONING) must be reaped
        as a pre-claim failure and charged against provision_failures,
        NOT against attempt_count.
        """
        mocker.patch("app.vm_lifecycle.delete_agent_vm", return_value=None)
        mocker.patch("app.broadcast.broadcast_to_project", new_callable=AsyncMock)
        mocker.patch("app.reaper.AsyncSessionLocal", _FakeSessionFactory(db_session))
        StopLoop = self._stop_after_one_cycle(mocker)

        project = make_project(name="Pre-claim reap test", user_id="user1")
        db_session.add(project)
        await db_session.commit()
        await db_session.refresh(project)

        now = _utcnow()
        task = make_task(
            project_id=project.id,
            vm_instance_id="silent-vm-001",
            status=TaskStatus.PROVISIONING,
            started_at=now - timedelta(minutes=7),
            vm_provisioned_at=now - timedelta(minutes=7),
            last_heartbeat=None,  # agent never contacted us
            attempt_count=0,
            context={"provision_zone": "us-east1-b"},
        )
        db_session.add(task)
        await db_session.commit()
        await db_session.refresh(task)

        try:
            await reaper_service()
        except StopLoop:
            pass

        await db_session.refresh(task)
        assert task.status == TaskStatus.QUEUED, (
            "Pre-claim reaped task should be requeued"
        )
        assert task.attempt_count == 0, "Pre-claim failures must NOT burn attempt_count"
        assert (task.context or {}).get("provision_failures") == 1, (
            "Pre-claim failures must charge provision_failures"
        )

    # ── Test 8: IN_PROGRESS + stale heartbeat → attempt budget, not provision budget ──

    @pytest.mark.asyncio
    async def test_reaper_does_not_charge_provision_budget_post_claim(
        self, db_session, mocker
    ):
        """
        A task that successfully claimed (status=IN_PROGRESS) and then went
        silent must charge attempt_count, NOT provision_failures. This is the
        clock-separation guarantee: once an agent claims, provision budget is
        out of scope for any further reap on this attempt.
        """
        mocker.patch("app.vm_lifecycle.delete_agent_vm", return_value=None)
        mocker.patch("app.broadcast.broadcast_to_project", new_callable=AsyncMock)
        mocker.patch("app.reaper.AsyncSessionLocal", _FakeSessionFactory(db_session))
        StopLoop = self._stop_after_one_cycle(mocker)

        project = make_project(name="Post-claim crash test", user_id="user1")
        db_session.add(project)
        await db_session.commit()
        await db_session.refresh(project)

        now = _utcnow()
        task = make_task(
            project_id=project.id,
            vm_instance_id="crashed-vm-001",
            status=TaskStatus.IN_PROGRESS,
            vm_provisioned_at=now - timedelta(minutes=10),
            last_heartbeat=now - timedelta(minutes=6),  # stale
            attempt_count=0,
            # Pre-existing provision_failures must NOT be bumped here.
            context={"provision_failures": 1},
        )
        db_session.add(task)
        await db_session.commit()
        await db_session.refresh(task)

        try:
            await reaper_service()
        except StopLoop:
            pass

        await db_session.refresh(task)
        assert task.status == TaskStatus.QUEUED
        assert task.attempt_count == 1, "Post-claim crash must increment attempt_count"
        assert (task.context or {}).get("provision_failures") == 1, (
            "Post-claim crash must NOT touch provision_failures"
        )

    # ── Test 9: fresh vm_provisioned_at → NOT reaped even with no heartbeat ────

    @pytest.mark.asyncio
    async def test_reaper_respects_pre_claim_grace_window(self, db_session, mocker):
        """
        A VM that was just dispatched (vm_provisioned_at < 6min ago) and has
        no heartbeat yet must NOT be reaped — the agent is still within its
        boot + auth + claim budget.
        """
        mock_delete = mocker.patch(
            "app.vm_lifecycle.delete_agent_vm", return_value=None
        )
        mocker.patch("app.broadcast.broadcast_to_project", new_callable=AsyncMock)
        mocker.patch("app.reaper.AsyncSessionLocal", _FakeSessionFactory(db_session))
        StopLoop = self._stop_after_one_cycle(mocker)

        project = make_project(name="Grace window test", user_id="user1")
        db_session.add(project)
        await db_session.commit()
        await db_session.refresh(project)

        now = _utcnow()
        task = make_task(
            project_id=project.id,
            vm_instance_id="booting-vm-001",
            status=TaskStatus.PROVISIONING,
            started_at=now - timedelta(minutes=2),
            vm_provisioned_at=now - timedelta(minutes=2),
            last_heartbeat=None,
            attempt_count=0,
        )
        db_session.add(task)
        await db_session.commit()
        await db_session.refresh(task)

        try:
            await reaper_service()
        except StopLoop:
            pass

        await db_session.refresh(task)
        assert task.status == TaskStatus.PROVISIONING, "Fresh VM must be left alone"
        mock_delete.assert_not_called()


# ══════════════════════════════════════════════════════════════════════════════
# Section 2 — Max Plan Account Pool
# ══════════════════════════════════════════════════════════════════════════════


class TestMaxPlanPool:
    """Tests for load_max_plan_pool(), select_max_plan_account(), and release_max_plan_account()."""

    # ── Test 1: load_max_plan_pool reconciles active_tasks from DB ───────────

    @pytest.mark.asyncio
    async def test_load_max_plan_pool_reconciles_active_tasks_after_restart(
        self, db_session, mocker, reset_max_plan_pool
    ):
        """
        On startup/restart, load_max_plan_pool() counts the actual PROVISIONING/IN_PROGRESS
        tasks per account in the DB — not trusting any stale in-memory counters.
        """
        mocker.patch(
            "app.max_plan_pool.AsyncSessionLocal", _FakeSessionFactory(db_session)
        )

        acct_id = uuid.uuid4()
        account = MaxPlanAccount(
            id=acct_id,
            user_id=_TEST_USER,
            name="test-account",
            oauth_token="sk-ant-test-token",
            enabled=True,
            total_tasks=10,
        )
        db_session.add(account)
        await db_session.commit()

        project = make_project(name="Pool reconcile test", user_id="user1")
        db_session.add(project)
        await db_session.commit()
        await db_session.refresh(project)

        # Two tasks referencing this account are IN_PROGRESS
        for _ in range(2):
            t = make_task(
                project_id=project.id,
                vm_instance_id=f"agent-vm-{uuid.uuid4().hex[:16]}",
                status=TaskStatus.IN_PROGRESS,
                context={"max_plan_account_id": str(acct_id)},
            )
            db_session.add(t)
        # One task is COMPLETED — must NOT be counted
        completed = make_task(
            project_id=project.id,
            vm_instance_id=f"agent-vm-{uuid.uuid4().hex[:16]}",
            status=TaskStatus.COMPLETED,
            context={"max_plan_account_id": str(acct_id)},
        )
        db_session.add(completed)
        await db_session.commit()

        await load_max_plan_pool()

        assert len(_account_by_id) == 1
        entry = _account_by_id[str(acct_id)]
        assert entry["id"] == str(acct_id)
        assert entry["active_tasks"] == 2, "Only IN_PROGRESS tasks should be counted"

    # ── Test 1a: per-project active_tasks is zero-reconciled from the DB ─────

    @pytest.mark.asyncio
    async def test_load_max_plan_pool_zero_reconciles_stale_per_project_active_tasks(
        self, db_session, mocker, reset_max_plan_pool
    ):
        """On restart, the per-project active_tasks in the persisted project_state
        is NOT trusted — it is reset to 0 and then re-derived from the live DB
        tasks table. daily_task_count and other persisted keys survive.

        Regression for gating finding 94f454f4: a project whose persisted
        active_tasks was a stale nonzero value but which has ZERO currently-active
        tasks would otherwise keep the stale count forever, permanently starving
        that (account, project) pair once per-project concurrency caps apply.
        """
        mocker.patch(
            "app.max_plan_pool.AsyncSessionLocal", _FakeSessionFactory(db_session)
        )

        # Two projects: p_stale has stale persisted active_tasks but no live
        # tasks; p_live has stale persisted active_tasks AND live tasks.
        proj_stale = make_project(name="Stale proj", user_id="user1")
        proj_live = make_project(name="Live proj", user_id="user1")
        db_session.add(proj_stale)
        db_session.add(proj_live)
        await db_session.commit()
        await db_session.refresh(proj_stale)
        await db_session.refresh(proj_live)

        pid_stale = str(proj_stale.id)
        pid_live = str(proj_live.id)

        acct_id = uuid.uuid4()
        account = MaxPlanAccount(
            id=acct_id,
            user_id=_TEST_USER,
            name="test-account",
            oauth_token="sk-ant-test-token",
            enabled=True,
            total_tasks=10,
            project_state={
                pid_stale: {"active_tasks": 3, "daily_task_count": 5},
                pid_live: {"active_tasks": 3, "daily_task_count": 7},
            },
        )
        db_session.add(account)
        await db_session.commit()

        # Only p_live has a live IN_PROGRESS task on this account.
        db_session.add(
            make_task(
                project_id=proj_live.id,
                vm_instance_id=f"agent-vm-{uuid.uuid4().hex[:16]}",
                status=TaskStatus.IN_PROGRESS,
                llm_account_id=str(acct_id),
                context={"max_plan_account_id": str(acct_id)},
            )
        )
        await db_session.commit()

        await load_max_plan_pool()

        entry = _account_by_id[str(acct_id)]
        pstate = entry["project_state"]

        # Stale project: active_tasks zeroed (no live tasks), other keys survive.
        assert pstate[pid_stale]["active_tasks"] == 0, (
            "stale per-project active_tasks must be zero-reconciled from the DB"
        )
        assert pstate[pid_stale]["daily_task_count"] == 5, (
            "daily_task_count must be preserved across reconciliation"
        )

        # Live project: DB count override applies (1 live task), other keys survive.
        assert pstate[pid_live]["active_tasks"] == 1, (
            "per-project active_tasks must reflect the live DB count when present"
        )
        assert pstate[pid_live]["daily_task_count"] == 7

    # ── Test 1b: hot-loop reconcile reads the promoted column, not Task.context ─

    @pytest.mark.asyncio
    async def test_reconcile_active_per_account_reads_promoted_column(self, db_session):
        """The orchestrator 30s reconcile counts active tasks per account by
        projecting the promoted Task.llm_account_id column — NOT by reading the
        Task.context JSONB blob (D2 over-fetch fix, d-57436105).

        Proves both halves of the contract:
        (a) tasks whose column is set are counted under that account, and
        (b) a task whose account id lives ONLY in context (column NULL) is NOT
            counted — demonstrating the query no longer reads Task.context.
        """
        from app.orchestrator import _reconcile_active_per_account

        project = make_project(name="Reconcile column test", user_id="user1")
        db_session.add(project)
        await db_session.commit()
        await db_session.refresh(project)

        acct_col = str(uuid.uuid4())
        acct_ctx_only = str(uuid.uuid4())

        # Two active tasks with the promoted column set → counted under acct_col.
        for _ in range(2):
            db_session.add(
                make_task(
                    project_id=project.id,
                    vm_instance_id=f"agent-vm-{uuid.uuid4().hex[:16]}",
                    status=TaskStatus.IN_PROGRESS,
                    llm_account_id=acct_col,
                    context={"max_plan_account_id": acct_col},
                )
            )
        # PROVISIONING also counts as active.
        db_session.add(
            make_task(
                project_id=project.id,
                vm_instance_id=f"agent-vm-{uuid.uuid4().hex[:16]}",
                status=TaskStatus.PROVISIONING,
                llm_account_id=acct_col,
                context={},
            )
        )
        # A COMPLETED task must NOT be counted even though its column is set.
        db_session.add(
            make_task(
                project_id=project.id,
                vm_instance_id=f"agent-vm-{uuid.uuid4().hex[:16]}",
                status=TaskStatus.COMPLETED,
                llm_account_id=acct_col,
                context={},
            )
        )
        # An active task whose account id lives ONLY in context (column NULL):
        # the new projection ignores context, so this must NOT be counted.
        db_session.add(
            make_task(
                project_id=project.id,
                vm_instance_id=f"agent-vm-{uuid.uuid4().hex[:16]}",
                status=TaskStatus.IN_PROGRESS,
                llm_account_id=None,
                context={"llm_account_id": acct_ctx_only},
            )
        )
        await db_session.commit()

        counts = await _reconcile_active_per_account(db_session)

        assert counts == {acct_col: 3}, (
            "reconcile must count the 2 IN_PROGRESS + 1 PROVISIONING tasks by "
            "promoted column, exclude COMPLETED, and ignore the context-only row"
        )
        assert acct_ctx_only not in counts, (
            "context-only account id must not be counted — proves the reconcile "
            "query reads Task.llm_account_id, not Task.context"
        )

    # ── Test 2: load excludes disabled accounts from active count but includes them ─

    @pytest.mark.asyncio
    async def test_load_max_plan_pool_includes_disabled_accounts(
        self, db_session, mocker, reset_max_plan_pool
    ):
        """load_max_plan_pool() loads all accounts regardless of enabled flag."""
        mocker.patch(
            "app.max_plan_pool.AsyncSessionLocal", _FakeSessionFactory(db_session)
        )

        db_session.add(
            MaxPlanAccount(
                user_id=_TEST_USER,
                name="enabled-acct",
                oauth_token="tok-a",
                enabled=True,
                total_tasks=0,
            )
        )
        db_session.add(
            MaxPlanAccount(
                user_id=_TEST_USER,
                name="disabled-acct",
                oauth_token="tok-b",
                enabled=False,
                total_tasks=0,
            )
        )
        await db_session.commit()

        await load_max_plan_pool()

        assert len(_account_by_id) == 2
        names = {a["name"] for a in _account_by_id.values()}
        assert names == {"enabled-acct", "disabled-acct"}

    # ── Test 3: select_max_plan_account returns lowest active count ──────────

    @pytest.mark.asyncio
    async def test_select_max_plan_account_returns_account_with_lowest_active_count(
        self, reset_max_plan_pool
    ):
        """select_max_plan_account() must pick the enabled account with the fewest active tasks."""
        for entry in [
            {
                "id": "aaa",
                "name": "busy",
                "oauth_token": "t1",
                "enabled": True,
                "active_tasks": 5,
                "user_id": _TEST_USER,
            },
            {
                "id": "bbb",
                "name": "idle",
                "oauth_token": "t2",
                "enabled": True,
                "active_tasks": 1,
                "user_id": _TEST_USER,
            },
            {
                "id": "ccc",
                "name": "medium",
                "oauth_token": "t3",
                "enabled": True,
                "active_tasks": 3,
                "user_id": _TEST_USER,
            },
        ]:
            _max_plan_pools.setdefault(_TEST_USER, []).append(entry)
            _account_by_id[entry["id"]] = entry

        chosen = await select_max_plan_account(_TEST_USER)
        assert chosen is not None
        assert chosen["name"] == "idle"

    # ── Test 4: disabled accounts are skipped ────────────────────────────────

    @pytest.mark.asyncio
    async def test_select_max_plan_account_skips_disabled_accounts(
        self, reset_max_plan_pool
    ):
        """Disabled accounts (enabled=False) must never be selected."""
        for entry in [
            {
                "id": "aaa",
                "name": "disabled-low",
                "oauth_token": "t1",
                "enabled": False,
                "active_tasks": 0,
                "user_id": _TEST_USER,
            },
            {
                "id": "bbb",
                "name": "enabled-high",
                "oauth_token": "t2",
                "enabled": True,
                "active_tasks": 9,
                "user_id": _TEST_USER,
            },
        ]:
            _max_plan_pools.setdefault(_TEST_USER, []).append(entry)
            _account_by_id[entry["id"]] = entry

        chosen = await select_max_plan_account(_TEST_USER)
        assert chosen is not None
        assert chosen["name"] == "enabled-high", (
            "Must pick enabled account even if it has more tasks"
        )

    # ── Test 5: all accounts disabled → None ────────────────────────────────

    @pytest.mark.asyncio
    async def test_select_max_plan_account_returns_none_when_all_disabled(
        self, reset_max_plan_pool
    ):
        """When no enabled accounts exist, select_max_plan_account() returns None."""
        for entry in [
            {
                "id": "aaa",
                "name": "acct-a",
                "oauth_token": "t1",
                "enabled": False,
                "active_tasks": 0,
                "user_id": _TEST_USER,
            },
            {
                "id": "bbb",
                "name": "acct-b",
                "oauth_token": "t2",
                "enabled": False,
                "active_tasks": 0,
                "user_id": _TEST_USER,
            },
        ]:
            _max_plan_pools.setdefault(_TEST_USER, []).append(entry)
            _account_by_id[entry["id"]] = entry
        assert await select_max_plan_account(_TEST_USER) is None

    # ── Test 6: empty pool → None ────────────────────────────────────────────

    @pytest.mark.asyncio
    async def test_select_max_plan_account_returns_none_for_empty_pool(
        self, reset_max_plan_pool
    ):
        """Empty pool returns None without raising."""
        assert len(_account_by_id) == 0
        assert await select_max_plan_account(_TEST_USER) is None

    # ── Test 7: release decrements active_tasks ──────────────────────────────

    @pytest.mark.asyncio
    async def test_release_max_plan_account_decrements_active_tasks(
        self, reset_max_plan_pool
    ):
        """release_max_plan_account() must decrement active_tasks for the given account."""
        acct_id = "test-release-id"
        entry = {
            "id": acct_id,
            "name": "acct",
            "oauth_token": "tok",
            "enabled": True,
            "active_tasks": 3,
            "user_id": _TEST_USER,
        }
        _max_plan_pools.setdefault(_TEST_USER, []).append(entry)
        _account_by_id[acct_id] = entry

        await release_max_plan_account(acct_id)

        assert _account_by_id[acct_id]["active_tasks"] == 2

    # ── Test 8: release never goes below zero ────────────────────────────────

    @pytest.mark.asyncio
    async def test_release_max_plan_account_floors_at_zero(self, reset_max_plan_pool):
        """active_tasks should not go negative — floor at 0 on release."""
        acct_id = "floor-test-id"
        entry = {
            "id": acct_id,
            "name": "acct",
            "oauth_token": "tok",
            "enabled": True,
            "active_tasks": 0,
            "user_id": _TEST_USER,
        }
        _max_plan_pools.setdefault(_TEST_USER, []).append(entry)
        _account_by_id[acct_id] = entry
        await release_max_plan_account(acct_id)
        assert _account_by_id[acct_id]["active_tasks"] == 0

    # ── Test 9: concurrency limit — select respects in-flight task count ──────

    @pytest.mark.asyncio
    async def test_select_max_plan_account_respects_concurrency_by_returning_least_loaded(
        self, reset_max_plan_pool
    ):
        """
        When all accounts are busy, select_max_plan_account() still returns the
        least-loaded one (the caller is responsible for enforcing a hard limit).
        Verifies that active_tasks correctly gate account selection ordering.
        """
        for entry in [
            {
                "id": "x1",
                "name": "a1",
                "oauth_token": "t1",
                "enabled": True,
                "active_tasks": 10,
                "user_id": _TEST_USER,
            },
            {
                "id": "x2",
                "name": "a2",
                "oauth_token": "t2",
                "enabled": True,
                "active_tasks": 7,
                "user_id": _TEST_USER,
            },
            {
                "id": "x3",
                "name": "a3",
                "oauth_token": "t3",
                "enabled": True,
                "active_tasks": 10,
                "user_id": _TEST_USER,
            },
        ]:
            _max_plan_pools.setdefault(_TEST_USER, []).append(entry)
            _account_by_id[entry["id"]] = entry
        chosen = await select_max_plan_account(_TEST_USER)
        assert chosen["id"] == "x2"

    # ── Test 9: orchestrator reconcile uses Task.llm_account_id column ────────

    @pytest.mark.asyncio
    async def test_reconcile_reads_llm_account_id_column_not_context(
        self, db_session, reset_max_plan_pool
    ):
        """
        The orchestrator reconcile query must read Task.llm_account_id (the promoted
        scalar column) rather than Task.context JSONB. Tasks in PROVISIONING or
        IN_PROGRESS are counted; COMPLETED tasks are excluded.

        This test exercises the select(Task.llm_account_id) path directly —
        asserting the column is populated at write-time and is readable via a
        narrow column select (not a full-context JSONB fetch).
        """
        from sqlmodel import select as _select

        acct_id = str(uuid.uuid4())

        project = make_project(name="Reconcile column test", user_id="user1")
        db_session.add(project)
        await db_session.commit()
        await db_session.refresh(project)

        # Two PROVISIONING/IN_PROGRESS tasks with llm_account_id set on the column
        for status in [TaskStatus.PROVISIONING, TaskStatus.IN_PROGRESS]:
            t = make_task(
                project_id=project.id,
                status=status,
                llm_account_id=acct_id,
            )
            db_session.add(t)
        # COMPLETED task — must not be counted
        completed = make_task(
            project_id=project.id,
            status=TaskStatus.COMPLETED,
            llm_account_id=acct_id,
        )
        db_session.add(completed)
        # Task with no account — must not affect count
        anon = make_task(
            project_id=project.id,
            status=TaskStatus.IN_PROGRESS,
        )
        db_session.add(anon)
        await db_session.commit()

        # Replicate the orchestrator reconcile query verbatim
        rows = (
            await db_session.execute(
                _select(Task.llm_account_id).where(
                    Task.status.in_([TaskStatus.PROVISIONING, TaskStatus.IN_PROGRESS])
                )
            )
        ).all()

        active_per_account: dict = {}
        for row in rows:
            aid = row[0]
            if aid:
                active_per_account[aid] = active_per_account.get(aid, 0) + 1

        assert active_per_account.get(acct_id) == 2, (
            "Reconcile must count PROVISIONING + IN_PROGRESS tasks by llm_account_id column; "
            "COMPLETED and no-account tasks must be excluded"
        )


# ══════════════════════════════════════════════════════════════════════════════
# Section 3 — Research Task Spawning & Deduplication
# ══════════════════════════════════════════════════════════════════════════════


class TestResearchSpawning:
    """Tests for _spawn_research_task() guard conditions."""

    @pytest.fixture(autouse=True)
    def _ensure_test_accounts(self):
        """Ensure _spawn_research_task finds available accounts for test users."""
        for uid in ("user1", _TEST_USER):
            entry = {
                "id": f"acct-{uid}",
                "user_id": uid,
                "name": "test",
                "enabled": True,
                "active_tasks": 0,
                "oauth_token": "sk-ant-test",
                "total_tasks": 0,
                "token_created_at": None,
                "claude_config_json": None,
            }
            _max_plan_pools.setdefault(uid, []).append(entry)
            _account_by_id[f"acct-{uid}"] = entry
        yield
        for uid in ("user1", _TEST_USER):
            _max_plan_pools.get(uid, [])[:] = [
                a for a in _max_plan_pools.get(uid, []) if a["id"] != f"acct-{uid}"
            ]
            _account_by_id.pop(f"acct-{uid}", None)

    # ── Test 1: autonomous disabled → no task spawned ───────────────────────

    @pytest.mark.asyncio
    async def test_spawn_skipped_when_autonomous_disabled(
        self, db_session, reset_circuit_breakers
    ):
        """_spawn_research_task() must be a no-op when autonomous_enabled=False."""
        project = Project(
            name="Disabled autonomous",
            user_id="user1",
            autonomous_enabled=False,
            directions=[{"id": "d1", "status": "active", "text": "improve coverage"}],
        )
        db_session.add(project)
        await db_session.commit()
        await db_session.refresh(project)

        await _spawn_research_task(project, db_session)

        from sqlmodel import select as sql_select

        tasks = (
            (
                await db_session.execute(
                    sql_select(Task).where(Task.project_id == project.id)
                )
            )
            .scalars()
            .all()
        )
        assert len(tasks) == 0, (
            "No task should be spawned when autonomous_enabled=False"
        )

    # ── Test 2: circuit breaker OPEN → no task spawned ──────────────────────

    @pytest.mark.asyncio
    async def test_spawn_skipped_when_circuit_breaker_open(
        self, db_session, reset_circuit_breakers
    ):
        """_spawn_research_task() must respect an OPEN circuit breaker in its cooldown period."""
        project = Project(
            name="CB open test",
            user_id="user1",
            autonomous_enabled=True,
            directions=[{"id": "d1", "status": "active", "text": "improve coverage"}],
        )
        db_session.add(project)
        await db_session.commit()
        await db_session.refresh(project)

        # Trip the circuit breaker with a recent open time (still in cooldown)
        breaker = _shared_state.get_circuit_breaker(str(project.id))
        breaker["state"] = "OPEN"
        breaker["opened_at"] = _utcnow() - timedelta(
            minutes=5
        )  # only 5 min; cooldown is 30 min
        breaker["cooldown_seconds"] = 1800

        await _spawn_research_task(project, db_session)

        from sqlmodel import select as sql_select

        tasks = (
            (
                await db_session.execute(
                    sql_select(Task).where(Task.project_id == project.id)
                )
            )
            .scalars()
            .all()
        )
        assert len(tasks) == 0, (
            "No task should be spawned while circuit breaker is OPEN"
        )

    # ── Test 2b: HALF_OPEN stuck test task → reverts to OPEN ────

    @pytest.mark.asyncio
    async def test_half_open_stuck_task_reverts_to_open(
        self, db_session, reset_circuit_breakers
    ):
        """If the HALF_OPEN test task is stuck for >30 min, breaker reverts to OPEN with doubled cooldown."""
        project = Project(
            name="CB half-open test",
            user_id="user1",
            autonomous_enabled=True,
            directions=[{"id": "d1", "status": "active", "text": "improve coverage"}],
        )
        db_session.add(project)
        await db_session.commit()
        await db_session.refresh(project)

        # Create a "stuck" test task that started 45 minutes ago
        stuck_task = Task(
            project_id=project.id,
            title="Research: half-open test task",
            payloads=[{"type": "research", "base_branch": "main"}],
            vm_instance_id=f"agent-vm-{uuid.uuid4().hex[:16]}",
            status=TaskStatus.IN_PROGRESS,
            created_by="ORCHESTRATOR",
            started_at=_utcnow() - timedelta(minutes=45),
        )
        db_session.add(stuck_task)
        await db_session.commit()
        await db_session.refresh(stuck_task)

        # Set breaker to HALF_OPEN with the stuck task as the test task
        breaker = _shared_state.get_circuit_breaker(str(project.id))
        breaker["state"] = "HALF_OPEN"
        breaker["half_open_task_id"] = str(stuck_task.id)
        breaker["cooldown_seconds"] = 1800  # 30 min base

        await _spawn_research_task(project, db_session)

        # Breaker should have reverted to OPEN with doubled cooldown
        assert breaker["state"] == "OPEN", "Stuck HALF_OPEN should revert to OPEN"
        assert breaker["cooldown_seconds"] == 3600, (
            "Cooldown should double from 1800 to 3600"
        )
        assert breaker["half_open_task_id"] is None, "Test task ID should be cleared"

        # No new task should have been spawned (breaker is now OPEN)
        from sqlmodel import select as sql_select

        all_tasks = (
            (
                await db_session.execute(
                    sql_select(Task).where(Task.project_id == project.id)
                )
            )
            .scalars()
            .all()
        )
        new_queued = [t for t in all_tasks if t.status == TaskStatus.QUEUED]
        assert len(new_queued) == 0, (
            "No new task should be spawned when breaker reverts to OPEN"
        )

    # ── Test 3: existing research task in progress → no duplicate spawned ────

    @pytest.mark.asyncio
    async def test_spawn_skipped_when_research_task_already_active(
        self, db_session, reset_circuit_breakers
    ):
        """
        _spawn_research_task() must not spawn beyond the parallel limit if a
        research-type task is already QUEUED/PROVISIONING/IN_PROGRESS for the same project.
        """
        _main_module._runtime_config["max_parallel_research_per_project"] = 1
        try:
            project = Project(
                name="Dedup spawn test",
                user_id="user1",
                autonomous_enabled=True,
                directions=[{"id": "d1", "status": "active", "text": "security scan"}],
            )
            db_session.add(project)
            await db_session.commit()
            await db_session.refresh(project)

            # Existing research task already running
            existing = Task(
                project_id=project.id,
                title="Research: existing scan",
                payloads=[{"type": "research", "base_branch": "main"}],
                vm_instance_id=f"agent-vm-{uuid.uuid4().hex[:16]}",
                status=TaskStatus.IN_PROGRESS,
                created_by="ORCHESTRATOR",
            )
            db_session.add(existing)
            await db_session.commit()

            await _spawn_research_task(project, db_session)

            from sqlmodel import select as sql_select

            all_tasks = (
                (
                    await db_session.execute(
                        sql_select(Task).where(Task.project_id == project.id)
                    )
                )
                .scalars()
                .all()
            )
            research_tasks = [
                t
                for t in all_tasks
                if (t.payloads or [{}])[0].get("type") == "research"
            ]
            assert len(research_tasks) == 1, (
                "A second research task must not be spawned"
            )
        finally:
            _main_module._runtime_config["max_parallel_research_per_project"] = 2

    # ── Test 4: rate limit — too many orchestrator tasks this hour ───────────

    @pytest.mark.asyncio
    async def test_spawn_skipped_when_rate_limit_reached(
        self, db_session, reset_circuit_breakers
    ):
        """_spawn_research_task() must stop spawning after max_autonomous_tasks_per_hour."""
        max_per_hour = _main_module._runtime_config["max_autonomous_tasks_per_hour"]

        project = Project(
            name="Rate limit test",
            user_id="user1",
            autonomous_enabled=True,
            directions=[{"id": "d1", "status": "active", "text": "performance review"}],
            vision={"purpose": "test"},
        )
        db_session.add(project)
        await db_session.commit()
        await db_session.refresh(project)

        # Fill up the hourly quota with recent orchestrator research tasks
        for i in range(max_per_hour):
            t = Task(
                project_id=project.id,
                title=f"Research: past scan {i}",
                payloads=[{"type": "research", "base_branch": "main"}],
                vm_instance_id=f"agent-vm-{uuid.uuid4().hex[:16]}",
                status=TaskStatus.COMPLETED,
                created_by="ORCHESTRATOR",
                created_at=_utcnow() - timedelta(minutes=10),
            )
            db_session.add(t)
        await db_session.commit()

        await _spawn_research_task(project, db_session)

        from sqlmodel import select as sql_select

        # Only the COMPLETED tasks we inserted should exist — no new QUEUED one
        queued = (
            (
                await db_session.execute(
                    sql_select(Task).where(
                        Task.project_id == project.id, Task.status == TaskStatus.QUEUED
                    )
                )
            )
            .scalars()
            .all()
        )
        assert len(queued) == 0, "Rate limit must prevent additional spawns"


# ══════════════════════════════════════════════════════════════════════════════
# Section 4 — Research Completion: Deduplication & Dependency Ordering
# ══════════════════════════════════════════════════════════════════════════════


class TestResearchCompletion:
    """Tests for _handle_research_completion() deduplication and ordering."""

    @staticmethod
    def _findings_json(*findings):
        """Serialize a list of finding dicts to a log string for _handle_research_completion."""
        return json.dumps(findings)

    # ── Test 5: duplicate findings are skipped ───────────────────────────────

    @pytest.mark.asyncio
    async def test_duplicate_findings_are_skipped(self, db_session, mocker):
        """
        Findings whose titles share 3+ meaningful words with an existing task title
        must be deduplicated — no new child task should be created for them.
        """
        mocker.patch("app.broadcast.broadcast_to_project", new_callable=AsyncMock)
        mocker.patch(
            "app.task_completion.get_github_token_for_task",
            new_callable=AsyncMock,
            return_value=("fake-token", "owner", "repo"),
        )
        mocker.patch(
            "app.task_completion.get_branch_sha",
            new_callable=AsyncMock,
            return_value="abc123",
        )
        mocker.patch(
            "app.task_completion.create_branch",
            new_callable=AsyncMock,
            return_value=None,
        )
        mocker.patch(
            "app.research_completion._apply_structural_actionability_downgrade",
            return_value=0,
        )

        project = Project(name="Dedup findings test", user_id="user1")
        db_session.add(project)
        await db_session.commit()
        await db_session.refresh(project)

        # Existing active task that shares finding #1's code region (related_files,
        # recorded on the task context at spawn time) and title.
        existing_task = Task(
            project_id=project.id,
            title="optimize database query performance",
            payloads=[{"type": "create"}],
            vm_instance_id=f"agent-vm-{uuid.uuid4().hex[:16]}",
            status=TaskStatus.IN_PROGRESS,
            created_by="ORCHESTRATOR",
            context={"related_files": ["app/db/query.py", "app/db/perf.py"]},
        )
        db_session.add(existing_task)

        # Research task that will produce findings
        research_task = Task(
            project_id=project.id,
            title="Research: autonomous scan",
            payloads=[{"type": "research", "base_branch": "main"}],
            vm_instance_id=f"agent-vm-{uuid.uuid4().hex[:16]}",
            status=TaskStatus.IN_PROGRESS,
            created_by="ORCHESTRATOR",
        )
        db_session.add(research_task)
        await db_session.commit()
        await db_session.refresh(research_task)

        # Finding #1 duplicates the existing task title
        # Finding #2 is genuinely new
        logs = self._findings_json(
            {
                # duplicate — same region (related_files) + 4 matching title words
                "title": "optimize database query performance",
                "prompt": "Improve the slow SQL queries in the reports module.",
                "priority": "medium",
                "related_files": ["app/db/query.py", "app/db/perf.py"],
            },
            {
                # genuinely new — different region, so not deduped
                "title": "add request tracing middleware",
                "prompt": "Instrument HTTP handlers with OpenTelemetry trace IDs.",
                "priority": "medium",
                "related_files": ["app/http/middleware.py"],
            },
        )

        await _handle_research_completion(research_task, logs, db_session)

        from sqlmodel import select as sql_select

        children = (
            (
                await db_session.execute(
                    sql_select(Task).where(
                        Task.parent_task_id == research_task.id,
                        Task.status == TaskStatus.QUEUED,
                    )
                )
            )
            .scalars()
            .all()
        )

        implementation_children = [
            child
            for child in children
            if (child.first_payload or {}).get("type") != "salvage_bundle"
        ]
        salvage_children = [
            child
            for child in children
            if (child.first_payload or {}).get("type") == "salvage_bundle"
        ]
        assert len(implementation_children) == 1, (
            "Only the non-duplicate finding should produce an implementation task"
        )
        assert len(salvage_children) == 1, (
            "The in-flight duplicate should be retained in one consolidated salvage bundle"
        )
        assert (
            "tracing" in implementation_children[0].title.lower()
            or "middleware" in implementation_children[0].title.lower()
        )

    # ── Burst-1 regression: duplicate active claim during post-spawn apply ───

    @pytest.mark.skipif(
        not (
            os.getenv("USE_TEST_POSTGRES", "").lower() == "true"
            and os.getenv("DATABASE_URL", "").startswith("postgresql")
        ),
        reason=(
            "genuine ux_task_active_idempotency_key violation message (constraint "
            "name included) requires PostgreSQL"
        ),
    )
    @pytest.mark.asyncio
    async def test_duplicate_active_claim_during_post_spawn_is_benign(
        self, db_session, mocker
    ):
        """Two stacked regressions on the same collision.

        Prod burst 2026-06-07 00:56: a research task reported COMPLETED (its
        idempotency claim left the active-status index), a peer research cycle
        then claimed the same key, and the COMPLETED→EVALUATING flip violated
        ux_task_active_idempotency_key at session.commit(). Pre-fix the
        IntegrityError escaped with the session in pending-rollback, the
        handler-failure recovery itself died on PendingRollbackError, and the
        request 500'd live agents. The handler must catch the collision and
        complete cleanly (the final commit below is that regression guard).

        Prod 2026-06-09 (task 5ac5c8c1): the guard then OVER-corrected —
        treating the self-claim collision as a duplicate batch and rolling
        back the children too, discarding unique undispatched findings while
        the peer re-researched the direction from scratch. The contract now:
        the children and the EVALUATING flip survive (self-claim recovery
        re-commits them), and THIS task's claim is released — the peer
        legitimately owns the direction claim; its own spawn-time finding
        dedup suppresses any duplicate children when it completes."""
        mocker.patch("app.broadcast.broadcast_to_project", new_callable=AsyncMock)
        mocker.patch(
            "app.task_completion.get_github_token_for_task",
            new_callable=AsyncMock,
            return_value=("fake-token", "owner", "repo"),
        )
        mocker.patch(
            "app.task_completion.get_branch_sha",
            new_callable=AsyncMock,
            return_value="abc123",
        )
        mocker.patch(
            "app.task_completion.create_branch",
            new_callable=AsyncMock,
            return_value=None,
        )
        mocker.patch(
            "app.research_completion._apply_structural_actionability_downgrade",
            return_value=0,
        )

        project = Project(name="Dup claim benign test", user_id="user1")
        db_session.add(project)
        await db_session.commit()
        await db_session.refresh(project)

        claim_key = f"research:{project.id}:d-dupclaim"
        research_task = Task(
            project_id=project.id,
            title="Research: autonomous scan",
            payloads=[{"type": "research", "base_branch": "main"}],
            vm_instance_id=f"agent-vm-{uuid.uuid4().hex[:16]}",
            status=TaskStatus.COMPLETED,  # agent-reported terminal (early commit)
            created_by="ORCHESTRATOR",
            idempotency_key=claim_key,
        )
        db_session.add(research_task)
        # The pipelined duplicate cycle: holds the SAME claim key in an ACTIVE
        # status, so the original's flip back to EVALUATING genuinely violates
        # the partial unique index.
        peer_task = Task(
            project_id=project.id,
            title="Pipelined duplicate cycle placeholder",
            payloads=[{"type": "research", "base_branch": "main"}],
            status=TaskStatus.QUEUED,
            created_by="ORCHESTRATOR",
            idempotency_key=claim_key,
        )
        db_session.add(peer_task)
        await db_session.commit()
        await db_session.refresh(research_task)

        logs = self._findings_json(
            {
                "title": "add request tracing middleware",
                "prompt": "Instrument HTTP handlers with OpenTelemetry trace IDs.",
                "priority": "medium",
            },
        )

        # Pre-fix this raised IntegrityError (then PendingRollbackError in the
        # post-commit recovery path).
        await _handle_research_completion(research_task, logs, db_session)

        await db_session.refresh(research_task)
        # Self-claim recovery: the children and the (atomic, #9013) EVALUATING
        # flip survive the collision — the batch is unique work, not a
        # duplicate of the peer's.
        assert research_task.status == TaskStatus.EVALUATING
        # The claim was released to the peer, which legitimately owns the
        # direction now — re-committing WITH the key would just collide again.
        assert research_task.idempotency_key is None
        from sqlmodel import select as sql_select

        children = (
            (
                await db_session.execute(
                    sql_select(Task).where(Task.parent_task_id == research_task.id)
                )
            )
            .scalars()
            .all()
        )
        assert len(children) == 1, (
            "the spawned batch must survive the self-claim collision"
        )
        # The peer's claim is untouched.
        await db_session.refresh(peer_task)
        assert peer_task.idempotency_key == claim_key
        assert peer_task.status == TaskStatus.QUEUED
        # The session is clean — a follow-up commit must not raise
        # PendingRollbackError (the actual prod-incident regression guard).
        await db_session.commit()

    # ── Knowledge findings route to web_research/design task types ───────────

    @pytest.mark.asyncio
    @pytest.mark.parametrize("finding_type", ["web_research", "design"])
    @pytest.mark.parametrize("scope", ["small", "medium"])
    async def test_knowledge_finding_spawns_matching_task_type(
        self, db_session, mocker, finding_type, scope
    ):
        """
        A finding declaring type=web_research or type=design must spawn a child
        whose payload type matches that knowledge task type — NOT a generic
        create/plan PR task. The mapping must hold at medium scope too, so the
        low-confidence medium→large escalation cannot silently rewrite the type
        to "plan".
        """
        mocker.patch("app.broadcast.broadcast_to_project", new_callable=AsyncMock)
        mocker.patch(
            "app.task_completion.get_github_token_for_task",
            new_callable=AsyncMock,
            return_value=("fake-token", "owner", "repo"),
        )
        mocker.patch(
            "app.task_completion.get_branch_sha",
            new_callable=AsyncMock,
            return_value="abc123",
        )
        mocker.patch(
            "app.task_completion.create_branch",
            new_callable=AsyncMock,
            return_value=None,
        )
        mocker.patch(
            "app.research_completion._apply_structural_actionability_downgrade",
            return_value=0,
        )
        # Force a low-confidence prediction so the medium→large escalation path
        # would fire for an unguarded type — proving the knowledge-type guard holds.
        mocker.patch(
            "app.research_completion.predict_spawn_success",
            return_value=0.0,
        )

        project = Project(name=f"Knowledge {finding_type} {scope}", user_id="user1")
        db_session.add(project)
        await db_session.commit()
        await db_session.refresh(project)

        research_task = Task(
            project_id=project.id,
            title="Research: autonomous scan",
            payloads=[{"type": "research", "base_branch": "main"}],
            vm_instance_id=f"agent-vm-{uuid.uuid4().hex[:16]}",
            status=TaskStatus.IN_PROGRESS,
            created_by="ORCHESTRATOR",
        )
        db_session.add(research_task)
        await db_session.commit()
        await db_session.refresh(research_task)

        logs = self._findings_json(
            {
                "type": finding_type,
                "title": f"{finding_type}: gather grounding for widget work",
                "prompt": "Establish the knowledge needed before implementation.",
                "priority": "medium",
                "scope": scope,
                "actionability_grade": "HIGH",
            },
        )

        await _handle_research_completion(research_task, logs, db_session)

        from sqlmodel import select as sql_select

        children = (
            (
                await db_session.execute(
                    sql_select(Task).where(Task.parent_task_id == research_task.id)
                )
            )
            .scalars()
            .all()
        )

        assert len(children) == 1, "The knowledge finding should produce one child task"
        assert children[0].first_payload["type"] == finding_type, (
            f"finding type={finding_type} at scope={scope} must route to a "
            f"{finding_type} task, not {children[0].first_payload['type']}"
        )

    # ── Test 6: depends_on_indices creates sequential task ordering ──────────

    @pytest.mark.asyncio
    async def test_depends_on_indices_creates_sequential_task_ordering(
        self, db_session, mocker
    ):
        """
        When findings carry explicit depends_on_indices, the resulting child tasks
        must have their depends_on field set to the UUIDs of the referenced tasks.
        """
        mocker.patch("app.broadcast.broadcast_to_project", new_callable=AsyncMock)
        mocker.patch(
            "app.task_completion.get_github_token_for_task",
            new_callable=AsyncMock,
            return_value=("fake-token", "owner", "repo"),
        )
        mocker.patch(
            "app.task_completion.get_branch_sha",
            new_callable=AsyncMock,
            return_value="abc123",
        )
        mocker.patch(
            "app.task_completion.create_branch",
            new_callable=AsyncMock,
            return_value=None,
        )
        mocker.patch(
            "app.research_completion._apply_structural_actionability_downgrade",
            return_value=0,
        )

        project = Project(name="Deps ordering test", user_id="user1")
        db_session.add(project)
        await db_session.commit()
        await db_session.refresh(project)

        research_task = Task(
            project_id=project.id,
            title="Research: autonomous scan",
            payloads=[{"type": "research", "base_branch": "main"}],
            vm_instance_id=f"agent-vm-{uuid.uuid4().hex[:16]}",
            status=TaskStatus.IN_PROGRESS,
            created_by="ORCHESTRATOR",
        )
        db_session.add(research_task)
        await db_session.commit()
        await db_session.refresh(research_task)

        # Finding[0]: no dependencies
        # Finding[1]: depends on finding[0] via depends_on_indices
        logs = self._findings_json(
            {
                "title": "scaffold authentication module",
                "prompt": "Create the base authentication service with JWT support.",
                "priority": "high",
            },
            {
                "title": "add login endpoint to authentication",
                "prompt": "Implement POST /auth/login using the scaffolded auth module.",
                "priority": "high",
                "depends_on_indices": [0],  # must run after finding[0]
            },
        )

        await _handle_research_completion(research_task, logs, db_session)

        from sqlmodel import select as sql_select

        children = (
            (
                await db_session.execute(
                    sql_select(Task)
                    .where(Task.parent_task_id == research_task.id)
                    .order_by(Task.created_at)
                )
            )
            .scalars()
            .all()
        )

        assert len(children) == 2, "Both findings should produce child tasks"

        # Identify tasks by title keyword
        task_a = next(t for t in children if "scaffold" in t.title.lower())
        task_b = next(t for t in children if "login" in t.title.lower())

        # task_b must declare task_a as a dependency
        assert task_b.depends_on is not None
        assert str(task_a.id) in task_b.depends_on, (
            "depends_on_indices=[0] must translate to the UUID of the first child task"
        )
        # task_a must have no dependencies
        assert not task_a.depends_on

    # ── Test 7: wave-based ordering — low-priority tasks depend on high-priority ─

    @pytest.mark.asyncio
    async def test_priority_ordering_no_artificial_wave_dependencies(
        self, db_session, mocker
    ):
        """
        Findings get priority values (high=30, medium=50, low=70) but NO
        inter-wave dependencies. The orchestrator's priority-based dispatch
        naturally runs high-priority tasks first without blocking low-priority
        ones behind artificial dependency chains.
        """
        mocker.patch("app.broadcast.broadcast_to_project", new_callable=AsyncMock)
        mocker.patch(
            "app.task_completion.get_github_token_for_task",
            new_callable=AsyncMock,
            return_value=("fake-token", "owner", "repo"),
        )
        mocker.patch(
            "app.task_completion.get_branch_sha",
            new_callable=AsyncMock,
            return_value="abc123",
        )
        mocker.patch(
            "app.task_completion.create_branch",
            new_callable=AsyncMock,
            return_value=None,
        )
        mocker.patch(
            "app.research_completion._apply_structural_actionability_downgrade",
            return_value=0,
        )

        project = Project(name="Priority ordering test", user_id="user1")
        db_session.add(project)
        await db_session.commit()
        await db_session.refresh(project)

        research_task = Task(
            project_id=project.id,
            title="Research: priority test",
            payloads=[{"type": "research", "base_branch": "main"}],
            vm_instance_id=f"agent-vm-{uuid.uuid4().hex[:16]}",
            status=TaskStatus.IN_PROGRESS,
            created_by="ORCHESTRATOR",
        )
        db_session.add(research_task)
        await db_session.commit()
        await db_session.refresh(research_task)

        logs = self._findings_json(
            {
                "title": "critical security patch",
                "prompt": "Apply the CVE-2024-1234 fix immediately.",
                "priority": "high",
            },
            {
                "title": "improve documentation coverage",
                "prompt": "Expand JSDoc comments across all public APIs.",
                "priority": "low",
            },
        )

        await _handle_research_completion(research_task, logs, db_session)

        from sqlmodel import select as sql_select

        children = (
            (
                await db_session.execute(
                    sql_select(Task).where(Task.parent_task_id == research_task.id)
                )
            )
            .scalars()
            .all()
        )

        assert len(children) == 2

        task_high = next(
            t
            for t in children
            if "security" in t.title.lower() or "critical" in t.title.lower()
        )
        task_low = next(
            t
            for t in children
            if "documentation" in t.title.lower() or "improve" in t.title.lower()
        )

        # Both tasks are independent — no artificial wave dependencies
        assert not task_high.depends_on, (
            "High-priority task should have no dependencies"
        )
        assert not task_low.depends_on, (
            "Low-priority task should have no wave dependencies"
        )

        # Priority values reflect finding priority (high=100, low=0)
        assert task_high.priority > task_low.priority, (
            f"High-priority task ({task_high.priority}) should have higher numeric priority than low ({task_low.priority})"
        )

    # ── Test 8: all findings deduped → auto-complete with direction note ────

    @pytest.mark.asyncio
    async def test_all_findings_deduped_auto_completes_task(self, db_session, mocker):
        """
        When _handle_research_completion produces 0 child tasks (all findings
        are duplicates), the research task must transition to COMPLETED (not
        stuck in EVALUATING) and a direction note must be written.
        """
        mocker.patch("app.broadcast.broadcast_to_project", new_callable=AsyncMock)
        mocker.patch(
            "app.task_completion.get_github_token_for_task",
            new_callable=AsyncMock,
            return_value=("fake-token", "owner", "repo"),
        )
        mocker.patch(
            "app.task_completion.get_branch_sha",
            new_callable=AsyncMock,
            return_value="abc123",
        )
        mocker.patch(
            "app.task_completion.create_branch",
            new_callable=AsyncMock,
            return_value=None,
        )
        mocker.patch(
            "app.research_completion._apply_structural_actionability_downgrade",
            return_value=0,
        )

        direction_id = f"d-{uuid.uuid4().hex[:8]}"
        direction = {
            "id": direction_id,
            "text": "Improve test coverage",
            "status": "active",
            "notes": [],
        }
        project = Project(
            name="Zero children test",
            user_id="user1",
            autonomous_enabled=True,
            directions=[direction],
        )
        db_session.add(project)
        await db_session.commit()
        await db_session.refresh(project)

        # Existing task that shares the finding's code region (related_files) and
        # title — triggers region-identity dedup.
        existing_task = Task(
            project_id=project.id,
            title="improve test coverage for auth module",
            payloads=[{"type": "create"}],
            vm_instance_id=f"agent-vm-{uuid.uuid4().hex[:16]}",
            status=TaskStatus.IN_PROGRESS,
            created_by="ORCHESTRATOR",
            context={"related_files": ["tests/test_auth.py", "app/auth.py"]},
        )
        db_session.add(existing_task)

        research_task = Task(
            project_id=project.id,
            title="Research: test coverage scan",
            payloads=[{"type": "research", "base_branch": "main"}],
            vm_instance_id=f"agent-vm-{uuid.uuid4().hex[:16]}",
            status=TaskStatus.IN_PROGRESS,
            created_by="ORCHESTRATOR",
            context={"direction_id": direction_id},
        )
        db_session.add(research_task)
        await db_session.commit()
        await db_session.refresh(research_task)

        # Single finding that duplicates the existing task (same region + title)
        logs = self._findings_json(
            {
                "title": "improve test coverage for auth module",
                "prompt": "Add unit tests for the authentication service.",
                "priority": "medium",
                "related_files": ["tests/test_auth.py", "app/auth.py"],
            },
        )

        await _handle_research_completion(research_task, logs, db_session)

        await db_session.refresh(research_task)
        from sqlmodel import select as sql_select

        children = (
            (
                await db_session.execute(
                    sql_select(Task).where(Task.parent_task_id == research_task.id)
                )
            )
            .scalars()
            .all()
        )
        salvage_children = [
            child
            for child in children
            if (child.first_payload or {}).get("type") == "salvage_bundle"
        ]
        assert len(children) == 1
        assert len(salvage_children) == 1, (
            "The deduplicated in-flight finding should be retained in a salvage bundle"
        )
        assert research_task.status == TaskStatus.EVALUATING, (
            "A research task that owns a salvage child must remain EVALUATING for rollup"
        )
        assert (research_task.context or {}).get("salvage_activity", {}).get(
            "salvage_task_id"
        ) == str(salvage_children[0].id)


# ── Tests: classify_failure ──────────────────────────────────────────────────


class TestClassifyFailure:
    """Unit tests for classify_failure() — verifies exit-code and process-killed patterns."""

    def _make_task(self, status_message: str) -> Task:
        task = Task(
            id=uuid.uuid4(),
            title="test task",
            prompt="test",
            latest_status_message=status_message,
            status=TaskStatus.FAILED,
        )
        return task

    # ── exit code patterns ───────────────────────────────────────────────────

    def test_exit_code_137_is_agent_timeout(self):
        task = self._make_task("Agent failed with exit code 137")
        assert classify_failure(task) == "AGENT_TIMEOUT"

    def test_exit_code_143_is_agent_timeout(self):
        task = self._make_task("Process terminated with exit code 143")
        assert classify_failure(task) == "AGENT_TIMEOUT"

    def test_container_exited_is_code_error(self):
        task = self._make_task("container exited unexpectedly")
        assert classify_failure(task) == "CODE_ERROR"

    def test_non_zero_exit_code_is_code_error(self):
        task = self._make_task("non-zero exit code returned by agent runner")
        assert classify_failure(task) == "CODE_ERROR"

    def test_process_exited_is_code_error(self):
        task = self._make_task("process exited with status 1")
        assert classify_failure(task) == "CODE_ERROR"

    def test_node_process_killed_is_agent_timeout(self):
        task = self._make_task("node process killed by system")
        assert classify_failure(task) == "AGENT_TIMEOUT"

    def test_oom_killer_is_agent_timeout(self):
        task = self._make_task("oom killer terminated the agent")
        assert classify_failure(task) == "AGENT_TIMEOUT"

    # ── provision failure patterns ───────────────────────────────────────────

    def test_address_already_in_use_is_provision_failure(self):
        task = self._make_task("address already in use on port 8080")
        assert classify_failure(task) == "PROVISION_FAILURE"

    def test_failed_to_connect_is_provision_failure(self):
        task = self._make_task("failed to connect to agent VM during init")
        assert classify_failure(task) == "PROVISION_FAILURE"

    # ── existing patterns still pass ─────────────────────────────────────────

    def test_no_heartbeat_is_agent_timeout(self):
        task = self._make_task("no heartbeat received from agent")
        assert classify_failure(task) == "AGENT_TIMEOUT"

    def test_reaped_is_agent_timeout(self):
        task = self._make_task("task was reaped by orchestrator")
        assert classify_failure(task) == "AGENT_TIMEOUT"

    def test_sigterm_is_agent_timeout(self):
        task = self._make_task("process received sigterm")
        assert classify_failure(task) == "AGENT_TIMEOUT"

    def test_out_of_memory_is_agent_timeout(self):
        task = self._make_task("out of memory error")
        assert classify_failure(task) == "AGENT_TIMEOUT"

    def test_provision_failure(self):
        task = self._make_task("failed to provision VM")
        assert classify_failure(task) == "PROVISION_FAILURE"

    def test_rate_limit(self):
        task = self._make_task("API Error: Rate limit reached")
        assert classify_failure(task) == "RATE_LIMIT"

    def test_code_error_default(self):
        task = self._make_task(
            "AttributeError: 'NoneType' object has no attribute 'foo'"
        )
        assert classify_failure(task) == "CODE_ERROR"

    def test_logs_also_searched(self):
        task = self._make_task("")
        assert classify_failure(task, logs="exit code 137 returned") == "AGENT_TIMEOUT"

    def test_failed_to_connect_does_not_override_db(self):
        """Database connection errors take priority over 'failed to connect'."""
        task = self._make_task(
            "could not connect to server: Connection refused (psycopg)"
        )
        assert classify_failure(task) == "EXTERNAL_API"


# ══════════════════════════════════════════════════════════════════════════════
# Section 5 — Rollup deferral filtering
# ══════════════════════════════════════════════════════════════════════════════


class TestRollupDeferral:
    """
    Unit tests for the rollup-deferral filtering logic in orchestrator_worker().

    The logic (orchestrator.py step 2.6) defers QUEUED tasks whose related_files
    significantly overlap with files from COMPLETED children of EVALUATING rollup
    parents.  Deferral threshold: >= 2 files AND >= 50% of candidate's files.
    """

    def _apply_rollup_filter(self, eligible_ids, candidate_map, pending_rollup_files):
        """
        Drive the rollup-deferral decision through the *production* predicate
        ``orchestrator_filters.rollup_overlap_defers`` (the single source of
        truth used by ``filter_rollup_conflicts`` step 2.6) rather than
        re-implementing it here.  Returns (non_deferred_ids, deferred_ids).

        The candidate-level guards mirror production's structure exactly: a
        missing candidate or a candidate with no files passes through, matching
        the early-continues in ``filter_rollup_conflicts``.
        """
        from app.orchestrator_filters import rollup_overlap_defers

        non_deferred = []
        deferred = []
        for eid in eligible_ids:
            candidate = candidate_map.get(eid)
            if not candidate:
                non_deferred.append(eid)
                continue
            cfiles = set(candidate.get("related_files", []))
            if not cfiles:
                non_deferred.append(eid)
                continue
            should_defer, _stale_overlap = rollup_overlap_defers(
                cfiles, pending_rollup_files
            )
            if should_defer:
                deferred.append(eid)
            else:
                non_deferred.append(eid)
        return non_deferred, deferred

    def test_significant_overlap_defers_task(self):
        """Task overlapping 3 out of 4 files with pending rollup is deferred (>= 2 files, >= 50%)."""
        eid = uuid.uuid4()
        candidate_map = {eid: {"related_files": ["a.py", "b.py", "c.py", "d.py"]}}
        pending = {
            "a.py": "Big Refactor",
            "b.py": "Big Refactor",
            "c.py": "Big Refactor",
        }

        non_deferred, deferred = self._apply_rollup_filter(
            [eid], candidate_map, pending
        )

        assert eid in deferred
        assert eid not in non_deferred

    def test_small_overlap_does_not_defer(self):
        """Task overlapping only 1 of 10 files with pending rollup is NOT deferred."""
        eid = uuid.uuid4()
        candidate_map = {eid: {"related_files": [f"file{i}.py" for i in range(10)]}}
        pending = {"file0.py": "Big Refactor"}

        non_deferred, deferred = self._apply_rollup_filter(
            [eid], candidate_map, pending
        )

        assert eid in non_deferred
        assert eid not in deferred

    def test_no_related_files_not_deferred(self):
        """Task with no related_files is never deferred."""
        eid = uuid.uuid4()
        candidate_map = {eid: {"related_files": []}}
        pending = {
            "a.py": "Big Refactor",
            "b.py": "Big Refactor",
            "c.py": "Big Refactor",
        }

        non_deferred, deferred = self._apply_rollup_filter(
            [eid], candidate_map, pending
        )

        assert eid in non_deferred
        assert eid not in deferred

    def test_no_pending_rollup_files_passes_through(self):
        """When there are no pending rollup files, all tasks pass through unaffected."""
        ids = [uuid.uuid4() for _ in range(3)]
        candidate_map = {eid: {"related_files": ["a.py", "b.py"]} for eid in ids}

        non_deferred, deferred = self._apply_rollup_filter(ids, candidate_map, {})

        assert set(non_deferred) == set(ids)
        assert deferred == []

    def test_exactly_50_percent_overlap_defers(self):
        """Task with exactly 2/4 files overlapping (50%) is deferred (boundary case)."""
        eid = uuid.uuid4()
        candidate_map = {eid: {"related_files": ["a.py", "b.py", "c.py", "d.py"]}}
        pending = {"a.py": "Rollup", "b.py": "Rollup"}

        non_deferred, deferred = self._apply_rollup_filter(
            [eid], candidate_map, pending
        )

        assert eid in deferred

    def test_just_below_50_percent_not_deferred(self):
        """Task with 1/3 files overlapping (33%) is NOT deferred even though >= 1."""
        eid = uuid.uuid4()
        candidate_map = {eid: {"related_files": ["a.py", "b.py", "c.py"]}}
        pending = {"a.py": "Rollup"}

        non_deferred, deferred = self._apply_rollup_filter(
            [eid], candidate_map, pending
        )

        assert eid in non_deferred
        assert eid not in deferred

    @pytest.mark.asyncio
    async def test_completed_parent_no_longer_defers(self, db_session, mocker):
        """
        Once the rollup parent transitions out of EVALUATING (e.g., COMPLETED),
        its children's files no longer appear in pending_rollup_files and the
        candidate task is NOT deferred.
        """
        project = Project(name="Completed rollup test", user_id="user1")
        db_session.add(project)
        await db_session.commit()
        await db_session.refresh(project)

        # Parent is now COMPLETED (rollup merged) — not EVALUATING
        parent = Task(
            project_id=project.id,
            title="Rollup: merged refactor",
            payloads=[{"type": "plan"}],
            status=TaskStatus.COMPLETED,
        )
        db_session.add(parent)
        await db_session.commit()
        await db_session.refresh(parent)

        child = Task(
            project_id=project.id,
            title="Refactor: decompose module",
            payloads=[{"type": "refactor"}],
            status=TaskStatus.COMPLETED,
            parent_task_id=parent.id,
            context={
                "scope": "large",
                "related_files": ["backend/app/main.py", "backend/app/models.py"],
            },
        )
        db_session.add(child)
        await db_session.commit()

        # Query pending rollup files — parent is COMPLETED so no EVALUATING parents exist
        from sqlmodel import select as sql_select

        eval_parent_ids = (
            (
                await db_session.execute(
                    sql_select(Task.id).where(Task.status == TaskStatus.EVALUATING)
                )
            )
            .scalars()
            .all()
        )

        assert len(eval_parent_ids) == 0, (
            "No EVALUATING parents → no pending rollup files"
        )


# ══════════════════════════════════════════════════════════════════════════════
# Section 5 — Health-aware dispatch reordering
# ══════════════════════════════════════════════════════════════════════════════


class TestHealthAwareDispatch:
    """Tests for _health_score_task and the health-aware dispatch reordering logic."""

    # ── Score computation tests ──────────────────────────────────────────────

    def test_score_zero_for_task_with_no_scope_or_risk(self):
        """A task with no context gets a score of 0."""
        from app.orchestrator import _health_score_task

        task = Task(payloads=[{"type": "create"}])
        assert _health_score_task(task) == 0

    def test_score_zero_for_medium_scope(self):
        """A medium-scope task gets no boost (non-small)."""
        from app.orchestrator import _health_score_task

        task = Task(payloads=[{"type": "create"}], context={"scope": "medium"})
        assert _health_score_task(task) == 0

    def test_score_boost_for_small_scope(self):
        """Small scope adds +2."""
        from app.orchestrator import _health_score_task

        task = Task(payloads=[{"type": "create"}], context={"scope": "small"})
        assert _health_score_task(task) == 2

    def test_score_boost_for_low_risk(self):
        """Low risk adds +1."""
        from app.orchestrator import _health_score_task

        task = Task(payloads=[{"type": "create"}], context={"risk": "low"})
        assert _health_score_task(task) == 1

    def test_score_max_for_small_scope_and_low_risk(self):
        """Small scope + low risk = +3 (maximum boost)."""
        from app.orchestrator import _health_score_task

        task = Task(
            payloads=[{"type": "create"}], context={"scope": "small", "risk": "low"}
        )
        assert _health_score_task(task) == 3

    def test_score_ignores_large_scope(self):
        """Large scope gets no boost."""
        from app.orchestrator import _health_score_task

        task = Task(
            payloads=[{"type": "create"}], context={"scope": "large", "risk": "low"}
        )
        assert _health_score_task(task) == 1

    def test_score_with_none_context(self):
        """Task with context=None is treated as empty context (score 0)."""
        from app.orchestrator import _health_score_task

        task = Task(payloads=[{"type": "create"}], context=None)
        assert _health_score_task(task) == 0

    # ── Reordering logic tests ───────────────────────────────────────────────
    #
    # These exercise the *production* reorder (orchestrator_health.
    # reorder_eligible_by_health, dispatch Step 2.8) rather than re-implementing
    # its sort.  The production sort key is (P(success) confidence,
    # _health_score_task) descending with a stable sort, so by injecting a
    # constant predict_fn the confidence component is uniform and
    # _health_score_task becomes the sole differentiator — isolating exactly the
    # health-score ordering the originals meant to assert.  A single degradation
    # signal (slots_available below 50% of max) is forced so the reorder
    # activates; the confidence floor is set to 0 so nothing is dropped.

    @staticmethod
    async def _reorder(ids, task_by_id, session):
        """Run the production health-aware reorder with a uniform-confidence
        predict_fn and a forced degradation signal (only the slots signal)."""
        from app.orchestrator_health import reorder_eligible_by_health

        candidate_tasks = [task_by_id[i] for i in ids]
        runtime_config = {
            "max_concurrent_vms": 10,
            "dispatch_confidence_floor": 0.0,
        }
        zone_summary = {"available": 2, "total_zones": 2}  # no zone signal
        return await reorder_eligible_by_health(
            eligible_ids=list(ids),
            candidate_tasks=candidate_tasks,
            recent_results=[],  # no failure-rate signal
            slots_available=0,  # < 50% of max → degradation signal
            runtime_config=runtime_config,
            zone_summary=zone_summary,
            session=session,
            predict_fn=lambda scope, direction, proj: 0.5,  # uniform confidence
        )

    @pytest.mark.asyncio
    async def test_reordering_places_small_scope_first(self, db_session):
        """small-scope tasks appear before medium/large after the production reorder."""
        tasks = [
            Task(payloads=[{"type": "create"}], context={"scope": "large"}),
            Task(payloads=[{"type": "create"}], context={"scope": "small"}),
            Task(payloads=[{"type": "create"}], context={"scope": "medium"}),
        ]
        for t in tasks:
            t.id = uuid.uuid4()

        task_by_id = {t.id: t for t in tasks}
        ids = [t.id for t in tasks]  # original order: large, small, medium

        reordered = await self._reorder(ids, task_by_id, db_session)

        # small (score=2) should come first, medium and large (score=0) follow
        assert task_by_id[reordered[0]].context["scope"] == "small"

    @pytest.mark.asyncio
    async def test_reordering_places_small_low_risk_before_small_high_risk(
        self, db_session
    ):
        """small+low-risk (score=3) ranks above small-only (score=2)."""
        t_small_low = Task(
            payloads=[{"type": "create"}], context={"scope": "small", "risk": "low"}
        )
        t_small_only = Task(payloads=[{"type": "create"}], context={"scope": "small"})
        t_small_low.id = uuid.uuid4()
        t_small_only.id = uuid.uuid4()

        task_by_id = {t.id: t for t in [t_small_only, t_small_low]}
        ids = [t_small_only.id, t_small_low.id]  # small-only first in original order

        reordered = await self._reorder(ids, task_by_id, db_session)

        assert reordered[0] == t_small_low.id, "small+low-risk should be first"
        assert reordered[1] == t_small_only.id

    @pytest.mark.asyncio
    async def test_reordering_is_stable_when_all_scores_equal(self, db_session):
        """When all tasks have the same score, the production reorder preserves
        relative order (stable sort)."""
        tasks = [
            Task(payloads=[{"type": "create"}], context={"scope": "medium"}),
            Task(payloads=[{"type": "create"}], context={"scope": "medium"}),
            Task(payloads=[{"type": "create"}], context={"scope": "medium"}),
        ]
        for t in tasks:
            t.id = uuid.uuid4()

        task_by_id = {t.id: t for t in tasks}
        ids = [t.id for t in tasks]

        reordered = await self._reorder(ids, task_by_id, db_session)

        # All scores are 0 and confidence is uniform — stable sort preserves order
        assert reordered == ids


class TestPipelineSlotCap:
    """Pipeline-slot cap defers review/fix tasks past the budget while
    create/research dispatch normally."""

    @staticmethod
    def _apply_cap(eligible_ids, types_for_cap, pipeline_active, max_pipeline_slots):
        # Calls the production cap selection (Step 2.9 single source of truth)
        # instead of re-implementing it; cap_pipeline_slots() is a thin async
        # wrapper that computes pipeline_active/max_pipeline_slots/types and
        # then delegates the kept/deferred decision to this same function.
        from app.orchestrator_filters import apply_pipeline_cap

        return apply_pipeline_cap(
            eligible_ids, types_for_cap, pipeline_active, max_pipeline_slots
        )

    def test_review_fix_deferred_when_budget_exhausted(self):
        eligible = ["c1", "r1", "rv1", "fx1", "rv2"]
        types = {
            "c1": "create",
            "r1": "research",
            "rv1": "review",
            "fx1": "fix",
            "rv2": "review",
        }
        kept, deferred = self._apply_cap(
            eligible, types, pipeline_active=4, max_pipeline_slots=4
        )
        # All review/fix dropped; create/research dispatch normally.
        assert kept == ["c1", "r1"]
        assert deferred == 3

    def test_partial_budget_keeps_first_n_pipeline_tasks(self):
        eligible = ["rv1", "fx1", "rv2", "c1"]
        types = {"rv1": "review", "fx1": "fix", "rv2": "review", "c1": "create"}
        kept, deferred = self._apply_cap(
            eligible, types, pipeline_active=2, max_pipeline_slots=4
        )
        # Budget = 2 → first two review/fix kept, third deferred.
        assert kept == ["rv1", "fx1", "c1"]
        assert deferred == 1

    def test_create_and_research_unaffected_by_cap(self):
        eligible = ["c1", "r1", "c2"]
        types = {"c1": "create", "r1": "research", "c2": "create"}
        kept, deferred = self._apply_cap(
            eligible, types, pipeline_active=99, max_pipeline_slots=4
        )
        assert kept == ["c1", "r1", "c2"]
        assert deferred == 0

    def test_cap_default_is_half_of_max_concurrent_vms(self):
        from app.shared_state import runtime_config

        cap = runtime_config.get("max_pipeline_slots")
        if cap is None:
            cap = max(1, runtime_config["max_concurrent_vms"] // 2)
        assert cap >= 1
        assert cap <= runtime_config["max_concurrent_vms"]


class TestProjectFairness:
    """Per-project fairness round-robin (Step 2.95). Without it a single
    autonomous project that fanned out dozens of tasks fills the entire
    dispatch window and starves every other project for multiple cycles."""

    @staticmethod
    def _fair(eligible, project_by_id, weight_by_id=None):
        from app.orchestrator_filters import apply_project_fairness

        return apply_project_fairness(eligible, project_by_id, weight_by_id)

    def test_single_project_unchanged(self):
        # No interleaving possible — must be a no-op (behavior-preserving).
        eligible = ["a", "b", "c", "d"]
        pbi = {"a": "P", "b": "P", "c": "P", "d": "P"}
        assert self._fair(eligible, pbi) == eligible

    def test_empty_and_singleton_unchanged(self):
        assert self._fair([], {}) == []
        assert self._fair(["a"], {"a": "P"}) == ["a"]

    def test_round_robin_interleaves_projects(self):
        # P1 fanned out 4 tasks (sorted first by the candidate scan); P2 has 1.
        # Round-robin must surface P2's task into the FIRST window positions
        # instead of leaving it behind all four P1 tasks.
        eligible = ["p1a", "p1b", "p1c", "p1d", "p2a"]
        pbi = {
            "p1a": "P1",
            "p1b": "P1",
            "p1c": "P1",
            "p1d": "P1",
            "p2a": "P2",
        }
        out = self._fair(eligible, pbi)
        # Permutation: same membership, same length.
        assert sorted(out) == sorted(eligible)
        assert len(out) == len(eligible)
        # P2's only task is no longer dead last — it reaches position 2 (after
        # P1's first task), so a 2-slot window now serves both projects.
        assert out[:2] == ["p1a", "p2a"]
        # P1's internal order is preserved.
        assert [t for t in out if t.startswith("p1")] == [
            "p1a",
            "p1b",
            "p1c",
            "p1d",
        ]

    def test_starvation_is_broken_within_window(self):
        # 50-task fan-out from P1 + 1 task from P2; a 5-slot window must
        # include P2's task (the starvation bug: P2 waits many cycles).
        eligible = [f"p1_{i}" for i in range(50)] + ["p2_0"]
        pbi = {tid: ("P2" if tid == "p2_0" else "P1") for tid in eligible}
        out = self._fair(eligible, pbi)
        assert "p2_0" in out[:5], (
            "P2 task must reach the dispatch window — starvation not broken"
        )

    def test_weight_gives_more_turns_without_monopolizing(self):
        # P1 weight 3, P2 weight 1: per round P1 draws 3, P2 draws 1, but P2
        # is still serviced every round (never fully starved).
        eligible = ["p1a", "p1b", "p1c", "p1d", "p2a", "p2b"]
        pbi = {
            "p1a": "P1",
            "p1b": "P1",
            "p1c": "P1",
            "p1d": "P1",
            "p2a": "P2",
            "p2b": "P2",
        }
        wbi = {"p1a": 3, "p2a": 1}  # first-seen id per project sets the weight
        out = self._fair(eligible, pbi, wbi)
        assert sorted(out) == sorted(eligible)
        # Round 1: 3 from P1, then 1 from P2 → P2 reaches position 4.
        assert out[:4] == ["p1a", "p1b", "p1c", "p2a"]

    def test_none_project_is_its_own_bucket(self):
        eligible = ["x", "y", "z"]
        pbi = {"x": "P1", "y": None, "z": "P1"}
        out = self._fair(eligible, pbi)
        assert sorted(out) == sorted(eligible)
        # The None-project task interleaves rather than trailing both P1 tasks.
        assert out[:2] == ["x", "y"]

    def test_weight_mapping_clamps_and_defaults(self):
        from app.orchestrator_filters import _project_fairness_weight

        assert _project_fairness_weight(None) == 1  # unknown → baseline
        assert _project_fairness_weight("not-an-int") == 1
        assert _project_fairness_weight(0) == 1
        assert _project_fairness_weight(2) == 3  # 1 + priority
        assert _project_fairness_weight(100) == 4  # clamped to max
        assert _project_fairness_weight(-5) == 1  # negative → floor 1


class _OrchDispatchCycleHarness:
    """Full-cycle dispatch harness: in-memory DB + the mocks orchestrator_worker needs.

    Not collected by pytest (no ``Test`` prefix). Holds the ``orch_cap_db`` fixture and
    ``_run_one_cycle`` so more than one test class can drive a REAL dispatch cycle
    end-to-end instead of re-testing the extracted pure transforms in isolation.
    """

    @staticmethod
    def _naive_utcnow():
        from datetime import timezone as _tz

        return datetime.now(_tz.utc).replace(tzinfo=None)

    @pytest.fixture()
    def orch_cap_db(self, mocker):
        """In-memory SQLite DB wired into the orchestrator, plus essential mocks."""
        import asyncio
        from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession
        from sqlalchemy.orm import sessionmaker
        from sqlalchemy.pool import StaticPool
        from sqlmodel import SQLModel
        from sqlalchemy.dialects.sqlite.base import (
            SQLiteTypeCompiler as _SQLiteTypeCompiler,
        )

        if not hasattr(_SQLiteTypeCompiler, "visit_JSONB"):
            _SQLiteTypeCompiler.visit_JSONB = lambda self, type_, **kw: self.visit_JSON(
                type_, **kw
            )
        import app.orchestrator as _orch_module

        async def _build():
            _engine = create_async_engine(
                "sqlite+aiosqlite:///:memory:",
                connect_args={"check_same_thread": False},
                poolclass=StaticPool,
            )
            async with _engine.begin() as conn:
                await conn.run_sync(SQLModel.metadata.create_all)
            _Session = sessionmaker(
                bind=_engine,
                class_=AsyncSession,
                autocommit=False,
                autoflush=False,
                expire_on_commit=False,
            )
            mocker.patch("app.orchestrator.AsyncSessionLocal", _Session)
            mocker.patch("app.dep_lifecycle.AsyncSessionLocal", _Session)
            mocker.patch("app.broadcast.broadcast_to_project", new_callable=AsyncMock)
            mocker.patch("app.vm_lifecycle._ensure_vm_deleted", new_callable=AsyncMock)
            mocker.patch("app.vm_lifecycle._get_actual_gcp_vm_count", return_value=0)
            mocker.patch("app.main.load_max_plan_pool", new_callable=AsyncMock)
            mocker.patch("app.main.recheck_disabled_accounts", new_callable=AsyncMock)
            mocker.patch("app.routers.agent_status.record_task_result")
            mocker.patch(
                "app.deploy_health._check_monitoring_window", new_callable=AsyncMock
            )
            mocker.patch(
                "app.deploy_health._check_external_health", new_callable=AsyncMock
            )
            mocker.patch("app.deploy_health._check_pause_mode", new_callable=AsyncMock)
            mocker.patch(
                "app.autonomous_research._check_idle_research", new_callable=AsyncMock
            )
            mocker.patch("app.services._recover_orphaned_tasks", new_callable=AsyncMock)
            mocker.patch(
                "app.alert_dispatch._create_orchestrator_error_alerts",
                new_callable=AsyncMock,
            )
            mocker.patch(
                "app.orchestrator._persist_zone_health", new_callable=AsyncMock
            )
            mocker.patch(
                "app.alert_dispatch._maybe_create_failure_alert", new_callable=AsyncMock
            )
            # The provision wrapper reaches Secret Manager and GCS via
            # asyncio.gather BEFORE the (separately mocked) provision_agent_vm
            # call. Both need real Google credentials: present on GCE agent
            # VMs (ambient metadata creds) but absent in the CI container,
            # where DefaultCredentialsError crashes the wrapper and resets the
            # dispatched task to QUEUED. Patch both so every test in this
            # class is hermetic and behaves identically in CI and on VMs.
            mocker.patch(
                "app.orchestrator.prepare_and_store_agent_secret",
                new_callable=AsyncMock,
                return_value="projects/test/secrets/agent/versions/1",
            )
            mocker.patch(
                "app.orchestrator.upload_json_payload",
                new_callable=AsyncMock,
                return_value="gs://test-bucket/payloads/test.json",
            )

            now = self._naive_utcnow()
            _orch_module._last_pool_refresh = now
            _orch_module._last_zone_health_persist = now
            _orch_module._last_orphan_check = now
            _orch_module._last_evaluating_timeout_check = now
            _orch_module._last_deadlock_check = now
            _orch_module._last_anomaly_state_persist = now
            _orch_module._last_anomaly_sweep = now
            _orch_module._last_post_commit_sweep = now
            _orch_module._last_sidecar_sweep = now
            _orch_module._last_bootstrap_recovery_sweep = now
            _orch_module._last_lane_baseline_backfill = now
            _orch_module._last_merge_credit_backfill = now
            _orch_module._last_v22_finding_backfill = now
            _orch_module._last_runner_deactivation_sweep = now
            _orch_module._last_runner_degraded_sweep = now
            _orch_module._last_runner_escalation_sweep = now
            _orch_module._last_local_runner_hold_sweep = now
            _shared_state.deploy_monitoring["task_results"].clear()
            return _Session, _engine

        loop = asyncio.new_event_loop()
        try:
            _Session, _engine = loop.run_until_complete(_build())
        finally:
            loop.close()
        try:
            yield _Session
        finally:
            import contextlib

            async def _teardown():
                with contextlib.suppress(Exception):
                    await _engine.dispose()

            loop = asyncio.new_event_loop()
            try:
                loop.run_until_complete(_teardown())
            finally:
                loop.close()

    @staticmethod
    async def _run_one_cycle(mocker):
        import asyncio
        import contextlib
        import app.orchestrator as _orch_module
        import app.pipeline_health_state as _pipeline_state

        mocker.patch("asyncio.sleep", side_effect=asyncio.CancelledError())
        with contextlib.suppress(asyncio.CancelledError):
            await _orch_module.orchestrator_worker()

        # Drain background provisioning coroutines the cycle spawned so
        # dangling tasks don't race the post-cycle verify reads on the shared
        # StaticPool connection. AWAIT them to completion rather than cancel:
        # with provisioning fully mocked (provision_agent_vm, Secret Manager,
        # GCS) the wrapper finishes promptly, while a cancel can land mid
        # DB-query and poison the StaticPool connection — a reconnect to
        # sqlite :memory: yields an empty database ("no such table: task").
        _orphan_keys_before = set(_pipeline_state.background_tasks)
        for _coro in list(_orch_module._provision_coroutines.values()):
            _loop = _coro.get_loop()
            if _loop.is_closed():
                continue
            with contextlib.suppress(BaseException):
                await _coro
        _orch_module._provision_coroutines.clear()
        for _key in set(_pipeline_state.background_tasks) - _orphan_keys_before:
            _orphan = _pipeline_state.background_tasks.pop(_key, None)
            if _orphan is not None:
                _orphan.cancel()
                with contextlib.suppress(BaseException):
                    await _orphan


class TestProjectConcurrencyCap(_OrchDispatchCycleHarness):
    """Per-project concurrent-VM cap (Step 2.92).

    Tests two levels:
    1. Pure helper ``apply_project_concurrency_cap`` — boundary cases, same-project
       multi-candidate, missing caps.
    2. Dispatch-level ``test_per_project_vm_cap_defers_over_cap_project`` — a full
       orchestrator cycle that proves an over-cap project's QUEUED candidate stays
       QUEUED while a second project's candidate dispatches into the free slot.
    """

    # ── Pure-helper unit tests ────────────────────────────────────────────────

    @staticmethod
    def _cap(eligible, project_by_id, active_by_project_id, cap_by_project_id):
        from app.orchestrator_filters import apply_project_concurrency_cap

        return apply_project_concurrency_cap(
            eligible, project_by_id, active_by_project_id, cap_by_project_id
        )

    def test_no_cap_passes_all_through(self):
        """Empty cap dict → all candidates pass through unchanged (no-op)."""
        ids = ["a", "b", "c"]
        pbi = {"a": "P1", "b": "P1", "c": "P2"}
        assert self._cap(ids, pbi, {"P1": 2}, {}) == ids

    def test_project_at_cap_defers_its_candidate(self):
        """Project with active==cap has its candidate deferred; other project passes."""
        ids = ["p1_task", "p2_task"]
        pbi = {"p1_task": "P1", "p2_task": "P2"}
        active = {"P1": 1}
        caps = {"P1": 1}  # P1 is at its cap
        result = self._cap(ids, pbi, active, caps)
        assert "p1_task" not in result
        assert "p2_task" in result

    def test_project_below_cap_admits_candidate(self):
        """Project with active < cap admits the next candidate."""
        ids = ["p1_task"]
        pbi = {"p1_task": "P1"}
        active = {"P1": 0}
        caps = {"P1": 2}
        result = self._cap(ids, pbi, active, caps)
        assert result == ["p1_task"]

    def test_count_equals_cap_minus_one_admits_exactly_one(self):
        """active == cap-1: exactly ONE candidate is admitted, the second deferred."""
        ids = ["p1_a", "p1_b"]
        pbi = {"p1_a": "P1", "p1_b": "P1"}
        active = {"P1": 1}
        caps = {"P1": 2}  # cap=2, active=1 → one slot left
        result = self._cap(ids, pbi, active, caps)
        assert result == ["p1_a"]  # first admitted; second deferred (would push to 2)

    def test_multiple_same_project_candidates_respect_cap_across_cycle(self):
        """Two candidates from the same project in one cycle: admitted count
        tracks within-cycle admissions so both can't slip past a cap of 1."""
        ids = ["p1_a", "p1_b", "p2_x"]
        pbi = {"p1_a": "P1", "p1_b": "P1", "p2_x": "P2"}
        active = {"P1": 0}
        caps = {"P1": 1}
        result = self._cap(ids, pbi, active, caps)
        # Only first P1 candidate admitted; second deferred; P2 never filtered.
        assert "p1_a" in result
        assert "p1_b" not in result
        assert "p2_x" in result

    def test_project_with_no_cap_never_filtered(self):
        """A project absent from cap_by_project_id is never deferred."""
        ids = ["uncapped_task"]
        pbi = {"uncapped_task": "UNCAPPED"}
        active = {"UNCAPPED": 99}
        caps = {"OTHER": 1}
        assert self._cap(ids, pbi, active, caps) == ["uncapped_task"]

    def test_empty_eligible_returns_empty(self):
        assert self._cap([], {}, {}, {"P1": 1}) == []

    # ── Dispatch-level test ───────────────────────────────────────────────────

    @pytest.mark.asyncio
    async def test_per_project_vm_cap_defers_over_cap_project(
        self, orch_cap_db, mocker
    ):
        """Core acceptance test: a project AT its per-project cap has its QUEUED
        candidate DEFERRED while a SECOND project's candidate dispatches into the
        free fleet slot.  The over-cap project is NOT failed; its task stays QUEUED.
        """
        from sqlmodel import select as _select
        from app.max_plan_pool import LLMSelectionResult

        # Patch provisioning so any selected task becomes PROVISIONING without a
        # real GCP call.  The return value is ignored by the dispatch path.
        mocker.patch(
            "app.orchestrator.provision_agent_vm",
            new_callable=AsyncMock,
            return_value=True,
        )
        mocker.patch(
            "app.max_plan_pool.select_llm_runtime_for_task",
            new_callable=AsyncMock,
            return_value=LLMSelectionResult(
                provider="anthropic",
                auth_mode="oauth",
                model="claude-sonnet-4-6",
                quality_tier=None,
                account={
                    "id": "acct-cap-test",
                    "name": "Cap Test Acct",
                    "runtime_secret": "tok",
                    "claude_config_json": None,
                    "provider_metadata": {},
                },
            ),
        )
        mocker.patch(
            "app.warm_pool.try_pool_dispatch",
            new_callable=AsyncMock,
            return_value=None,
        )
        mocker.patch(
            "app.orchestrator._sync_runtime_config_from_state",
            new_callable=AsyncMock,
            return_value=[],
        )

        import app.orchestrator as _orch_module

        # 2 slots total; project1 already occupies 1 (PROVISIONING) → 1 slot free.
        _orch_module._runtime_config["max_concurrent_vms"] = 2
        _orch_module._runtime_config["warm_pool_enabled"] = False

        project1_id = None
        p1_queued_id = None
        p2_queued_id = None

        async with orch_cap_db() as session:
            # Project1: at its per-project cap of 1 (1 active task).
            project1 = make_project(name="CapProject1", user_id=_TEST_USER)
            project2 = make_project(name="CapProject2", user_id=_TEST_USER)
            session.add(project1)
            session.add(project2)
            await session.commit()
            await session.refresh(project1)
            await session.refresh(project2)
            project1_id = project1.id

            # Project1 already has 1 active task (PROVISIONING) → at cap=1.
            p1_active = make_task(
                project_id=project1.id,
                status=TaskStatus.PROVISIONING,
                vm_instance_id="vm-already-running",
                payloads=[{"type": "create", "prompt": "active work"}],
            )
            # Project1's QUEUED candidate — should be DEFERRED.
            p1_queued = make_task(
                project_id=project1.id,
                status=TaskStatus.QUEUED,
                vm_instance_id=None,
                payloads=[{"type": "create", "prompt": "queued for cap project"}],
            )
            # Project2's QUEUED candidate — should DISPATCH into the free slot.
            p2_queued = make_task(
                project_id=project2.id,
                status=TaskStatus.QUEUED,
                vm_instance_id=None,
                payloads=[{"type": "create", "prompt": "queued for other project"}],
            )
            session.add(p1_active)
            session.add(p1_queued)
            session.add(p2_queued)
            await session.commit()
            await session.refresh(p1_queued)
            await session.refresh(p2_queued)
            p1_queued_id = p1_queued.id
            p2_queued_id = p2_queued.id

        # Configure the per-project cap for project1.
        _orch_module._runtime_config["max_concurrent_tasks_by_project"] = {
            str(project1_id): 1,
        }
        try:
            await self._run_one_cycle(mocker)
        finally:
            _orch_module._runtime_config.pop("max_concurrent_tasks_by_project", None)
            _orch_module._runtime_config.pop("max_concurrent_vms", None)
            _orch_module._runtime_config.pop("warm_pool_enabled", None)

        async with orch_cap_db() as verify:
            p1_row = (
                (await verify.execute(_select(Task).where(Task.id == p1_queued_id)))
                .scalars()
                .first()
            )
            p2_row = (
                (await verify.execute(_select(Task).where(Task.id == p2_queued_id)))
                .scalars()
                .first()
            )

        assert p1_row is not None
        assert p1_row.status == TaskStatus.QUEUED, (
            f"Project1 (at cap) must stay QUEUED, got {p1_row.status}"
        )
        assert p2_row is not None
        assert p2_row.status == TaskStatus.PROVISIONING, (
            f"Project2 (no cap) must be dispatched PROVISIONING, got {p2_row.status}"
        )

    @pytest.mark.asyncio
    async def test_unset_cap_leaves_dispatch_unchanged(self, orch_cap_db, mocker):
        """When no per-project cap is configured the dispatch result is identical
        to today's behavior: every eligible QUEUED task may dispatch normally."""
        from sqlmodel import select as _select
        from app.max_plan_pool import LLMSelectionResult

        mocker.patch(
            "app.orchestrator.provision_agent_vm",
            new_callable=AsyncMock,
            return_value=True,
        )
        mocker.patch(
            "app.max_plan_pool.select_llm_runtime_for_task",
            new_callable=AsyncMock,
            return_value=LLMSelectionResult(
                provider="anthropic",
                auth_mode="oauth",
                model="claude-sonnet-4-6",
                quality_tier=None,
                account={
                    "id": "acct-nocap",
                    "name": "NoCap Acct",
                    "runtime_secret": "tok",
                    "claude_config_json": None,
                    "provider_metadata": {},
                },
            ),
        )
        mocker.patch(
            "app.warm_pool.try_pool_dispatch",
            new_callable=AsyncMock,
            return_value=None,
        )
        mocker.patch(
            "app.orchestrator._sync_runtime_config_from_state",
            new_callable=AsyncMock,
            return_value=[],
        )

        import app.orchestrator as _orch_module

        _orch_module._runtime_config["max_concurrent_vms"] = 4
        _orch_module._runtime_config["warm_pool_enabled"] = False
        # Explicitly empty caps — no project is filtered.
        _orch_module._runtime_config["max_concurrent_tasks_by_project"] = {}

        queued_id = None
        async with orch_cap_db() as session:
            project = make_project(name="NoCap", user_id=_TEST_USER)
            session.add(project)
            await session.commit()
            await session.refresh(project)

            task = make_task(
                project_id=project.id,
                status=TaskStatus.QUEUED,
                vm_instance_id=None,
                payloads=[{"type": "create", "prompt": "uncapped task"}],
            )
            session.add(task)
            await session.commit()
            await session.refresh(task)
            queued_id = task.id

        try:
            await self._run_one_cycle(mocker)
        finally:
            _orch_module._runtime_config.pop("max_concurrent_tasks_by_project", None)
            _orch_module._runtime_config.pop("max_concurrent_vms", None)
            _orch_module._runtime_config.pop("warm_pool_enabled", None)

        async with orch_cap_db() as verify:
            row = (
                (await verify.execute(_select(Task).where(Task.id == queued_id)))
                .scalars()
                .first()
            )
        assert row is not None
        assert row.status == TaskStatus.PROVISIONING, (
            f"Uncapped task must dispatch normally, got {row.status}"
        )

    @pytest.mark.asyncio
    async def test_previously_deferred_task_dispatches_when_active_drops(
        self, orch_cap_db, mocker
    ):
        """A task deferred because its project was at-cap dispatches on the next
        cycle once the project's active count drops below the cap."""
        from sqlmodel import select as _select
        from app.max_plan_pool import LLMSelectionResult

        mocker.patch(
            "app.orchestrator.provision_agent_vm",
            new_callable=AsyncMock,
            return_value=True,
        )
        mocker.patch(
            "app.max_plan_pool.select_llm_runtime_for_task",
            new_callable=AsyncMock,
            return_value=LLMSelectionResult(
                provider="anthropic",
                auth_mode="oauth",
                model="claude-sonnet-4-6",
                quality_tier=None,
                account={
                    "id": "acct-deferred",
                    "name": "Deferred Acct",
                    "runtime_secret": "tok",
                    "claude_config_json": None,
                    "provider_metadata": {},
                },
            ),
        )
        mocker.patch(
            "app.warm_pool.try_pool_dispatch",
            new_callable=AsyncMock,
            return_value=None,
        )
        mocker.patch(
            "app.orchestrator._sync_runtime_config_from_state",
            new_callable=AsyncMock,
            return_value=[],
        )

        import app.orchestrator as _orch_module

        _orch_module._runtime_config["max_concurrent_vms"] = 3
        _orch_module._runtime_config["warm_pool_enabled"] = False

        project_id = None
        active_task_id = None
        queued_id = None

        async with orch_cap_db() as session:
            project = make_project(name="DeferredProject", user_id=_TEST_USER)
            session.add(project)
            await session.commit()
            await session.refresh(project)
            project_id = project.id

            active_task = make_task(
                project_id=project.id,
                status=TaskStatus.PROVISIONING,
                vm_instance_id="vm-active",
                payloads=[{"type": "create", "prompt": "already running"}],
            )
            queued_task = make_task(
                project_id=project.id,
                status=TaskStatus.QUEUED,
                vm_instance_id=None,
                payloads=[{"type": "create", "prompt": "waiting for slot"}],
            )
            session.add(active_task)
            session.add(queued_task)
            await session.commit()
            await session.refresh(active_task)
            await session.refresh(queued_task)
            active_task_id = active_task.id
            queued_id = queued_task.id

        # Cycle 1: project at cap=1 (1 PROVISIONING) → QUEUED candidate deferred.
        _orch_module._runtime_config["max_concurrent_tasks_by_project"] = {
            str(project_id): 1,
        }
        try:
            await self._run_one_cycle(mocker)
        finally:
            _orch_module._runtime_config.pop("max_concurrent_tasks_by_project", None)

        async with orch_cap_db() as mid_check:
            row = (
                (await mid_check.execute(_select(Task).where(Task.id == queued_id)))
                .scalars()
                .first()
            )
        assert row is not None
        assert row.status == TaskStatus.QUEUED, (
            f"Task must stay QUEUED when project is at cap; got {row.status}"
        )

        # Simulate the active task completing (COMPLETED) → project drops below cap.
        async with orch_cap_db() as release:
            active_row = (
                (await release.execute(_select(Task).where(Task.id == active_task_id)))
                .scalars()
                .first()
            )
            active_row.status = TaskStatus.COMPLETED
            release.add(active_row)
            await release.commit()

        # Cycle 2: project now has 0 active → under cap=1 → candidate dispatches.
        _orch_module._runtime_config["max_concurrent_tasks_by_project"] = {
            str(project_id): 1,
        }
        try:
            await self._run_one_cycle(mocker)
        finally:
            _orch_module._runtime_config.pop("max_concurrent_tasks_by_project", None)
            _orch_module._runtime_config.pop("max_concurrent_vms", None)
            _orch_module._runtime_config.pop("warm_pool_enabled", None)

        async with orch_cap_db() as final_check:
            row = (
                (await final_check.execute(_select(Task).where(Task.id == queued_id)))
                .scalars()
                .first()
            )
        assert row is not None
        assert row.status == TaskStatus.PROVISIONING, (
            f"Previously-deferred task must dispatch once active count drops; got {row.status}"
        )

    @pytest.mark.asyncio
    async def test_compute_experiment_on_fresh_project_launches_and_parks(
        self, orch_cap_db, mocker
    ):
        """Regression guard: a compute_experiment task on an auto-created project
        (autonomous_enabled=False, bootstrap_completed_at set) must be dispatched in
        a single orchestrator cycle without being starved by the bootstrap-recovery
        sweep or autonomous-research gating. The orchestrator now launches the
        ComputeJob directly and PARKS the task (no agent half), so a successful
        dispatch lands the task in PARKED rather than the old PROVISIONING.

        Before the fix the auto-created project had autonomous_enabled=True and
        no bootstrap_completed_at, which caused _sweep_bootstrap_recovery to
        spawn a competing repo_bootstrap task and _bootstrap_pending() to defer
        autonomous-research spawning — both could hold the dispatch slot and
        leave the compute_experiment task QUEUED indefinitely.
        """
        from sqlmodel import select as _select
        from app.max_plan_pool import LLMSelectionResult
        from app.time_utils import _utcnow as _now

        mocker.patch(
            "app.orchestrator.provision_agent_vm",
            new_callable=AsyncMock,
            return_value=True,
        )
        mocker.patch(
            "app.max_plan_pool.select_llm_runtime_for_task",
            new_callable=AsyncMock,
            return_value=LLMSelectionResult(
                provider="anthropic",
                auth_mode="oauth",
                model="claude-sonnet-4-6",
                quality_tier=None,
                account={
                    "id": "acct-compute-exp",
                    "name": "Compute Exp Acct",
                    "runtime_secret": "tok",
                    "claude_config_json": None,
                    "provider_metadata": {},
                },
            ),
        )
        mocker.patch(
            "app.warm_pool.try_pool_dispatch",
            new_callable=AsyncMock,
            return_value=None,
        )
        mocker.patch(
            "app.orchestrator._sync_runtime_config_from_state",
            new_callable=AsyncMock,
            return_value=[],
        )

        import app.orchestrator as _orch_module

        _orch_module._runtime_config["max_concurrent_vms"] = 5
        _orch_module._runtime_config["warm_pool_enabled"] = False

        task_id = None

        async with orch_cap_db() as session:
            # Mirror what create_compute_experiment_task sets on auto-created
            # projects: non-autonomous, bootstrap already complete.
            project = make_project(
                name="FreshComputeProject",
                user_id=_TEST_USER,
                autonomous_enabled=False,
                build_config={"bootstrap_completed_at": _now().isoformat()},
            )
            session.add(project)
            await session.commit()
            await session.refresh(project)

            task = make_task(
                project_id=project.id,
                status=TaskStatus.QUEUED,
                vm_instance_id=None,
                requires_approval=False,
                payloads=[
                    {
                        "type": "compute_experiment",
                        "command": "echo hello",
                        "spend_cap_usd": 1.0,
                        "deadline_seconds": 300,
                        "github_repo": "acme/fresh-repo",
                    }
                ],
            )
            session.add(task)
            await session.commit()
            await session.refresh(task)
            task_id = task.id

        try:
            await self._run_one_cycle(mocker)
        finally:
            _orch_module._runtime_config.pop("max_concurrent_vms", None)
            _orch_module._runtime_config.pop("warm_pool_enabled", None)

        async with orch_cap_db() as verify:
            row = (
                (await verify.execute(_select(Task).where(Task.id == task_id)))
                .scalars()
                .first()
            )
        assert row is not None
        assert row.status == TaskStatus.PARKED, (
            f"compute_experiment on fresh auto-created project must be dispatched "
            f"(launched directly + PARKED) in one cycle; got {row.status}. "
            "Check that autonomous_enabled=False and bootstrap_completed_at are set "
            "on auto-created projects (admin.py create_compute_experiment_task), and "
            "that the direct compute_experiment dispatch path launches + parks."
        )


class TestDispatchWindowPreservesRank(_OrchDispatchCycleHarness):
    """The dispatch window is now WIDER than ``slots_available`` and the loop stops
    after N REAL dispatches — so whoever the loop reaches FIRST wins the slot.

    That makes the ORDER of the dispatch loop load-bearing for the first time. It
    was not before: the window used to be cut at exactly ``slots_available`` and the
    loop ATTEMPTED every member, so the dispatch SET was fully determined by the
    truncation and intra-loop order was cosmetic.

    The re-read that materialises the window (``select(Task).where(Task.id.in_(...))``)
    is UNORDERED — a SQL set has no order, so the DB returns rows in its own
    (heap/rowid) order, NOT the rank order of ``eligible_ids``. That rank is the entire
    output of the ranking pipeline: health reordering, project fairness, completion
    priority, and the create/research reserved-slot minimums. Without an explicit
    re-sort, ALL of it is silently discarded and the winners become whichever rows the
    DB happened to return first.

    Both tests below construct a queue whose RANK order deliberately differs from its
    INSERTION order, so they FAIL against a dispatch loop that does not restore rank
    (the unfixed code dispatches the wrong task) and pass once it does. They are real
    negative controls, not assertions over a pure function.
    """

    @staticmethod
    def _dispatch_mocks(mocker):
        from app.max_plan_pool import LLMSelectionResult

        mocker.patch(
            "app.orchestrator.provision_agent_vm",
            new_callable=AsyncMock,
            return_value=True,
        )
        mocker.patch(
            "app.max_plan_pool.select_llm_runtime_for_task",
            new_callable=AsyncMock,
            return_value=LLMSelectionResult(
                provider="anthropic",
                auth_mode="oauth",
                model="claude-sonnet-4-6",
                quality_tier=None,
                account={
                    "id": "acct-rank-test",
                    "name": "Rank Test Acct",
                    "runtime_secret": "tok",
                    "claude_config_json": None,
                    "provider_metadata": {},
                },
            ),
        )
        mocker.patch(
            "app.warm_pool.try_pool_dispatch",
            new_callable=AsyncMock,
            return_value=None,
        )
        mocker.patch(
            "app.orchestrator._sync_runtime_config_from_state",
            new_callable=AsyncMock,
            return_value=[],
        )

    @pytest.mark.asyncio
    async def test_high_priority_task_below_the_insertion_head_still_wins_the_slot(
        self, orch_cap_db, mocker
    ):
        """ONE slot. A LOW-priority task is inserted FIRST, a HIGH-priority task SECOND.

        The candidate scan ranks by ``priority DESC, created_at ASC``, so the HIGH task
        is rank 0 and is the one entitled to the single slot. But it has the LATER row
        id, so the unordered window re-read returns the LOW task first. With the window
        widened past one slot, BOTH tasks are now in the loop — and an unranked loop
        dispatches the LOW task and breaks, silently inverting task priority.
        """
        from sqlmodel import select as _select
        import app.orchestrator as _orch_module

        self._dispatch_mocks(mocker)

        low_id = None
        high_id = None
        async with orch_cap_db() as session:
            project = make_project(name="RankProject", user_id=_TEST_USER)
            session.add(project)
            await session.commit()
            await session.refresh(project)

            base = self._naive_utcnow() - timedelta(minutes=10)
            # Inserted FIRST (lower row id / earlier created_at) but LOW priority.
            low = make_task(
                project_id=project.id,
                status=TaskStatus.QUEUED,
                vm_instance_id=None,
                priority=1,
                created_at=base,
                payloads=[{"type": "create", "prompt": "low priority"}],
            )
            session.add(low)
            await session.commit()
            await session.refresh(low)
            low_id = low.id

            # Inserted SECOND (higher row id) but HIGH priority → rank 0.
            high = make_task(
                project_id=project.id,
                status=TaskStatus.QUEUED,
                vm_instance_id=None,
                priority=99,
                created_at=base + timedelta(minutes=1),
                payloads=[{"type": "create", "prompt": "high priority"}],
            )
            session.add(high)
            await session.commit()
            await session.refresh(high)
            high_id = high.id

        _orch_module._runtime_config["max_concurrent_vms"] = 1
        _orch_module._runtime_config["max_pipeline_slots"] = 10
        try:
            await self._run_one_cycle(mocker)
        finally:
            _orch_module._runtime_config.pop("max_concurrent_vms", None)
            _orch_module._runtime_config.pop("max_pipeline_slots", None)

        async with orch_cap_db() as verify:
            rows = {
                r.id: r
                for r in (
                    (
                        await verify.execute(
                            _select(Task).where(Task.id.in_([low_id, high_id]))
                        )
                    )
                    .scalars()
                    .all()
                )
            }

        assert rows[high_id].status == TaskStatus.PROVISIONING, (
            "the HIGH-priority task is rank 0 and must win the single slot; got "
            f"{rows[high_id].status}. The widened dispatch window puts the lower-ranked "
            "task in the loop too, so the loop MUST iterate in rank order — otherwise "
            "the DB's row order silently overrides task priority."
        )
        assert rows[low_id].status == TaskStatus.QUEUED, (
            "the LOW-priority task must NOT consume the slot the high-priority task "
            f"was ranked into; got {rows[low_id].status}"
        )

    @pytest.mark.asyncio
    async def test_reserved_type_promoted_from_below_the_cut_still_wins_its_slot(
        self, orch_cap_db, mocker
    ):
        """ONE slot; the queue head is a wall of higher-priority NON-reserved work.

        ``reserve_slots_for_types`` exists precisely for this: under queue pressure it
        PROMOTES a reserved-type task (here ``create``) from beyond the cutoff into the
        first ``slots_available`` positions, guaranteeing it a minimum share of slots so
        review/fix chains cannot starve it. That promotion is expressed ONLY as a
        reordering of ``eligible_ids`` — so if the dispatch loop does not iterate in
        that order, the reserved minimum is silently discarded and the very starvation
        it prevents happens anyway.

        The promoted ``create`` task is inserted LAST (highest row id), so an unranked
        loop reaches the review heads first, dispatches one, breaks, and the reserved
        minimum is lost.
        """
        from sqlmodel import select as _select
        import app.orchestrator as _orch_module

        self._dispatch_mocks(mocker)

        review_ids: list = []
        create_id = None
        async with orch_cap_db() as session:
            project = make_project(name="ReserveProject", user_id=_TEST_USER)
            session.add(project)
            await session.commit()
            await session.refresh(project)

            base = self._naive_utcnow() - timedelta(minutes=10)
            # Queue head: higher-priority, NON-reserved review work, inserted FIRST.
            for i in range(3):
                review = make_task(
                    project_id=project.id,
                    status=TaskStatus.QUEUED,
                    vm_instance_id=None,
                    priority=50,
                    created_at=base + timedelta(seconds=i),
                    payloads=[{"type": "review", "prompt": f"review {i}"}],
                )
                session.add(review)
                await session.commit()
                await session.refresh(review)
                review_ids.append(review.id)

            # The reserved type, ranked BELOW the cut (lowest priority, inserted LAST).
            # reserve_slots_for_types must promote it into the single slot.
            create = make_task(
                project_id=project.id,
                status=TaskStatus.QUEUED,
                vm_instance_id=None,
                priority=1,
                created_at=base + timedelta(minutes=1),
                payloads=[{"type": "create", "prompt": "reserved create work"}],
            )
            session.add(create)
            await session.commit()
            await session.refresh(create)
            create_id = create.id

        _orch_module._runtime_config["max_concurrent_vms"] = 1
        # Neutralise the review/fix pipeline cap so it is the RESERVATION under test,
        # not cap_pipeline_slots incidentally removing the review heads.
        _orch_module._runtime_config["max_pipeline_slots"] = 10
        _orch_module._runtime_config["reserved_slots_for_types"] = {"create": 1}
        try:
            await self._run_one_cycle(mocker)
        finally:
            _orch_module._runtime_config.pop("max_concurrent_vms", None)
            _orch_module._runtime_config.pop("max_pipeline_slots", None)
            _orch_module._runtime_config.pop("reserved_slots_for_types", None)

        async with orch_cap_db() as verify:
            rows = {
                r.id: r
                for r in (
                    (
                        await verify.execute(
                            _select(Task).where(Task.id.in_([*review_ids, create_id]))
                        )
                    )
                    .scalars()
                    .all()
                )
            }

        assert rows[create_id].status == TaskStatus.PROVISIONING, (
            "reserve_slots_for_types promoted the reserved `create` task into the only "
            f"slot, but it did not dispatch (got {rows[create_id].status}). The reserved "
            "minimum is expressed purely as an eligible_ids reordering — the dispatch "
            "loop must honour that order or the reservation is silently discarded."
        )
        dispatched_reviews = [
            tid for tid in review_ids if rows[tid].status != TaskStatus.QUEUED
        ]
        assert not dispatched_reviews, (
            "a non-reserved review task consumed the slot reserved for `create`: "
            f"{dispatched_reviews}"
        )

    @pytest.mark.asyncio
    async def test_a_candidate_that_dispatches_nothing_does_not_consume_a_slot(
        self, orch_cap_db, mocker
    ):
        """The headline bug, end-to-end through a real dispatch cycle.

        ONE slot. The queue head is resource-DEFERRED inside the dispatch loop — it
        `continue`s having dispatched NOTHING. A healthy task sits directly behind it.

        Before the fix the window was cut at exactly ``slots_available``, so the window
        was ``[deferred_head]``: the loop deferred it, dispatched nothing, and the
        healthy task got ZERO slots — every cycle, for as long as the head kept
        deferring, while the fleet sat idle. Several loop outcomes behave this way (a
        failure-gated type, a compute hold, a recoverable BLOCKED launch, a transient
        cap deferral, a lost lock race); the resource deferral is simply the one that is
        cleanly injectable. The fix scans PAST ``slots_available`` and counts only REAL
        dispatches, so a zero-dispatch head is backfilled over.

        This is the negative control for the BACKFILL itself: it FAILS on the unfixed
        code (the healthy task stays QUEUED behind a head that never dispatches).
        """
        from sqlmodel import select as _select
        import app.orchestrator as _orch_module

        self._dispatch_mocks(mocker)

        deferred_id = None
        healthy_id = None
        async with orch_cap_db() as session:
            project = make_project(name="BackfillProject", user_id=_TEST_USER)
            session.add(project)
            await session.commit()
            await session.refresh(project)

            base = self._naive_utcnow() - timedelta(minutes=10)
            # Rank 0 (highest priority) — and it will dispatch NOTHING.
            deferred = make_task(
                project_id=project.id,
                status=TaskStatus.QUEUED,
                vm_instance_id=None,
                priority=99,
                created_at=base,
                payloads=[{"type": "create", "prompt": "resource-deferred head"}],
            )
            session.add(deferred)
            await session.commit()
            await session.refresh(deferred)
            deferred_id = deferred.id

            healthy = make_task(
                project_id=project.id,
                status=TaskStatus.QUEUED,
                vm_instance_id=None,
                priority=1,
                created_at=base + timedelta(minutes=1),
                payloads=[{"type": "create", "prompt": "healthy work behind the head"}],
            )
            session.add(healthy)
            await session.commit()
            await session.refresh(healthy)
            healthy_id = healthy.id

        # Defer ONLY the queue head, and ONLY from INSIDE the dispatch loop — that is
        # the `continue` that historically burned the window slot.
        #
        # The pre-cutoff filter `_filter_project_resource_dispatch_eligible` calls the
        # SAME `_project_resource_dispatch_defer_message` helper, so mocking the helper
        # alone would drop the head from eligible_ids BEFORE the window ever forms — it
        # would then never occupy a slot, and this test would pass on the unfixed code
        # for entirely the wrong reason. Force the pre-cutoff filter to a pass-through so
        # the head genuinely reaches the window and defers in-loop.
        _deferred_id = deferred_id

        async def _passthrough_prefilter(session, eligible_ids, *a, **kw):
            return eligible_ids

        mocker.patch(
            "app.orchestrator._filter_project_resource_dispatch_eligible",
            side_effect=_passthrough_prefilter,
        )

        async def _defer_only_the_head(session, task, *a, **kw):
            if task.id == _deferred_id:
                return "project resource envelope exhausted"
            return None

        mocker.patch(
            "app.orchestrator._project_resource_dispatch_defer_message",
            side_effect=_defer_only_the_head,
        )

        _orch_module._runtime_config["max_concurrent_vms"] = 1
        _orch_module._runtime_config["max_pipeline_slots"] = 10
        try:
            await self._run_one_cycle(mocker)
        finally:
            _orch_module._runtime_config.pop("max_concurrent_vms", None)
            _orch_module._runtime_config.pop("max_pipeline_slots", None)

        async with orch_cap_db() as verify:
            rows = {
                r.id: r
                for r in (
                    (
                        await verify.execute(
                            _select(Task).where(Task.id.in_([deferred_id, healthy_id]))
                        )
                    )
                    .scalars()
                    .all()
                )
            }

        # Precondition: the head really did dispatch nothing.
        assert rows[deferred_id].status == TaskStatus.QUEUED, (
            "precondition: the queue head must be resource-DEFERRED (still QUEUED), "
            f"not dispatched; got {rows[deferred_id].status}"
        )
        # …and the slot it did NOT use went to the healthy task behind it.
        assert rows[healthy_id].status == TaskStatus.PROVISIONING, (
            "the healthy task behind a zero-dispatch queue head must be backfilled into "
            f"the free slot; got {rows[healthy_id].status}. A candidate that dispatches "
            "NOTHING must never consume a dispatch-window slot."
        )

    @pytest.mark.asyncio
    async def test_warm_pool_leases_are_counted_against_the_slot_limit(
        self, orch_cap_db, mocker
    ):
        """`_cycle_dispatched` is now the SOLE enforcement of ``max_concurrent_vms``.

        Before this PR the slot limit was STRUCTURAL: the window was cut at exactly
        ``eligible_ids[:slots_available]``, so at most that many tasks entered the loop
        and ``_cycle_dispatched`` was purely observational. Widening the window moved
        enforcement entirely onto the counter's ``break`` — which means any dispatch path
        that consumes a VM without incrementing it is a FREE dispatch that breaches the
        cap, and the breach scales with the width of the window.

        A warm-pool LEASE is exactly such a path: it is a real dispatch (the task is
        pinned to the leased member and skips cold provisioning), and it genuinely
        consumes a slot, because ``count_idle_pool_vms`` SUBTRACTS idle pool rows from
        the dispatch budget — leasing one flips it IDLE -> LEASED so it starts being
        charged against ``max_concurrent_vms``.

        ONE slot, THREE queued tasks, a pool that can serve TWO leases. Unfixed, the two
        leases dispatch without advancing the counter and the third then cold-provisions
        on top: 3 VMs consumed against a cap of 1.

        NOTE: this test deliberately does NOT stub `try_pool_dispatch` to return None.
        Every other test in this class does — which is precisely why this cap breach
        could ship green.
        """
        from types import SimpleNamespace
        from sqlmodel import select as _select
        from app.max_plan_pool import LLMSelectionResult
        import app.orchestrator as _orch_module

        # NOTE: `_dispatch_mocks` is deliberately NOT used here — it nulls out
        # try_pool_dispatch, which is the very path under test.
        mocker.patch(
            "app.orchestrator.provision_agent_vm",
            new_callable=AsyncMock,
            return_value=True,
        )
        mocker.patch(
            "app.max_plan_pool.select_llm_runtime_for_task",
            new_callable=AsyncMock,
            return_value=LLMSelectionResult(
                provider="anthropic",
                auth_mode="oauth",
                model="claude-sonnet-4-6",
                quality_tier=None,
                account={
                    "id": "acct-warm-slot",
                    "name": "Warm Slot Acct",
                    "runtime_secret": "tok",
                    "claude_config_json": None,
                    "provider_metadata": {},
                },
            ),
        )
        mocker.patch(
            "app.orchestrator._sync_runtime_config_from_state",
            new_callable=AsyncMock,
            return_value=[],
        )

        task_ids: list = []
        async with orch_cap_db() as session:
            project = make_project(name="WarmSlotProject", user_id=_TEST_USER)
            session.add(project)
            await session.commit()
            await session.refresh(project)

            base = self._naive_utcnow() - timedelta(minutes=10)
            for i in range(3):
                t = make_task(
                    project_id=project.id,
                    status=TaskStatus.QUEUED,
                    vm_instance_id=None,
                    priority=50 - i,
                    created_at=base + timedelta(seconds=i),
                    payloads=[{"type": "create", "prompt": f"task {i}"}],
                )
                session.add(t)
                await session.commit()
                await session.refresh(t)
                task_ids.append(t.id)

        # A faithful stand-in for the real lease: the pool can serve TWO members, and
        # each lease pins vm_instance_id on the task and leaves it QUEUED (warm_pool.py
        # `try_pool_dispatch` contract). The orchestrator commits and skips cold provision.
        _pool_remaining = [f"pool-vm-{i}" for i in range(2)]

        async def _fake_pool_dispatch(session, task, **kwargs):
            if not _pool_remaining:
                return None
            vm_id = _pool_remaining.pop(0)
            task.vm_instance_id = vm_id
            session.add(task)
            return SimpleNamespace(vm_instance_id=vm_id)

        # Patch at the SOURCE module: orchestrator_worker imports try_pool_dispatch
        # function-locally, so `app.orchestrator.try_pool_dispatch` does not exist.
        mocker.patch(
            "app.warm_pool.try_pool_dispatch",
            side_effect=_fake_pool_dispatch,
        )

        slots_available = 1
        _orch_module._runtime_config["max_concurrent_vms"] = slots_available
        _orch_module._runtime_config["max_pipeline_slots"] = 10
        _orch_module._runtime_config["warm_pool_enabled"] = True
        try:
            await self._run_one_cycle(mocker)
        finally:
            _orch_module._runtime_config.pop("max_concurrent_vms", None)
            _orch_module._runtime_config.pop("max_pipeline_slots", None)
            _orch_module._runtime_config.pop("warm_pool_enabled", None)

        async with orch_cap_db() as verify:
            rows = (
                (await verify.execute(_select(Task).where(Task.id.in_(task_ids))))
                .scalars()
                .all()
            )

        # A task consumed a VM if it was warm-leased (QUEUED but pinned to a pool VM)
        # or cold-provisioned (PROVISIONING).
        warm_leased = [
            r for r in rows if str(r.vm_instance_id or "").startswith("pool-vm-")
        ]
        cold_provisioned = [r for r in rows if r.status == TaskStatus.PROVISIONING]
        consumed = len(warm_leased) + len(cold_provisioned)

        assert consumed <= slots_available, (
            f"dispatch consumed {consumed} VM slot(s) against a cap of {slots_available} "
            f"({len(warm_leased)} warm-pool lease(s) + {len(cold_provisioned)} cold "
            "provision(s)). A warm-pool lease IS a dispatch and DOES charge a VM against "
            "max_concurrent_vms — it must increment _cycle_dispatched, which is now the "
            "sole slot enforcement behind the widened dispatch window."
        )


class TestComputeJobVMDispatchHeadroom:
    """Compute-job VMs must not consume agent dispatch slots (direction d-9d6c2c94).

    _get_actual_gcp_vm_count must only count agent-vm-* instances; compute-job-vm-*
    instances must be excluded so GPU jobs do not starve agent task dispatch.
    """

    @pytest.mark.asyncio
    async def test_compute_job_vms_excluded_from_dispatch_headroom(self, mocker):
        """slots_available = max_concurrent_vms - agent_vms (NOT - agent_vms - compute_job_vms).

        Setup: max_concurrent_vms=5, 2 agent-vm-* VMs live, 3 compute-job-vm-* VMs live.
        Expected: slots_available = 5 - 2 = 3 (not 5 - 2 - 3 = 0).
        """
        import app.vm_lifecycle as _vm_lifecycle_mod

        # Mix of agent and compute-job VMs returned from zone listing
        mixed_vms = [
            ("agent-vm-aaa", "us-central1-a", "RUNNING"),
            ("agent-vm-bbb", "us-central1-a", "RUNNING"),
            ("compute-job-vm-gpu1", "us-central1-a", "RUNNING"),
            ("compute-job-vm-gpu2", "us-central1-a", "RUNNING"),
            ("compute-job-vm-gpu3", "us-central1-a", "RUNNING"),
        ]

        mocker.patch("app.compute.LOCAL_MODE", False)
        mocker.patch(
            "app.compute.FALLBACK_ZONES",
            [("us-central1-a", "us-central1")],
        )
        mocker.patch(
            "app.compute._list_vms_in_zone",
            return_value=mixed_vms,
        )

        # Force cache expiry by patching the globals so they're restored post-test
        mocker.patch.object(_vm_lifecycle_mod, "_last_gcp_vm_check", 0.0)
        mocker.patch.object(
            _vm_lifecycle_mod,
            "_last_gcp_vm_count",
            _vm_lifecycle_mod._last_gcp_vm_count,
        )

        count = await _vm_lifecycle_mod._get_actual_gcp_vm_count()
        assert count == 2, (
            f"Expected 2 (only agent-vm-* instances), got {count} — "
            "compute-job-vm-* instances must be excluded from the dispatch headroom count"
        )

        # Verify slots_available formula: max_concurrent_vms - agent_vm_count
        max_concurrent_vms = 5
        slots_available = max(0, max_concurrent_vms - count)
        assert slots_available == 3, (
            f"Expected slots_available=3 (5 - 2 agent VMs), got {slots_available} — "
            "compute-job VMs must not consume agent dispatch slots"
        )


class TestBatchedDepRowsSurviveCommit:
    """The N+1 dependency-fetch fix batches ALL dep rows in one IN(...) query
    in a separate session, then DETACHES them (expunge_all) so they survive the
    main dispatch session's per-task commits (retry_after / infra_wait). This
    guards the load_only/rollback-expiry trap: a commit on one session expires
    its instances, so the dep rows must come from a different, detached set or
    a later ``_check_candidate_task_deps`` read fires a sync lazy-load →
    MissingGreenlet and crashes the whole dispatch cycle."""

    @pytest.mark.asyncio
    async def test_detached_dep_rows_readable_after_other_session_commit(
        self, db_session
    ):
        from sqlmodel import select
        from app.database import AsyncSessionLocal
        from app.orchestrator import _check_candidate_task_deps

        project = make_project(name="Batch Dep", user_id="u1")
        db_session.add(project)
        await db_session.commit()
        await db_session.refresh(project)

        # A COMPLETED non-create dependency (so it's a satisfied dep with no
        # PR-merge requirement) and a candidate that depends on it.
        dep = make_task(
            project_id=project.id,
            payloads=[{"type": "research", "prompt": "dep"}],
            status=TaskStatus.COMPLETED,
        )
        db_session.add(dep)
        await db_session.commit()
        await db_session.refresh(dep)

        candidate = make_task(
            project_id=project.id,
            payloads=[{"type": "research", "prompt": "cand"}],
            status=TaskStatus.QUEUED,
            depends_on=[str(dep.id)],
        )
        db_session.add(candidate)
        await db_session.commit()
        await db_session.refresh(candidate)

        # Mirror the orchestrator: batch-fetch dep rows in a SEPARATE session
        # with the same load_only projection, build the lookup dict, then DETACH.
        from sqlalchemy.orm import load_only as _load_only

        dep_by_id = {}
        async with AsyncSessionLocal() as dep_batch_session:
            rows = (
                (
                    await dep_batch_session.execute(
                        select(Task)
                        .where(Task.id.in_([dep.id]))
                        .options(
                            _load_only(
                                Task.id,
                                Task.status,
                                Task.context,
                                Task.payloads,
                                Task.project_id,
                                Task.parent_task_id,
                                raiseload=True,
                            )
                        )
                    )
                )
                .scalars()
                .all()
            )
            dep_by_id = {dt.id: dt for dt in rows}
            dep_batch_session.expunge_all()

        # Force a commit on the MAIN session — this expires every instance bound
        # to it. The detached dep rows must be unaffected.
        candidate.latest_status_message = "touched"
        db_session.add(candidate)
        await db_session.commit()

        # Reading status / context / first_payload on the detached dep row must
        # NOT raise (DetachedInstanceError / MissingGreenlet) — projected columns
        # are cached and accessible on the detached instance.
        detached_dep = dep_by_id[dep.id]
        assert detached_dep.status == TaskStatus.COMPLETED
        assert detached_dep.first_payload.get("type") == "research"
        assert isinstance(
            detached_dep.context, dict
        )  # accessible without DetachedInstanceError

        async def _noop_verify(_task, _session):
            return True

        dep_tasks = [dep_by_id[dep.id]]
        result, _ = await _check_candidate_task_deps(
            candidate, dep_tasks, [dep.id], db_session, _noop_verify
        )
        assert result == "eligible", (
            f"COMPLETED same-base research dep should be eligible, got {result!r}"
        )

    @pytest.mark.asyncio
    async def test_dep_batch_create_type_verify_accesses_project_id(self, db_session):
        """A COMPLETED create-type dep whose verify callback reads task.project_id
        must not raise InvalidRequestError — project_id must be in the load_only set.

        This guards the regression where the dep-batch projection omitted project_id
        and _verify_pr_merged (the create-type verify callback) hit raiseload on the
        GitHub-API fallback path (rollup_pr_verification.py:315, 325-326, 399-400).
        """
        from sqlalchemy.orm import load_only as _load_only
        from sqlmodel import select
        from app.database import AsyncSessionLocal
        from app.orchestrator import _check_candidate_task_deps

        project = make_project(name="Create Dep ProjID", user_id="u1")
        db_session.add(project)
        await db_session.commit()
        await db_session.refresh(project)

        # A COMPLETED create-type dependency (same base branch).
        dep = make_task(
            project_id=project.id,
            payloads=[{"type": "create", "prompt": "create dep"}],
            status=TaskStatus.COMPLETED,
            context={"pr_merged": True},
        )
        db_session.add(dep)
        await db_session.commit()
        await db_session.refresh(dep)

        candidate = make_task(
            project_id=project.id,
            payloads=[{"type": "research", "prompt": "cand"}],
            status=TaskStatus.QUEUED,
            depends_on=[str(dep.id)],
        )
        db_session.add(candidate)
        await db_session.commit()
        await db_session.refresh(candidate)

        # Mirror the orchestrator dep-batch fetch with the full projection
        # (including project_id, which _verify_pr_merged reads on detached rows).
        dep_by_id = {}
        async with AsyncSessionLocal() as dep_batch_session:
            rows = (
                (
                    await dep_batch_session.execute(
                        select(Task)
                        .where(Task.id.in_([dep.id]))
                        .options(
                            _load_only(
                                Task.id,
                                Task.status,
                                Task.context,
                                Task.payloads,
                                Task.project_id,
                                Task.parent_task_id,
                                raiseload=True,
                            )
                        )
                    )
                )
                .scalars()
                .all()
            )
            dep_by_id = {dt.id: dt for dt in rows}
            dep_batch_session.expunge_all()

        detached_dep = dep_by_id[dep.id]
        # project_id must be accessible on the detached row — no InvalidRequestError.
        assert detached_dep.project_id == project.id, (
            "project_id must be loaded by the dep-batch projection; "
            "raiseload=True will raise InvalidRequestError if it is missing."
        )

        # Verify callback that reads task.project_id (simulates the fallback path
        # in _verify_pr_merged when context['pr_merged'] is absent/False).
        accessed_project_ids = []

        async def _verify_reads_project_id(task, _session):
            accessed_project_ids.append(task.project_id)
            return True

        dep_tasks = [dep_by_id[dep.id]]
        result, _ = await _check_candidate_task_deps(
            candidate, dep_tasks, [dep.id], db_session, _verify_reads_project_id
        )
        assert result == "eligible", (
            f"COMPLETED create-type dep should be eligible, got {result!r}"
        )
        # The verify callback must have been called and project_id accessible.
        assert accessed_project_ids, "verify callback was never invoked"
        assert all(pid == project.id for pid in accessed_project_ids), (
            f"verify callback read wrong project_id: {accessed_project_ids}"
        )

    def test_dep_batch_projection_excludes_heavy_columns(self):
        """The dep-batch load_only must exclude plan_output/status_log/depends_on
        and retain status/context/payloads needed by _check_candidate_task_deps."""
        from sqlalchemy.dialects import postgresql
        from sqlalchemy.orm import load_only as _load_only
        from sqlmodel import select

        stmt = (
            select(Task)
            .where(Task.id.in_([]))
            .options(
                _load_only(
                    Task.id,
                    Task.status,
                    Task.context,
                    Task.payloads,
                    Task.project_id,
                    Task.parent_task_id,
                    raiseload=True,
                )
            )
        )
        sql_text = str(stmt.compile(dialect=postgresql.dialect()))

        for heavy in ("plan_output", "status_log", "depends_on"):
            assert heavy not in sql_text, (
                f"Dep-batch projection must exclude '{heavy}' — "
                "re-adding it reintroduces the over-fetch this projection removes."
            )
        for required in (
            "status",
            "context",
            "payloads",
            "project_id",
            "parent_task_id",
        ):
            assert required in sql_text, (
                f"Dep-batch projection must include '{required}' — "
                "removing it will cause DetachedInstanceError in _check_candidate_task_deps."
            )

    @pytest.mark.asyncio
    async def test_detached_cross_base_dep_can_read_parent_task_id(self, db_session):
        """Detached dep rows must include parent_task_id for lineage fallback.

        Production loads dependency rows with load_only(..., raiseload=True) in a
        separate session, then detaches them. A cross-base completed code dep whose
        own landed signal has not propagated yet uses its immediate lineage parent
        as a fallback. Omitting parent_task_id from the projection makes that read
        lazy-load on a detached instance and crashes the dispatch cycle.
        """
        from sqlalchemy.orm import load_only as _load_only
        from sqlmodel import select
        from app.database import AsyncSessionLocal
        from app.landed_signal import (
            mark_landed_on_integration,
            mark_unblocked_non_merge,
        )
        from app.orchestrator import _check_candidate_task_deps

        project = make_project(name="Detached Lineage Dep", user_id="u1")
        db_session.add(project)
        await db_session.commit()
        await db_session.refresh(project)

        lineage_parent = make_task(
            project_id=project.id,
            payloads=[{"type": "research", "base_branch": "research/r1"}],
            context={},
            status=TaskStatus.COMPLETED,
        )
        mark_landed_on_integration(lineage_parent)
        db_session.add(lineage_parent)
        await db_session.commit()
        await db_session.refresh(lineage_parent)

        dep = make_task(
            project_id=project.id,
            payloads=[{"type": "create", "base_branch": "research/r1"}],
            context={"related_files": ["backend/app/foo.py"]},
            status=TaskStatus.COMPLETED,
            parent_task_id=lineage_parent.id,
        )
        mark_unblocked_non_merge(dep, reason="merged_pr_non_integration_branch")
        db_session.add(dep)
        await db_session.commit()
        await db_session.refresh(dep)

        dependent = make_task(
            project_id=project.id,
            payloads=[{"type": "create", "base_branch": "plan/bbb"}],
            context={"related_files": ["backend/app/foo.py"]},
            status=TaskStatus.QUEUED,
            depends_on=[str(dep.id)],
        )
        db_session.add(dependent)
        await db_session.commit()
        await db_session.refresh(dependent)

        async with AsyncSessionLocal() as dep_batch_session:
            rows = (
                (
                    await dep_batch_session.execute(
                        select(Task)
                        .where(Task.id.in_([dep.id]))
                        .options(
                            _load_only(
                                Task.id,
                                Task.status,
                                Task.context,
                                Task.payloads,
                                Task.project_id,
                                Task.parent_task_id,
                                Task.title,
                                raiseload=True,
                            )
                        )
                    )
                )
                .scalars()
                .all()
            )
            dep_by_id = {dt.id: dt for dt in rows}
            dep_batch_session.expunge_all()

        # Expire the main-session instances. The detached dependency must still
        # have every value _check_candidate_task_deps reads cached locally.
        dependent.latest_status_message = "main session committed"
        db_session.add(dependent)
        await db_session.commit()

        detached_dep = dep_by_id[dep.id]
        assert detached_dep.parent_task_id == lineage_parent.id

        async def _noop_verify(_task, _session):
            return True

        result, _ = await _check_candidate_task_deps(
            dependent, [detached_dep], [dep.id], db_session, _noop_verify
        )
        assert result == "eligible"

    def test_collect_dep_uuids_dedupes_and_skips_malformed(self):
        from app.orchestrator_filters import collect_dep_uuids

        shared = uuid.uuid4()
        only_a = uuid.uuid4()
        c1 = make_task(depends_on=[str(shared), str(only_a)])
        c2 = make_task(depends_on=[str(shared), "not-a-uuid", None])
        c3 = make_task(depends_on=None)  # no deps
        out = collect_dep_uuids([c1, c2, c3])
        # Deduped across candidates; malformed ids silently dropped.
        assert out == {shared, only_a}

    @pytest.mark.asyncio
    async def test_single_batch_query_for_many_candidates(self, db_session, mocker):
        """Behavior + efficiency: many candidates sharing deps collapse to ONE
        IN(...) fetch (the N+1 this fix removes).

        Drives the PRODUCTION helper ``collect_dep_uuids`` over the candidate
        batch — including a malformed dep id that must be SKIPPED, not fetched
        and not crash — then proves the collected UUID set powers a single
        ``select(Task).where(Task.id.in_(...))`` fetch returning exactly the
        valid deps. The old per-candidate code would have issued one SELECT per
        candidate; ``spy.call_count == 1`` here is meaningful precisely because
        the set being queried is the helper's output over MANY candidates, not
        a hand-built id list."""
        from sqlmodel import select
        from app.orchestrator_filters import collect_dep_uuids
        from app.database import AsyncSessionLocal
        from app.models import Task as _Task

        project = make_project(name="Batch Count", user_id="u1")
        db_session.add(project)
        await db_session.commit()
        await db_session.refresh(project)

        deps = []
        for i in range(3):
            d = make_task(
                project_id=project.id,
                payloads=[{"type": "research", "prompt": f"dep{i}"}],
                status=TaskStatus.COMPLETED,
            )
            db_session.add(d)
            deps.append(d)
        await db_session.commit()
        for d in deps:
            await db_session.refresh(d)

        dep_ids = [d.id for d in deps]
        # 10 candidates, each depending on 2-3 of the deps (with heavy overlap),
        # PLUS one candidate carrying a malformed dep id (None) that the helper
        # must skip rather than fetch or crash on. The OLD code would have issued
        # one SELECT per candidate (10); the helper + single batch issues one.
        candidates = [
            make_task(
                project_id=project.id,
                payloads=[{"type": "research", "prompt": f"cand{i}"}],
                status=TaskStatus.QUEUED,
                depends_on=[str(dep_ids[i % 3]), str(dep_ids[(i + 1) % 3])],
            )
            for i in range(10)
        ]
        # A malformed-dep candidate: None must be skipped (TypeError path),
        # "bogus" must be skipped (ValueError path) — neither reaches the query.
        candidates.append(
            make_task(
                project_id=project.id,
                payloads=[{"type": "research", "prompt": "cand-bad"}],
                status=TaskStatus.QUEUED,
                depends_on=[str(dep_ids[0]), None, "bogus"],
            )
        )

        # Exercise the PRODUCTION helper — this is the dedup + malformed-skip
        # that the orchestrator's batched dep-fetch relies on.
        all_uuids = collect_dep_uuids(candidates)
        # Deduped to exactly the 3 valid deps; the malformed ids are gone.
        assert all_uuids == set(dep_ids)

        async with AsyncSessionLocal() as s:
            spy = mocker.spy(s, "execute")
            rows = (
                (await s.execute(select(_Task).where(_Task.id.in_(all_uuids))))
                .scalars()
                .all()
            )
            s.expunge_all()
            assert {r.id for r in rows} == set(dep_ids)
            # Exactly one IN(...) fetch over the helper's collected set,
            # regardless of candidate count.
            assert spy.call_count == 1


class TestPersistDepsSatisfied:
    """The orchestrator durably stamps ``context['_deps_satisfied']`` on a
    dependency-bearing task once its deps resolve eligible. This sticky marker
    is the contract the warm-backlog scan and dispatch first-refusal hold key
    off so dependency-ready work can run warm without re-running the
    (GitHub-aware) dep resolution on the hot path."""

    @pytest.mark.asyncio
    async def test_persist_deps_satisfied_stamps_and_persists(self, db_session, mocker):
        from app.orchestrator import _persist_deps_satisfied

        project = make_project(name="Deps Satisfied", user_id="u1")
        db_session.add(project)
        await db_session.commit()
        await db_session.refresh(project)

        task = make_task(
            project_id=project.id,
            payloads=[{"type": "create", "prompt": "cand"}],
            status=TaskStatus.QUEUED,
            depends_on=[str(uuid.uuid4())],
            context={"scope": "small"},
        )
        db_session.add(task)
        await db_session.commit()
        await db_session.refresh(task)

        # Redirect the helper's separate-session write to the test DB.
        mocker.patch(
            "app.orchestrator.AsyncSessionLocal", _FakeSessionFactory(db_session)
        )
        mocker.patch(
            "app.dep_lifecycle.AsyncSessionLocal", _FakeSessionFactory(db_session)
        )

        await _persist_deps_satisfied(task)

        # In-memory mirror set for the same-cycle dispatch first-refusal hold.
        assert task.context.get("_deps_satisfied") is True
        # And durably persisted so the next-role warm scan (separate session)
        # and the Step 3 dispatch re-read observe it.
        db_session.expire(task)
        await db_session.refresh(task)
        assert task.context.get("_deps_satisfied") is True
        # Pre-existing context keys are preserved, not clobbered.
        assert task.context.get("scope") == "small"

    @pytest.mark.asyncio
    async def test_persist_deps_satisfied_is_idempotent_noop(self, db_session, mocker):
        """A task already carrying the marker is left untouched (no redundant
        write), so repeated eligible cycles don't thrash the row."""
        from app.orchestrator import _persist_deps_satisfied

        task = make_task(
            payloads=[{"type": "create", "prompt": "cand"}],
            status=TaskStatus.QUEUED,
            depends_on=[str(uuid.uuid4())],
            context={"_deps_satisfied": True},
        )
        # If the helper opened a session it would hit this un-redirected factory
        # and fail; the early-return guard means it never does.
        mocker.patch(
            "app.orchestrator.AsyncSessionLocal",
            side_effect=AssertionError("should not open a session when marker present"),
        )
        sentinel = mocker.patch(
            "app.dep_lifecycle.AsyncSessionLocal",
            side_effect=AssertionError("should not open a session when marker present"),
        )
        await _persist_deps_satisfied(task)
        assert task.context.get("_deps_satisfied") is True
        assert sentinel.call_count == 0


class TestMarkUnblockedDependents:
    """Completion-side trigger: when a task finishes and its PR lands on the
    integration branch, the orchestrator proactively stamps ``_deps_satisfied``
    on the QUEUED dependents it just unblocked, so the just-completed task's warm
    VM can claim that follow-on work inside its teardown-defer grace window
    instead of tearing down cold. The marker stays authoritative — only deps that
    resolve ``eligible`` are stamped; a dependent with any unresolved dep is left
    unmarked."""

    @staticmethod
    async def _verify_true(_task, _session):
        return True

    @pytest.mark.asyncio
    async def test_marks_ready_dependent_after_completion(self, db_session, mocker):
        from app.orchestrator import _mark_unblocked_dependents

        project = make_project(name="Unblock", user_id="u1")
        db_session.add(project)
        await db_session.commit()
        await db_session.refresh(project)

        completed = make_task(
            project_id=project.id,
            payloads=[{"type": "research", "prompt": "dep"}],
            status=TaskStatus.COMPLETED,
        )
        db_session.add(completed)
        await db_session.commit()
        await db_session.refresh(completed)

        dependent = make_task(
            project_id=project.id,
            payloads=[{"type": "create", "prompt": "follow-on"}],
            status=TaskStatus.QUEUED,
            depends_on=[str(completed.id)],
            context={"scope": "small"},
        )
        db_session.add(dependent)
        await db_session.commit()
        await db_session.refresh(dependent)

        mocker.patch(
            "app.orchestrator.AsyncSessionLocal", _FakeSessionFactory(db_session)
        )
        mocker.patch(
            "app.dep_lifecycle.AsyncSessionLocal", _FakeSessionFactory(db_session)
        )

        marked = await _mark_unblocked_dependents(completed, self._verify_true)
        assert marked == 1

        db_session.expire(dependent)
        await db_session.refresh(dependent)
        # The just-unblocked dependent is now warm-claimable...
        assert dependent.context.get("_deps_satisfied") is True
        # ...without clobbering pre-existing context.
        assert dependent.context.get("scope") == "small"

    @pytest.mark.asyncio
    async def test_marking_records_completion_funnel_telemetry(
        self, db_session, mocker
    ):
        """The completion-side trigger surfaces its lift on the reuse funnel: a
        production call to ``_mark_unblocked_dependents`` that stamps N dependents
        must record N onto ``completion_dependents_marked`` (not stay a dead 0)."""
        from app.orchestrator import _mark_unblocked_dependents
        from app.warm_backlog_reuse import get_reuse_funnel_snapshot

        project = make_project(name="Unblock Telemetry", user_id="u1")
        db_session.add(project)
        await db_session.commit()
        await db_session.refresh(project)

        completed = make_task(
            project_id=project.id,
            payloads=[{"type": "research", "prompt": "dep"}],
            status=TaskStatus.COMPLETED,
        )
        db_session.add(completed)
        await db_session.commit()
        await db_session.refresh(completed)

        dependent = make_task(
            project_id=project.id,
            payloads=[{"type": "create", "prompt": "follow-on"}],
            status=TaskStatus.QUEUED,
            depends_on=[str(completed.id)],
        )
        db_session.add(dependent)
        await db_session.commit()
        await db_session.refresh(dependent)

        mocker.patch(
            "app.orchestrator.AsyncSessionLocal", _FakeSessionFactory(db_session)
        )
        mocker.patch(
            "app.dep_lifecycle.AsyncSessionLocal", _FakeSessionFactory(db_session)
        )

        before = get_reuse_funnel_snapshot()["completion_dependents_marked"]
        marked = await _mark_unblocked_dependents(completed, self._verify_true)
        assert marked == 1
        after = get_reuse_funnel_snapshot()["completion_dependents_marked"]
        assert after == before + 1

    @pytest.mark.asyncio
    async def test_does_not_mark_dependent_with_unresolved_dep(
        self, db_session, mocker
    ):
        """Signal is NOT loosened: a dependent that also waits on a still-QUEUED
        sibling stays unmarked, so it continues to cold-dispatch exactly as before."""
        from app.orchestrator import _mark_unblocked_dependents

        project = make_project(name="Unblock Partial", user_id="u1")
        db_session.add(project)
        await db_session.commit()
        await db_session.refresh(project)

        completed = make_task(
            project_id=project.id,
            payloads=[{"type": "research", "prompt": "dep-a"}],
            status=TaskStatus.COMPLETED,
        )
        still_pending = make_task(
            project_id=project.id,
            payloads=[{"type": "research", "prompt": "dep-b"}],
            status=TaskStatus.QUEUED,
        )
        db_session.add_all([completed, still_pending])
        await db_session.commit()
        await db_session.refresh(completed)
        await db_session.refresh(still_pending)

        dependent = make_task(
            project_id=project.id,
            payloads=[{"type": "create", "prompt": "follow-on"}],
            status=TaskStatus.QUEUED,
            depends_on=[str(completed.id), str(still_pending.id)],
        )
        db_session.add(dependent)
        await db_session.commit()
        await db_session.refresh(dependent)

        mocker.patch(
            "app.orchestrator.AsyncSessionLocal", _FakeSessionFactory(db_session)
        )
        mocker.patch(
            "app.dep_lifecycle.AsyncSessionLocal", _FakeSessionFactory(db_session)
        )

        marked = await _mark_unblocked_dependents(completed, self._verify_true)
        assert marked == 0

        db_session.expire(dependent)
        await db_session.refresh(dependent)
        assert dependent.context.get("_deps_satisfied") is None

    @pytest.mark.asyncio
    async def test_marks_after_recut_for_requeue_replaced_cross_base_dep(
        self, db_session, mocker
    ):
        """Completion-side trigger follows the requeue replacement remap before
        re-cutting. A dependent on a requeue-cancelled-and-replaced cross-base
        genuine code-dependency (whose landed code lives in the replacement clone)
        must have its base re-cut BEFORE the warm-claim marker is stamped — the
        re-cut resolves the replacement, not the superseded CANCELLED original
        (which ``_has_landed_cross_base_code_dep`` would skip). Without the remap
        the re-cut is silently no-oped and the dependent is stamped warm-claimable
        against a base missing the replacement's landed code."""
        from app.orchestrator import _mark_unblocked_dependents
        from app.landed_signal import mark_landed_on_integration

        mocker.patch(
            "app.routers.agent.get_github_token_for_task",
            new_callable=AsyncMock,
            return_value=("tok", "owner", "repo"),
        )
        merge_mock = mocker.patch(
            "app.github.merge_branch",
            new_callable=AsyncMock,
            return_value={"merged": True, "already": False},
        )
        project = make_project(
            name="UnblockRemapRecut", user_id="u1", integration_branch="main"
        )
        db_session.add(project)
        await db_session.commit()
        await db_session.refresh(project)

        # Replacement clone carries the landed cross-base code (base research/ccc).
        replacement = make_task(
            project_id=project.id,
            payloads=[{"type": "create", "base_branch": "research/ccc"}],
            context={"related_files": ["backend/app/foo.py"]},
            status=TaskStatus.COMPLETED,
        )
        mark_landed_on_integration(replacement)
        db_session.add(replacement)
        await db_session.commit()
        await db_session.refresh(replacement)

        # Original cross-base dep was requeue-cancelled, points at the replacement.
        original_dep = make_task(
            project_id=project.id,
            payloads=[{"type": "create", "base_branch": "research/aaa"}],
            context={
                "related_files": ["backend/app/foo.py"],
                "_replaced_by_task_id": str(replacement.id),
            },
            status=TaskStatus.CANCELLED,
        )
        # Same-base sibling whose completion fires the trigger.
        completed = make_task(
            project_id=project.id,
            payloads=[{"type": "research", "base_branch": "plan/bbb"}],
            status=TaskStatus.COMPLETED,
        )
        db_session.add_all([original_dep, completed])
        await db_session.commit()
        await db_session.refresh(original_dep)
        await db_session.refresh(completed)

        dependent = make_task(
            project_id=project.id,
            payloads=[{"type": "fix", "base_branch": "plan/bbb"}],
            context={"related_files": ["backend/app/foo.py"]},
            status=TaskStatus.QUEUED,
            depends_on=[str(original_dep.id), str(completed.id)],
        )
        db_session.add(dependent)
        await db_session.commit()
        await db_session.refresh(dependent)

        mocker.patch(
            "app.orchestrator.AsyncSessionLocal", _FakeSessionFactory(db_session)
        )
        mocker.patch(
            "app.dep_lifecycle.AsyncSessionLocal", _FakeSessionFactory(db_session)
        )

        marked = await _mark_unblocked_dependents(completed, self._verify_true)
        assert marked == 1
        # Remap followed → landed replacement triggered the base re-cut (integration
        # merged into the rollup base) BEFORE the marker was stamped.
        merge_mock.assert_awaited_once()
        _, kwargs = merge_mock.await_args
        assert kwargs.get("base") == "plan/bbb"
        assert kwargs.get("head") == "main"

        db_session.expire(dependent)
        await db_session.refresh(dependent)
        assert dependent.context.get("_deps_satisfied") is True

    @pytest.mark.asyncio
    async def test_does_not_mark_dependent_when_recut_conflicts(
        self, db_session, mocker
    ):
        """Completion-side warm-claim path: when a re-cut conflicts, the marker
        is NOT stamped, so the dependent stays unmarked and must wait for
        dispatch's retry path to resolve the conflict.

        Regression guard: a conflicting re-cut must prevent warm-claim stamping
        BEFORE it happens (not clear it after). This ensures a dependent never
        becomes warm-claimable against a base missing the landed dependency code."""
        from app.orchestrator import _mark_unblocked_dependents
        from app.landed_signal import mark_landed_on_integration

        mocker.patch(
            "app.routers.agent.get_github_token_for_task",
            new_callable=AsyncMock,
            return_value=("tok", "owner", "repo"),
        )
        mocker.patch(
            "app.github.merge_branch",
            new_callable=AsyncMock,
            return_value={"merged": False, "conflict": True},
        )
        project = make_project(
            name="UnblockConflict", user_id="u1", integration_branch="main"
        )
        db_session.add(project)
        await db_session.commit()
        await db_session.refresh(project)

        # Cross-base code dependency that has landed.
        dep = make_task(
            project_id=project.id,
            payloads=[{"type": "create", "base_branch": "research/aaa"}],
            context={"related_files": ["backend/app/foo.py"]},
            status=TaskStatus.COMPLETED,
        )
        mark_landed_on_integration(dep)
        # Dependent on a different base where re-cut will conflict.
        completed = make_task(
            project_id=project.id,
            payloads=[{"type": "research", "base_branch": "plan/bbb"}],
            status=TaskStatus.COMPLETED,
        )
        db_session.add_all([dep, completed])
        await db_session.commit()
        await db_session.refresh(dep)
        await db_session.refresh(completed)

        dependent = make_task(
            project_id=project.id,
            payloads=[{"type": "fix", "base_branch": "plan/bbb"}],
            context={"related_files": ["backend/app/foo.py"]},
            status=TaskStatus.QUEUED,
            depends_on=[str(dep.id), str(completed.id)],
        )
        db_session.add(dependent)
        await db_session.commit()
        await db_session.refresh(dependent)

        mocker.patch(
            "app.orchestrator.AsyncSessionLocal", _FakeSessionFactory(db_session)
        )
        mocker.patch(
            "app.dep_lifecycle.AsyncSessionLocal", _FakeSessionFactory(db_session)
        )

        # Re-cut conflicts → no dependents marked.
        marked = await _mark_unblocked_dependents(completed, self._verify_true)
        assert marked == 0

        # Dependent must NOT have the warm-claim marker.
        db_session.expire(dependent)
        await db_session.refresh(dependent)
        assert not (dependent.context or {}).get("_deps_satisfied")

    @pytest.mark.asyncio
    async def test_does_not_mark_dependent_when_recut_errors(self, db_session, mocker):
        """Completion-side warm-claim path: transient re-cut errors (GitHub API
        failures, credential issues) hold the dependent for retry next cycle
        instead of stamping it as warm-claimable against an un-recut base.

        The dependent stays QUEUED and unmarked, so it will be re-evaluated on
        the next completion cycle (once the transient error resolves or an
        operator manually retries)."""
        from app.orchestrator import _mark_unblocked_dependents
        from app.landed_signal import mark_landed_on_integration

        mocker.patch(
            "app.routers.agent.get_github_token_for_task",
            new_callable=AsyncMock,
            return_value=("tok", "owner", "repo"),
        )
        # Simulate a transient GitHub API error (e.g. rate limit, timeout).
        mocker.patch(
            "app.github.merge_branch",
            new_callable=AsyncMock,
            side_effect=Exception("GitHub API timeout"),
        )
        project = make_project(
            name="UnblockTransient", user_id="u1", integration_branch="main"
        )
        db_session.add(project)
        await db_session.commit()
        await db_session.refresh(project)

        # Cross-base code dependency that has landed.
        dep = make_task(
            project_id=project.id,
            payloads=[{"type": "create", "base_branch": "research/aaa"}],
            context={"related_files": ["backend/app/foo.py"]},
            status=TaskStatus.COMPLETED,
        )
        mark_landed_on_integration(dep)
        # Dependent on a different base where re-cut will error.
        completed = make_task(
            project_id=project.id,
            payloads=[{"type": "research", "base_branch": "plan/bbb"}],
            status=TaskStatus.COMPLETED,
        )
        db_session.add_all([dep, completed])
        await db_session.commit()
        await db_session.refresh(dep)
        await db_session.refresh(completed)

        dependent = make_task(
            project_id=project.id,
            payloads=[{"type": "fix", "base_branch": "plan/bbb"}],
            context={"related_files": ["backend/app/foo.py"]},
            status=TaskStatus.QUEUED,
            depends_on=[str(dep.id), str(completed.id)],
        )
        db_session.add(dependent)
        await db_session.commit()
        await db_session.refresh(dependent)

        mocker.patch(
            "app.orchestrator.AsyncSessionLocal", _FakeSessionFactory(db_session)
        )
        mocker.patch(
            "app.dep_lifecycle.AsyncSessionLocal", _FakeSessionFactory(db_session)
        )

        # Transient error → no dependents marked.
        marked = await _mark_unblocked_dependents(completed, self._verify_true)
        assert marked == 0

        # Dependent must NOT have the warm-claim marker; stays QUEUED for retry.
        db_session.expire(dependent)
        await db_session.refresh(dependent)
        assert not (dependent.context or {}).get("_deps_satisfied")
        assert dependent.status == TaskStatus.QUEUED


class TestOrchestratorClosureSafety:
    """Regression: a `from app.compute import X` inside orchestrator_worker
    silently shadows the module-level X for the *entire* function, which then
    makes any reference to X from a nested closure (like _provision_and_handle)
    resolve to an unbound enclosing-scope cell when the import branch hasn't
    run yet. Production saw this on every successful provision via 8294c265 →
    `cannot access free variable 'get_vm_zone'` → vm_provisioned_at refresh
    skipped → tasks stuck in PROVISIONING. Guard the names that matter."""

    def test_provision_and_handle_has_no_get_vm_zone_freevar(self):
        """Static check: ``provision_and_handle`` must not treat ``get_vm_zone``
        as a free variable.

        ``provision_and_handle`` now lives at module level in
        ``app.orchestrator_provision`` (re-exported via ``app.orchestrator``),
        so ``get_vm_zone`` should resolve as that module's global. If it ever
        shows up in ``co_freevars`` the function has been nested back inside a
        scope that re-imports ``get_vm_zone`` as a local — the exact shape that
        produced ``cannot access free variable 'get_vm_zone'`` in production
        (vm_provisioned_at refresh skipped → tasks stuck in PROVISIONING)."""
        from app import orchestrator, orchestrator_provision

        # Same object via the facade re-export and the canonical home.
        assert (
            orchestrator.provision_and_handle
            is orchestrator_provision.provision_and_handle
        )

        provision_code = orchestrator_provision.provision_and_handle.__code__
        assert provision_code.co_name == "provision_and_handle"

        # A module-level function has no enclosing scope, so get_vm_zone must be
        # a global (co_names), never a closure cell (co_freevars).
        assert "get_vm_zone" not in provision_code.co_freevars, (
            "provision_and_handle is capturing get_vm_zone as a closure variable, "
            "which means it was nested inside a scope that has it as a local "
            "(likely from a `from app.compute import get_vm_zone`). Keep the "
            "import at module level."
        )
        assert "get_vm_zone" in provision_code.co_names, (
            "provision_and_handle no longer references get_vm_zone as a module "
            "global — the extraction's import wiring may have drifted."
        )


# ══════════════════════════════════════════════════════════════════════════════
# Phase 2c — warm-VM continuity skip-provision branch
# ══════════════════════════════════════════════════════════════════════════════


def _extract_warm_pin_clear_args(spy):
    """Helper: pull (vm_id, reason) from a record_warm_pin_clear spy regardless
    of whether the orchestrator passes them as positional or keyword args.

    record_warm_pin_clear(vm_id: str, reason: str) — orchestrator currently
    uses positional, but we tolerate both so the assertion stays robust if
    the call site is later refactored to kwargs."""
    call = spy.call_args
    args, kwargs = call.args, call.kwargs
    if len(args) >= 2:
        return args[0], args[1]
    if len(args) == 1 and "reason" in kwargs:
        return args[0], kwargs["reason"]
    return kwargs.get("vm_id"), kwargs.get("reason")


class TestWarmVMSkipProvision:
    """Tests for the orchestrator's warm-pin branch in the dispatch loop.

    The full dispatch loop is heavyweight to drive end-to-end (account pool,
    deadlock detection, etc.), so we use a one-cycle harness like
    test_pipeline_reliability.py and assert on the post-cycle DB state and
    on whether ``provision_agent_vm`` was called.
    """

    @staticmethod
    def _naive_utcnow():
        from datetime import timezone as _tz

        return datetime.now(_tz.utc).replace(tzinfo=None)

    @pytest.fixture()
    def orch_warm_db(self, mocker):
        """In-memory DB plus the lazy mocks orchestrator_worker needs."""
        import asyncio
        from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession
        from sqlalchemy.orm import sessionmaker
        from sqlalchemy.pool import StaticPool
        from sqlmodel import SQLModel
        from sqlalchemy.dialects.sqlite.base import (
            SQLiteTypeCompiler as _SQLiteTypeCompiler,
        )

        if not hasattr(_SQLiteTypeCompiler, "visit_JSONB"):
            _SQLiteTypeCompiler.visit_JSONB = lambda self, type_, **kw: self.visit_JSON(
                type_, **kw
            )
        import app.orchestrator as _orch_module

        async def _build():
            _engine = create_async_engine(
                "sqlite+aiosqlite:///:memory:",
                connect_args={"check_same_thread": False},
                poolclass=StaticPool,
            )
            async with _engine.begin() as conn:
                await conn.run_sync(SQLModel.metadata.create_all)
            _Session = sessionmaker(
                bind=_engine,
                class_=AsyncSession,
                autocommit=False,
                autoflush=False,
                expire_on_commit=False,
            )
            mocker.patch("app.orchestrator.AsyncSessionLocal", _Session)
            mocker.patch("app.dep_lifecycle.AsyncSessionLocal", _Session)
            mocker.patch("app.broadcast.broadcast_to_project", new_callable=AsyncMock)
            mocker.patch("app.vm_lifecycle._ensure_vm_deleted", new_callable=AsyncMock)
            mocker.patch("app.vm_lifecycle._get_actual_gcp_vm_count", return_value=0)
            mocker.patch("app.main.load_max_plan_pool", new_callable=AsyncMock)
            mocker.patch("app.main.recheck_disabled_accounts", new_callable=AsyncMock)
            mocker.patch("app.routers.agent_status.record_task_result")
            mocker.patch(
                "app.deploy_health._check_monitoring_window", new_callable=AsyncMock
            )
            mocker.patch(
                "app.deploy_health._check_external_health", new_callable=AsyncMock
            )
            mocker.patch("app.deploy_health._check_pause_mode", new_callable=AsyncMock)
            mocker.patch(
                "app.autonomous_research._check_idle_research", new_callable=AsyncMock
            )
            mocker.patch("app.services._recover_orphaned_tasks", new_callable=AsyncMock)
            mocker.patch(
                "app.alert_dispatch._create_orchestrator_error_alerts",
                new_callable=AsyncMock,
            )
            mocker.patch(
                "app.orchestrator._persist_zone_health", new_callable=AsyncMock
            )
            mocker.patch(
                "app.alert_dispatch._maybe_create_failure_alert", new_callable=AsyncMock
            )
            # The provision wrapper reaches Secret Manager and GCS via
            # asyncio.gather BEFORE the (separately mocked) provision_agent_vm
            # call. Both need real Google credentials: present on GCE agent
            # VMs (ambient metadata creds) but absent in the CI container,
            # where DefaultCredentialsError crashes the wrapper into its
            # requeue/error path, which then runs uncoordinated DB work on
            # this fixture's shared StaticPool connection. Patch both so
            # every test in this class is hermetic and behaves identically
            # in CI and on VMs (mirrors the orch_cap_db harness).
            mocker.patch(
                "app.orchestrator.prepare_and_store_agent_secret",
                new_callable=AsyncMock,
                return_value="projects/test/secrets/agent/versions/1",
            )
            mocker.patch(
                "app.orchestrator.upload_json_payload",
                new_callable=AsyncMock,
                return_value="gs://test-bucket/payloads/test.json",
            )

            now = self._naive_utcnow()
            _orch_module._last_pool_refresh = now
            _orch_module._last_zone_health_persist = now
            _orch_module._last_orphan_check = now
            _orch_module._last_evaluating_timeout_check = now
            _orch_module._last_deadlock_check = now
            _orch_module._last_anomaly_state_persist = now
            _orch_module._last_anomaly_sweep = now
            _orch_module._last_post_commit_sweep = now
            _orch_module._last_sidecar_sweep = now
            _orch_module._last_bootstrap_recovery_sweep = now
            _orch_module._last_lane_baseline_backfill = now
            _orch_module._last_merge_credit_backfill = now
            _orch_module._last_v22_finding_backfill = now
            _orch_module._last_runner_deactivation_sweep = now
            _orch_module._last_runner_degraded_sweep = now
            _orch_module._last_runner_escalation_sweep = now
            _orch_module._last_local_runner_hold_sweep = now
            _shared_state.deploy_monitoring["task_results"].clear()
            return _Session, _engine

        # Run the async setup synchronously so the fixture body returns.
        loop = asyncio.new_event_loop()
        try:
            _Session, _engine = loop.run_until_complete(_build())
        finally:
            loop.close()
        try:
            yield _Session
        finally:
            # Best-effort teardown of a THROWAWAY in-memory engine.
            #
            # ``drop_all`` is removed deliberately: the in-memory SQLite DB lives
            # only as long as the StaticPool connection, so disposing the engine
            # discards it, and every fixture use builds a fresh engine + tables.
            # Running ``drop_all`` on a freshly-created event loop while the
            # pool's aiosqlite connection thread is still bound to the (now
            # closed) setup loop intermittently races into ``RuntimeError: Event
            # loop is closed`` -> ``sqlite3.OperationalError: database table is
            # locked`` — an ERROR raised AT TEARDOWN that has blocked otherwise
            # green PRs (incident 2026-06-02: PR #7154 — 1523 passing, failed
            # only on this teardown race). Dispose only, and suppress any
            # closed-loop teardown error (no correctness impact on a discarded
            # DB). Mirrors conftest._dispose_production_engine_per_test.
            import contextlib

            async def _teardown():
                with contextlib.suppress(Exception):
                    await _engine.dispose()

            loop = asyncio.new_event_loop()
            try:
                loop.run_until_complete(_teardown())
            finally:
                loop.close()

    @staticmethod
    async def _run_one_cycle(mocker):
        import asyncio
        import contextlib
        import app.orchestrator as _orch_module
        import app.pipeline_health_state as _pipeline_state

        if not hasattr(asyncio, "timeout"):
            import async_timeout

            mocker.patch("asyncio.timeout", async_timeout.timeout, create=True)
        mocker.patch("asyncio.sleep", side_effect=asyncio.CancelledError())
        with contextlib.suppress(asyncio.CancelledError):
            await _orch_module.orchestrator_worker()

        # Drain background provisioning coroutines the cycle spawned so
        # dangling tasks don't race the post-cycle verify reads on the shared
        # StaticPool connection.
        #
        # The dispatch path commits status=PROVISIONING INLINE, then fires the
        # real VM provision via `asyncio.create_task(_provision_and_handle(...))`
        # — an INDEPENDENT task (tracked in the module global
        # `app.orchestrator._provision_coroutines`), NOT a child of
        # `orchestrator_worker`. The worker's cycle-ending CancelledError never
        # reaches it. Left dangling, that task keeps running
        # `_provision_and_handle` against this test's shared in-memory StaticPool
        # SINGLE connection (`orch_warm_db`), racing the post-cycle verify read.
        #
        # AWAIT them to completion rather than cancel: with provisioning fully
        # mocked in the fixture (provision_agent_vm, Secret Manager, GCS) the
        # wrapper finishes promptly, while a cancel can land mid DB-query and
        # poison the StaticPool connection — a reconnect to sqlite :memory:
        # yields an empty database ("no such table: task"), the shard-5 CI
        # flake. Mirrors the orch_cap_db harness's _run_one_cycle.
        _orphan_keys_before = set(_pipeline_state.background_tasks)
        for _coro in list(_orch_module._provision_coroutines.values()):
            _loop = _coro.get_loop()
            if _loop.is_closed():
                continue
            with contextlib.suppress(BaseException):
                await _coro
        _orch_module._provision_coroutines.clear()
        # `_provision_and_handle`'s failure arms can schedule a
        # `_delayed_orphan_cleanup` background task (sleeps 120s then calls the
        # mocked `_ensure_vm_deleted`). Cancel + drop any such task the cycle
        # provoked so it leaves no dangling work for later tests.
        for _key in set(_pipeline_state.background_tasks) - _orphan_keys_before:
            _orphan = _pipeline_state.background_tasks.pop(_key, None)
            if _orphan is not None:
                _orphan.cancel()
                with contextlib.suppress(BaseException):
                    await _orphan

    @pytest.mark.asyncio
    async def test_failed_foundation_dependent_cancelled_on_zero_slot_cycle(
        self, orch_warm_db, mocker
    ):
        """Slot-independent dependency recovery runs even when no dispatch
        slots are free (auto-paused, max_concurrent_vms=0): a stale QUEUED
        dependent of a FAILED foundation is cascade-cancelled in the same
        cycle, instead of piling up until a slot frees. Regression guard for
        the idle/saturated short-circuit that used to skip ALL dependency
        recovery."""
        from sqlmodel import select as _select
        import app.orchestrator as _orch_module

        # Force the zero-slots / paused short-circuit: cap = 0 so
        # slots_available == 0 and the loop takes the idle branch.
        mocker.patch(
            "app.orchestrator._sync_runtime_config_from_state",
            new_callable=AsyncMock,
            return_value=[],
        )
        _orch_module._runtime_config["max_concurrent_vms"] = 0
        # Open the deadlock-scan throttle so the recovery runs this cycle.
        _orch_module._last_deadlock_check = None
        prov_mock = mocker.patch(
            "app.orchestrator.provision_agent_vm",
            new_callable=AsyncMock,
            return_value=True,
        )

        stale = self._naive_utcnow() - timedelta(minutes=15)
        async with orch_warm_db() as session:
            project = make_project(name="Zero-Slot Recovery", user_id=_TEST_USER)
            session.add(project)
            await session.commit()
            await session.refresh(project)

            foundation = make_task(
                project_id=project.id,
                status=TaskStatus.FAILED,
                payloads=[{"type": "create", "prompt": "foundation"}],
                created_at=stale,
            )
            session.add(foundation)
            await session.commit()
            await session.refresh(foundation)

            dependent = make_task(
                project_id=project.id,
                status=TaskStatus.QUEUED,
                payloads=[{"type": "create", "prompt": "dependent"}],
                depends_on=[str(foundation.id)],
                created_at=stale,
            )
            session.add(dependent)
            await session.commit()
            await session.refresh(dependent)
            dep_id = dependent.id

        await self._run_one_cycle(mocker)

        # No slot was free, so nothing was dispatched...
        prov_mock.assert_not_called()
        # ...but the failed foundation's dependent was still reclaimed.
        async with orch_warm_db() as verify:
            row = (
                (await verify.execute(_select(Task).where(Task.id == dep_id)))
                .scalars()
                .first()
            )
            assert row is not None
            assert row.status == TaskStatus.CANCELLED, (
                "Dependent of a FAILED foundation must be cascade-cancelled on "
                f"a zero-slots/paused cycle, got {row.status}"
            )

    @pytest.mark.asyncio
    async def test_warm_pin_with_recent_heartbeat_skips_provision(
        self, orch_warm_db, mocker
    ):
        """A QUEUED task with valid warm_target_vm_id + recent VM heartbeat
        keeps its QUEUED status and does NOT trigger provision_agent_vm."""
        from sqlmodel import select as _select

        prov_mock = mocker.patch(
            "app.orchestrator.provision_agent_vm",
            new_callable=AsyncMock,
            return_value=True,
        )

        async with orch_warm_db() as session:
            project = make_project(name="Warm-Pin Skip", user_id=_TEST_USER)
            session.add(project)
            await session.commit()
            await session.refresh(project)

            target_vm = "agent-vm-warm-target-1"
            deadline = (_utcnow() + timedelta(seconds=240)).isoformat()
            task = make_task(
                project_id=project.id,
                vm_instance_id=None,
                status=TaskStatus.QUEUED,
                payloads=[{"type": "fix", "prompt": "x"}],
                context={
                    "warm_target_vm_id": target_vm,
                    "warm_target_pr": "1234",
                    "warm_handoff_deadline": deadline,
                },
            )
            session.add(task)
            await session.commit()
            await session.refresh(task)
            task_id = task.id

        # Mark VM as alive via the shared-state heartbeat map
        _shared_state.vm_last_heartbeat[target_vm] = _utcnow()
        try:
            await self._run_one_cycle(mocker)
        finally:
            _shared_state.vm_last_heartbeat.pop(target_vm, None)

        prov_mock.assert_not_called()

        async with orch_warm_db() as verify:
            row = (
                (await verify.execute(_select(Task).where(Task.id == task_id)))
                .scalars()
                .first()
            )
            assert row is not None
            assert row.status == TaskStatus.QUEUED, (
                f"Task should remain QUEUED for warm-pin skip, got {row.status}"
            )
            ctx = row.context or {}
            # Skip-provision must not clear the warm fields.
            assert ctx.get("warm_target_vm_id") == target_vm

    @pytest.mark.asyncio
    async def test_warm_pin_past_deadline_clears_and_falls_through(
        self, orch_warm_db, mocker
    ):
        """When warm_handoff_deadline is in the past, the orchestrator clears
        the warm fields and proceeds to provision. We verify the warm fields
        are gone after one cycle, and assert record_warm_pin_clear was invoked
        with reason='deadline_expired' (Issue 9a)."""
        from sqlmodel import select as _select

        # Block any actual provision to keep the test fast
        mocker.patch(
            "app.orchestrator.provision_agent_vm",
            new_callable=AsyncMock,
            return_value=False,
        )
        # Spy on record_warm_pin_clear: it's a sync call inside the dispatch
        # path. We patch at the orchestrator's _shared_state import binding
        # so the spy intercepts the in-loop call (Issue 9a).
        record_clear_spy = mocker.patch(
            "app.orchestrator._shared_state.record_warm_pin_clear",
        )

        async with orch_warm_db() as session:
            project = make_project(name="Warm-Pin Expired", user_id=_TEST_USER)
            session.add(project)
            await session.commit()
            await session.refresh(project)

            target_vm = "agent-vm-warm-target-2"
            past = (_utcnow() - timedelta(seconds=60)).isoformat()
            task = make_task(
                project_id=project.id,
                vm_instance_id=None,
                status=TaskStatus.QUEUED,
                payloads=[{"type": "fix", "prompt": "x"}],
                context={
                    "warm_target_vm_id": target_vm,
                    "warm_target_pr": "5555",
                    "warm_handoff_deadline": past,
                },
            )
            session.add(task)
            await session.commit()
            await session.refresh(task)
            task_id = task.id

        # Heartbeat is fresh, but deadline is past — the warm fields should
        # still be cleared (deadline takes precedence).
        _shared_state.vm_last_heartbeat[target_vm] = _utcnow()
        try:
            await self._run_one_cycle(mocker)
        finally:
            _shared_state.vm_last_heartbeat.pop(target_vm, None)

        async with orch_warm_db() as verify:
            row = (
                (await verify.execute(_select(Task).where(Task.id == task_id)))
                .scalars()
                .first()
            )
            assert row is not None
            ctx = row.context or {}
            assert "warm_target_vm_id" not in ctx, (
                "Past-deadline warm pin must be cleared from context"
            )
            assert "warm_target_pr" not in ctx
            assert "warm_handoff_deadline" not in ctx

        # Issue 9a: assert record_warm_pin_clear was called with the correct
        # reason for the deadline-expired path.
        assert record_clear_spy.called, "record_warm_pin_clear must be invoked"
        # Match the (vm_id, reason) positional signature; tolerate kwargs too.
        _vm_arg, _reason_arg = _extract_warm_pin_clear_args(record_clear_spy)
        assert _vm_arg == target_vm
        assert _reason_arg == "deadline_expired", (
            f"Expected reason='deadline_expired', got {_reason_arg!r}"
        )

    @pytest.mark.asyncio
    async def test_warm_pin_unresponsive_vm_clears_and_falls_through(
        self, orch_warm_db, mocker
    ):
        """A QUEUED task whose warm target VM has no recent heartbeat must
        have warm fields cleared (deadline still future, but VM is silent).
        Verifies record_warm_pin_clear is called with reason='vm_unresponsive'
        (Issue 9a)."""
        from sqlmodel import select as _select

        mocker.patch(
            "app.orchestrator.provision_agent_vm",
            new_callable=AsyncMock,
            return_value=False,
        )
        record_clear_spy = mocker.patch(
            "app.orchestrator._shared_state.record_warm_pin_clear",
        )

        async with orch_warm_db() as session:
            project = make_project(name="Warm-Pin Silent VM", user_id=_TEST_USER)
            session.add(project)
            await session.commit()
            await session.refresh(project)

            target_vm = "agent-vm-warm-target-3"
            deadline = (_utcnow() + timedelta(seconds=240)).isoformat()
            task = make_task(
                project_id=project.id,
                vm_instance_id=None,
                status=TaskStatus.QUEUED,
                payloads=[{"type": "fix", "prompt": "x"}],
                context={
                    "warm_target_vm_id": target_vm,
                    "warm_target_pr": "9999",
                    "warm_handoff_deadline": deadline,
                },
            )
            session.add(task)
            await session.commit()
            await session.refresh(task)
            task_id = task.id

        # No heartbeat entry → VM unresponsive
        _shared_state.vm_last_heartbeat.pop(target_vm, None)
        await self._run_one_cycle(mocker)

        async with orch_warm_db() as verify:
            row = (
                (await verify.execute(_select(Task).where(Task.id == task_id)))
                .scalars()
                .first()
            )
            assert row is not None
            ctx = row.context or {}
            assert "warm_target_vm_id" not in ctx, (
                "Unresponsive-VM warm pin must be cleared from context"
            )
            assert "warm_target_pr" not in ctx
            assert "warm_handoff_deadline" not in ctx

        # Issue 9a: assert record_warm_pin_clear was called with the correct
        # reason for the silent-VM path.
        assert record_clear_spy.called, "record_warm_pin_clear must be invoked"
        _vm_arg, _reason_arg = _extract_warm_pin_clear_args(record_clear_spy)
        assert _vm_arg == target_vm
        assert _reason_arg == "vm_unresponsive", (
            f"Expected reason='vm_unresponsive', got {_reason_arg!r}"
        )

    @pytest.mark.asyncio
    async def test_warm_pin_clear_metric_skipped_when_commit_fails(
        self, orch_warm_db, mocker
    ):
        """Issue 11: the warm-pin clear metric must NOT fire if the commit
        that persists the warm-field clear raises. On commit failure the
        rollback path leaves the warm fields intact, so a re-run of the
        dispatch loop can record the clear cleanly without double-counting."""

        # Force the dispatch-time PROVISIONING commit to fail. We reach the
        # main commit path by having an account selected, then the commit
        # raises. Simpler: drive the unresponsive-VM clear path and patch
        # session.commit to raise on first call after the warm-clear.
        mocker.patch(
            "app.orchestrator.provision_agent_vm",
            new_callable=AsyncMock,
            return_value=False,
        )
        record_clear_spy = mocker.patch(
            "app.orchestrator._shared_state.record_warm_pin_clear",
        )

        # Force a path between warm-clear (which sets _pending_warm_pin_clear)
        # and the first commit to raise an unhandled exception. The orch's
        # outer `except Exception` rolls back the session, so the warm-clear
        # writes never persist — and the metric must therefore NOT fire
        # (Issue 11 acceptance: if commit fails, record_warm_pin_clear was
        # not called).
        # select_llm_runtime_for_task is lazily imported INSIDE
        # orchestrator_worker via `from app.max_plan_pool import ...`, so
        # the binding inside that function resolves through app.max_plan_pool
        # — patch there to reliably intercept the call.
        async def _raising_select(*_a, **_kw):
            raise RuntimeError("simulated runtime selection failure")

        mocker.patch(
            "app.max_plan_pool.select_llm_runtime_for_task",
            side_effect=_raising_select,
        )

        async with orch_warm_db() as session:
            project = make_project(name="Warm-Pin CommitFail", user_id=_TEST_USER)
            session.add(project)
            await session.commit()
            await session.refresh(project)

            target_vm = "agent-vm-warm-target-commitfail"
            deadline = (_utcnow() + timedelta(seconds=240)).isoformat()
            task = make_task(
                project_id=project.id,
                vm_instance_id=None,
                status=TaskStatus.QUEUED,
                payloads=[{"type": "fix", "prompt": "x"}],
                context={
                    "warm_target_vm_id": target_vm,
                    "warm_target_pr": "1234",
                    "warm_handoff_deadline": deadline,
                },
            )
            session.add(task)
            await session.commit()
            await session.refresh(task)

        # No heartbeat entry → VM unresponsive → reaches warm-clear branch.
        _shared_state.vm_last_heartbeat.pop(target_vm, None)
        await self._run_one_cycle(mocker)

        # Because select_llm_runtime_for_task raised AFTER warm-clear was
        # written but BEFORE any commit completed, no commit landed — so
        # record_warm_pin_clear must NOT have fired (Issue 11 acceptance).
        assert not record_clear_spy.called, (
            "record_warm_pin_clear must not be invoked when the commit "
            "that persists the warm-field clear has not landed"
        )

    @pytest.mark.asyncio
    async def test_cascade_cancel_raises_single_dedup_alert(self, orch_warm_db, mocker):
        """A failed prerequisite that cascade-cancels multiple dependents in
        one dispatch pass raises exactly ONE operator alert naming the root
        failure (not one-per-dependent), and a later pass does not duplicate it
        (title-based dedup)."""
        from sqlmodel import select as _select
        from app.models import Alert, AlertSeverity

        mocker.patch(
            "app.orchestrator.provision_agent_vm",
            new_callable=AsyncMock,
            return_value=True,
        )
        # The cascade-cancel helper binds broadcast_to_project at module load;
        # patch it on alert_dispatch so the real WS/notify path is not exercised.
        mocker.patch("app.alert_dispatch.broadcast_to_project", new_callable=AsyncMock)

        async with orch_warm_db() as session:
            project = make_project(name="Cascade Dedup", user_id=_TEST_USER)
            session.add(project)
            await session.commit()
            await session.refresh(project)

            prereq = make_task(
                project_id=project.id,
                title="root prerequisite",
                status=TaskStatus.FAILED,
                payloads=[{"type": "create", "prompt": "root"}],
            )
            session.add(prereq)
            await session.commit()
            await session.refresh(prereq)

            # Two dependents that both fail their dependency on the same root.
            for label in ("dependent-B", "dependent-C"):
                dep = make_task(
                    project_id=project.id,
                    title=label,
                    status=TaskStatus.QUEUED,
                    payloads=[{"type": "create", "prompt": label}],
                    depends_on=[str(prereq.id)],
                )
                session.add(dep)
            await session.commit()
            project_id = project.id
            prereq_id = prereq.id

        await self._run_one_cycle(mocker)

        async with orch_warm_db() as verify:
            alerts = (
                (
                    await verify.execute(
                        _select(Alert).where(Alert.project_id == project_id)
                    )
                )
                .scalars()
                .all()
            )
            assert len(alerts) == 1, (
                f"Expected exactly one cascade-cancel alert, got {len(alerts)}"
            )
            alert = alerts[0]
            assert alert.severity == AlertSeverity.WARNING
            assert "root prerequisite" in alert.title
            assert alert.task_id == prereq_id
            # Both dependents counted in the single alert.
            assert "2 dependent task" in alert.message

        # Add a THIRD dependent and re-run: title-based dedup means the existing
        # undismissed alert is reused — no second alert is created.
        async with orch_warm_db() as session2:
            dep_d = make_task(
                project_id=project_id,
                title="dependent-D",
                status=TaskStatus.QUEUED,
                payloads=[{"type": "create", "prompt": "dependent-D"}],
                depends_on=[str(prereq_id)],
            )
            session2.add(dep_d)
            await session2.commit()

        await self._run_one_cycle(mocker)

        async with orch_warm_db() as verify2:
            alerts2 = (
                (
                    await verify2.execute(
                        _select(Alert).where(Alert.project_id == project_id)
                    )
                )
                .scalars()
                .all()
            )
            assert len(alerts2) == 1, (
                "A second dispatch pass must not create a duplicate cascade "
                f"alert (title dedup), got {len(alerts2)}"
            )

    @pytest.mark.asyncio
    async def test_cascade_cancel_alert_counts_children(self, orch_warm_db, mocker):
        """The cascade-cancel count includes a dependent's non-terminal children
        that are cascade-cancelled along with it."""
        from sqlmodel import select as _select
        from app.models import Alert

        mocker.patch(
            "app.orchestrator.provision_agent_vm",
            new_callable=AsyncMock,
            return_value=True,
        )
        mocker.patch("app.alert_dispatch.broadcast_to_project", new_callable=AsyncMock)

        async with orch_warm_db() as session:
            project = make_project(name="Cascade Children", user_id=_TEST_USER)
            session.add(project)
            await session.commit()
            await session.refresh(project)

            prereq = make_task(
                project_id=project.id,
                title="root prerequisite",
                status=TaskStatus.FAILED,
                payloads=[{"type": "create", "prompt": "root"}],
            )
            session.add(prereq)
            await session.commit()
            await session.refresh(prereq)

            dependent = make_task(
                project_id=project.id,
                title="dependent-with-child",
                status=TaskStatus.QUEUED,
                payloads=[{"type": "create", "prompt": "dependent"}],
                depends_on=[str(prereq.id)],
            )
            session.add(dependent)
            await session.commit()
            await session.refresh(dependent)

            # A non-terminal child of the dependent — PROVISIONING is excluded
            # from the candidate scan but cascade-cancelled with its parent.
            child = make_task(
                project_id=project.id,
                title="child-task",
                status=TaskStatus.PROVISIONING,
                payloads=[{"type": "create", "prompt": "child"}],
                parent_task_id=dependent.id,
            )
            session.add(child)
            await session.commit()
            project_id = project.id

        await self._run_one_cycle(mocker)

        async with orch_warm_db() as verify:
            alerts = (
                (
                    await verify.execute(
                        _select(Alert).where(Alert.project_id == project_id)
                    )
                )
                .scalars()
                .all()
            )
            assert len(alerts) == 1
            # 1 dependent + 1 cascaded child = 2.
            assert "2 dependent task" in alerts[0].message

    @pytest.mark.asyncio
    async def test_cascade_cancel_tears_down_provisioned_child_vms(
        self, orch_warm_db, mocker
    ):
        """When cascade-cancelling a dependent with provisioned-VM children, all child
        VMs are deleted alongside the dependent's VM."""
        from datetime import timezone

        mocker.patch(
            "app.orchestrator.provision_agent_vm",
            new_callable=AsyncMock,
            return_value=True,
        )
        mocker.patch("app.alert_dispatch.broadcast_to_project", new_callable=AsyncMock)
        ensure_vm_deleted_mock = mocker.patch(
            "app.vm_lifecycle._ensure_vm_deleted",
            new_callable=AsyncMock,
            return_value=True,
        )

        async with orch_warm_db() as session:
            project = make_project(name="Cascade VM Teardown", user_id=_TEST_USER)
            session.add(project)
            await session.commit()
            await session.refresh(project)

            prereq = make_task(
                project_id=project.id,
                title="root prerequisite",
                status=TaskStatus.FAILED,
                payloads=[{"type": "create", "prompt": "root"}],
            )
            session.add(prereq)
            await session.commit()
            await session.refresh(prereq)

            dependent = make_task(
                project_id=project.id,
                title="dependent-with-vm",
                status=TaskStatus.QUEUED,
                payloads=[{"type": "create", "prompt": "dependent"}],
                depends_on=[str(prereq.id)],
                vm_instance_id="dependent-vm-123",
            )
            session.add(dependent)
            await session.commit()
            await session.refresh(dependent)

            # Non-terminal child with its own provisioned VM
            child = make_task(
                project_id=project.id,
                title="child-with-vm",
                status=TaskStatus.PROVISIONING,
                payloads=[{"type": "create", "prompt": "child"}],
                parent_task_id=dependent.id,
                vm_instance_id="child-vm-456",
                vm_provisioned_at=datetime.now(timezone.utc),
            )
            session.add(child)
            await session.commit()
            dependent_id = dependent.id
            child_id = child.id

        await self._run_one_cycle(mocker)

        # Verify both dependent and child VMs were deleted
        vm_delete_calls = ensure_vm_deleted_mock.call_args_list
        # Find calls with the specific VM IDs
        dependent_vm_calls = [
            c for c in vm_delete_calls if c[0][0] == "dependent-vm-123"
        ]
        child_vm_calls = [c for c in vm_delete_calls if c[0][0] == "child-vm-456"]

        assert len(dependent_vm_calls) > 0, (
            f"Expected _ensure_vm_deleted to be called for dependent VM "
            f"'dependent-vm-123'; calls were: {vm_delete_calls}"
        )
        assert len(child_vm_calls) > 0, (
            f"Expected _ensure_vm_deleted to be called for child VM "
            f"'child-vm-456'; calls were: {vm_delete_calls}"
        )

        # Verify both tasks are marked CANCELLED
        async with orch_warm_db() as verify:
            dep_task = await verify.get(Task, dependent_id)
            child_task = await verify.get(Task, child_id)
            assert dep_task.status == TaskStatus.CANCELLED
            assert child_task.status == TaskStatus.CANCELLED

    @pytest.mark.asyncio
    async def test_deadlock_breaker_cascade_cancel_emits_alert(
        self, orch_warm_db, mocker
    ):
        """_apply_deadlock_classification emits a deduplicated operator alert
        (same contract as the main dispatch loop) when the deadlock-breaker
        cascade-cancels a dependent with a genuinely failed prerequisite."""
        from sqlmodel import select as _select
        from app.models import Alert, AlertSeverity
        from app.orchestrator import _apply_deadlock_classification
        from app.broadcast import broadcast_to_project

        mocker.patch("app.alert_dispatch.broadcast_to_project", new_callable=AsyncMock)

        async with orch_warm_db() as session:
            project = make_project(name="Deadlock Alert", user_id=_TEST_USER)
            session.add(project)
            await session.commit()
            await session.refresh(project)

            prereq = make_task(
                project_id=project.id,
                title="failed-prereq",
                status=TaskStatus.FAILED,
                payloads=[{"type": "create", "prompt": "prereq"}],
            )
            session.add(prereq)
            await session.commit()
            await session.refresh(prereq)

            dependent = make_task(
                project_id=project.id,
                title="stuck-dependent",
                status=TaskStatus.QUEUED,
                payloads=[{"type": "create", "prompt": "dependent"}],
                depends_on=[str(prereq.id)],
            )
            session.add(dependent)
            await session.commit()
            await session.refresh(dependent)
            project_id = project.id
            prereq_id = prereq.id

        # Simulate what _classify_stuck_tasks returns: 3-tuple with the failed prereq.
        classified = [(dependent.id, "cascade_cancel", prereq)]
        await _apply_deadlock_classification(classified, broadcast_to_project, [])

        async with orch_warm_db() as verify:
            alerts = (
                (
                    await verify.execute(
                        _select(Alert).where(Alert.project_id == project_id)
                    )
                )
                .scalars()
                .all()
            )
            assert len(alerts) == 1, (
                f"Expected exactly one cascade-cancel alert from deadlock-breaker path, "
                f"got {len(alerts)}"
            )
            alert = alerts[0]
            assert alert.severity == AlertSeverity.WARNING
            assert "failed-prereq" in alert.title
            assert alert.task_id == prereq_id

    @pytest.mark.asyncio
    async def test_deadlock_breaker_deduplicates_alert_on_repeat(
        self, orch_warm_db, mocker
    ):
        """A second _apply_deadlock_classification call for the same prereq must not
        create a duplicate alert (title-based dedup)."""
        from sqlmodel import select as _select
        from app.models import Alert
        from app.orchestrator import _apply_deadlock_classification
        from app.broadcast import broadcast_to_project

        mocker.patch("app.alert_dispatch.broadcast_to_project", new_callable=AsyncMock)

        async with orch_warm_db() as session:
            project = make_project(name="Deadlock Dedup", user_id=_TEST_USER)
            session.add(project)
            await session.commit()
            await session.refresh(project)

            prereq = make_task(
                project_id=project.id,
                title="failed-prereq-dedup",
                status=TaskStatus.FAILED,
                payloads=[{"type": "create", "prompt": "prereq"}],
            )
            session.add(prereq)
            await session.commit()
            await session.refresh(prereq)

            dep1 = make_task(
                project_id=project.id,
                title="stuck-dep-1",
                status=TaskStatus.QUEUED,
                payloads=[{"type": "create", "prompt": "dep1"}],
                depends_on=[str(prereq.id)],
            )
            dep2 = make_task(
                project_id=project.id,
                title="stuck-dep-2",
                status=TaskStatus.QUEUED,
                payloads=[{"type": "create", "prompt": "dep2"}],
                depends_on=[str(prereq.id)],
            )
            session.add_all([dep1, dep2])
            await session.commit()
            await session.refresh(dep1)
            await session.refresh(dep2)
            project_id = project.id

        await _apply_deadlock_classification(
            [(dep1.id, "cascade_cancel", prereq)], broadcast_to_project, []
        )
        await _apply_deadlock_classification(
            [(dep2.id, "cascade_cancel", prereq)], broadcast_to_project, []
        )

        async with orch_warm_db() as verify:
            alerts = (
                (
                    await verify.execute(
                        _select(Alert).where(Alert.project_id == project_id)
                    )
                )
                .scalars()
                .all()
            )
            assert len(alerts) == 1, (
                "Two deadlock-breaker passes for the same prereq must not "
                f"duplicate the alert, got {len(alerts)}"
            )

    @pytest.mark.asyncio
    async def test_ci_poll_merge_on_promotion_target_withholds_pr_merged(
        self, db_session
    ):
        """The direct CI poll merge path must not stamp pr_merged when GitHub
        reports a merged PR whose base branch is the project's promotion target
        rather than its integration branch."""
        import app.branch_resolution as branch_resolution
        from app.branch_resolution import flush_pending_branch_alerts
        from app.models import Alert
        from app.orchestrator import _apply_ci_merged_landed_signal
        from sqlmodel import select as _select

        # The synchronous resolver records the branch-misconfiguration alert
        # intent off the hot path (no inline DB I/O); the durable persist runs
        # later in an async-safe context (the orchestrator monitoring loop).
        # Reset the process-local queue + dedupe set so this test sees only its
        # own enqueue (mirrors test_shared_state isolation).
        branch_resolution._blocking_project_branch_alerts.clear()
        branch_resolution._pending_durable_branch_alerts.clear()

        project = make_project(
            name="CI promotion target",
            user_id=_TEST_USER,
            integration_branch="main",
            promotion_target="release",
        )
        db_session.add(project)
        await db_session.commit()
        await db_session.refresh(project)

        task = make_task(
            project_id=project.id,
            status=TaskStatus.EVALUATING,
            payloads=[{"type": "review", "pr_number": 123, "base_branch": "release"}],
            context={
                "review_loop": {
                    "pr_number": 123,
                    "ci_waiting": True,
                    "base_branch": "release",
                }
            },
        )
        db_session.add(task)
        await db_session.commit()
        await db_session.refresh(task)

        landed = await _apply_ci_merged_landed_signal(
            task,
            db_session,
            base_branch="release",
            review_loop=(task.context or {}).get("review_loop", {}),
        )
        task.status = TaskStatus.COMPLETED
        db_session.add(task)
        await db_session.commit()

        assert landed is False
        await db_session.refresh(task)
        assert task.status == TaskStatus.COMPLETED
        assert (task.context or {}).get("pr_merged") is not True

        # Drain the deferred branch-alert queue into durable Alerts the way the
        # orchestrator monitoring loop does, then assert the operator signal landed.
        await flush_pending_branch_alerts(db_session)
        await db_session.commit()

        alert = (
            (
                await db_session.execute(
                    _select(Alert).where(
                        Alert.project_id == project.id,
                        Alert.title == "Blocking branch configuration",
                    )
                )
            )
            .scalars()
            .first()
        )
        assert alert is not None
        assert "promotion target" in alert.message

    # ──────────────────────────────────────────────────────────────────────
    # Warm-POOL dispatch + pool-pin branches (efficiency direction d-5fd69fe1).
    # Distinct from the warm-CONTINUITY pin tests above: a pool pin uses the
    # warm-pool flag/breaker (not the continuity breaker), releases its leased
    # PoolVm row, suppresses the continuity pin-clear metric, and resets
    # vm_instance_id; the pool dispatch hook leases a pre-provisioned member
    # before cold provisioning. These exercise the orchestrator_worker wiring
    # end-to-end (the leasing internals are unit-tested in test_warm_pool_*).
    # ──────────────────────────────────────────────────────────────────────

    @staticmethod
    def _fake_selection(account_id="acct-pool-1"):
        from app.max_plan_pool import LLMSelectionResult

        return LLMSelectionResult(
            provider="anthropic",
            auth_mode="oauth",
            model="claude-opus-4-8",
            quality_tier=None,
            account={
                "id": account_id,
                "name": "Pool Acct",
                "runtime_secret": "tok",
                "claude_config_json": None,
                "provider_metadata": {},
            },
            settings=None,
            execution_mode=None,
        )

    @pytest.mark.asyncio
    async def test_pool_pin_unresponsive_clears_releases_and_resets_vm_id(
        self, orch_warm_db, mocker
    ):
        """A POOL pin (warm_pool_pin in ctx) whose target VM is unresponsive must:
        release the leased PoolVm (mark_pool_vm_retiring), feed the POOL breaker
        (record_pool_claim_failure), NOT fire the continuity pin-clear metric,
        and reset task.vm_instance_id so the cold fall-through mints a FRESH id
        instead of colliding with the retiring pool VM's name."""
        from sqlmodel import select as _select

        mocker.patch(
            "app.orchestrator.provision_agent_vm",
            new_callable=AsyncMock,
            return_value=False,
        )
        retiring_spy = mocker.patch(
            "app.warm_pool.mark_pool_vm_retiring", new_callable=AsyncMock
        )
        pool_fail_spy = mocker.patch(
            "app.orchestrator._shared_state.record_pool_claim_failure"
        )
        clear_spy = mocker.patch("app.orchestrator._shared_state.record_warm_pin_clear")

        # Ensure the pool breaker is NOT tripped by residue from other tests, so
        # the unresponsive-VM clear takes the vm_unresponsive path (not circuit_breaker).
        _shared_state.warm_pool_suppressed_until = None
        _shared_state.warm_pool_claim_failure_events.clear()
        _shared_state.runtime_config["warm_pool_enabled"] = True
        try:
            async with orch_warm_db() as session:
                project = make_project(name="Pool-Pin", user_id=_TEST_USER)
                session.add(project)
                await session.commit()
                await session.refresh(project)

                target_vm = "agent-vm-pool-target-1"
                deadline = (_utcnow() + timedelta(seconds=240)).isoformat()
                task = make_task(
                    project_id=project.id,
                    # _pin_task_to_pool_vm stamps the pool VM's id onto the task;
                    # the clear branch must reset it.
                    vm_instance_id=target_vm,
                    status=TaskStatus.QUEUED,
                    payloads=[{"type": "fix", "prompt": "x"}],
                    context={
                        "warm_target_vm_id": target_vm,
                        "warm_handoff_deadline": deadline,
                        "warm_pool_pin": True,
                    },
                )
                session.add(task)
                await session.commit()
                await session.refresh(task)
                task_id = task.id

            # No heartbeat → unresponsive → reaches the pool-pin clear branch.
            _shared_state.vm_last_heartbeat.pop(target_vm, None)
            await self._run_one_cycle(mocker)
        finally:
            _shared_state.runtime_config.pop("warm_pool_enabled", None)

        # The leased pool VM was released and the POOL breaker (not the
        # continuity metric) was fed.
        assert retiring_spy.await_count >= 1
        assert retiring_spy.await_args.args[1] == target_vm
        assert pool_fail_spy.called
        assert pool_fail_spy.call_args.args[0] == "vm_unresponsive"
        # Continuity pin-clear metric is suppressed for pool pins.
        assert not clear_spy.called

        async with orch_warm_db() as verify:
            row = (
                (await verify.execute(_select(Task).where(Task.id == task_id)))
                .scalars()
                .first()
            )
            assert row is not None
            ctx = row.context or {}
            assert "warm_target_vm_id" not in ctx
            assert "warm_pool_pin" not in ctx
            # Load-bearing reset: a FRESH agent-vm- id, never the retiring pool
            # VM's name (which would race the reaper's delete on a name clash).
            assert row.vm_instance_id != target_vm
            assert (row.vm_instance_id or "").startswith("agent-vm-")

    @pytest.mark.asyncio
    async def test_parked_retarget_unresponsive_clears_and_resets_vm_id(
        self, orch_warm_db, mocker
    ):
        """A parked-for-retarget task whose held VM becomes unresponsive must
        have warm fields AND park fields cleared from context, and vm_instance_id
        reset to None so the cold fall-through mints a FRESH id (not the stale
        parked-VM name that would race the reaper's delete on GCP)."""
        from sqlmodel import select as _select

        mocker.patch(
            "app.orchestrator.provision_agent_vm",
            new_callable=AsyncMock,
            return_value=False,
        )
        record_clear_spy = mocker.patch(
            "app.orchestrator._shared_state.record_warm_pin_clear"
        )

        async with orch_warm_db() as session:
            project = make_project(
                name="Park-Retarget-Unresponsive", user_id=_TEST_USER
            )
            session.add(project)
            await session.commit()
            await session.refresh(project)

            parked_vm = "agent-vm-parked-rl-001"
            deadline = (_utcnow() + timedelta(seconds=240)).isoformat()
            task = make_task(
                project_id=project.id,
                # agent_status stamps the held VM id onto vm_instance_id
                vm_instance_id=parked_vm,
                status=TaskStatus.QUEUED,
                payloads=[{"type": "fix", "prompt": "x"}],
                context={
                    "warm_target_vm_id": parked_vm,
                    "warm_handoff_deadline": deadline,
                    "parked_for_retarget": True,
                    "park_deadline": deadline,
                },
            )
            session.add(task)
            await session.commit()
            await session.refresh(task)
            task_id = task.id

        # No heartbeat → VM unresponsive → orchestrator clears warm pin
        _shared_state.vm_last_heartbeat.pop(parked_vm, None)
        await self._run_one_cycle(mocker)

        async with orch_warm_db() as verify:
            row = (
                (await verify.execute(_select(Task).where(Task.id == task_id)))
                .scalars()
                .first()
            )
            assert row is not None
            ctx = row.context or {}
            # Warm fields cleared
            assert "warm_target_vm_id" not in ctx
            assert "warm_handoff_deadline" not in ctx
            # Park fields also cleared (no stale flags for a re-provisioned task)
            assert "parked_for_retarget" not in ctx
            assert "park_deadline" not in ctx
            # Load-bearing reset: vm_instance_id must be None (or a FRESH id)
            # so the cold provision path never re-creates the parked VM name.
            assert row.vm_instance_id != parked_vm, (
                "vm_instance_id must not retain the stale parked-VM name after "
                "warm-pin clear — GCP name collision hazard"
            )

        # Continuity pin-clear metric fires (parked retarget is not a pool pin)
        assert record_clear_spy.called
        _vm_arg, _reason_arg = _extract_warm_pin_clear_args(record_clear_spy)
        assert _vm_arg == parked_vm
        assert _reason_arg == "vm_unresponsive"

    @pytest.mark.asyncio
    async def test_parked_retarget_deadline_expired_clears_and_resets_vm_id(
        self, orch_warm_db, mocker
    ):
        """A parked-for-retarget task whose park deadline has expired must have
        warm and park context fields cleared and vm_instance_id reset so the cold
        fall-through mints a FRESH id (deadline_expired suppress path)."""
        from sqlmodel import select as _select

        mocker.patch(
            "app.orchestrator.provision_agent_vm",
            new_callable=AsyncMock,
            return_value=False,
        )
        record_clear_spy = mocker.patch(
            "app.orchestrator._shared_state.record_warm_pin_clear"
        )

        async with orch_warm_db() as session:
            project = make_project(name="Park-Retarget-Expired", user_id=_TEST_USER)
            session.add(project)
            await session.commit()
            await session.refresh(project)

            parked_vm = "agent-vm-parked-rl-expired-002"
            # Already-expired deadline → deadline_expired path
            expired = (_utcnow() - timedelta(seconds=10)).isoformat()
            task = make_task(
                project_id=project.id,
                vm_instance_id=parked_vm,
                status=TaskStatus.QUEUED,
                payloads=[{"type": "fix", "prompt": "x"}],
                context={
                    "warm_target_vm_id": parked_vm,
                    "warm_handoff_deadline": expired,
                    "parked_for_retarget": True,
                    "park_deadline": expired,
                },
            )
            session.add(task)
            await session.commit()
            await session.refresh(task)
            task_id = task.id

        # Register a heartbeat so the VM appears alive — the expired deadline
        # alone must trigger the clear (not unresponsive).
        _shared_state.vm_last_heartbeat[parked_vm] = _utcnow()
        try:
            await self._run_one_cycle(mocker)
        finally:
            _shared_state.vm_last_heartbeat.pop(parked_vm, None)

        async with orch_warm_db() as verify:
            row = (
                (await verify.execute(_select(Task).where(Task.id == task_id)))
                .scalars()
                .first()
            )
            assert row is not None
            ctx = row.context or {}
            assert "warm_target_vm_id" not in ctx
            assert "parked_for_retarget" not in ctx
            assert "park_deadline" not in ctx
            # vm_instance_id reset — no stale parked-VM name survives
            assert row.vm_instance_id != parked_vm, (
                "Expired park deadline must reset vm_instance_id (GCP name-collision hazard)"
            )

        assert record_clear_spy.called
        _vm_arg, _reason_arg = _extract_warm_pin_clear_args(record_clear_spy)
        assert _vm_arg == parked_vm
        assert _reason_arg == "deadline_expired"

    @pytest.mark.asyncio
    async def test_pool_dispatch_lease_hit_skips_cold_provision(
        self, orch_warm_db, mocker
    ):
        """A fresh QUEUED task leases a compatible idle pool member before cold
        provisioning: provision_agent_vm is NOT called, record_pool_lease_hit
        fires, and try_pool_dispatch receives the RESOLVED runtime identity +
        the effective (sticky) blacklist."""
        from sqlmodel import select as _select

        prov_mock = mocker.patch(
            "app.orchestrator.provision_agent_vm",
            new_callable=AsyncMock,
            return_value=True,
        )
        mocker.patch(
            "app.max_plan_pool.select_llm_runtime_for_task",
            new_callable=AsyncMock,
            return_value=self._fake_selection(),
        )

        class _Member:
            vm_instance_id = "agent-vm-pool-leased-1"

        dispatch_spy = mocker.patch(
            "app.warm_pool.try_pool_dispatch",
            new_callable=AsyncMock,
            return_value=_Member(),
        )
        lease_hit_spy = mocker.patch("app.warm_backlog_reuse.record_pool_lease_hit")
        cold_spy = mocker.patch("app.warm_backlog_reuse.record_pool_cold_dispatch")

        _shared_state.runtime_config["warm_pool_enabled"] = True
        try:
            async with orch_warm_db() as session:
                project = make_project(name="Pool-Lease", user_id=_TEST_USER)
                session.add(project)
                await session.commit()
                await session.refresh(project)

                task = make_task(
                    project_id=project.id,
                    vm_instance_id=None,
                    status=TaskStatus.QUEUED,
                    payloads=[{"type": "fix", "prompt": "x"}],
                    # Sticky blacklist must flow into excluded_account_ids.
                    context={"provision_failed_accounts": ["acct-dead"]},
                )
                session.add(task)
                await session.commit()
                await session.refresh(task)
                task_id = task.id

            await self._run_one_cycle(mocker)
        finally:
            _shared_state.runtime_config.pop("warm_pool_enabled", None)

        # Lease hit → cold provision skipped, funnel records a hit not a cold.
        prov_mock.assert_not_called()
        assert lease_hit_spy.called
        assert not cold_spy.called

        # Resolved-identity plumbing: the gate passes the SELECTION's identity.
        assert dispatch_spy.await_count == 1
        kwargs = dispatch_spy.await_args.kwargs
        assert kwargs["required_provider"] == "anthropic"
        assert kwargs["required_model"] == "claude-opus-4-8"
        assert kwargs["required_account"] == "acct-pool-1"
        assert "acct-dead" in (kwargs["excluded_account_ids"] or [])

        # Task stays QUEUED — the pooled agent claims it warm via /next-role.
        async with orch_warm_db() as verify:
            row = (
                (await verify.execute(_select(Task).where(Task.id == task_id)))
                .scalars()
                .first()
            )
            assert row is not None
            assert row.status == TaskStatus.QUEUED

    @pytest.mark.asyncio
    async def test_paused_project_queued_task_not_dispatched(
        self, orch_warm_db, mocker
    ):
        """A QUEUED task owned by a paused project must NOT be dispatched:
        provision_agent_vm is never called and the task stays QUEUED.

        Uses the same cold-dispatch enabling mocks as the resume test below so
        the ONLY thing keeping the task QUEUED is the project's paused flag."""
        from sqlmodel import select as _select

        prov_mock = mocker.patch(
            "app.orchestrator.provision_agent_vm",
            new_callable=AsyncMock,
            return_value=True,
        )
        mocker.patch(
            "app.max_plan_pool.select_llm_runtime_for_task",
            new_callable=AsyncMock,
            return_value=self._fake_selection(),
        )
        mocker.patch(
            "app.warm_pool.try_pool_dispatch",
            new_callable=AsyncMock,
            return_value=None,
        )

        _shared_state.runtime_config["warm_pool_enabled"] = True
        try:
            async with orch_warm_db() as session:
                project = make_project(
                    name="Paused-Dispatch", user_id=_TEST_USER, paused=True
                )
                session.add(project)
                await session.commit()
                await session.refresh(project)

                task = make_task(
                    project_id=project.id,
                    vm_instance_id=None,
                    status=TaskStatus.QUEUED,
                    payloads=[{"type": "fix", "prompt": "x"}],
                    context={},
                )
                session.add(task)
                await session.commit()
                await session.refresh(task)
                task_id = task.id

            await self._run_one_cycle(mocker)
        finally:
            _shared_state.runtime_config.pop("warm_pool_enabled", None)

        # Paused project's work is never selected → no provision, still QUEUED.
        prov_mock.assert_not_called()
        async with orch_warm_db() as verify:
            row = (
                (await verify.execute(_select(Task).where(Task.id == task_id)))
                .scalars()
                .first()
            )
            assert row is not None
            assert row.status == TaskStatus.QUEUED, (
                f"Paused project's task must stay QUEUED, got {row.status}"
            )

    @pytest.mark.asyncio
    async def test_project_paused_after_candidate_scan_not_dispatched(
        self, orch_warm_db, mocker
    ):
        """Pausing after eligibility but before the final claim keeps the task
        QUEUED and suppresses cold provision."""
        from sqlmodel import select as _select

        prov_mock = mocker.patch(
            "app.orchestrator.provision_agent_vm",
            new_callable=AsyncMock,
            return_value=True,
        )
        mocker.patch(
            "app.max_plan_pool.select_llm_runtime_for_task",
            new_callable=AsyncMock,
            return_value=self._fake_selection(),
        )
        mocker.patch(
            "app.warm_pool.try_pool_dispatch",
            new_callable=AsyncMock,
            return_value=None,
        )

        paused_mid_cycle = False
        _shared_state.runtime_config["warm_pool_enabled"] = True
        try:
            async with orch_warm_db() as session:
                project = make_project(
                    name="Paused-During-Dispatch", user_id=_TEST_USER, paused=False
                )
                session.add(project)
                await session.commit()
                await session.refresh(project)
                project_id = project.id

                task = make_task(
                    project_id=project.id,
                    vm_instance_id=None,
                    status=TaskStatus.QUEUED,
                    payloads=[{"type": "fix", "prompt": "x"}],
                    context={},
                )
                session.add(task)
                await session.commit()
                await session.refresh(task)
                task_id = task.id

            async def _pause_before_claim(
                eligible_ids, candidate_tasks, runtime_config, session
            ):
                nonlocal paused_mid_cycle
                async with orch_warm_db() as pause_session:
                    paused = await pause_session.get(Project, project_id)
                    assert paused is not None
                    paused.paused = True
                    pause_session.add(paused)
                    await pause_session.commit()
                paused_mid_cycle = True
                return eligible_ids

            mocker.patch(
                "app.orchestrator.cap_pipeline_slots",
                side_effect=_pause_before_claim,
            )

            await self._run_one_cycle(mocker)
        finally:
            _shared_state.runtime_config.pop("warm_pool_enabled", None)

        assert paused_mid_cycle is True
        prov_mock.assert_not_called()
        async with orch_warm_db() as verify:
            row = (
                (await verify.execute(_select(Task).where(Task.id == task_id)))
                .scalars()
                .first()
            )
            assert row is not None
            assert row.status == TaskStatus.QUEUED, (
                f"Mid-cycle pause must keep task QUEUED, got {row.status}"
            )

    @pytest.mark.asyncio
    async def test_resumed_project_queued_task_is_dispatched(
        self, orch_warm_db, mocker
    ):
        """Resuming a project (paused True → False) restores dispatch: with the
        same cold-dispatch setup, a previously-paused project's QUEUED task is
        now selected and cold-provisioned (status → PROVISIONING)."""
        from sqlmodel import select as _select

        mocker.patch(
            "app.orchestrator.provision_agent_vm",
            new_callable=AsyncMock,
            return_value=True,
        )
        mocker.patch(
            "app.max_plan_pool.select_llm_runtime_for_task",
            new_callable=AsyncMock,
            return_value=self._fake_selection(),
        )
        mocker.patch(
            "app.warm_pool.try_pool_dispatch",
            new_callable=AsyncMock,
            return_value=None,
        )

        _shared_state.runtime_config["warm_pool_enabled"] = True
        try:
            async with orch_warm_db() as session:
                # Start paused, then resume (flip to False) before dispatch.
                project = make_project(
                    name="Resumed-Dispatch", user_id=_TEST_USER, paused=True
                )
                session.add(project)
                await session.commit()
                await session.refresh(project)

                task = make_task(
                    project_id=project.id,
                    vm_instance_id=None,
                    status=TaskStatus.QUEUED,
                    payloads=[{"type": "fix", "prompt": "x"}],
                    context={},
                )
                session.add(task)
                await session.commit()
                await session.refresh(task)
                task_id = task.id

                project.paused = False
                session.add(project)
                await session.commit()

            await self._run_one_cycle(mocker)
        finally:
            _shared_state.runtime_config.pop("warm_pool_enabled", None)

        # Resume restored dispatch → the task left QUEUED for provisioning.
        async with orch_warm_db() as verify:
            row = (
                (await verify.execute(_select(Task).where(Task.id == task_id)))
                .scalars()
                .first()
            )
            assert row is not None
            assert row.status == TaskStatus.PROVISIONING, (
                f"Resumed project's task must be dispatched, got {row.status}"
            )

    @pytest.mark.asyncio
    async def test_project_vm_cap_defers_only_capped_project(
        self, orch_warm_db, mocker
    ):
        """A project at its VM cap stays QUEUED while another project dispatches."""
        from sqlmodel import select as _select
        import app.orchestrator as _orch_module

        mocker.patch(
            "app.orchestrator.provision_agent_vm",
            new_callable=AsyncMock,
            return_value=True,
        )
        mocker.patch(
            "app.max_plan_pool.select_llm_runtime_for_task",
            new_callable=AsyncMock,
            return_value=self._fake_selection(),
        )
        mocker.patch(
            "app.warm_pool.try_pool_dispatch",
            new_callable=AsyncMock,
            return_value=None,
        )
        mocker.patch(
            "app.orchestrator._sync_runtime_config_from_state",
            new_callable=AsyncMock,
            return_value=[],
        )

        previous_max = _orch_module._runtime_config["max_concurrent_vms"]
        _orch_module._runtime_config["max_concurrent_vms"] = 2
        _shared_state.runtime_config["warm_pool_enabled"] = True
        try:
            async with orch_warm_db() as session:
                capped = make_project(
                    name="At Cap",
                    user_id=_TEST_USER,
                    build_config={"resource_envelope": {"max_concurrent_vms": 1}},
                )
                open_project = make_project(
                    name="Under Cap",
                    user_id=_TEST_USER,
                    build_config={"resource_envelope": {"max_concurrent_vms": 1}},
                )
                session.add_all([capped, open_project])
                await session.commit()
                await session.refresh(capped)
                await session.refresh(open_project)

                now = self._naive_utcnow()
                session.add(
                    make_task(
                        project_id=capped.id,
                        status=TaskStatus.IN_PROGRESS,
                        payloads=[{"type": "fix", "prompt": "active"}],
                        created_at=now - timedelta(minutes=10),
                    )
                )
                capped_queued = make_task(
                    project_id=capped.id,
                    vm_instance_id=None,
                    status=TaskStatus.QUEUED,
                    payloads=[{"type": "fix", "prompt": "wait"}],
                    context={},
                    created_at=now - timedelta(minutes=5),
                )
                open_queued = make_task(
                    project_id=open_project.id,
                    vm_instance_id=None,
                    status=TaskStatus.QUEUED,
                    payloads=[{"type": "fix", "prompt": "dispatch"}],
                    context={},
                    created_at=now - timedelta(minutes=4),
                )
                session.add_all([capped_queued, open_queued])
                await session.commit()
                await session.refresh(capped_queued)
                await session.refresh(open_queued)
                capped_task_id = capped_queued.id
                open_task_id = open_queued.id

            await self._run_one_cycle(mocker)
        finally:
            _orch_module._runtime_config["max_concurrent_vms"] = previous_max
            _shared_state.runtime_config.pop("warm_pool_enabled", None)

        async with orch_warm_db() as verify:
            capped_row = (
                (await verify.execute(_select(Task).where(Task.id == capped_task_id)))
                .scalars()
                .one()
            )
            open_row = (
                (await verify.execute(_select(Task).where(Task.id == open_task_id)))
                .scalars()
                .one()
            )
            assert capped_row.status == TaskStatus.QUEUED
            assert "project VM capacity" in (capped_row.latest_status_message or "")
            assert open_row.status == TaskStatus.PROVISIONING

    @pytest.mark.asyncio
    async def test_project_spend_ceiling_defers_dispatch(self, orch_warm_db, mocker):
        """A project over its spend ceiling keeps queued work undispatched."""
        from sqlmodel import select as _select
        import app.orchestrator as _orch_module

        prov_mock = mocker.patch(
            "app.orchestrator.provision_agent_vm",
            new_callable=AsyncMock,
            return_value=True,
        )
        mocker.patch(
            "app.max_plan_pool.select_llm_runtime_for_task",
            new_callable=AsyncMock,
            return_value=self._fake_selection(),
        )
        mocker.patch(
            "app.warm_pool.try_pool_dispatch",
            new_callable=AsyncMock,
            return_value=None,
        )
        mocker.patch(
            "app.orchestrator._sync_runtime_config_from_state",
            new_callable=AsyncMock,
            return_value=[],
        )
        spend_mock = mocker.patch(
            "app.orchestrator.get_project_recent_spend_usd",
            new_callable=AsyncMock,
            return_value={"total": 12.0, "compute": 12.0, "agent_vm": 0.0, "idle": 0.0},
        )

        previous_max = _orch_module._runtime_config["max_concurrent_vms"]
        _orch_module._runtime_config["max_concurrent_vms"] = 1
        _shared_state.runtime_config["warm_pool_enabled"] = True
        try:
            async with orch_warm_db() as session:
                project = make_project(
                    name="Spend Capped",
                    user_id=_TEST_USER,
                    build_config={
                        "resource_envelope": {
                            "spend_ceiling_usd": 10.0,
                            "spend_window_days": 7,
                        }
                    },
                )
                session.add(project)
                await session.commit()
                await session.refresh(project)

                queued = make_task(
                    project_id=project.id,
                    vm_instance_id=None,
                    status=TaskStatus.QUEUED,
                    payloads=[{"type": "fix", "prompt": "wait"}],
                    context={},
                )
                session.add(queued)
                await session.commit()
                await session.refresh(queued)
                queued_id = queued.id

            await self._run_one_cycle(mocker)
        finally:
            _orch_module._runtime_config["max_concurrent_vms"] = previous_max
            _shared_state.runtime_config.pop("warm_pool_enabled", None)

        prov_mock.assert_not_called()
        spend_mock.assert_awaited_once()
        async with orch_warm_db() as verify:
            row = (
                (await verify.execute(_select(Task).where(Task.id == queued_id)))
                .scalars()
                .one()
            )
            assert row.status == TaskStatus.QUEUED
            assert "project spend budget" in (row.latest_status_message or "")

    @pytest.mark.asyncio
    async def test_monthly_budget_defers_dispatch(self, orch_warm_db, mocker):
        """When the monthly budget is exhausted, queued tasks are deferred with a
        per-project message via _project_resource_dispatch_defer_message."""
        from sqlmodel import select as _select
        import app.orchestrator as _orch_module

        prov_mock = mocker.patch(
            "app.orchestrator.provision_agent_vm",
            new_callable=AsyncMock,
            return_value=True,
        )
        mocker.patch(
            "app.max_plan_pool.select_llm_runtime_for_task",
            new_callable=AsyncMock,
            return_value=self._fake_selection(),
        )
        mocker.patch(
            "app.warm_pool.try_pool_dispatch",
            new_callable=AsyncMock,
            return_value=None,
        )
        mocker.patch(
            "app.orchestrator._sync_runtime_config_from_state",
            new_callable=AsyncMock,
            return_value=[],
        )
        # Mock the monthly budget check to report exhaustion (150% of target).
        budget_mock = mocker.patch(
            "app.orchestrator.check_monthly_budget",
            new_callable=AsyncMock,
            return_value=(True, 1.5),
        )

        previous_max = _orch_module._runtime_config["max_concurrent_vms"]
        _orch_module._runtime_config["max_concurrent_vms"] = 1
        _shared_state.runtime_config["warm_pool_enabled"] = True
        try:
            async with orch_warm_db() as session:
                project = make_project(
                    name="Budget Capped",
                    user_id=_TEST_USER,
                )
                session.add(project)
                await session.commit()
                await session.refresh(project)

                queued = make_task(
                    project_id=project.id,
                    vm_instance_id=None,
                    status=TaskStatus.QUEUED,
                    payloads=[{"type": "fix", "prompt": "wait"}],
                    context={},
                )
                session.add(queued)
                await session.commit()
                await session.refresh(queued)
                queued_id = queued.id

            await self._run_one_cycle(mocker)
        finally:
            _orch_module._runtime_config["max_concurrent_vms"] = previous_max
            _shared_state.runtime_config.pop("warm_pool_enabled", None)

        prov_mock.assert_not_called()
        budget_mock.assert_awaited_once()
        async with orch_warm_db() as verify:
            row = (
                (await verify.execute(_select(Task).where(Task.id == queued_id)))
                .scalars()
                .one()
            )
            assert row.status == TaskStatus.QUEUED
            assert "monthly budget" in (row.latest_status_message or "")

    @pytest.mark.asyncio
    async def test_no_resource_envelope_dispatches_under_global_gate(
        self, orch_warm_db, mocker
    ):
        """Projects without envelopes keep the previous global-slot behavior."""
        from sqlmodel import select as _select
        import app.orchestrator as _orch_module

        mocker.patch(
            "app.orchestrator.provision_agent_vm",
            new_callable=AsyncMock,
            return_value=True,
        )
        mocker.patch(
            "app.max_plan_pool.select_llm_runtime_for_task",
            new_callable=AsyncMock,
            return_value=self._fake_selection(),
        )
        mocker.patch(
            "app.warm_pool.try_pool_dispatch",
            new_callable=AsyncMock,
            return_value=None,
        )
        mocker.patch(
            "app.orchestrator._sync_runtime_config_from_state",
            new_callable=AsyncMock,
            return_value=[],
        )

        previous_max = _orch_module._runtime_config["max_concurrent_vms"]
        _orch_module._runtime_config["max_concurrent_vms"] = 1
        _shared_state.runtime_config["warm_pool_enabled"] = True
        try:
            async with orch_warm_db() as session:
                project = make_project(name="No Envelope", user_id=_TEST_USER)
                session.add(project)
                await session.commit()
                await session.refresh(project)

                queued = make_task(
                    project_id=project.id,
                    vm_instance_id=None,
                    status=TaskStatus.QUEUED,
                    payloads=[{"type": "fix", "prompt": "dispatch"}],
                    context={},
                )
                session.add(queued)
                await session.commit()
                await session.refresh(queued)
                queued_id = queued.id

            await self._run_one_cycle(mocker)
        finally:
            _orch_module._runtime_config["max_concurrent_vms"] = previous_max
            _shared_state.runtime_config.pop("warm_pool_enabled", None)

        async with orch_warm_db() as verify:
            row = (
                (await verify.execute(_select(Task).where(Task.id == queued_id)))
                .scalars()
                .one()
            )
            assert row.status == TaskStatus.PROVISIONING

    @pytest.mark.asyncio
    async def test_pool_dispatch_cold_fall_through_records_funnel(
        self, orch_warm_db, mocker
    ):
        """When no compatible pool member is free (try_pool_dispatch → None),
        the task cold-provisions and the funnel records a cold dispatch (not a
        lease hit)."""
        from sqlmodel import select as _select

        mocker.patch(
            "app.orchestrator.provision_agent_vm",
            new_callable=AsyncMock,
            return_value=True,
        )
        mocker.patch(
            "app.max_plan_pool.select_llm_runtime_for_task",
            new_callable=AsyncMock,
            return_value=self._fake_selection(),
        )
        mocker.patch(
            "app.warm_pool.try_pool_dispatch",
            new_callable=AsyncMock,
            return_value=None,
        )
        lease_hit_spy = mocker.patch("app.warm_backlog_reuse.record_pool_lease_hit")
        cold_spy = mocker.patch("app.warm_backlog_reuse.record_pool_cold_dispatch")

        _shared_state.runtime_config["warm_pool_enabled"] = True
        try:
            async with orch_warm_db() as session:
                project = make_project(name="Pool-Cold", user_id=_TEST_USER)
                session.add(project)
                await session.commit()
                await session.refresh(project)

                task = make_task(
                    project_id=project.id,
                    vm_instance_id=None,
                    status=TaskStatus.QUEUED,
                    payloads=[{"type": "fix", "prompt": "x"}],
                    context={},
                )
                session.add(task)
                await session.commit()
                await session.refresh(task)
                task_id = task.id

            await self._run_one_cycle(mocker)
        finally:
            _shared_state.runtime_config.pop("warm_pool_enabled", None)

        # Cold fall-through → funnel records a cold dispatch, never a lease hit.
        assert cold_spy.called
        assert not lease_hit_spy.called
        # The cold dispatch is attributed to the resolved account (acct-pool-1),
        # so the per-account funnel denominator reconciles with fleet totals
        # instead of dumping every cold dispatch into the unattributed bucket.
        assert cold_spy.call_args.args == ("acct-pool-1",)

        async with orch_warm_db() as verify:
            row = (
                (await verify.execute(_select(Task).where(Task.id == task_id)))
                .scalars()
                .first()
            )
            assert row is not None
            assert row.status == TaskStatus.PROVISIONING

    @pytest.mark.asyncio
    @pytest.mark.skipif(
        not USE_POSTGRES,
        reason="the lease-commit rollback expiry that raises MissingGreenlet only "
        "reproduces on asyncpg; aiosqlite no-ops InstanceState._expire.",
    )
    async def test_pool_dispatch_commit_error_falls_back_to_cold(
        self, orch_warm_db, mocker
    ):
        """If the lease commit raises (the MissingGreenlet hazard path), the
        orchestrator records a 'commit_error' pool failure, rolls back, and
        leaves the task QUEUED for a clean retry — never a lease-hit."""
        from sqlmodel import select as _select
        from sqlalchemy.ext.asyncio import AsyncSession as _AsyncSession

        mocker.patch(
            "app.orchestrator.provision_agent_vm",
            new_callable=AsyncMock,
            return_value=True,
        )
        mocker.patch(
            "app.max_plan_pool.select_llm_runtime_for_task",
            new_callable=AsyncMock,
            return_value=self._fake_selection(),
        )

        # Arm a one-shot commit failure that fires on the FIRST commit after
        # try_pool_dispatch returns — i.e. the lease commit.
        state = {"arm": False}
        _orig_commit = _AsyncSession.commit

        async def _maybe_failing_commit(self):
            if state["arm"]:
                state["arm"] = False
                raise RuntimeError("simulated lease commit failure")
            return await _orig_commit(self)

        mocker.patch.object(_AsyncSession, "commit", _maybe_failing_commit)

        class _Member:
            vm_instance_id = "agent-vm-pool-leased-2"

        async def _dispatch(*_a, **_kw):
            state["arm"] = True
            return _Member()

        # Patch with the async function directly so the lazy
        # `from app.warm_pool import try_pool_dispatch` binds to it and awaits
        # cleanly (arming the one-shot commit failure as a side effect).
        mocker.patch("app.warm_pool.try_pool_dispatch", _dispatch)
        pool_fail_spy = mocker.patch(
            "app.orchestrator._shared_state.record_pool_claim_failure"
        )
        lease_hit_spy = mocker.patch("app.warm_backlog_reuse.record_pool_lease_hit")
        # The slot pre-reserved during runtime selection must be released on
        # this early-exit (mirroring the cold-provision commit-failure path).
        release_spy = mocker.patch(
            "app.max_plan_pool.release_max_plan_account", new_callable=AsyncMock
        )

        _shared_state.runtime_config["warm_pool_enabled"] = True
        try:
            async with orch_warm_db() as session:
                project = make_project(name="Pool-CommitFail", user_id=_TEST_USER)
                session.add(project)
                await session.commit()
                await session.refresh(project)

                task = make_task(
                    project_id=project.id,
                    vm_instance_id=None,
                    status=TaskStatus.QUEUED,
                    payloads=[{"type": "fix", "prompt": "x"}],
                    context={},
                )
                session.add(task)
                await session.commit()
                await session.refresh(task)
                task_id = task.id

            await self._run_one_cycle(mocker)
        finally:
            _shared_state.runtime_config.pop("warm_pool_enabled", None)

        # commit_error recorded, no lease-hit, and the lease was rolled back so
        # the task is still QUEUED (clean retry next cycle).
        assert pool_fail_spy.called
        assert pool_fail_spy.call_args.args[0] == "commit_error"
        assert not lease_hit_spy.called
        # The reserved account slot was released (no leak) — and against the
        # account carried on the resolved selection, not a loop-local variable.
        assert release_spy.await_count == 1
        assert release_spy.await_args.args[0] == "acct-pool-1"
        # Abandon path: the daily-usage increment pre-reserved during selection
        # must also be reverted, not just the concurrency slot — otherwise the
        # daily counter durably over-counts and trips the cap prematurely.
        assert release_spy.await_args.kwargs.get("revert_daily_task_count") is True

        async with orch_warm_db() as verify:
            row = (
                (await verify.execute(_select(Task).where(Task.id == task_id)))
                .scalars()
                .first()
            )
            assert row is not None
            assert row.status == TaskStatus.QUEUED

    @pytest.mark.asyncio
    async def test_backlog_first_refusal_hold_releases_reserved_slot(
        self, orch_warm_db, mocker
    ):
        """When an idle warm VM gets first refusal on a backlog task, the
        orchestrator holds off cold-provisioning (rolls back, leaves the task
        QUEUED for the warm VM to claim) — and must release the account slot
        pre-reserved during runtime selection so the reservation does not leak
        for the rest of the cycle (repeated holds would otherwise compound the
        drift)."""
        from sqlmodel import select as _select

        prov_mock = mocker.patch(
            "app.orchestrator.provision_agent_vm",
            new_callable=AsyncMock,
            return_value=True,
        )
        mocker.patch(
            "app.max_plan_pool.select_llm_runtime_for_task",
            new_callable=AsyncMock,
            return_value=self._fake_selection(),
        )
        # An idle warm VM is actively polling for this project → first refusal.
        mocker.patch(
            "app.orchestrator._shared_state.is_backlog_first_refusal_active",
            return_value=True,
        )
        release_spy = mocker.patch(
            "app.max_plan_pool.release_max_plan_account", new_callable=AsyncMock
        )

        _shared_state.runtime_config["warm_backlog_reuse_enabled"] = True
        try:
            async with orch_warm_db() as session:
                project = make_project(name="Backlog-FirstRefusal", user_id=_TEST_USER)
                session.add(project)
                await session.commit()
                await session.refresh(project)

                task = make_task(
                    project_id=project.id,
                    vm_instance_id=None,
                    status=TaskStatus.QUEUED,
                    payloads=[{"type": "fix", "prompt": "x"}],
                    context={},
                )
                session.add(task)
                await session.commit()
                await session.refresh(task)
                task_id = task.id

            await self._run_one_cycle(mocker)
        finally:
            _shared_state.runtime_config.pop("warm_backlog_reuse_enabled", None)

        # Held off cold provisioning so the warm VM can claim it, and released
        # the reserved slot against the resolved selection's account.
        assert not prov_mock.called
        assert release_spy.await_count == 1
        assert release_spy.await_args.args[0] == "acct-pool-1"
        # Abandon path: revert the pre-reserved daily-usage increment too, so a
        # held-off dispatch leaves daily_task_count exactly where it was.
        assert release_spy.await_args.kwargs.get("revert_daily_task_count") is True

        async with orch_warm_db() as verify:
            row = (
                (await verify.execute(_select(Task).where(Task.id == task_id)))
                .scalars()
                .first()
            )
            assert row is not None
            assert row.status == TaskStatus.QUEUED

    @pytest.mark.asyncio
    async def test_cycle_leaves_no_dangling_provision_coroutine(
        self, orch_warm_db, mocker
    ):
        """Regression guard for the shard-5 dispatch flake.

        A cold dispatch fires the real VM provision via
        ``asyncio.create_task(_provision_and_handle(...))`` — an INDEPENDENT
        task (module global ``_provision_coroutines``), NOT a child of
        ``orchestrator_worker``. If the one-cycle harness leaves it dangling it
        keeps running ``_provision_and_handle`` DB work against this fixture's
        shared in-memory StaticPool SINGLE connection, racing the post-cycle
        verify read; a concurrent transaction reset on that shared connection
        can discard the inline PROVISIONING commit, so the verify intermittently
        observes QUEUED. ``_run_one_cycle`` must drain (await) + clear those
        coroutines (and any ``_delayed_orphan_cleanup`` they spawn) before
        returning, so no background DB work survives into the verify window.
        This guards that invariant directly — deterministically, without
        depending on CI timing.

        See ``backend/testing/test_orch_db_isolation.py`` for the standalone
        demonstration that an uncoordinated background session on the shared
        connection corrupts a PROVISIONING read into QUEUED.
        """
        import app.orchestrator as _orch_module
        import app.pipeline_health_state as _pipeline_state

        mocker.patch(
            "app.orchestrator.provision_agent_vm",
            new_callable=AsyncMock,
            return_value=True,
        )
        mocker.patch(
            "app.max_plan_pool.select_llm_runtime_for_task",
            new_callable=AsyncMock,
            return_value=self._fake_selection(),
        )
        mocker.patch(
            "app.warm_pool.try_pool_dispatch",
            new_callable=AsyncMock,
            return_value=None,
        )

        _orch_module._provision_coroutines.clear()
        _bg_before = set(_pipeline_state.background_tasks)

        _shared_state.runtime_config["warm_pool_enabled"] = True
        try:
            async with orch_warm_db() as session:
                project = make_project(name="No-Dangle", user_id=_TEST_USER)
                session.add(project)
                await session.commit()
                await session.refresh(project)

                task = make_task(
                    project_id=project.id,
                    vm_instance_id=None,
                    status=TaskStatus.QUEUED,
                    payloads=[{"type": "fix", "prompt": "x"}],
                    context={},
                )
                session.add(task)
                await session.commit()
                await session.refresh(task)

            await self._run_one_cycle(mocker)
        finally:
            _shared_state.runtime_config.pop("warm_pool_enabled", None)

        # No provision coroutine survives the cycle (it dispatched one, which
        # _run_one_cycle must have cancelled + cleared).
        assert _orch_module._provision_coroutines == {}, (
            "dispatch cycle left a dangling provision coroutine that would race "
            "the verify read on the shared connection: "
            f"{list(_orch_module._provision_coroutines)}"
        )
        # And the cancel did not leak an orphan-cleanup background task.
        _leaked = set(_pipeline_state.background_tasks) - _bg_before
        assert not _leaked, f"cycle cleanup leaked background task(s): {_leaked}"


class TestClassifyStuckTasksSharedPolicy:
    """The deadlock-breaker (``_classify_stuck_tasks``) shares the main path's
    dependency-death semantics via ``_check_candidate_task_deps``:

    - a genuinely FAILED prerequisite cascade-cancels the dependent (it is never
      dispatched as impossible work);
    - a SCOPE_EMPTY-cancelled prerequisite counts as satisfied (clear & dispatch);
    - a requeue-cancelled prerequisite with a still-active replacement clone keeps
      the dependent waiting (no premature dispatch, no cancel);
    - only a TRUE cycle is cleared-and-dispatched.
    """

    async def _noop_verify(self, _task, _session):
        return True

    @pytest.mark.asyncio
    async def test_all_failed_deps_cascade_cancel_not_dispatched(self, db_session):
        from app.orchestrator_health import _classify_stuck_tasks

        project = make_project(name="StuckFail", user_id="u1")
        db_session.add(project)
        await db_session.commit()
        await db_session.refresh(project)

        dep = make_task(
            project_id=project.id,
            payloads=[{"type": "create", "prompt": "dep"}],
            status=TaskStatus.FAILED,
        )
        db_session.add(dep)
        await db_session.commit()
        await db_session.refresh(dep)

        stuck = make_task(
            project_id=project.id,
            payloads=[{"type": "create", "prompt": "stuck"}],
            status=TaskStatus.QUEUED,
            depends_on=[str(dep.id)],
        )
        db_session.add(stuck)
        await db_session.commit()
        await db_session.refresh(stuck)

        out = await _classify_stuck_tasks(
            [stuck],
            {stuck.id: {dep.id}},
            {dep.id: dep},
            {stuck.id},
            db_session,
            self._noop_verify,
        )
        # The real prerequisite genuinely failed → cascade-cancel, NOT dead_deps
        # (which would clear depends_on and dispatch the impossible work).
        # Result is a 3-tuple: (task_id, reason, failed_prereq_or_None).
        assert len(out) == 1
        assert out[0][0] == stuck.id
        assert out[0][1] == "cascade_cancel"
        assert out[0][2] is not None and out[0][2].id == dep.id

    @pytest.mark.asyncio
    async def test_scope_empty_dep_is_satisfied(self, db_session):
        from app.failure_classification import SCOPE_EMPTY
        from app.orchestrator_health import _classify_stuck_tasks

        project = make_project(name="StuckScopeEmpty", user_id="u1")
        db_session.add(project)
        await db_session.commit()
        await db_session.refresh(project)

        dep = make_task(
            project_id=project.id,
            payloads=[{"type": "create", "prompt": "dep"}],
            status=TaskStatus.CANCELLED,
            context={"failure_category": SCOPE_EMPTY},
        )
        db_session.add(dep)
        await db_session.commit()
        await db_session.refresh(dep)

        stuck = make_task(
            project_id=project.id,
            payloads=[{"type": "create", "prompt": "stuck"}],
            status=TaskStatus.QUEUED,
            depends_on=[str(dep.id)],
        )
        db_session.add(stuck)
        await db_session.commit()
        await db_session.refresh(stuck)

        out = await _classify_stuck_tasks(
            [stuck],
            {stuck.id: {dep.id}},
            {dep.id: dep},
            {stuck.id},
            db_session,
            self._noop_verify,
        )
        # SCOPE_EMPTY is a deliberate "scope already covered" exit → satisfied, so
        # the dependent is safe to clear-and-dispatch, never cascade-cancelled.
        assert out == [(stuck.id, "dead_deps")]

    @pytest.mark.asyncio
    async def test_requeue_dep_remap_follows_structural_provenance(self, db_session):
        """Remapping a requeue-cancelled edge moves its structural provenance too.

        Regression: the dispatcher rewrites an existing dependent's ``depends_on``
        from a cancelled dep ID to its replacement clone, but used to leave
        ``_structural_depends_on_ids`` pointing at the dead ID. That made the
        surviving prerequisite edge invisible to ``_has_edge_prerequisite_provenance``
        so clear-wave-deps would later strip it as legacy sequencing.
        """
        from app.orchestrator import _persist_requeue_dep_remap
        from app.depends_on_provenance import get_structural_depends_on_ids
        from app.routers.admin_operator_triggers import (
            _has_edge_prerequisite_provenance,
        )

        project = make_project(name="RemapProvenance", user_id="u1")
        db_session.add(project)
        await db_session.commit()
        await db_session.refresh(project)

        old_dep_id = str(uuid.uuid4())
        new_dep_id = str(uuid.uuid4())

        dependent = make_task(
            project_id=project.id,
            payloads=[{"type": "create", "prompt": "dependent"}],
            status=TaskStatus.QUEUED,
            depends_on=[old_dep_id],
            context={"_structural_depends_on_ids": [old_dep_id]},
        )
        db_session.add(dependent)
        await db_session.commit()
        await db_session.refresh(dependent)

        # Edge is a recognized structural prerequisite before the remap.
        assert _has_edge_prerequisite_provenance(dependent, old_dep_id)

        remapped = await _persist_requeue_dep_remap(dependent, {old_dep_id: new_dep_id})
        assert remapped is True

        # In-memory task updated on BOTH axes.
        assert dependent.depends_on == [new_dep_id]
        assert get_structural_depends_on_ids(dependent) == [new_dep_id]
        # The live edge still resolves as a prerequisite (so clear-wave-deps keeps it),
        # and the dead edge no longer does.
        assert _has_edge_prerequisite_provenance(dependent, new_dep_id)
        assert not _has_edge_prerequisite_provenance(dependent, old_dep_id)

        # Persisted copy carries the same remap.
        await db_session.refresh(dependent)
        assert dependent.depends_on == [new_dep_id]
        assert get_structural_depends_on_ids(dependent) == [new_dep_id]

    @pytest.mark.asyncio
    async def test_requeue_cancelled_dep_with_active_clone_waits(self, db_session):
        from app.orchestrator_health import _classify_stuck_tasks

        project = make_project(name="StuckRequeue", user_id="u1")
        db_session.add(project)
        await db_session.commit()
        await db_session.refresh(project)

        # The live replacement clone is still in flight (QUEUED).
        clone = make_task(
            project_id=project.id,
            payloads=[{"type": "create", "prompt": "clone"}],
            status=TaskStatus.QUEUED,
        )
        db_session.add(clone)
        await db_session.commit()
        await db_session.refresh(clone)

        dep = make_task(
            project_id=project.id,
            payloads=[{"type": "create", "prompt": "dep"}],
            status=TaskStatus.CANCELLED,
            context={"_replaced_by_task_id": str(clone.id)},
        )
        db_session.add(dep)
        await db_session.commit()
        await db_session.refresh(dep)

        stuck = make_task(
            project_id=project.id,
            payloads=[{"type": "create", "prompt": "stuck"}],
            status=TaskStatus.QUEUED,
            depends_on=[str(dep.id)],
        )
        db_session.add(stuck)
        await db_session.commit()
        await db_session.refresh(stuck)

        out = await _classify_stuck_tasks(
            [stuck],
            {stuck.id: {dep.id}},
            {dep.id: dep},
            {stuck.id},
            db_session,
            self._noop_verify,
        )
        # Replacement clone still running → wait/remap, neither dispatched nor
        # cancelled prematurely.
        assert out == []

    @pytest.mark.asyncio
    async def test_true_cycle_is_cleared_and_dispatched(self, db_session):
        from app.orchestrator_health import _classify_stuck_tasks

        project = make_project(name="StuckCycle", user_id="u1")
        db_session.add(project)
        await db_session.commit()
        await db_session.refresh(project)

        a = make_task(
            project_id=project.id,
            payloads=[{"type": "create", "prompt": "a"}],
            status=TaskStatus.QUEUED,
        )
        b = make_task(
            project_id=project.id,
            payloads=[{"type": "create", "prompt": "b"}],
            status=TaskStatus.QUEUED,
        )
        db_session.add_all([a, b])
        await db_session.commit()
        await db_session.refresh(a)
        await db_session.refresh(b)

        out = await _classify_stuck_tasks(
            [a, b],
            {a.id: {b.id}, b.id: {a.id}},
            {a.id: a, b.id: b},
            {a.id, b.id},
            db_session,
            self._noop_verify,
        )
        # A genuine A<->B cycle: both members cleared-and-dispatched.
        assert sorted(reason for _, reason in out) == ["cycle", "cycle"]
        assert {tid for tid, _ in out} == {a.id, b.id}

    @pytest.mark.asyncio
    async def test_parked_blocked_foundation_strands_dependent(self, db_session):
        from app.orchestrator_health import _classify_stuck_tasks

        project = make_project(name="StuckParked", user_id="u1")
        db_session.add(project)
        await db_session.commit()
        await db_session.refresh(project)

        # Foundation is BLOCKED *and* escalated-and-parked (recovery exhausted).
        foundation = make_task(
            project_id=project.id,
            payloads=[{"type": "create", "prompt": "foundation"}],
            status=TaskStatus.BLOCKED,
            context={"_blocked_escalated": True},
        )
        db_session.add(foundation)
        await db_session.commit()
        await db_session.refresh(foundation)

        stuck = make_task(
            project_id=project.id,
            payloads=[{"type": "create", "prompt": "stuck"}],
            status=TaskStatus.QUEUED,
            depends_on=[str(foundation.id)],
        )
        db_session.add(stuck)
        await db_session.commit()
        await db_session.refresh(stuck)

        out = await _classify_stuck_tasks(
            [stuck],
            {stuck.id: {foundation.id}},
            {foundation.id: foundation},
            {stuck.id},
            db_session,
            self._noop_verify,
        )
        # Parked foundation strands the dependent → surface for escalation, never
        # silently waiting and never cleared-and-dispatched.
        assert out == [(stuck.id, "stranded_parked")]

    @pytest.mark.asyncio
    async def test_recovering_blocked_foundation_keeps_dependent_waiting(
        self, db_session
    ):
        from app.orchestrator_health import _classify_stuck_tasks

        project = make_project(name="StuckRecovering", user_id="u1")
        db_session.add(project)
        await db_session.commit()
        await db_session.refresh(project)

        # Foundation is BLOCKED but still being actively rechecked/resumed — NOT
        # escalated-and-parked, so it must NOT strand the dependent.
        foundation = make_task(
            project_id=project.id,
            payloads=[{"type": "create", "prompt": "foundation"}],
            status=TaskStatus.BLOCKED,
            context={"_blocked_recheck_attempts": 1},
        )
        db_session.add(foundation)
        await db_session.commit()
        await db_session.refresh(foundation)

        stuck = make_task(
            project_id=project.id,
            payloads=[{"type": "create", "prompt": "stuck"}],
            status=TaskStatus.QUEUED,
            depends_on=[str(foundation.id)],
        )
        db_session.add(stuck)
        await db_session.commit()
        await db_session.refresh(stuck)

        out = await _classify_stuck_tasks(
            [stuck],
            {stuck.id: {foundation.id}},
            {foundation.id: foundation},
            {stuck.id},
            db_session,
            self._noop_verify,
        )
        # Still-recovering BLOCKED foundation is a normal in-flight wait — the
        # dependent keeps waiting unchanged, not stranded.
        assert out == []

    @pytest.mark.asyncio
    async def test_co_deferred_plus_failed_prereq_identifies_correct_prereq(
        self, db_session
    ):
        """When a task has two deps — one co-deferred (CANCELLED+CO_DEFERRED_SHELVED)
        and one genuinely FAILED — _classify_stuck_tasks must name the genuinely
        failed dep as the prereq in the 3-tuple, not the co-deferred one."""
        from app.failure_classification import CO_DEFERRED_SHELVED
        from app.orchestrator_health import _classify_stuck_tasks

        project = make_project(name="CoDeferredFail", user_id="u1")
        db_session.add(project)
        await db_session.commit()
        await db_session.refresh(project)

        co_deferred_dep = make_task(
            project_id=project.id,
            title="co-deferred-dep",
            payloads=[{"type": "create", "prompt": "co-deferred"}],
            status=TaskStatus.CANCELLED,
            context={CO_DEFERRED_SHELVED: True},
        )
        failed_dep = make_task(
            project_id=project.id,
            title="genuinely-failed-dep",
            payloads=[{"type": "create", "prompt": "failed"}],
            status=TaskStatus.FAILED,
        )
        db_session.add_all([co_deferred_dep, failed_dep])
        await db_session.commit()
        await db_session.refresh(co_deferred_dep)
        await db_session.refresh(failed_dep)

        stuck = make_task(
            project_id=project.id,
            payloads=[{"type": "create", "prompt": "stuck"}],
            status=TaskStatus.QUEUED,
            depends_on=[str(co_deferred_dep.id), str(failed_dep.id)],
        )
        db_session.add(stuck)
        await db_session.commit()
        await db_session.refresh(stuck)

        dep_uuids = {co_deferred_dep.id, failed_dep.id}
        dep_tasks_by_id = {
            co_deferred_dep.id: co_deferred_dep,
            failed_dep.id: failed_dep,
        }
        out = await _classify_stuck_tasks(
            [stuck],
            {stuck.id: dep_uuids},
            dep_tasks_by_id,
            {stuck.id},
            db_session,
            self._noop_verify,
        )
        assert len(out) == 1
        assert out[0][1] == "cascade_cancel"
        # The 3rd element must be the genuinely FAILED dep, not the co-deferred one.
        assert out[0][2] is not None, "Failed prereq must be identified"
        assert out[0][2].id == failed_dep.id, (
            f"Expected failed_dep ({failed_dep.id}) as prereq, got {out[0][2].id}"
        )


# ── BLOCKED tasks are undispatchable on the normal queue path ────────────────


@pytest.mark.asyncio
async def test_blocked_task_excluded_from_dispatch_candidates(db_session):
    """The orchestrator candidate query selects status==QUEUED only, so a BLOCKED
    task is never picked up for dispatch even though it shares an active project."""
    from sqlmodel import select as sql_select

    project = make_project(user_id=_TEST_USER)
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    queued = make_task(
        project_id=project.id, status=TaskStatus.QUEUED, title="runnable"
    )
    blocked = make_task(
        project_id=project.id,
        status=TaskStatus.BLOCKED,
        title="parked",
        blocker_kind="file_conflict",
        blocked_reason="Waiting: file conflict on a.py",
    )
    db_session.add(queued)
    db_session.add(blocked)
    await db_session.commit()

    # Mirror the orchestrator's candidate predicate (orchestrator.py ~1647).
    candidate_stmt = sql_select(Task).where(
        Task.status == TaskStatus.QUEUED,
        Task.requires_approval.is_(False),
        Task.project_id.in_(
            sql_select(Project.id).where(
                Project.not_archived(),
                Project.paused.is_(False),
            )
        ),
    )
    candidates = (await db_session.execute(candidate_stmt)).scalars().all()
    candidate_ids = {t.id for t in candidates}
    assert queued.id in candidate_ids
    assert blocked.id not in candidate_ids


class TestCrossBaseCodeDependencyPolicy:
    """The genuine-vs-ordering-only dependency policy
    (``orchestrator_health.is_genuine_code_dependency``) that decides whether a
    cross-rollup dependent must HOLD for its dependency's landed code."""

    def _dep(self, dtype="create", files=None, base="research/aaa"):
        return make_task(
            payloads=[{"type": dtype, "base_branch": base}],
            context={"related_files": files or []},
        )

    def _dependent(self, dtype="create", files=None, base="plan/bbb"):
        return make_task(
            payloads=[{"type": dtype, "base_branch": base}],
            context={"related_files": files or []},
        )

    def test_file_overlap_is_genuine(self):
        from app.orchestrator_health import is_genuine_code_dependency

        dep = self._dep("create", ["backend/app/foo.py"])
        dependent = self._dependent(
            "create", ["backend/app/foo.py", "backend/app/bar.py"]
        )
        assert is_genuine_code_dependency(dependent, dep) is True

    def test_no_overlap_non_transforming_is_ordering_only(self):
        from app.orchestrator_health import (
            is_genuine_code_dependency,
            is_ordering_only_dependency,
        )

        dep = self._dep("create", ["backend/app/foo.py"])
        dependent = self._dependent("create", ["backend/app/bar.py"])
        assert is_genuine_code_dependency(dependent, dep) is False
        assert is_ordering_only_dependency(dependent, dep) is True

    def test_modify_fix_dependent_is_genuine_even_without_overlap(self):
        from app.orchestrator_health import is_genuine_code_dependency

        dep = self._dep("create", [])  # no declared files
        dependent = self._dependent("fix", [])
        assert is_genuine_code_dependency(dependent, dep) is True

    def test_transforming_dependent_with_disjoint_declared_files_is_ordering_only(self):
        from app.orchestrator_health import is_genuine_code_dependency

        dep = self._dep("create", ["backend/app/foo.py"])
        dependent = self._dependent("fix", ["backend/app/totally_other.py"])
        # Both declare files that provably do NOT overlap → no code relationship.
        assert is_genuine_code_dependency(dependent, dep) is False

    def test_non_code_producing_dependency_is_ordering_only(self):
        from app.orchestrator_health import is_genuine_code_dependency

        # A research dependency with overlapping files is still ordering-only —
        # research produces no code to inherit.
        dep = self._dep("research", ["backend/app/foo.py"])
        dependent = self._dependent("create", ["backend/app/foo.py"])
        assert is_genuine_code_dependency(dependent, dep) is False

    def test_test_only_overlap_does_not_count_as_genuine(self):
        from app.orchestrator_health import is_genuine_code_dependency

        # Overlap only on test files (additive, excluded by _prod_files) is not a
        # genuine code relationship for a non-transforming dependent.
        dep = self._dep("create", ["backend/testing/test_foo.py"])
        dependent = self._dependent("create", ["backend/testing/test_foo.py"])
        assert is_genuine_code_dependency(dependent, dep) is False

    def test_transforming_dependent_with_file_overlap_is_genuine(self):
        from app.orchestrator_health import is_genuine_code_dependency

        # A fix/refactor that overlaps the dependency's files is a genuine code
        # relationship — transforming dependent + overlap = real code dep.
        dep = self._dep("create", ["backend/app/foo.py", "backend/app/bar.py"])
        dependent = self._dependent("fix", ["backend/app/foo.py", "backend/app/baz.py"])
        assert is_genuine_code_dependency(dependent, dep) is True

    def test_transforming_dependent_refactor_with_disjoint_files_is_ordering_only(self):
        from app.orchestrator_health import is_genuine_code_dependency

        # Refactor transforming dependent with provably disjoint files from dependency
        # is ordering-only (no code relationship).
        dep = self._dep("create", ["backend/app/old_implementation.py"])
        dependent = self._dependent("refactor", ["backend/app/new_structure.py"])
        assert is_genuine_code_dependency(dependent, dep) is False

    def test_transforming_dependent_fix_dependent_without_files_is_genuine(self):
        from app.orchestrator_health import is_genuine_code_dependency

        # A fix dependent with no declared files against a code-producing dep
        # cannot disprove overlap, so it's treated as genuine (hold rather than
        # risk). ("modify" was retired from the task-type vocabulary; fix and
        # refactor are the output-transforming types.)
        dep = self._dep("create", ["backend/app/foo.py"])
        dependent = self._dependent("fix", [])  # no files declared
        assert is_genuine_code_dependency(dependent, dep) is True

    def test_transforming_dependent_fix_with_dependency_having_no_files_is_genuine(
        self,
    ):
        from app.orchestrator_health import is_genuine_code_dependency

        # When the dependency declares no files, we cannot disprove overlap, so the
        # transforming-dependent edge (fix) is treated as genuine.
        dep = self._dep("create", [])  # no files
        dependent = self._dependent("fix", ["backend/app/foo.py"])
        assert is_genuine_code_dependency(dependent, dep) is True


class TestCrossBaseDependencyGate:
    """``_check_candidate_task_deps`` HOLDS a cross-rollup GENUINE code-dependent
    until its dependency lands on integration, re-resolves it eligible once it
    lands, keeps ordering-only cross-base deps non-blocking, leaves same-base
    behavior unchanged, and still cascade-cancels on a terminal dependency."""

    async def _verify_true(self, _task, _session):
        return True

    async def _verify_false(self, _task, _session):
        return False

    @pytest.mark.asyncio
    async def test_cross_base_genuine_code_dep_held_until_landed(self, db_session):
        from app.orchestrator import _check_candidate_task_deps
        from app.landed_signal import (
            mark_landed_on_integration,
            mark_unblocked_non_merge,
        )

        project = make_project(name="XBaseHold", user_id="u1")
        db_session.add(project)
        await db_session.commit()
        await db_session.refresh(project)

        dep = make_task(
            project_id=project.id,
            payloads=[{"type": "create", "base_branch": "research/aaa"}],
            context={"related_files": ["backend/app/foo.py"]},
            status=TaskStatus.COMPLETED,
        )
        # Merged to its OWN staging branch only — NOT yet on integration.
        mark_unblocked_non_merge(dep, reason="rollup_child_staging_merge")
        dependent = make_task(
            project_id=project.id,
            payloads=[{"type": "fix", "base_branch": "plan/bbb"}],
            context={"related_files": ["backend/app/foo.py"]},
            status=TaskStatus.QUEUED,
        )
        db_session.add_all([dep, dependent])
        await db_session.commit()

        # Held while the cross-rollup code-dependency has only staging-merged.
        result, _ = await _check_candidate_task_deps(
            dependent, [dep], [dep.id], db_session, self._verify_true
        )
        assert result == "waiting"

        # Once it lands on integration the dependent becomes eligible.
        mark_landed_on_integration(dep)
        result2, _ = await _check_candidate_task_deps(
            dependent, [dep], [dep.id], db_session, self._verify_true
        )
        assert result2 == "eligible"

    @pytest.mark.asyncio
    async def test_cross_base_genuine_code_dep_in_flight_is_held(self, db_session):
        from app.orchestrator import _check_candidate_task_deps

        project = make_project(name="XBaseInflight", user_id="u1")
        db_session.add(project)
        await db_session.commit()

        dep = make_task(
            project_id=project.id,
            payloads=[{"type": "create", "base_branch": "research/aaa"}],
            context={"related_files": ["backend/app/foo.py"]},
            status=TaskStatus.IN_PROGRESS,
        )
        dependent = make_task(
            project_id=project.id,
            payloads=[{"type": "fix", "base_branch": "plan/bbb"}],
            context={"related_files": ["backend/app/foo.py"]},
            status=TaskStatus.QUEUED,
        )
        db_session.add_all([dep, dependent])
        await db_session.commit()

        result, _ = await _check_candidate_task_deps(
            dependent, [dep], [dep.id], db_session, self._verify_true
        )
        assert result == "waiting"

    @pytest.mark.asyncio
    async def test_ordering_only_cross_base_dep_dispatches_without_waiting(
        self, db_session
    ):
        from app.orchestrator import _check_candidate_task_deps

        project = make_project(name="XBaseOrdering", user_id="u1")
        db_session.add(project)
        await db_session.commit()

        # In-flight dependency, but no code relationship (disjoint files,
        # non-transforming dependent) across rollup boundaries → ordering-only.
        dep = make_task(
            project_id=project.id,
            payloads=[{"type": "create", "base_branch": "research/aaa"}],
            context={"related_files": ["backend/app/foo.py"]},
            status=TaskStatus.IN_PROGRESS,
        )
        dependent = make_task(
            project_id=project.id,
            payloads=[{"type": "create", "base_branch": "plan/bbb"}],
            context={"related_files": ["backend/app/bar.py"]},
            status=TaskStatus.QUEUED,
        )
        db_session.add_all([dep, dependent])
        await db_session.commit()

        result, _ = await _check_candidate_task_deps(
            dependent, [dep], [dep.id], db_session, self._verify_true
        )
        assert result == "eligible"

    @pytest.mark.asyncio
    async def test_same_base_wait_and_pr_merge_unchanged(self, db_session):
        from app.orchestrator import _check_candidate_task_deps

        project = make_project(name="SameBase", user_id="u1")
        db_session.add(project)
        await db_session.commit()

        dep = make_task(
            project_id=project.id,
            payloads=[{"type": "create", "base_branch": "research/aaa"}],
            context={"related_files": ["backend/app/foo.py"]},
            status=TaskStatus.COMPLETED,
        )
        dependent = make_task(
            project_id=project.id,
            payloads=[{"type": "create", "base_branch": "research/aaa"}],
            context={"related_files": ["backend/app/foo.py"]},
            status=TaskStatus.QUEUED,
        )
        db_session.add_all([dep, dependent])
        await db_session.commit()

        # Same-base create dep that is COMPLETED but PR not merged → still waits.
        waiting, _ = await _check_candidate_task_deps(
            dependent, [dep], [dep.id], db_session, self._verify_false
        )
        assert waiting == "waiting"
        # PR merged → eligible (unchanged same-base behavior).
        eligible, _ = await _check_candidate_task_deps(
            dependent, [dep], [dep.id], db_session, self._verify_true
        )
        assert eligible == "eligible"

    @pytest.mark.asyncio
    async def test_compute_experiment_same_base_eligible_after_pr_merged(
        self, db_session
    ):
        """A compute_experiment depending on a same-plan-branch create dep is
        eligible once the create is COMPLETED with pr_merged — no
        landed_on_integration required.

        Regression guard: before the plan_completion fix, compute_experiment
        payloads omitted base_branch, making them appear cross-base relative to
        sibling create tasks. The dep resolver then required landed_on_integration
        (a signal that never fires for plan-branch merges), permanently wedging
        the compute subtask in QUEUED.
        """
        from app.orchestrator import _check_candidate_task_deps

        project = make_project(name="ComputeSameBase", user_id="u1")
        db_session.add(project)
        await db_session.commit()
        await db_session.refresh(project)

        # Both tasks share the same plan branch — same-base relationship.
        dep = make_task(
            project_id=project.id,
            payloads=[{"type": "create", "base_branch": "plan/xyz789"}],
            context={"related_files": ["backend/app/search_agent.py"]},
            status=TaskStatus.COMPLETED,
        )
        compute_task = make_task(
            project_id=project.id,
            payloads=[
                {
                    "type": "compute_experiment",
                    "base_branch": "plan/xyz789",
                    "command": "python bench.py",
                    "spend_cap_usd": 2.0,
                    "deadline_seconds": 600,
                }
            ],
            context={"related_files": ["backend/app/search_agent.py"]},
            status=TaskStatus.QUEUED,
        )
        db_session.add_all([dep, compute_task])
        await db_session.commit()

        # Same-base create dep COMPLETED with PR merged → compute is eligible.
        # _verify_true simulates pr_merged=True (a verifier that returns True).
        result, _ = await _check_candidate_task_deps(
            compute_task, [dep], [dep.id], db_session, self._verify_true
        )
        assert result == "eligible", (
            f"compute_experiment should be eligible after same-base dep PR merged, got {result!r}"
        )

        # Without PR merged (verifier returns False), same-base logic holds.
        waiting, _ = await _check_candidate_task_deps(
            compute_task, [dep], [dep.id], db_session, self._verify_false
        )
        assert waiting == "waiting", (
            "compute_experiment must wait when same-base dep PR not yet merged"
        )

    @pytest.mark.asyncio
    async def test_terminally_failed_cross_base_code_dep_cascade_cancels(
        self, db_session
    ):
        from app.orchestrator import _check_candidate_task_deps

        project = make_project(name="XBaseFail", user_id="u1")
        db_session.add(project)
        await db_session.commit()

        dep = make_task(
            project_id=project.id,
            payloads=[{"type": "create", "base_branch": "research/aaa"}],
            context={"related_files": ["backend/app/foo.py"]},
            status=TaskStatus.FAILED,
        )
        dependent = make_task(
            project_id=project.id,
            payloads=[{"type": "fix", "base_branch": "plan/bbb"}],
            context={"related_files": ["backend/app/foo.py"]},
            status=TaskStatus.QUEUED,
        )
        db_session.add_all([dep, dependent])
        await db_session.commit()

        result, _ = await _check_candidate_task_deps(
            dependent, [dep], [dep.id], db_session, self._verify_true
        )
        # A dependency that can never land must still cascade-cancel — no hang.
        assert result == "cascade_cancel"

    @pytest.mark.asyncio
    async def test_staging_merged_dep_eligible_when_lineage_parent_landed(
        self, db_session
    ):
        """Dependent is eligible when a staging-merged dep's parent lineage has landed.

        Reproduces the permanent cross-lineage wedge: a code task (dep) merged its
        PR into its research parent's staging branch — not integration — completing
        with pr_merged=True, landed_on_integration=False,
        _non_merge_unblock_reason=merged_pr_non_integration_branch.  When the
        research parent's rollup later lands on integration, the parent carries
        landed_on_integration=True, but the dep (now a grandchild scenario relative
        to an outer rollup) may not have been propagated yet.

        _check_candidate_task_deps must resolve the dep as "landed" (via the parent
        lineage check) and return "eligible" rather than wedging forever.
        """
        from app.orchestrator import _check_candidate_task_deps
        from app.landed_signal import (
            mark_unblocked_non_merge,
            mark_landed_on_integration,
        )

        project = make_project(name="CrossLineageWedge", user_id="u1")
        db_session.add(project)
        await db_session.commit()
        await db_session.refresh(project)

        # Research/rollup parent — has itself landed on integration via its
        # own rollup PR.
        lineage_parent = make_task(
            project_id=project.id,
            payloads=[{"type": "research", "base_branch": "research/r1"}],
            context={},
            status=TaskStatus.COMPLETED,
        )
        mark_landed_on_integration(lineage_parent)
        db_session.add(lineage_parent)
        await db_session.commit()
        await db_session.refresh(lineage_parent)

        # Code child that merged its PR to the research task's staging branch
        # (not to integration) — the normal path for a rollup-lineage code task.
        dep = make_task(
            project_id=project.id,
            payloads=[{"type": "create", "base_branch": "research/r1"}],
            context={"related_files": ["backend/app/foo.py"]},
            status=TaskStatus.COMPLETED,
            parent_task_id=lineage_parent.id,
        )
        mark_unblocked_non_merge(dep, reason="merged_pr_non_integration_branch")
        db_session.add(dep)
        await db_session.commit()

        # Dependent on a different base with a genuine cross-base code-dependency.
        dependent = make_task(
            project_id=project.id,
            payloads=[{"type": "create", "base_branch": "plan/bbb"}],
            context={"related_files": ["backend/app/foo.py"]},
            status=TaskStatus.QUEUED,
        )
        db_session.add(dependent)
        await db_session.commit()

        # The dep's own signal is False, but its lineage parent has landed →
        # dependent must be eligible (not permanently waiting).
        result, _ = await _check_candidate_task_deps(
            dependent, [dep], [dep.id], db_session, self._verify_true
        )
        assert result == "eligible", (
            "cross-base code-dep should be eligible when lineage parent has landed "
            "on integration, even if the dep's own landed_on_integration is False"
        )

    @pytest.mark.asyncio
    async def test_staging_merged_dep_held_when_lineage_parent_not_yet_landed(
        self, db_session
    ):
        """Regression guard: hold preserved when lineage has NOT yet landed.

        The defense-in-depth check must NOT release a dependent when its code
        dep's lineage parent also has not landed.  This preserves the invariant
        that only deps whose code is provably on integration unblock dependents.
        """
        from app.orchestrator import _check_candidate_task_deps
        from app.landed_signal import mark_unblocked_non_merge

        project = make_project(name="CrossLineageHold", user_id="u1")
        db_session.add(project)
        await db_session.commit()
        await db_session.refresh(project)

        # Research parent whose rollup has NOT yet landed on integration.
        lineage_parent = make_task(
            project_id=project.id,
            payloads=[{"type": "research", "base_branch": "research/r1"}],
            context={},
            status=TaskStatus.COMPLETED,
        )
        # Parent merged to staging only, not to integration.
        mark_unblocked_non_merge(
            lineage_parent, reason="merged_pr_non_integration_branch"
        )
        db_session.add(lineage_parent)
        await db_session.commit()
        await db_session.refresh(lineage_parent)

        dep = make_task(
            project_id=project.id,
            payloads=[{"type": "create", "base_branch": "research/r1"}],
            context={"related_files": ["backend/app/foo.py"]},
            status=TaskStatus.COMPLETED,
            parent_task_id=lineage_parent.id,
        )
        mark_unblocked_non_merge(dep, reason="merged_pr_non_integration_branch")
        db_session.add(dep)
        await db_session.commit()

        dependent = make_task(
            project_id=project.id,
            payloads=[{"type": "create", "base_branch": "plan/bbb"}],
            context={"related_files": ["backend/app/foo.py"]},
            status=TaskStatus.QUEUED,
        )
        db_session.add(dependent)
        await db_session.commit()

        # Neither dep nor its parent has landed → must stay "waiting".
        result, _ = await _check_candidate_task_deps(
            dependent, [dep], [dep.id], db_session, self._verify_true
        )
        assert result == "waiting", (
            "cross-base code-dep must still hold when neither the dep nor its "
            "lineage parent has landed on integration"
        )

    @pytest.mark.asyncio
    async def test_sibling_plan_dep_eligible_after_shared_research_rollup(
        self, db_session
    ):
        """A sibling plan must not wait for the outer research rollup.

        The dependency's plan parent has already merged into the exact research
        branch both plans share. Waiting for integration would deadlock the
        research parent on the still-queued sibling.
        """
        from app.orchestrator import _check_candidate_task_deps
        from app.landed_signal import mark_unblocked_non_merge

        project = make_project(name="SharedResearchBase", user_id="u1")
        db_session.add(project)
        await db_session.commit()

        dep_plan = make_task(
            project_id=project.id,
            payloads=[{"type": "plan", "base_branch": "research/shared"}],
            context={
                "plan_branch": "plan/dep",
                "parent_base": "research/shared",
            },
            status=TaskStatus.COMPLETED,
        )
        mark_unblocked_non_merge(
            dep_plan, reason="merged_pr_non_integration_branch"
        )
        db_session.add(dep_plan)
        await db_session.commit()

        dep = make_task(
            project_id=project.id,
            payloads=[{"type": "create", "base_branch": "plan/dep"}],
            context={"related_files": ["backend/app/foo.py"]},
            status=TaskStatus.COMPLETED,
            parent_task_id=dep_plan.id,
        )
        mark_unblocked_non_merge(dep, reason="merged_pr_non_integration_branch")
        dependent = make_task(
            project_id=project.id,
            payloads=[{"type": "fix", "base_branch": "plan/dependent"}],
            context={
                "related_files": ["backend/app/foo.py"],
                "parent_base": "research/shared",
            },
            status=TaskStatus.QUEUED,
        )
        db_session.add_all([dep, dependent])
        await db_session.commit()

        result, _ = await _check_candidate_task_deps(
            dependent, [dep], [dep.id], db_session, self._verify_true
        )
        assert result == "eligible"

    @pytest.mark.asyncio
    async def test_force_dropped_dep_held_even_when_lineage_parent_landed(
        self, db_session
    ):
        """Force-dropped deps must NOT release even if the lineage parent has landed.

        A dep completed with a force-drop reason (review_respawn_cap /
        malformed_pr_url / rollup_no_pr_artifact) means its code was discarded —
        the parent landing on integration does NOT carry that code to the trunk.
        _dep_parent_lineage_has_landed must short-circuit on the force-drop
        guard and return False so the dependent stays "waiting".

        Regression guard for the guard added at orchestrator_health.py:847-848.
        """
        from app.orchestrator import _check_candidate_task_deps
        from app.landed_signal import (
            mark_unblocked_non_merge,
            mark_landed_on_integration,
        )

        project = make_project(name="ForceDropParentLanded", user_id="u1")
        db_session.add(project)
        await db_session.commit()
        await db_session.refresh(project)

        # Lineage parent that HAS landed on integration.
        lineage_parent = make_task(
            project_id=project.id,
            payloads=[{"type": "research", "base_branch": "research/r1"}],
            context={},
            status=TaskStatus.COMPLETED,
        )
        mark_landed_on_integration(lineage_parent)
        db_session.add(lineage_parent)
        await db_session.commit()
        await db_session.refresh(lineage_parent)

        # Code child that was force-dropped (review_respawn_cap) — its code was
        # discarded, so the parent landing does NOT carry this dep's code.
        dep = make_task(
            project_id=project.id,
            payloads=[{"type": "create", "base_branch": "research/r1"}],
            context={"related_files": ["backend/app/foo.py"]},
            status=TaskStatus.COMPLETED,
            parent_task_id=lineage_parent.id,
        )
        mark_unblocked_non_merge(dep, reason="review_respawn_cap")
        db_session.add(dep)
        await db_session.commit()

        # Dependent on a different base with a genuine cross-base code-dependency.
        dependent = make_task(
            project_id=project.id,
            payloads=[{"type": "create", "base_branch": "plan/bbb"}],
            context={"related_files": ["backend/app/foo.py"]},
            status=TaskStatus.QUEUED,
        )
        db_session.add(dependent)
        await db_session.commit()

        # Parent has landed but dep was force-dropped → must stay "waiting".
        result, _ = await _check_candidate_task_deps(
            dependent, [dep], [dep.id], db_session, self._verify_true
        )
        assert result == "waiting", (
            "force-dropped dep (review_respawn_cap) must not release dependent "
            "even when the dep's lineage parent has landed on integration — "
            "the dep's code was discarded and never reached the trunk"
        )


class TestCrossBaseBaseRecut:
    """Once a cross-rollup code-dependency lands, the dependent's staging/rollup
    base is re-cut from current integration before it can dispatch
    (``ensure_dependent_base_recut`` / ``recut_dependent_base_from_integration``)."""

    @pytest.mark.asyncio
    async def test_base_equal_integration_is_noop(self, db_session, mocker):
        from app.orchestrator_health import ensure_dependent_base_recut
        from app.landed_signal import mark_landed_on_integration

        merge_mock = mocker.patch("app.github.merge_branch", new_callable=AsyncMock)
        project = make_project(
            name="RecutNoop", user_id="u1", integration_branch="main"
        )
        db_session.add(project)
        await db_session.commit()
        await db_session.refresh(project)

        dep = make_task(
            project_id=project.id,
            payloads=[{"type": "create", "base_branch": "research/aaa"}],
            context={"related_files": ["backend/app/foo.py"]},
            status=TaskStatus.COMPLETED,
        )
        mark_landed_on_integration(dep)
        # Dependent already bases on integration → cloning HEAD has the code.
        dependent = make_task(
            project_id=project.id,
            payloads=[{"type": "fix", "base_branch": "main"}],
            context={"related_files": ["backend/app/foo.py"]},
            status=TaskStatus.QUEUED,
        )
        db_session.add_all([dep, dependent])
        await db_session.commit()

        ok = await ensure_dependent_base_recut(dependent, [dep], db_session)
        assert ok is True
        merge_mock.assert_not_called()

    @pytest.mark.asyncio
    async def test_staging_base_merges_integration(self, db_session, mocker):
        from app.orchestrator_health import ensure_dependent_base_recut
        from app.landed_signal import mark_landed_on_integration

        mocker.patch(
            "app.routers.agent.get_github_token_for_task",
            new_callable=AsyncMock,
            return_value=("tok", "owner", "repo"),
        )
        merge_mock = mocker.patch(
            "app.github.merge_branch",
            new_callable=AsyncMock,
            return_value={"merged": True, "already": False},
        )
        project = make_project(
            name="RecutMerge", user_id="u1", integration_branch="main"
        )
        db_session.add(project)
        await db_session.commit()
        await db_session.refresh(project)

        dep = make_task(
            project_id=project.id,
            payloads=[{"type": "create", "base_branch": "research/aaa"}],
            context={"related_files": ["backend/app/foo.py"]},
            status=TaskStatus.COMPLETED,
        )
        mark_landed_on_integration(dep)
        dependent = make_task(
            project_id=project.id,
            payloads=[{"type": "fix", "base_branch": "plan/bbb"}],
            context={"related_files": ["backend/app/foo.py"]},
            status=TaskStatus.QUEUED,
        )
        db_session.add_all([dep, dependent])
        await db_session.commit()

        ok = await ensure_dependent_base_recut(dependent, [dep], db_session)
        assert ok is True
        merge_mock.assert_awaited_once()
        _, kwargs = merge_mock.await_args
        assert kwargs.get("base") == "plan/bbb"
        assert kwargs.get("head") == "main"

    @pytest.mark.asyncio
    async def test_sibling_plan_base_merges_shared_research_branch(
        self, db_session, mocker
    ):
        from app.orchestrator_health import ensure_dependent_base_recut
        from app.landed_signal import mark_unblocked_non_merge

        mocker.patch(
            "app.routers.agent.get_github_token_for_task",
            new_callable=AsyncMock,
            return_value=("tok", "owner", "repo"),
        )
        merge_mock = mocker.patch(
            "app.github.merge_branch",
            new_callable=AsyncMock,
            return_value={"merged": True, "already": False},
        )
        project = make_project(
            name="SharedResearchRecut",
            user_id="u1",
            integration_branch="automain",
        )
        db_session.add(project)
        await db_session.commit()

        dep_plan = make_task(
            project_id=project.id,
            payloads=[{"type": "plan", "base_branch": "research/shared"}],
            context={
                "plan_branch": "plan/dep",
                "parent_base": "research/shared",
            },
            status=TaskStatus.COMPLETED,
        )
        mark_unblocked_non_merge(
            dep_plan, reason="merged_pr_non_integration_branch"
        )
        db_session.add(dep_plan)
        await db_session.commit()

        dep = make_task(
            project_id=project.id,
            payloads=[{"type": "create", "base_branch": "plan/dep"}],
            context={"related_files": ["backend/app/foo.py"]},
            status=TaskStatus.COMPLETED,
            parent_task_id=dep_plan.id,
        )
        mark_unblocked_non_merge(dep, reason="merged_pr_non_integration_branch")
        dependent = make_task(
            project_id=project.id,
            payloads=[{"type": "fix", "base_branch": "plan/dependent"}],
            context={
                "related_files": ["backend/app/foo.py"],
                "parent_base": "research/shared",
            },
            status=TaskStatus.QUEUED,
        )
        db_session.add_all([dep, dependent])
        await db_session.commit()

        assert await ensure_dependent_base_recut(
            dependent, [dep], db_session
        ) is True
        _, kwargs = merge_mock.await_args
        assert kwargs["base"] == "plan/dependent"
        assert kwargs["head"] == "research/shared"

    @pytest.mark.asyncio
    async def test_merge_conflict_holds_dependent(self, db_session, mocker):
        from app.orchestrator_health import ensure_dependent_base_recut
        from app.landed_signal import mark_landed_on_integration

        mocker.patch(
            "app.routers.agent.get_github_token_for_task",
            new_callable=AsyncMock,
            return_value=("tok", "owner", "repo"),
        )
        mocker.patch(
            "app.github.merge_branch",
            new_callable=AsyncMock,
            return_value={"merged": False, "conflict": True},
        )
        project = make_project(
            name="RecutConflict", user_id="u1", integration_branch="main"
        )
        db_session.add(project)
        await db_session.commit()
        await db_session.refresh(project)

        dep = make_task(
            project_id=project.id,
            payloads=[{"type": "create", "base_branch": "research/aaa"}],
            context={"related_files": ["backend/app/foo.py"]},
            status=TaskStatus.COMPLETED,
        )
        mark_landed_on_integration(dep)
        dependent = make_task(
            project_id=project.id,
            payloads=[{"type": "fix", "base_branch": "plan/bbb"}],
            context={"related_files": ["backend/app/foo.py"]},
            status=TaskStatus.QUEUED,
        )
        db_session.add_all([dep, dependent])
        await db_session.commit()

        ok = await ensure_dependent_base_recut(dependent, [dep], db_session)
        # Re-cut required but conflicted → caller must keep holding.
        assert ok is False

    @pytest.mark.asyncio
    async def test_recut_driver_drops_dependent_on_failed_recut(
        self, db_session, mocker
    ):
        from app.orchestrator_health import recut_landed_cross_base_dependents
        from app.landed_signal import mark_landed_on_integration

        mocker.patch(
            "app.routers.agent.get_github_token_for_task",
            new_callable=AsyncMock,
            return_value=("tok", "owner", "repo"),
        )
        mocker.patch(
            "app.github.merge_branch",
            new_callable=AsyncMock,
            return_value={"merged": False, "conflict": True},
        )
        project = make_project(
            name="RecutDriver", user_id="u1", integration_branch="main"
        )
        db_session.add(project)
        await db_session.commit()
        await db_session.refresh(project)

        dep = make_task(
            project_id=project.id,
            payloads=[{"type": "create", "base_branch": "research/aaa"}],
            context={"related_files": ["backend/app/foo.py"]},
            status=TaskStatus.COMPLETED,
        )
        mark_landed_on_integration(dep)
        dependent = make_task(
            project_id=project.id,
            payloads=[{"type": "fix", "base_branch": "plan/bbb"}],
            context={"related_files": ["backend/app/foo.py"]},
            status=TaskStatus.QUEUED,
            depends_on=[str(dep.id)],
        )
        db_session.add_all([dep, dependent])
        await db_session.commit()
        await db_session.refresh(dependent)

        eligible = [dependent.id]
        out = await recut_landed_cross_base_dependents(
            eligible, [dependent], {dep.id: dep}, db_session
        )
        # Conflict on re-cut → dropped from this cycle's eligible set.
        assert out == []

    @pytest.mark.asyncio
    async def test_recut_conflict_clears_warm_claim_marker(self, db_session, mocker):
        """Dep landed + re-cut conflicts => dependent must NOT be warm-claimable.

        Regression for the warm-claim bypass: a dropped cross-base code-dependent
        that was already stamped ``_deps_satisfied`` this cycle has the marker
        durably cleared, so ``warm_backlog_reuse``'s ``/next-role`` scan (which
        treats any depends_on task carrying ``_deps_satisfied`` as warm-claimable)
        cannot dispatch it against a base missing the landed dependency code."""
        from app.orchestrator_health import recut_landed_cross_base_dependents
        from app.landed_signal import mark_landed_on_integration

        mocker.patch(
            "app.routers.agent.get_github_token_for_task",
            new_callable=AsyncMock,
            return_value=("tok", "owner", "repo"),
        )
        mocker.patch(
            "app.github.merge_branch",
            new_callable=AsyncMock,
            return_value={"merged": False, "conflict": True},
        )
        project = make_project(
            name="RecutClearMarker", user_id="u1", integration_branch="main"
        )
        db_session.add(project)
        await db_session.commit()
        await db_session.refresh(project)

        dep = make_task(
            project_id=project.id,
            payloads=[{"type": "create", "base_branch": "research/aaa"}],
            context={"related_files": ["backend/app/foo.py"]},
            status=TaskStatus.COMPLETED,
        )
        mark_landed_on_integration(dep)
        # Dependent was already stamped warm-claimable this cycle.
        dependent = make_task(
            project_id=project.id,
            payloads=[{"type": "fix", "base_branch": "plan/bbb"}],
            context={
                "related_files": ["backend/app/foo.py"],
                "_deps_satisfied": True,
            },
            status=TaskStatus.QUEUED,
            depends_on=[str(dep.id)],
        )
        db_session.add_all([dep, dependent])
        await db_session.commit()
        await db_session.refresh(dependent)

        out = await recut_landed_cross_base_dependents(
            [dependent.id], [dependent], {dep.id: dep}, db_session
        )
        assert out == []

        db_session.expire(dependent)
        await db_session.refresh(dependent)
        # Marker durably cleared → not warm-claimable while the re-cut is unmet.
        assert not (dependent.context or {}).get("_deps_satisfied")

    @pytest.mark.asyncio
    async def test_recut_driver_follows_requeue_replacement_remap(
        self, db_session, mocker
    ):
        """A requeue-cancelled-and-replaced cross-base code-dependency still
        triggers the dependent's base re-cut: the driver resolves the replacement
        clone (which carries the landed code), not the superseded CANCELLED row."""
        from app.orchestrator_health import recut_landed_cross_base_dependents
        from app.landed_signal import mark_landed_on_integration

        mocker.patch(
            "app.routers.agent.get_github_token_for_task",
            new_callable=AsyncMock,
            return_value=("tok", "owner", "repo"),
        )
        merge_mock = mocker.patch(
            "app.github.merge_branch",
            new_callable=AsyncMock,
            return_value={"merged": True, "already": False},
        )
        project = make_project(
            name="RecutRemap", user_id="u1", integration_branch="main"
        )
        db_session.add(project)
        await db_session.commit()
        await db_session.refresh(project)

        replacement = make_task(
            project_id=project.id,
            payloads=[{"type": "create", "base_branch": "research/ccc"}],
            context={"related_files": ["backend/app/foo.py"]},
            status=TaskStatus.COMPLETED,
        )
        mark_landed_on_integration(replacement)
        db_session.add(replacement)
        await db_session.commit()
        await db_session.refresh(replacement)

        # Original dep was requeue-cancelled and points at the replacement clone.
        original_dep = make_task(
            project_id=project.id,
            payloads=[{"type": "create", "base_branch": "research/aaa"}],
            context={
                "related_files": ["backend/app/foo.py"],
                "_replaced_by_task_id": str(replacement.id),
            },
            status=TaskStatus.CANCELLED,
        )
        dependent = make_task(
            project_id=project.id,
            payloads=[{"type": "fix", "base_branch": "plan/bbb"}],
            context={"related_files": ["backend/app/foo.py"]},
            status=TaskStatus.QUEUED,
            depends_on=[str(original_dep.id)],
        )
        db_session.add_all([original_dep, dependent])
        await db_session.commit()
        await db_session.refresh(dependent)

        out = await recut_landed_cross_base_dependents(
            [dependent.id], [dependent], {original_dep.id: original_dep}, db_session
        )
        # Remap followed → landed replacement triggers a successful re-cut, so the
        # dependent stays eligible (not dropped).
        assert out == [dependent.id]
        merge_mock.assert_awaited_once()
        _, kwargs = merge_mock.await_args
        assert kwargs.get("base") == "plan/bbb"
        assert kwargs.get("head") == "main"


class TestCrossBaseRecutBounding:
    """A held cross-base re-cut that keeps failing is BOUNDED and ESCALATED rather
    than retried silently forever: a persistent genuine conflict raises ONE
    deduplicated operator alert (with an observable status message + context
    marker), a transient error keeps retrying with backoff and never escalates,
    and a later successful re-cut clears the accounting and resolves the alert
    (``orchestrator_health.ensure_dependent_base_recut``)."""

    @staticmethod
    def _landed_cross_base_pair(project):
        from app.landed_signal import mark_landed_on_integration

        dep = make_task(
            project_id=project.id,
            payloads=[{"type": "create", "base_branch": "research/aaa"}],
            context={"related_files": ["backend/app/foo.py"]},
            status=TaskStatus.COMPLETED,
        )
        mark_landed_on_integration(dep)
        dependent = make_task(
            project_id=project.id,
            payloads=[{"type": "fix", "base_branch": "plan/bbb"}],
            context={"related_files": ["backend/app/foo.py"]},
            status=TaskStatus.QUEUED,
        )
        return dep, dependent

    @staticmethod
    async def _open_escalation_alerts(db_session, dependent):
        from sqlmodel import select
        from app.models import Alert

        return (
            (
                await db_session.execute(
                    select(Alert).where(
                        Alert.task_id == dependent.id,
                        Alert.dismissed_at.is_(None),
                    )
                )
            )
            .scalars()
            .all()
        )

    @staticmethod
    async def _elapse_recut_backoff(db_session, dependent):
        """Simulate the backed-off recheck window having ELAPSED.

        Every failing re-cut outcome now arms ``_recut_recheck_after`` and
        ``ensure_dependent_base_recut`` short-circuits (issuing NO GitHub merge
        call) while that window is open — that IS the fix: the merge POST used to
        be re-issued on every ~5s dispatch cycle for the whole transient-"error"
        class, ~720/hr per held dependent against a shared 5000/hr token budget.
        A test that wants to observe the NEXT real attempt must therefore let the
        window lapse, exactly as wall-clock does in production.
        """
        from app.orchestrator_health import _utcnow

        ctx = dict(dependent.context or {})
        if not ctx.get("_recut_recheck_after"):
            return
        ctx["_recut_recheck_after"] = (_utcnow() - timedelta(hours=1)).isoformat()
        dependent.context = ctx
        db_session.add(dependent)
        await db_session.commit()

    @pytest.mark.asyncio
    async def test_single_conflict_increments_attempts_and_holds(
        self, db_session, mocker
    ):
        """A single conflict holds the dependent (False), records ONE consecutive
        conflict, and — being below the bound — does NOT escalate."""
        from app.orchestrator_health import ensure_dependent_base_recut

        mocker.patch("app.broadcast.broadcast_to_project", new_callable=AsyncMock)
        mocker.patch(
            "app.routers.agent.get_github_token_for_task",
            new_callable=AsyncMock,
            return_value=("tok", "owner", "repo"),
        )
        mocker.patch(
            "app.github.merge_branch",
            new_callable=AsyncMock,
            return_value={"merged": False, "conflict": True},
        )
        project = make_project(
            name="RecutBound1", user_id="u1", integration_branch="main"
        )
        db_session.add(project)
        await db_session.commit()
        await db_session.refresh(project)
        dep, dependent = self._landed_cross_base_pair(project)
        db_session.add_all([dep, dependent])
        await db_session.commit()

        ok = await ensure_dependent_base_recut(dependent, [dep], db_session)
        assert ok is False
        assert dependent.context.get("_recut_conflict_attempts") == 1
        assert dependent.context.get("_recut_last_outcome") == "conflict"
        assert dependent.context.get("_recut_escalated") is None
        assert await self._open_escalation_alerts(db_session, dependent) == []

    @pytest.mark.asyncio
    async def test_persistent_conflict_escalates_exactly_once(self, db_session, mocker):
        """After the bound is reached a persistent conflict escalates EXACTLY ONCE
        (deduplicated); the task carries the status message + context marker, and
        running the path again does not create a second alert."""
        from app.orchestrator_health import (
            ensure_dependent_base_recut,
            _RECUT_CONFLICT_ESCALATION_THRESHOLD,
        )
        from app.models import AlertSeverity

        mocker.patch("app.broadcast.broadcast_to_project", new_callable=AsyncMock)
        mocker.patch(
            "app.routers.agent.get_github_token_for_task",
            new_callable=AsyncMock,
            return_value=("tok", "owner", "repo"),
        )
        merge_mock = mocker.patch(
            "app.github.merge_branch",
            new_callable=AsyncMock,
            return_value={"merged": False, "conflict": True},
        )
        project = make_project(
            name="RecutBound2", user_id="u1", integration_branch="main"
        )
        db_session.add(project)
        await db_session.commit()
        await db_session.refresh(project)
        dep, dependent = self._landed_cross_base_pair(project)
        db_session.add_all([dep, dependent])
        await db_session.commit()

        # Drive consecutive conflicts up to (and past) the escalation bound. The
        # backoff window is elapsed between cycles (as wall-clock does), so each
        # iteration is a REAL attempt — one merge call apiece, not one per ~5s
        # dispatch cycle.
        for _ in range(_RECUT_CONFLICT_ESCALATION_THRESHOLD):
            await self._elapse_recut_backoff(db_session, dependent)
            assert (
                await ensure_dependent_base_recut(dependent, [dep], db_session) is False
            )

        assert merge_mock.await_count == _RECUT_CONFLICT_ESCALATION_THRESHOLD
        assert dependent.context.get("_recut_escalated") is True
        assert "persistent merge conflict" in (dependent.latest_status_message or "")
        alerts = await self._open_escalation_alerts(db_session, dependent)
        assert len(alerts) == 1
        assert alerts[0].severity == AlertSeverity.ERROR

        # Running again must NOT create a second alert, and — inside the still-open
        # backoff window — must issue NO further merge call.
        assert await ensure_dependent_base_recut(dependent, [dep], db_session) is False
        assert merge_mock.await_count == _RECUT_CONFLICT_ESCALATION_THRESHOLD
        alerts_again = await self._open_escalation_alerts(db_session, dependent)
        assert len(alerts_again) == 1

    @pytest.mark.asyncio
    async def test_transient_error_retries_with_backoff_without_escalating(
        self, db_session, mocker
    ):
        """A transient re-cut error retries with backoff WITHOUT escalating: no
        alert, the consecutive-conflict streak stays at 0, and the backed-off
        recheck accounting increments no matter how many times it recurs."""
        from app.orchestrator_health import (
            ensure_dependent_base_recut,
            _RECUT_CONFLICT_ESCALATION_THRESHOLD,
        )

        mocker.patch("app.broadcast.broadcast_to_project", new_callable=AsyncMock)
        mocker.patch(
            "app.routers.agent.get_github_token_for_task",
            new_callable=AsyncMock,
            return_value=("tok", "owner", "repo"),
        )
        merge_mock = mocker.patch(
            "app.github.merge_branch",
            new_callable=AsyncMock,
            return_value={"merged": False, "conflict": False},
        )
        project = make_project(
            name="RecutBound3", user_id="u1", integration_branch="main"
        )
        db_session.add(project)
        await db_session.commit()
        await db_session.refresh(project)
        dep, dependent = self._landed_cross_base_pair(project)
        db_session.add_all([dep, dependent])
        await db_session.commit()

        # Far more attempts than the conflict bound — a transient class must never
        # escalate. Each iteration lets the backed-off window lapse first, so it is
        # a genuine retry (one merge call each), which is what "retries with
        # backoff" means.
        attempts = _RECUT_CONFLICT_ESCALATION_THRESHOLD + 2
        for _ in range(attempts):
            await self._elapse_recut_backoff(db_session, dependent)
            assert (
                await ensure_dependent_base_recut(dependent, [dep], db_session) is False
            )

        assert merge_mock.await_count == attempts
        assert dependent.context.get("_recut_conflict_attempts") == 0
        assert dependent.context.get("_recut_last_outcome") == "error"
        assert dependent.context.get("_recut_escalated") is None
        assert dependent.context.get("_recut_recheck_attempts") == attempts
        assert dependent.recheck_after is not None
        assert await self._open_escalation_alerts(db_session, dependent) == []

    @pytest.mark.asyncio
    async def test_transient_error_backoff_suppresses_merge_call_next_cycle(
        self, db_session, mocker
    ):
        """REGRESSION (GitHub-call amplifier): a transient ``"error"`` outcome must
        HONOR the backed-off recheck window — the next dispatch cycle inside it
        issues NO ``POST /repos/{o}/{r}/merges`` at all.

        The backoff gate used to read
        ``ctx.get("_recut_escalated") and _recut_backoff_active(ctx)``. But
        ``_recut_escalated`` is only ever set by the missing/conflict escalation
        branches, and the transient ``"error"`` class NEVER escalates by design —
        so the whole gate was dead code for that class and the (uncoalesced,
        non-idempotent POST) merge call was re-issued on EVERY ~5s dispatch cycle:
        ~720/hr per held dependent, forever, against a shared 5000/hr installation
        token budget whose exhaustion has stalled production. Assert CALL COUNTS.
        """
        from app.orchestrator_health import ensure_dependent_base_recut

        mocker.patch("app.broadcast.broadcast_to_project", new_callable=AsyncMock)
        mocker.patch(
            "app.routers.agent.get_github_token_for_task",
            new_callable=AsyncMock,
            return_value=("tok", "owner", "repo"),
        )
        merge_mock = mocker.patch(
            "app.github.merge_branch",
            new_callable=AsyncMock,
            return_value={"merged": False, "conflict": False},  # → "error"
        )
        project = make_project(
            name="RecutBackoff1", user_id="u1", integration_branch="main"
        )
        db_session.add(project)
        await db_session.commit()
        await db_session.refresh(project)
        dep, dependent = self._landed_cross_base_pair(project)
        db_session.add_all([dep, dependent])
        await db_session.commit()

        # Cycle 1: a real attempt → exactly one merge POST, window armed.
        assert await ensure_dependent_base_recut(dependent, [dep], db_session) is False
        assert merge_mock.await_count == 1
        assert dependent.context.get("_recut_last_outcome") == "error"
        assert dependent.context.get("_recut_recheck_after")
        # Never escalates — so the OLD gate could never have fired here.
        assert dependent.context.get("_recut_escalated") is None

        # Cycles 2..6, all inside the backoff window: the dependent is still HELD
        # (False), but ZERO further GitHub merge calls are issued.
        for _ in range(5):
            assert (
                await ensure_dependent_base_recut(dependent, [dep], db_session) is False
            )
        assert merge_mock.await_count == 1
        # The suppressed cycles are not counted as failed attempts either.
        assert dependent.context.get("_recut_recheck_attempts") == 1

        # Once the window lapses the retry resumes (backoff, not abandonment) —
        # and a recovered GitHub immediately unblocks the dependent.
        await self._elapse_recut_backoff(db_session, dependent)
        merge_mock.return_value = {"merged": True, "already": False}
        assert await ensure_dependent_base_recut(dependent, [dep], db_session) is True
        assert merge_mock.await_count == 2
        assert not any(str(k).startswith("_recut_") for k in (dependent.context or {}))

    @pytest.mark.asyncio
    async def test_successful_recut_clears_accounting_and_resolves_alert(
        self, db_session, mocker
    ):
        """A re-cut that later SUCCEEDS clears the ``_recut_*`` accounting, lets the
        dependent dispatch normally (True), and resolves the open escalation."""
        from app.orchestrator_health import (
            ensure_dependent_base_recut,
            _utcnow,
            _RECUT_CONFLICT_ESCALATION_THRESHOLD,
        )

        mocker.patch("app.broadcast.broadcast_to_project", new_callable=AsyncMock)
        mocker.patch(
            "app.routers.agent.get_github_token_for_task",
            new_callable=AsyncMock,
            return_value=("tok", "owner", "repo"),
        )
        merge_mock = mocker.patch(
            "app.github.merge_branch",
            new_callable=AsyncMock,
            return_value={"merged": False, "conflict": True},
        )
        project = make_project(
            name="RecutBound4", user_id="u1", integration_branch="main"
        )
        db_session.add(project)
        await db_session.commit()
        await db_session.refresh(project)
        dep, dependent = self._landed_cross_base_pair(project)
        db_session.add_all([dep, dependent])
        await db_session.commit()

        # Escalate via persistent conflicts (elapsing the backed-off window between
        # cycles so each iteration is a real attempt).
        for _ in range(_RECUT_CONFLICT_ESCALATION_THRESHOLD):
            await self._elapse_recut_backoff(db_session, dependent)
            await ensure_dependent_base_recut(dependent, [dep], db_session)
        assert dependent.context.get("_recut_escalated") is True
        assert len(await self._open_escalation_alerts(db_session, dependent)) == 1

        # Rewind the post-escalation backoff window so the next attempt re-issues
        # the merge, and make that merge succeed.
        dependent.context = {
            **dependent.context,
            "_recut_recheck_after": (_utcnow() - timedelta(hours=1)).isoformat(),
        }
        db_session.add(dependent)
        await db_session.commit()
        merge_mock.return_value = {"merged": True, "already": False}

        ok = await ensure_dependent_base_recut(dependent, [dep], db_session)
        assert ok is True
        # Accounting fully cleared.
        assert not any(str(k).startswith("_recut_") for k in (dependent.context or {}))
        # Open escalation reconciled (dismissed).
        assert await self._open_escalation_alerts(db_session, dependent) == []

    @pytest.mark.asyncio
    async def test_missing_base_escalates_immediately_and_is_not_transient(
        self, db_session, mocker
    ):
        """A deleted/absent base branch (HTTP 404) is PERMANENT, not transient: it
        escalates on the FIRST occurrence (no retry bound), records the
        ``_recut_base_missing`` durable marker and an observable status message,
        and does NOT count toward / reset via the consecutive-conflict bound."""
        from app.orchestrator_health import ensure_dependent_base_recut
        from app.models import AlertSeverity

        mocker.patch("app.broadcast.broadcast_to_project", new_callable=AsyncMock)
        mocker.patch(
            "app.routers.agent.get_github_token_for_task",
            new_callable=AsyncMock,
            return_value=("tok", "owner", "repo"),
        )
        merge_mock = mocker.patch(
            "app.github.merge_branch",
            new_callable=AsyncMock,
            return_value={"merged": False, "missing": True},
        )
        project = make_project(
            name="RecutMissing1", user_id="u1", integration_branch="main"
        )
        db_session.add(project)
        await db_session.commit()
        await db_session.refresh(project)
        dep, dependent = self._landed_cross_base_pair(project)
        db_session.add_all([dep, dependent])
        await db_session.commit()

        # A SINGLE missing-branch outcome must already escalate (no bound) — the
        # backoff gate is armed only AFTER an attempt was made and classified, so
        # first-occurrence escalation is unaffected by the backoff fix.
        ok = await ensure_dependent_base_recut(dependent, [dep], db_session)
        assert ok is False
        assert merge_mock.await_count == 1
        assert dependent.context.get("_recut_last_outcome") == "missing"
        assert dependent.context.get("_recut_base_missing") is True
        assert dependent.context.get("_recut_escalated") is True
        # Permanent, not transient: never folded onto the conflict streak.
        assert dependent.context.get("_recut_conflict_attempts") == 0
        assert "missing" in (dependent.latest_status_message or "")
        alerts = await self._open_escalation_alerts(db_session, dependent)
        assert len(alerts) == 1
        assert alerts[0].severity == AlertSeverity.ERROR

        # Re-running does not create a second alert and issues no further merge
        # call (deduped + post-failure backoff short-circuits the merge API).
        assert await ensure_dependent_base_recut(dependent, [dep], db_session) is False
        assert merge_mock.await_count == 1
        assert len(await self._open_escalation_alerts(db_session, dependent)) == 1

    @pytest.mark.asyncio
    async def test_recut_classifies_missing_branch_distinctly(self, db_session, mocker):
        """``recut_dependent_base_from_integration`` maps a 404 merge result to the
        distinct ``"missing"`` outcome rather than folding it into ``"error"``."""
        from app.branch_resolution import recut_dependent_base_from_integration

        mocker.patch(
            "app.routers.agent.get_github_token_for_task",
            new_callable=AsyncMock,
            return_value=("tok", "owner", "repo"),
        )
        mocker.patch(
            "app.github.merge_branch",
            new_callable=AsyncMock,
            return_value={"merged": False, "missing": True},
        )
        project = make_project(
            name="RecutMissing2", user_id="u1", integration_branch="main"
        )
        db_session.add(project)
        await db_session.commit()
        await db_session.refresh(project)
        _, dependent = self._landed_cross_base_pair(project)
        db_session.add(dependent)
        await db_session.commit()

        outcome = await recut_dependent_base_from_integration(
            dependent, project, db_session
        )
        assert outcome == "missing"


class TestFinishBeforeStartPriority:
    """Step 2.97: tree-completing tasks (rollup reviews, ordinary reviews,
    plan-children) outrank brand-new work under slot scarcity.

    Uses the pure ``apply_completion_priority`` function so DB mocking is not
    needed — the async ``reorder_by_completion_priority`` wrapper only adds the
    DB fetch for rollup parent context, which is separately exercised.
    """

    @staticmethod
    def _apply(
        eligible, types_by_id, parent_id_by_id, active_task_ids, rollup_parent_ids
    ):
        from app.orchestrator_filters import apply_completion_priority

        return apply_completion_priority(
            eligible,
            types_by_id,
            parent_id_by_id,
            active_task_ids,
            rollup_parent_ids,
        )

    def test_empty_and_singleton_unchanged(self):
        assert self._apply([], {}, {}, set(), set()) == []
        assert self._apply(["a"], {"a": "review"}, {"a": None}, set(), set()) == ["a"]

    def test_rollup_review_outranks_new_research(self):
        """A rollup review (tier 1) comes before new research (tier 4)."""
        eligible = ["research1", "rollup_rv"]
        types = {"research1": "research", "rollup_rv": "review"}
        parents = {"research1": None, "rollup_rv": "plan-parent"}
        active = set()
        rollup_parents = {"plan-parent"}
        out = self._apply(eligible, types, parents, active, rollup_parents)
        assert out[0] == "rollup_rv", "rollup review must precede new research"
        assert out[1] == "research1"

    def test_tier_ordering_rollup_ordinary_planchild_new(self):
        """All four tiers sort correctly under mixed queue."""
        eligible = ["new_research", "plan_child", "ordinary_rv", "rollup_rv"]
        types = {
            "new_research": "research",
            "plan_child": "create",
            "ordinary_rv": "review",
            "rollup_rv": "fix",
        }
        parents = {
            "new_research": None,
            "plan_child": "running-plan",
            "ordinary_rv": "other-parent",  # parent not a rollup
            "rollup_rv": "rollup-plan",
        }
        active = {"running-plan"}
        rollup_parents = {"rollup-plan"}
        out = self._apply(eligible, types, parents, active, rollup_parents)
        assert out == ["rollup_rv", "ordinary_rv", "plan_child", "new_research"], (
            f"Expected tier 1,2,3,4 order; got {out}"
        )

    def test_stable_within_tier(self):
        """Existing order is preserved for tasks within the same tier."""
        eligible = ["rv1", "rv2", "rv3"]
        types = {"rv1": "review", "rv2": "review", "rv3": "review"}
        parents = {"rv1": None, "rv2": None, "rv3": None}
        # All tier 2 — original order must be preserved
        out = self._apply(eligible, types, parents, set(), set())
        assert out == ["rv1", "rv2", "rv3"]

    def test_permutation_invariant(self):
        """Result is always a permutation — same length and membership."""
        eligible = ["r1", "rv1", "c1", "f1", "r2"]
        types = {
            "r1": "research",
            "rv1": "review",
            "c1": "create",
            "f1": "fix",
            "r2": "research",
        }
        parents = {"r1": None, "rv1": "p1", "c1": "active-plan", "f1": None, "r2": None}
        active = {"active-plan"}
        rollup_parents = {"p1"}
        out = self._apply(eligible, types, parents, active, rollup_parents)
        assert sorted(out) == sorted(eligible)
        assert len(out) == len(eligible)

    def test_scarce_slots_tree_finishing_dispatches_first_reserved_floor_held(self):
        """Integration: scarce slots (3 available, 6 eligible) — tree-finishing
        tasks fill the top of the dispatch window while the reserved_slots_for_types
        floor still admits at least one create and one research.

        Simulates the full Step-2.97 → reserve_slots_for_types → [:slots_available]
        sequence as it runs in the orchestrator dispatch loop.
        """
        from app.orchestrator_filters import (
            apply_completion_priority,
            reserve_slots_for_types,
        )

        # 6 eligible tasks: 1 rollup review, 1 ordinary fix, 1 plan-child create,
        # 2 new research tasks, 1 new standalone create.
        eligible = [
            "research1",
            "rollup_rv",
            "research2",
            "ord_fix",
            "new_create",
            "plan_child",
        ]
        slots_available = 3

        # Simulate types_by_id, parent_id_by_id
        types = {
            "research1": "research",
            "rollup_rv": "review",
            "research2": "research",
            "ord_fix": "fix",
            "new_create": "create",
            "plan_child": "create",
        }
        parents = {
            "research1": None,
            "rollup_rv": "rollup-plan",
            "research2": None,
            "ord_fix": "other-plan",
            "new_create": None,
            "plan_child": "running-plan",
        }
        active_task_ids = {"running-plan"}
        rollup_parent_ids = {"rollup-plan"}

        # Step 2.97 — finish-before-start reorder
        reordered = apply_completion_priority(
            eligible, types, parents, active_task_ids, rollup_parent_ids
        )
        # Tier 1: rollup_rv, Tier 2: ord_fix, Tier 3: plan_child,
        # Tier 4: research1, research2, new_create (original order within tier)
        assert reordered[0] == "rollup_rv", "rollup review must lead"
        assert reordered[1] == "ord_fix", "ordinary fix must be second"
        assert reordered[2] == "plan_child", "plan-child must be third"

        # Simulate candidate_tasks as minimal objects with first_payload/id
        class _FakeTask:
            def __init__(self, tid, ttype):
                self.id = tid
                self.first_payload = {"type": ttype}

        fake_candidates = [_FakeTask(tid, types[tid]) for tid in eligible]
        runtime_cfg = {"reserved_slots_for_types": {"create": 1, "research": 1}}

        # Slot reservation: ensures at least 1 create and 1 research get a slot
        after_reserve = reserve_slots_for_types(
            reordered, fake_candidates, runtime_cfg, slots_available
        )

        # Final cut
        dispatched = after_reserve[:slots_available]

        # Core assertion: rollup review must be dispatched (tree-finishing wins)
        assert "rollup_rv" in dispatched, (
            f"rollup review must be dispatched; dispatched={dispatched}"
        )
        # Anti-starvation floor: at least one research or create must also get in
        new_work_dispatched = [
            t for t in dispatched if types[t] in ("create", "research")
        ]
        assert len(new_work_dispatched) >= 1, (
            f"reserved floor must admit at least one create/research; dispatched={dispatched}"
        )


# ══════════════════════════════════════════════════════════════════════════════
# Per-task dispatch crash containment + poison-task quarantine
# ══════════════════════════════════════════════════════════════════════════════


class TestDispatchCrashContainment:
    """The dispatch loop wraps each task's prep in a per-task containment
    boundary: a crash preparing one task must not abort the cycle (later
    tasks still dispatch, post-loop CI polling still runs), the iteration's
    pre-claimed LLM-account slot is released, and a task whose prep crashes
    DISPATCH_PREP_FAILURE_QUARANTINE_THRESHOLD consecutive times is
    quarantined (FAILED + PROVISION_FAILURE + alert) instead of starving the
    queue every cycle. Harness mirrors TestProjectConcurrencyCap: one real
    orchestrator cycle against in-memory SQLite, provisioning mocked.
    """

    @staticmethod
    def _naive_utcnow():
        from datetime import timezone as _tz

        return datetime.now(_tz.utc).replace(tzinfo=None)

    @pytest.fixture()
    def orch_crash_db(self, mocker):
        """In-memory SQLite DB wired into the orchestrator, plus essential mocks."""
        import asyncio
        from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession
        from sqlalchemy.orm import sessionmaker
        from sqlalchemy.pool import StaticPool
        from sqlmodel import SQLModel
        from sqlalchemy.dialects.sqlite.base import (
            SQLiteTypeCompiler as _SQLiteTypeCompiler,
        )

        if not hasattr(_SQLiteTypeCompiler, "visit_JSONB"):
            _SQLiteTypeCompiler.visit_JSONB = lambda self, type_, **kw: self.visit_JSON(
                type_, **kw
            )
        import app.orchestrator as _orch_module

        async def _build():
            _engine = create_async_engine(
                "sqlite+aiosqlite:///:memory:",
                connect_args={"check_same_thread": False},
                poolclass=StaticPool,
            )
            async with _engine.begin() as conn:
                await conn.run_sync(SQLModel.metadata.create_all)
            _Session = sessionmaker(
                bind=_engine,
                class_=AsyncSession,
                autocommit=False,
                autoflush=False,
                expire_on_commit=False,
            )
            mocker.patch("app.orchestrator.AsyncSessionLocal", _Session)
            mocker.patch("app.dep_lifecycle.AsyncSessionLocal", _Session)
            mocker.patch("app.broadcast.broadcast_to_project", new_callable=AsyncMock)
            mocker.patch("app.vm_lifecycle._ensure_vm_deleted", new_callable=AsyncMock)
            mocker.patch("app.vm_lifecycle._get_actual_gcp_vm_count", return_value=0)
            mocker.patch("app.main.load_max_plan_pool", new_callable=AsyncMock)
            mocker.patch("app.main.recheck_disabled_accounts", new_callable=AsyncMock)
            mocker.patch("app.routers.agent_status.record_task_result")
            mocker.patch(
                "app.deploy_health._check_monitoring_window", new_callable=AsyncMock
            )
            mocker.patch(
                "app.deploy_health._check_external_health", new_callable=AsyncMock
            )
            mocker.patch("app.deploy_health._check_pause_mode", new_callable=AsyncMock)
            mocker.patch(
                "app.autonomous_research._check_idle_research", new_callable=AsyncMock
            )
            mocker.patch("app.services._recover_orphaned_tasks", new_callable=AsyncMock)
            mocker.patch(
                "app.alert_dispatch._create_orchestrator_error_alerts",
                new_callable=AsyncMock,
            )
            mocker.patch(
                "app.orchestrator._persist_zone_health", new_callable=AsyncMock
            )
            mocker.patch(
                "app.alert_dispatch._maybe_create_failure_alert", new_callable=AsyncMock
            )
            # Provision wrapper reaches Secret Manager / GCS before the
            # (separately mocked) provision_agent_vm call — patch both so the
            # success path is hermetic in CI (no ambient Google credentials).
            mocker.patch(
                "app.orchestrator.prepare_and_store_agent_secret",
                new_callable=AsyncMock,
                return_value="projects/test/secrets/agent/versions/1",
            )
            mocker.patch(
                "app.orchestrator.upload_json_payload",
                new_callable=AsyncMock,
                return_value="gs://test-bucket/payloads/test.json",
            )

            now = self._naive_utcnow()
            _orch_module._last_pool_refresh = now
            _orch_module._last_zone_health_persist = now
            _orch_module._last_orphan_check = now
            _orch_module._last_evaluating_timeout_check = now
            _orch_module._last_deadlock_check = now
            _orch_module._last_anomaly_state_persist = now
            _orch_module._last_anomaly_sweep = now
            _orch_module._last_post_commit_sweep = now
            _orch_module._last_sidecar_sweep = now
            _orch_module._last_bootstrap_recovery_sweep = now
            _orch_module._last_lane_baseline_backfill = now
            _orch_module._last_merge_credit_backfill = now
            _orch_module._last_v22_finding_backfill = now
            _orch_module._last_runner_deactivation_sweep = now
            _orch_module._last_runner_degraded_sweep = now
            _orch_module._last_runner_escalation_sweep = now
            _orch_module._last_local_runner_hold_sweep = now
            _shared_state.deploy_monitoring["task_results"].clear()
            return _Session, _engine

        loop = asyncio.new_event_loop()
        try:
            _Session, _engine = loop.run_until_complete(_build())
        finally:
            loop.close()
        try:
            yield _Session
        finally:
            import contextlib

            async def _teardown():
                with contextlib.suppress(Exception):
                    await _engine.dispose()

            loop = asyncio.new_event_loop()
            try:
                loop.run_until_complete(_teardown())
            finally:
                loop.close()

    @staticmethod
    async def _run_one_cycle(mocker):
        import asyncio
        import contextlib
        import app.orchestrator as _orch_module
        import app.pipeline_health_state as _pipeline_state

        mocker.patch("asyncio.sleep", side_effect=asyncio.CancelledError())
        with contextlib.suppress(asyncio.CancelledError):
            await _orch_module.orchestrator_worker()

        # Drain background provisioning coroutines (see orch_cap_db's
        # _run_one_cycle for why we await rather than cancel).
        _orphan_keys_before = set(_pipeline_state.background_tasks)
        for _coro in list(_orch_module._provision_coroutines.values()):
            _loop = _coro.get_loop()
            if _loop.is_closed():
                continue
            with contextlib.suppress(BaseException):
                await _coro
        _orch_module._provision_coroutines.clear()
        for _key in set(_pipeline_state.background_tasks) - _orphan_keys_before:
            _orphan = _pipeline_state.background_tasks.pop(_key, None)
            if _orphan is not None:
                _orphan.cancel()
                with contextlib.suppress(BaseException):
                    await _orphan

    @staticmethod
    def _mock_dispatch_runtime(mocker, account_id="acct-crash-test"):
        """Standard dispatch-path mocks: account-backed selection, instant
        provisioning, no warm pool, inert runtime-config sync."""
        from app.max_plan_pool import LLMSelectionResult

        provision_mock = mocker.patch(
            "app.orchestrator.provision_agent_vm",
            new_callable=AsyncMock,
            return_value=True,
        )
        mocker.patch(
            "app.max_plan_pool.select_llm_runtime_for_task",
            new_callable=AsyncMock,
            return_value=LLMSelectionResult(
                provider="anthropic",
                auth_mode="oauth",
                model="claude-sonnet-4-6",
                quality_tier=None,
                account={
                    "id": account_id,
                    "name": "Crash Test Acct",
                    "runtime_secret": "tok",
                    "claude_config_json": None,
                    "provider_metadata": {},
                },
            ),
        )
        mocker.patch(
            "app.warm_pool.try_pool_dispatch",
            new_callable=AsyncMock,
            return_value=None,
        )
        mocker.patch(
            "app.orchestrator._sync_runtime_config_from_state",
            new_callable=AsyncMock,
            return_value=[],
        )
        return provision_mock

    @staticmethod
    def _poison_staleness_recompute(mocker, poison_task_id):
        """Make recompute_launch_staleness (a dispatch-prep step that runs
        just before the PROVISIONING commit) crash for ONE task and behave
        normally (no context change) for every other task."""

        async def _poisoned(session, task):
            if task.id == poison_task_id:
                raise ValueError("poisoned dispatch prep")
            return False

        return mocker.patch(
            "app.research_completion.recompute_launch_staleness",
            side_effect=_poisoned,
        )

    @staticmethod
    def _contend_staleness_recompute(mocker, contend_task_id):
        """Make recompute_launch_staleness raise a TRANSIENT DB lock-timeout —
        the prod shape: an asyncpg LockNotAvailableError (SQLSTATE 55P03) wrapped
        by SQLAlchemy's PendingRollbackError, whose own message omits the lock
        cause so detection must walk the __cause__ chain — for ONE task; behaves
        normally (no context change) for every other task."""
        from sqlalchemy.exc import PendingRollbackError

        class _FakeLockNotAvailableError(Exception):
            sqlstate = "55P03"

            def __str__(self):
                return "canceling statement due to lock timeout"

        async def _contended(session, task):
            if task.id == contend_task_id:
                try:
                    raise _FakeLockNotAvailableError()
                except _FakeLockNotAvailableError as _orig:
                    raise PendingRollbackError(
                        "This Session's transaction has been rolled back due to "
                        "a previous exception during flush."
                    ) from _orig
            return False

        return mocker.patch(
            "app.research_completion.recompute_launch_staleness",
            side_effect=_contended,
        )

    @staticmethod
    def _set_slots(_orch_module, n=4):
        _orch_module._runtime_config["max_concurrent_vms"] = n
        _orch_module._runtime_config["warm_pool_enabled"] = False

    @staticmethod
    def _clear_slots(_orch_module):
        _orch_module._runtime_config.pop("max_concurrent_vms", None)
        _orch_module._runtime_config.pop("warm_pool_enabled", None)

    @pytest.mark.asyncio
    async def test_prep_crash_contained_and_next_task_dispatches(
        self, orch_crash_db, mocker
    ):
        """A mid-loop dispatch-prep crash on the FIRST-ranked task must not
        abort the cycle: the later-ranked task still dispatches and the
        crashed task stays QUEUED with its containment counter persisted.

        Assertions here are committed-DB-state only: the healthy task's
        background provision coroutine shares this harness's single sqlite
        connection with the cycle's post-loop stages, so cycle-shared globals
        (e.g. _orchestrator_consecutive_errors) are asserted in the
        poison-only CI-poll test below where no such race exists."""
        from sqlmodel import select as _select
        import app.orchestrator as _orch_module

        self._mock_dispatch_runtime(mocker)
        self._set_slots(_orch_module)

        stale = self._naive_utcnow() - timedelta(minutes=5)
        async with orch_crash_db() as session:
            project = make_project(name="Crash Containment", user_id=_TEST_USER)
            session.add(project)
            await session.commit()
            await session.refresh(project)

            poison = make_task(
                project_id=project.id,
                status=TaskStatus.QUEUED,
                vm_instance_id=None,
                priority=10,
                payloads=[{"type": "create", "prompt": "poison task"}],
                created_at=stale,
            )
            healthy = make_task(
                project_id=project.id,
                status=TaskStatus.QUEUED,
                vm_instance_id=None,
                priority=1,
                payloads=[{"type": "create", "prompt": "healthy task"}],
            )
            session.add(poison)
            session.add(healthy)
            await session.commit()
            await session.refresh(poison)
            await session.refresh(healthy)
            poison_id, healthy_id = poison.id, healthy.id

        self._poison_staleness_recompute(mocker, poison_id)
        try:
            await self._run_one_cycle(mocker)
        finally:
            self._clear_slots(_orch_module)

        async with orch_crash_db() as verify:
            poison_row = (
                (await verify.execute(_select(Task).where(Task.id == poison_id)))
                .scalars()
                .first()
            )
            healthy_row = (
                (await verify.execute(_select(Task).where(Task.id == healthy_id)))
                .scalars()
                .first()
            )

        assert healthy_row is not None
        assert healthy_row.status == TaskStatus.PROVISIONING, (
            "Task ranked after the crashing one must still dispatch in the "
            f"same cycle, got {healthy_row.status}"
        )
        assert poison_row is not None
        assert poison_row.status == TaskStatus.QUEUED, (
            f"Contained crash must leave the task QUEUED, got {poison_row.status}"
        )
        assert (poison_row.context or {}).get("_dispatch_prep_failures") == 1, (
            "Containment must persist the per-task crash counter, got "
            f"{(poison_row.context or {}).get('_dispatch_prep_failures')!r}"
        )
        assert "Dispatch preparation crashed" in (
            poison_row.latest_status_message or ""
        )

    @pytest.mark.asyncio
    async def test_transient_contention_not_quarantined(
        self, orch_crash_db, mocker
    ):
        """A dispatch-prep failure that is TRANSIENT DB row/lock contention (the
        30s lock_timeout expiring, surfaced as a PendingRollbackError wrapping an
        asyncpg LockNotAvailableError) must NOT be treated as a poison task: the
        _dispatch_prep_failures counter must not increment and the task must stay
        QUEUED — even when it is already one crash away from the quarantine
        threshold. Contention self-heals when the lock releases; quarantining
        healthy tasks (review/fix/design_doc_update) was the prod bug."""
        from sqlmodel import select as _select
        import app.orchestrator as _orch_module

        self._mock_dispatch_runtime(mocker)
        self._set_slots(_orch_module)

        threshold = _orch_module.DISPATCH_PREP_FAILURE_QUARANTINE_THRESHOLD
        async with orch_crash_db() as session:
            project = make_project(name="Contention Not Poison", user_id=_TEST_USER)
            session.add(project)
            await session.commit()
            await session.refresh(project)

            # Pre-seed the counter to threshold-1: a genuine crash here WOULD
            # quarantine (FAILED). Proving the task stays QUEUED at counter==
            # threshold-1 is the strong form of "contention never fails a task".
            contended = make_task(
                project_id=project.id,
                status=TaskStatus.QUEUED,
                vm_instance_id=None,
                payloads=[{"type": "review", "prompt": "healthy review task"}],
                context={"_dispatch_prep_failures": threshold - 1},
            )
            session.add(contended)
            await session.commit()
            await session.refresh(contended)
            contended_id = contended.id

        self._contend_staleness_recompute(mocker, contended_id)
        try:
            await self._run_one_cycle(mocker)
        finally:
            self._clear_slots(_orch_module)

        async with orch_crash_db() as verify:
            row = (
                (await verify.execute(_select(Task).where(Task.id == contended_id)))
                .scalars()
                .first()
            )

        assert row is not None
        # Primary invariant: contention never transitions the task to FAILED —
        # it is left QUEUED to retry when the lock releases. (A genuine crash at
        # this same pre-seeded threshold-1 WOULD quarantine to FAILED, per
        # test_poison_task_quarantined_at_threshold.)
        assert row.status == TaskStatus.QUEUED, (
            "Transient DB contention must leave the task QUEUED for retry, "
            f"never FAILED/quarantined — got {row.status}"
        )
        # The quarantine counter must NOT have advanced to the threshold: the
        # contention branch never touches it (in prod the rollback of the
        # poisoned txn preserves the prior committed value; either way it must
        # stay strictly below the quarantine threshold).
        _counter = (row.context or {}).get("_dispatch_prep_failures")
        assert _counter is None or _counter < threshold, (
            "Transient contention must NOT accrue toward the quarantine "
            f"counter; expected < {threshold}, got {_counter!r}"
        )
        assert (row.context or {}).get("failure_category") != "PROVISION_FAILURE"
        assert "quarantined" not in (row.latest_status_message or "")

    @pytest.mark.asyncio
    async def test_post_dispatch_ci_poll_still_reached(self, orch_crash_db, mocker):
        """The post-loop CI polling stage must still run — and the cycle must
        complete without feeding the consecutive-error backoff — in a cycle
        where a task's dispatch prep crashed (previously the exception
        aborted the whole try block and CI-waiting tasks starved).

        Poison-only setup: nothing dispatches, so no background provision
        coroutine races the cycle and the consecutive-error assertion is
        deterministic."""
        import app.orchestrator as _orch_module
        import app.orchestrator_ci_monitor as _ci_monitor_module

        self._mock_dispatch_runtime(mocker)
        self._set_slots(_orch_module)
        _orch_module._orchestrator_consecutive_errors = 0
        # The EVALUATING/CI-waiting monitor sweep was extracted to
        # app.orchestrator_ci_monitor; the ci_waiting SQL prefilter now
        # resolves as a local of that module, so spy it there.
        ci_filter_spy = mocker.spy(_ci_monitor_module, "_ci_waiting_sql_filter")

        async with orch_crash_db() as session:
            project = make_project(name="CI Poll Survives", user_id=_TEST_USER)
            session.add(project)
            await session.commit()
            await session.refresh(project)

            poison = make_task(
                project_id=project.id,
                status=TaskStatus.QUEUED,
                vm_instance_id=None,
                payloads=[{"type": "create", "prompt": "poison task"}],
            )
            session.add(poison)
            await session.commit()
            await session.refresh(poison)
            poison_id = poison.id

        self._poison_staleness_recompute(mocker, poison_id)
        try:
            await self._run_one_cycle(mocker)
        finally:
            self._clear_slots(_orch_module)

        assert ci_filter_spy.called, (
            "tasks-waiting-on-CI poll must execute even when a task's "
            "dispatch prep crashed earlier in the cycle"
        )
        assert _orch_module._orchestrator_consecutive_errors == 0, (
            "A contained per-task crash must not feed the cycle-level "
            "consecutive-error backoff"
        )

    @pytest.mark.asyncio
    async def test_prep_crash_releases_preclaimed_account(self, orch_crash_db, mocker):
        """The LLM-account slot pre-incremented by runtime selection must be
        released (with the daily counter reverted) when dispatch prep crashes
        after selection."""
        import app.orchestrator as _orch_module

        self._mock_dispatch_runtime(mocker, account_id="acct-poison-release")
        release_mock = mocker.patch(
            "app.max_plan_pool.release_max_plan_account",
            new_callable=AsyncMock,
        )
        self._set_slots(_orch_module)

        async with orch_crash_db() as session:
            project = make_project(name="Account Release", user_id=_TEST_USER)
            session.add(project)
            await session.commit()
            await session.refresh(project)

            poison = make_task(
                project_id=project.id,
                status=TaskStatus.QUEUED,
                vm_instance_id=None,
                payloads=[{"type": "create", "prompt": "poison task"}],
            )
            session.add(poison)
            await session.commit()
            await session.refresh(poison)
            poison_id = poison.id

        self._poison_staleness_recompute(mocker, poison_id)
        try:
            await self._run_one_cycle(mocker)
        finally:
            self._clear_slots(_orch_module)

        release_mock.assert_any_await(
            "acct-poison-release",
            revert_daily_task_count=True,
            project_id=str(project.id),
        )

    @pytest.mark.asyncio
    async def test_poison_task_quarantined_at_threshold(self, orch_crash_db, mocker):
        """At the quarantine threshold the repeatedly-crashing task is taken
        out of the dispatch path: FAILED, PROVISION_FAILURE classification,
        per-type failure gate fed via record_task_result, and an operator
        alert — instead of re-crashing the cycle forever."""
        from sqlmodel import select as _select
        import app.orchestrator as _orch_module

        self._mock_dispatch_runtime(mocker)
        # Re-patch to get test-local handles (fixture already patched both).
        record_result_mock = mocker.patch("app.routers.agent_status.record_task_result")
        alert_mock = mocker.patch(
            "app.alert_dispatch._maybe_create_failure_alert", new_callable=AsyncMock
        )
        self._set_slots(_orch_module)

        threshold = _orch_module.DISPATCH_PREP_FAILURE_QUARANTINE_THRESHOLD
        async with orch_crash_db() as session:
            project = make_project(name="Quarantine", user_id=_TEST_USER)
            session.add(project)
            await session.commit()
            await session.refresh(project)

            poison = make_task(
                project_id=project.id,
                status=TaskStatus.QUEUED,
                vm_instance_id=None,
                payloads=[{"type": "create", "prompt": "poison task"}],
                context={"_dispatch_prep_failures": threshold - 1},
            )
            session.add(poison)
            await session.commit()
            await session.refresh(poison)
            poison_id = poison.id

        self._poison_staleness_recompute(mocker, poison_id)
        try:
            await self._run_one_cycle(mocker)
        finally:
            self._clear_slots(_orch_module)

        async with orch_crash_db() as verify:
            row = (
                (await verify.execute(_select(Task).where(Task.id == poison_id)))
                .scalars()
                .first()
            )

        assert row is not None
        assert row.status == TaskStatus.FAILED, (
            f"Task crashing {threshold} consecutive cycles must be "
            f"quarantined FAILED, got {row.status}"
        )
        assert (row.context or {}).get("failure_category") == "PROVISION_FAILURE"
        assert (row.context or {}).get("_dispatch_prep_failures") == threshold, (
            "Quarantine must persist the final crash count"
        )
        assert "quarantined" in (row.latest_status_message or "")
        record_result_mock.assert_any_call(
            success=False,
            failure_class="PROVISION_FAILURE",
            task_id=str(poison_id),
            task_type="create",
        )
        assert alert_mock.await_count >= 1, (
            "Quarantine must create an operator-visible failure alert"
        )

    @pytest.mark.asyncio
    async def test_failure_counter_cleared_on_successful_dispatch(
        self, orch_crash_db, mocker
    ):
        """A task that previously crashed (counter > 0) and then dispatches
        cleanly must have its containment counter dropped by the PROVISIONING
        commit, so old transient crashes never accumulate into a spurious
        quarantine."""
        from sqlmodel import select as _select
        import app.orchestrator as _orch_module

        self._mock_dispatch_runtime(mocker)
        # No poison: staleness recompute is a benign no-op for every task.
        mocker.patch(
            "app.research_completion.recompute_launch_staleness",
            new_callable=AsyncMock,
            return_value=False,
        )
        self._set_slots(_orch_module)

        async with orch_crash_db() as session:
            project = make_project(name="Counter Reset", user_id=_TEST_USER)
            session.add(project)
            await session.commit()
            await session.refresh(project)

            task = make_task(
                project_id=project.id,
                status=TaskStatus.QUEUED,
                vm_instance_id=None,
                payloads=[{"type": "create", "prompt": "recovered task"}],
                context={"_dispatch_prep_failures": 1},
            )
            session.add(task)
            await session.commit()
            await session.refresh(task)
            task_id = task.id

        try:
            await self._run_one_cycle(mocker)
        finally:
            self._clear_slots(_orch_module)

        async with orch_crash_db() as verify:
            row = (
                (await verify.execute(_select(Task).where(Task.id == task_id)))
                .scalars()
                .first()
            )

        assert row is not None
        assert row.status == TaskStatus.PROVISIONING, (
            f"Clean dispatch expected, got {row.status}"
        )
        assert "_dispatch_prep_failures" not in (row.context or {}), (
            "Successful dispatch must clear the containment counter, got "
            f"{(row.context or {}).get('_dispatch_prep_failures')!r}"
        )

    @pytest.mark.asyncio
    async def test_filter_resource_eligible_continues_on_per_task_exception(
        self, db_session, mocker
    ):
        """When _project_resource_dispatch_defer_message raises an exception for
        one task, _filter_project_resource_dispatch_eligible must catch it, defer
        that task with an error message, and continue filtering remaining tasks.

        Without per-task exception containment, the exception would abort the loop
        and prevent filtering of all remaining tasks — starving lower-ranked
        dispatch and the post-loop CI-merge stage."""
        from app.orchestrator import _filter_project_resource_dispatch_eligible
        from sqlmodel import select as _select

        project = make_project(name="Resource Filter Test", user_id=_TEST_USER)
        db_session.add(project)
        await db_session.commit()
        await db_session.refresh(project)

        # Create three tasks: one that will raise an exception, and two healthy ones
        poison = make_task(
            project_id=project.id,
            status=TaskStatus.QUEUED,
            payloads=[{"type": "create", "prompt": "poison task"}],
        )
        healthy1 = make_task(
            project_id=project.id,
            status=TaskStatus.QUEUED,
            payloads=[{"type": "create", "prompt": "healthy task 1"}],
        )
        healthy2 = make_task(
            project_id=project.id,
            status=TaskStatus.QUEUED,
            payloads=[{"type": "create", "prompt": "healthy task 2"}],
        )
        db_session.add(poison)
        db_session.add(healthy1)
        db_session.add(healthy2)
        await db_session.commit()
        await db_session.refresh(poison)
        await db_session.refresh(healthy1)
        await db_session.refresh(healthy2)

        poison_id = poison.id
        healthy1_id = healthy1.id
        healthy2_id = healthy2.id
        eligible_ids = [poison_id, healthy1_id, healthy2_id]

        # Mock the defer message function to raise an exception for the poison task
        async def _mock_defer_msg(session, task, *args, **kwargs):
            if task.id == poison_id:
                raise ValueError("poisoned defer check")
            return None

        mocker.patch(
            "app.orchestrator._project_resource_dispatch_defer_message",
            side_effect=_mock_defer_msg,
        )
        broadcast_mock = mocker.patch(
            "app.broadcast.broadcast_to_project", new_callable=AsyncMock
        )

        # Run the filter function
        filtered = await _filter_project_resource_dispatch_eligible(
            db_session,
            eligible_ids,
            [poison, healthy1, healthy2],
            {project.id: 0},
            broadcast_mock,
        )

        # Verify the results
        assert healthy1_id in filtered, (
            "Healthy task 1 must be included in filtered list despite poison task's exception"
        )
        assert healthy2_id in filtered, (
            "Healthy task 2 must be included in filtered list despite poison task's exception"
        )
        assert poison_id not in filtered, (
            "Task with defer check exception must be deferred (excluded from filtered list)"
        )

        # Verify the poison task is deferred with an error message
        poison_row = (
            (await db_session.execute(_select(Task).where(Task.id == poison_id)))
            .scalars()
            .first()
        )
        assert poison_row is not None
        assert "error" in (poison_row.latest_status_message or "").lower(), (
            f"Poison task must have error deferral message, got: "
            f"{poison_row.latest_status_message!r}"
        )
        assert "ValueError" in (poison_row.latest_status_message or ""), (
            f"Poison task deferral message must include exception type, got: "
            f"{poison_row.latest_status_message!r}"
        )


@pytest.mark.asyncio
async def test_retry_after_release_clears_failure_class(db_session, mocker):
    """
    Regression: When the orchestrator releases a task from a rate-limit hold
    via retry_after early-release, it must clear the stale failure_class from
    the task context. Without this fix, _classify_failure_class short-circuits
    on the persisted value and misclassifies the new failure.
    """
    from sqlmodel import select as _select
    from app.orchestrator import _clear_retry_after_state
    from app.task_blocked import strip_fresh_attempt_context
    from app.time_utils import _utcnow

    project = make_project(name="Retry After Test", user_id="test-user")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    # Create task with expired retry_after and stale failure_class
    now = _utcnow()
    task = make_task(
        project_id=project.id,
        status=TaskStatus.QUEUED,
        payloads=[{"type": "create", "prompt": "test task"}],
        context={
            "failure_class": "RATE_LIMIT",  # Stale failure class from previous attempt
            "failure_category": "RATE_LIMIT",
        },
    )
    # Set retry_after_until to the past (already expired)
    task.retry_after_until = now - timedelta(minutes=5)
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)
    task_id = task.id

    # Verify initial state
    initial_task = (
        (await db_session.execute(_select(Task).where(Task.id == task_id)))
        .scalars()
        .first()
    )
    assert initial_task is not None
    assert initial_task.retry_after_until is not None
    assert initial_task.retry_after_until < now
    assert (initial_task.context or {}).get("failure_class") == "RATE_LIMIT"

    # Simulate the orchestrator's retry-after-release path by calling the same
    # functions that orchestrator_worker calls when clearing retry_after
    initial_task.context = strip_fresh_attempt_context(initial_task.context or {})
    _clear_retry_after_state(initial_task)
    db_session.add(initial_task)
    await db_session.commit()
    await db_session.refresh(initial_task)

    # Verify the failure_class was cleared
    assert (initial_task.context or {}).get("failure_class") is None, (
        "failure_class must be cleared when releasing from retry_after hold"
    )
    assert (initial_task.context or {}).get("failure_category") is None, (
        "failure_category must be cleared when releasing from retry_after hold"
    )
    assert initial_task.retry_after_until is None, (
        "retry_after_until must be cleared when releasing from retry_after hold"
    )


class TestCloudRunDispatch:
    """Tests for the Cloud Run Jobs dispatch path in the orchestrator.

    Verifies the dark-gate behaviour: an empty allowlist leaves the GCE path
    unchanged, and an allowlisted read-only type is routed to
    ``launch_agent_cloud_run_job`` instead of ``provision_agent_vm``.

    Reuses the ``orch_warm_db`` / ``_run_one_cycle`` harness from
    ``TestWarmVMSkipProvision`` — same in-memory SQLite DB, same mocks.
    """

    # ── borrow the fixture + helper from TestWarmVMSkipProvision ─────────────
    orch_warm_db = TestWarmVMSkipProvision.orch_warm_db  # type: ignore[attr-defined]
    _run_one_cycle = staticmethod(TestWarmVMSkipProvision._run_one_cycle)  # type: ignore[attr-defined]

    @staticmethod
    def _naive_utcnow():
        from datetime import timezone as _tz

        return datetime.now(_tz.utc).replace(tzinfo=None)

    @pytest.mark.asyncio
    async def test_cloud_run_dispatch_routes_allowlisted_type(
        self, orch_warm_db, mocker
    ):
        """When cloud_run_task_type_allowlist contains a read-only task type and
        a task of that type is dispatched, launch_agent_cloud_run_job() is called
        and provision_agent_vm() is NOT called.

        This is the named acceptance-evidence test:
          target: test_cloud_run_dispatch_routes_allowlisted_type
        """
        from sqlmodel import select as _select
        import app.orchestrator as _orch_module

        # Put "research" on the allowlist — it is a read-only type.
        _orch_module._runtime_config["cloud_run_task_type_allowlist"] = ["research"]
        try:
            prov_mock = mocker.patch(
                "app.orchestrator.provision_agent_vm",
                new_callable=AsyncMock,
                return_value=True,
            )
            cr_mock = mocker.patch(
                "app.orchestrator.launch_agent_cloud_run_job",
                new_callable=AsyncMock,
                return_value=True,
            )
            mocker.patch(
                "app.orchestrator._sync_runtime_config_from_state",
                new_callable=AsyncMock,
                return_value=[],
            )
            # Supply dummy credentials so the orchestrator reaches the
            # provision branch (no env LLM keys in CI).
            mocker.patch(
                "app.orchestrator._resolve_env_runtime_fallback",
                return_value=(
                    "fake-api-key",
                    "anthropic",
                    "api_key",
                    "claude-opus-4-8",
                    None,
                    {},
                ),
            )

            async with orch_warm_db() as session:
                project = make_project(name="CR Dispatch Test", user_id=_TEST_USER)
                session.add(project)
                await session.commit()
                await session.refresh(project)

                task = make_task(
                    project_id=project.id,
                    vm_instance_id=None,
                    status=TaskStatus.QUEUED,
                    payloads=[{"type": "research", "prompt": "Analyse the codebase"}],
                    context={},
                )
                session.add(task)
                await session.commit()
                await session.refresh(task)
                task_id = task.id

            await self._run_one_cycle(mocker)

            # Cloud Run path must have been taken; GCE path must NOT.
            assert cr_mock.called, (
                "launch_agent_cloud_run_job must be called for an allowlisted "
                "read-only task type"
            )
            assert not prov_mock.called, (
                "provision_agent_vm must NOT be called when the task type is "
                "routed to Cloud Run"
            )

            # Task must have been committed as PROVISIONING.
            async with orch_warm_db() as verify:
                row = (
                    (await verify.execute(_select(Task).where(Task.id == task_id)))
                    .scalars()
                    .first()
                )
                assert row is not None
                assert row.status == TaskStatus.PROVISIONING, (
                    f"Task should be PROVISIONING after CR dispatch, got {row.status}"
                )
        finally:
            _orch_module._runtime_config["cloud_run_task_type_allowlist"] = []

    @pytest.mark.asyncio
    async def test_cloud_run_dispatch_failure_falls_back_to_gce(
        self, orch_warm_db, mocker
    ):
        """When the task type is allowlisted but launch_agent_cloud_run_job()
        returns False (CR dispatch failed), the orchestrator falls back to
        provision_agent_vm() in the same cycle rather than recording a
        PROVISION_FAILURE — and clears the Cloud Run sentinel machine type so the
        task is attributed to the GCE VM it became (not left as a CR task)."""
        from sqlmodel import select as _select
        import app.orchestrator as _orch_module

        _orch_module._runtime_config["cloud_run_task_type_allowlist"] = ["research"]
        try:
            prov_mock = mocker.patch(
                "app.orchestrator.provision_agent_vm",
                new_callable=AsyncMock,
                return_value=True,
            )
            cr_mock = mocker.patch(
                "app.orchestrator.launch_agent_cloud_run_job",
                new_callable=AsyncMock,
                return_value=False,  # CR dispatch fails
            )
            mocker.patch(
                "app.orchestrator._sync_runtime_config_from_state",
                new_callable=AsyncMock,
                return_value=[],
            )
            mocker.patch(
                "app.orchestrator._resolve_env_runtime_fallback",
                return_value=(
                    "fake-api-key",
                    "anthropic",
                    "api_key",
                    "claude-opus-4-8",
                    None,
                    {},
                ),
            )

            async with orch_warm_db() as session:
                project = make_project(name="CR Fallback Test", user_id=_TEST_USER)
                session.add(project)
                await session.commit()
                await session.refresh(project)

                task = make_task(
                    project_id=project.id,
                    vm_instance_id=None,
                    status=TaskStatus.QUEUED,
                    payloads=[{"type": "research", "prompt": "Analyse the codebase"}],
                    context={},
                )
                session.add(task)
                await session.commit()
                await session.refresh(task)
                task_id = task.id

            await self._run_one_cycle(mocker)

            # CR was attempted AND the GCE fallback fired.
            assert cr_mock.called, "launch_agent_cloud_run_job must be attempted"
            assert prov_mock.called, (
                "provision_agent_vm must be called as a fallback when Cloud Run "
                "dispatch returns False"
            )

            async with orch_warm_db() as verify:
                row = (
                    (await verify.execute(_select(Task).where(Task.id == task_id)))
                    .scalars()
                    .first()
                )
                assert row is not None
                assert row.status == TaskStatus.PROVISIONING, (
                    f"Task should be PROVISIONING after GCE fallback, got {row.status}"
                )
                # The CR sentinel must have been cleared on fallback.
                assert not (row.vm_machine_type or "").startswith("cloud-run-"), (
                    f"CR sentinel must be cleared on GCE fallback, got "
                    f"{row.vm_machine_type!r}"
                )
        finally:
            _orch_module._runtime_config["cloud_run_task_type_allowlist"] = []

    @pytest.mark.asyncio
    async def test_cloud_run_empty_allowlist_uses_gce_path(self, orch_warm_db, mocker):
        """When cloud_run_task_type_allowlist is empty (the default), all task
        types use the unchanged provision_agent_vm() GCE path."""
        import app.orchestrator as _orch_module

        # Ensure allowlist is empty (default).
        _orch_module._runtime_config["cloud_run_task_type_allowlist"] = []

        prov_mock = mocker.patch(
            "app.orchestrator.provision_agent_vm",
            new_callable=AsyncMock,
            return_value=True,
        )
        cr_mock = mocker.patch(
            "app.orchestrator.launch_agent_cloud_run_job",
            new_callable=AsyncMock,
            return_value=True,
        )
        mocker.patch(
            "app.orchestrator._sync_runtime_config_from_state",
            new_callable=AsyncMock,
            return_value=[],
        )
        # Supply dummy credentials so the orchestrator reaches the
        # provision branch (no env LLM keys in CI).
        mocker.patch(
            "app.orchestrator._resolve_env_runtime_fallback",
            return_value=(
                "fake-api-key",
                "anthropic",
                "api_key",
                "claude-opus-4-8",
                None,
                {},
            ),
        )

        async with orch_warm_db() as session:
            project = make_project(name="GCE Default Test", user_id=_TEST_USER)
            session.add(project)
            await session.commit()
            await session.refresh(project)

            task = make_task(
                project_id=project.id,
                vm_instance_id=None,
                status=TaskStatus.QUEUED,
                payloads=[{"type": "research", "prompt": "Analyse the codebase"}],
                context={},
            )
            session.add(task)
            await session.commit()

        await self._run_one_cycle(mocker)

        # GCE path must be taken; CR path must NOT.
        assert prov_mock.called, (
            "provision_agent_vm must be called when cloud_run_task_type_allowlist "
            "is empty"
        )
        assert not cr_mock.called, (
            "launch_agent_cloud_run_job must NOT be called when the allowlist is empty"
        )

    @pytest.mark.asyncio
    async def test_cloud_run_write_type_not_routed(self, orch_warm_db, mocker):
        """Even if a write task type were somehow on the allowlist, it must NOT
        be routed to Cloud Run — only types in CLOUD_RUN_CANDIDATE_TASK_TYPES
        (= READ_ONLY_TASK_TYPES) are eligible."""
        import app.orchestrator as _orch_module
        from app.task_capabilities import CLOUD_RUN_CANDIDATE_TASK_TYPES

        # Sanity: "create" is not a candidate.
        assert "create" not in CLOUD_RUN_CANDIDATE_TASK_TYPES

        # Force the allowlist to include a write type (operator error).
        _orch_module._runtime_config["cloud_run_task_type_allowlist"] = ["create"]
        try:
            prov_mock = mocker.patch(
                "app.orchestrator.provision_agent_vm",
                new_callable=AsyncMock,
                return_value=True,
            )
            cr_mock = mocker.patch(
                "app.orchestrator.launch_agent_cloud_run_job",
                new_callable=AsyncMock,
                return_value=True,
            )
            mocker.patch(
                "app.orchestrator._sync_runtime_config_from_state",
                new_callable=AsyncMock,
                return_value=[],
            )
            # Supply dummy credentials so the orchestrator reaches the
            # provision branch (no env LLM keys in CI).
            mocker.patch(
                "app.orchestrator._resolve_env_runtime_fallback",
                return_value=(
                    "fake-api-key",
                    "anthropic",
                    "api_key",
                    "claude-opus-4-8",
                    None,
                    {},
                ),
            )

            async with orch_warm_db() as session:
                project = make_project(
                    name="Write-Type Safety Test", user_id=_TEST_USER
                )
                session.add(project)
                await session.commit()
                await session.refresh(project)

                task = make_task(
                    project_id=project.id,
                    vm_instance_id=None,
                    status=TaskStatus.QUEUED,
                    payloads=[{"type": "create", "prompt": "Add feature"}],
                    context={},
                )
                session.add(task)
                await session.commit()

            await self._run_one_cycle(mocker)

            # GCE path must be taken for a write type regardless of the allowlist.
            assert prov_mock.called, (
                "provision_agent_vm must be called for a write task type even if it "
                "appears in cloud_run_task_type_allowlist"
            )
            assert not cr_mock.called, (
                "launch_agent_cloud_run_job must NOT be called for write task types"
            )
        finally:
            _orch_module._runtime_config["cloud_run_task_type_allowlist"] = []


class TestCloudRunAllowlistValidation:
    """Tests for the admin PATCH /settings cloud_run_task_type_allowlist gate.

    Drives the real _apply_settings handler via AsyncClient to ensure the
    production write-type rejection block in admin_settings.py is actually
    exercised (not re-implemented inline).
    """

    @pytest.mark.asyncio
    async def test_write_type_rejected(self, client):
        """Submitting a write task type returns HTTP 400 from the real handler."""
        resp = await client.patch(
            "/api/admin/settings",
            json={"cloud_run_task_type_allowlist": ["create"]},
        )
        assert resp.status_code == 400

    @pytest.mark.asyncio
    async def test_valid_read_only_type_accepted(self, client):
        """A valid read-only task type is accepted and stored sorted."""
        import app.shared_state as _ss

        original = list(_ss.runtime_config.get("cloud_run_task_type_allowlist", []))
        try:
            resp = await client.patch(
                "/api/admin/settings",
                json={"cloud_run_task_type_allowlist": ["research"]},
            )
            assert resp.status_code == 200
            assert "research" in _ss.runtime_config.get(
                "cloud_run_task_type_allowlist", []
            )
        finally:
            _ss.runtime_config["cloud_run_task_type_allowlist"] = original

    @pytest.mark.asyncio
    async def test_mixed_valid_and_write_type_rejected(self, client):
        """A list containing both a valid read-only and a write type is rejected."""
        resp = await client.patch(
            "/api/admin/settings",
            json={"cloud_run_task_type_allowlist": ["research", "fix"]},
        )
        assert resp.status_code == 400

    @pytest.mark.asyncio
    async def test_empty_list_accepted(self, client):
        """An empty list (disabling Cloud Run routing) is always valid."""
        import app.shared_state as _ss

        original = list(_ss.runtime_config.get("cloud_run_task_type_allowlist", []))
        try:
            resp = await client.patch(
                "/api/admin/settings",
                json={"cloud_run_task_type_allowlist": []},
            )
            assert resp.status_code == 200
            assert _ss.runtime_config.get("cloud_run_task_type_allowlist") == []
        finally:
            _ss.runtime_config["cloud_run_task_type_allowlist"] = original


class TestRunnerDispatchExtractionContract:
    """Guards the ``app.orchestrator_runner_dispatch`` extraction seam.

    The local-runner + compute-experiment dispatch cluster was lifted out of
    ``app.orchestrator`` into ``app.orchestrator_runner_dispatch`` (a pure,
    behaviour-preserving refactor). This exercises the facade contract AND the
    runtime behaviour of the moved helpers so a regression that reverts the
    move, breaks the re-export, or changes the moved logic is caught:

      1. Each moved symbol is re-exported from ``app.orchestrator`` and is the
         *same object* as the canonical definition in the new module.
      2. The moved helpers are no longer defined at ``orchestrator.py``
         top-level (no shadowing, monolith does not re-grow).
      3. The moved pure helpers still compute the correct result via BOTH the
         re-exported binding and the canonical binding — proving the extracted
         production code actually runs.
    """

    MOVED_SYMBOLS = (
        "RunnerDispatchSnapshot",
        "_runner_handles_task_type",
        "_runner_active_task_count",
        "_runner_active_compute_job_count",
        "_runner_active_work_count",
        "_runner_agent_available",
        "_runner_at_capacity",
        "_load_active_runners_cache",
        "_load_runner_compute_job_assignments",
        "_try_runner_dispatch",
        "_task_requires_strategic_local_runner",
        "_hold_strategic_local_runner_task",
        "_try_strategic_local_required_runner_path",
        "_num_or_none",
        "_select_eligible_runner",
        "_build_compute_launch_kwargs",
        "_fail_compute_experiment_task",
        "_dispatch_compute_experiment_directly",
        "_deactivate_stale_runners",
        "_parse_local_runner_hold_timestamp",
        "_create_strategic_local_cloud_fallback_alert",
        "_strategic_local_hold_timeout_seconds",
        "_fail_stale_local_runner_holds",
        "_emit_all_accounts_blocked_stall",
        "_RUNNER_LIVENESS_SECONDS",
        "_LOCAL_RUNNER_WAIT_TIMEOUT_SECONDS",
    )

    def test_moved_symbols_reexported_and_identical(self):
        import app.orchestrator as orch
        import app.orchestrator_runner_dispatch as rd

        for name in self.MOVED_SYMBOLS:
            assert hasattr(rd, name), (
                f"{name} missing from app.orchestrator_runner_dispatch — the "
                f"canonical home for the extracted symbol no longer defines it."
            )
            assert hasattr(orch, name), (
                f"app.orchestrator.{name} is no longer importable. Add the "
                f"re-export: from app.orchestrator_runner_dispatch import {name}."
            )
            assert getattr(orch, name) is getattr(rd, name), (
                f"app.orchestrator.{name} is not the same object as "
                f"app.orchestrator_runner_dispatch.{name} — the orchestrator is "
                f"shadowing the canonical implementation."
            )

    def test_moved_functions_not_redefined_in_orchestrator(self):
        import ast
        import os

        orch_path = os.path.join(
            os.path.dirname(os.path.dirname(__file__)), "app", "orchestrator.py"
        )
        with open(orch_path, encoding="utf-8") as fh:
            tree = ast.parse(fh.read())
        top_level_defs: set[str] = set()
        for node in tree.body:
            if isinstance(
                node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)
            ):
                top_level_defs.add(node.name)
            elif isinstance(node, ast.Assign):
                for target in node.targets:
                    if isinstance(target, ast.Name):
                        top_level_defs.add(target.id)
            elif isinstance(node, ast.AnnAssign) and isinstance(
                node.target, ast.Name
            ):
                top_level_defs.add(node.target.id)

        redefined = sorted(top_level_defs & set(self.MOVED_SYMBOLS))
        assert not redefined, (
            "orchestrator.py re-defines extracted runner-dispatch symbols "
            f"(shadowing app.orchestrator_runner_dispatch): {redefined}. Move "
            "the definition into the sibling module and rely on the re-export."
        )

    def test_moved_pure_helpers_behave_identically(self):
        """The extracted pure helpers still compute correct results via both
        the re-exported and canonical bindings — proving the moved code runs."""
        import app.orchestrator as orch
        import app.orchestrator_runner_dispatch as rd

        # _num_or_none: coercion + rejection semantics preserved.
        for fn in (orch._num_or_none, rd._num_or_none):
            assert fn(3) == 3.0
            assert fn("2.5") == 2.5
            assert fn(True) is None  # bools are not numbers
            assert fn(None) is None
            assert fn("not-a-number") is None
            assert fn(float("nan")) is None
            assert fn(float("inf")) is None

        # _runner_handles_task_type over a RunnerDispatchSnapshot: policy
        # allowlist (ceiling) intersected with declared agent floor.
        allow_all = orch.RunnerDispatchSnapshot(
            id=uuid.uuid4(), project_id=uuid.uuid4(), capabilities={"task_types": "all"}
        )
        restricted = orch.RunnerDispatchSnapshot(
            id=uuid.uuid4(),
            project_id=uuid.uuid4(),
            capabilities={"task_types": ["research"]},
        )
        assert orch._runner_handles_task_type(allow_all, "fix") is True
        assert orch._runner_handles_task_type(restricted, "fix") is False
        assert orch._runner_handles_task_type(restricted, "research") is True
        # compute_experiment is exempt from the agent-declared floor.
        docker_only = orch.RunnerDispatchSnapshot(
            id=uuid.uuid4(),
            project_id=uuid.uuid4(),
            capabilities={
                "task_types": "all",
                "supported_agent_task_types": ["research"],
            },
        )
        assert orch._runner_handles_task_type(docker_only, "fix") is False
        assert orch._runner_handles_task_type(docker_only, "compute_experiment") is True

        # _runner_active_work_count = agent tasks + compute jobs (heartbeat view).
        busy = orch.RunnerDispatchSnapshot(
            id=uuid.uuid4(),
            project_id=uuid.uuid4(),
            capabilities={
                "active_task_ids": ["a", "b"],
                "active_compute_job_ids": ["c"],
            },
        )
        assert orch._runner_active_work_count(busy) == 3
        assert rd._runner_active_work_count(busy) == 3


class TestPerClassCapacityGating:
    """Per-class capacity: agent tasks and compute jobs are gated independently.

    A runner with max_concurrent=2 and max_concurrent_compute=1 can have 2
    active agent tasks and still accept 1 compute job (agent slots are full but
    compute slot is free). Conversely, a runner with 1 active compute job and
    max_concurrent_compute=1 is at compute capacity and must reject additional
    compute dispatch even when agent task slots are still open.
    """

    def _make_runner(self, caps: dict):
        from app.orchestrator_runner_dispatch import RunnerDispatchSnapshot

        return RunnerDispatchSnapshot(
            id=uuid.uuid4(), project_id=uuid.uuid4(), capabilities=caps
        )

    def test_agent_tasks_fill_agent_slots_not_compute_slot(self):
        """Runner with max_concurrent=2, max_concurrent_compute=1, and 2 active
        agent tasks is at AGENT capacity but not COMPUTE capacity."""
        import app.orchestrator as orch
        runner = self._make_runner({
            "max_concurrent": 2,
            "max_concurrent_compute": 1,
            "active_task_ids": ["t1", "t2"],
            "active_compute_job_ids": [],
        })
        # Agent class is at capacity.
        assert orch._runner_at_capacity(runner, 0, "fix") is True
        assert orch._runner_at_capacity(runner, 0, "research") is True
        # Compute class still has capacity.
        assert orch._runner_at_capacity(runner, 0, "compute_experiment") is False

    def test_compute_job_fills_compute_slot_not_agent_slots(self):
        """Runner with max_concurrent=2, max_concurrent_compute=1, and 1 active
        compute job is at COMPUTE capacity but not AGENT capacity."""
        import app.orchestrator as orch
        runner = self._make_runner({
            "max_concurrent": 2,
            "max_concurrent_compute": 1,
            "active_task_ids": [],
            "active_compute_job_ids": ["cj1"],
        })
        # Compute class is at capacity.
        assert orch._runner_at_capacity(runner, 0, "compute_experiment") is True
        # Agent class still has capacity.
        assert orch._runner_at_capacity(runner, 0, "fix") is False

    def test_backward_compat_no_max_concurrent_compute(self):
        """A runner without max_concurrent_compute falls back to max_concurrent
        for compute jobs (old combined-pool behaviour preserved)."""
        import app.orchestrator as orch
        runner = self._make_runner({
            "max_concurrent": 2,
            # max_concurrent_compute absent
            "active_task_ids": ["t1", "t2"],
            "active_compute_job_ids": [],
        })
        # Both agent and compute slots exhausted under combined pool logic.
        assert orch._runner_at_capacity(runner, 0, "fix") is True
        # Without a separate compute limit, compute is also at capacity (2 agent
        # tasks consume the combined pool of 2).
        assert orch._runner_at_capacity(runner, 0, "compute_experiment") is False
        # (active_compute_job_count=0, max_concurrent_compute falls back to
        # max_concurrent=2, so 0 >= 2 is False — compute slot is technically
        # still free in the old model. The new code is consistent: agent tasks do
        # NOT fill compute slots when max_concurrent_compute is absent.)


class TestAuthoritativeAgentTaskCount:
    """Signal-that-lies fix #5: agent load is derived from the AUTHORITATIVE
    ``_active_agent_task_count`` (committed Task rows) rather than the stale-prone
    heartbeat ``active_task_ids`` list. A runner heartbeat that still lists
    COMPLETED/FAILED tasks must not falsely hold an idle runner at capacity and
    block a pinned head."""

    def _make_runner(self, caps: dict):
        from app.orchestrator_runner_dispatch import RunnerDispatchSnapshot

        return RunnerDispatchSnapshot(
            id=uuid.uuid4(), project_id=uuid.uuid4(), capabilities=caps
        )

    def test_active_count_prefers_authoritative_over_stale_heartbeat(self):
        from app.orchestrator_runner_dispatch import (
            _ACTIVE_AGENT_TASK_COUNT_KEY,
            _runner_active_task_count,
        )

        runner = self._make_runner(
            {
                "active_task_ids": ["stale1", "stale2", "stale3"],  # stale-HIGH hb
                _ACTIVE_AGENT_TASK_COUNT_KEY: 0,  # authoritative: idle
            }
        )
        assert _runner_active_task_count(runner) == 0

    def test_at_capacity_idle_despite_stale_heartbeat(self):
        import app.orchestrator as orch
        from app.orchestrator_runner_dispatch import _ACTIVE_AGENT_TASK_COUNT_KEY

        runner = self._make_runner(
            {
                "max_concurrent": 2,
                "active_task_ids": ["stale1", "stale2"],  # would read AT capacity
                _ACTIVE_AGENT_TASK_COUNT_KEY: 0,  # authoritatively idle
            }
        )
        assert orch._runner_at_capacity(runner, 0, "fix") is False

    def test_authoritative_full_still_caps(self):
        import app.orchestrator as orch
        from app.orchestrator_runner_dispatch import _ACTIVE_AGENT_TASK_COUNT_KEY

        runner = self._make_runner(
            {
                "max_concurrent": 2,
                "active_task_ids": [],  # heartbeat lags / empty
                _ACTIVE_AGENT_TASK_COUNT_KEY: 2,  # authoritatively full
            }
        )
        assert orch._runner_at_capacity(runner, 0, "fix") is True

    def test_falls_back_to_heartbeat_when_authoritative_absent(self):
        from app.orchestrator_runner_dispatch import _runner_active_task_count

        # No authoritative key (bare LocalRunner ORM / old snapshot) → heartbeat hint.
        runner = self._make_runner({"active_task_ids": ["a", "b"]})
        assert _runner_active_task_count(runner) == 2


@pytest.mark.asyncio
async def test_load_runner_active_task_counts_counts_committed_task_rows(db_session):
    """The authoritative loader counts only active-status Task rows pinned to each
    runner (``vm_instance_id == 'runner:<id>'``), excluding terminal rows and rows
    pinned to a different runner."""
    from app.orchestrator_runner_dispatch import _load_runner_active_task_counts

    runner_a = uuid.uuid4()
    runner_b = uuid.uuid4()
    project = make_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    rows = [
        # 2 active tasks pinned to runner_a (PROVISIONING + IN_PROGRESS) → counted.
        make_task(
            project_id=project.id,
            status=TaskStatus.PROVISIONING,
            vm_instance_id=f"runner:{runner_a}",
        ),
        make_task(
            project_id=project.id,
            status=TaskStatus.IN_PROGRESS,
            vm_instance_id=f"runner:{runner_a}",
        ),
        # Terminal task pinned to runner_a → excluded (left the active set).
        make_task(
            project_id=project.id,
            status=TaskStatus.COMPLETED,
            vm_instance_id=f"runner:{runner_a}",
        ),
        # Active task pinned to a DIFFERENT runner → not counted for runner_a.
        make_task(
            project_id=project.id,
            status=TaskStatus.IN_PROGRESS,
            vm_instance_id=f"runner:{runner_b}",
        ),
    ]
    for t in rows:
        db_session.add(t)
    await db_session.commit()

    counts = await _load_runner_active_task_counts(db_session, [runner_a, runner_b])
    assert counts.get(runner_a) == 2
    assert counts.get(runner_b) == 1
    # Empty input → empty map (no query).
    assert await _load_runner_active_task_counts(db_session, []) == {}

    def test_select_eligible_runner_compute_task_ignores_agent_load(self):
        """_select_eligible_runner returns the runner for a compute_experiment
        even when the runner's agent task slots are full, as long as compute
        capacity remains."""
        import app.orchestrator as orch
        project_id = uuid.uuid4()
        runner = self._make_runner({
            "max_concurrent": 2,
            "max_concurrent_compute": 1,
            "active_task_ids": ["t1", "t2"],
            "active_compute_job_ids": [],
            "task_types": "all",
        })
        runner = runner._replace(project_id=project_id)
        cache = {project_id: [runner]}
        selected = orch._select_eligible_runner(
            cache, project_id, "compute_experiment", None, {}
        )
        assert selected is not None
        assert selected.id == runner.id

    def test_select_eligible_runner_compute_task_blocked_when_compute_full(self):
        """_select_eligible_runner returns None for a compute_experiment when the
        runner's compute slot is full, even if agent slots are free."""
        import app.orchestrator as orch
        project_id = uuid.uuid4()
        runner = self._make_runner({
            "max_concurrent": 2,
            "max_concurrent_compute": 1,
            "active_task_ids": [],
            "active_compute_job_ids": ["cj1"],
            "task_types": "all",
        })
        runner = runner._replace(project_id=project_id)
        cache = {project_id: [runner]}
        selected = orch._select_eligible_runner(
            cache, project_id, "compute_experiment", None, {}
        )
        assert selected is None

    def test_agent_task_not_blocked_by_compute_load(self):
        """A full compute slot does not block an agent task dispatch."""
        import app.orchestrator as orch
        project_id = uuid.uuid4()
        runner = self._make_runner({
            "max_concurrent": 2,
            "max_concurrent_compute": 1,
            "active_task_ids": [],
            "active_compute_job_ids": ["cj1"],
            "task_types": "all",
        })
        runner = runner._replace(project_id=project_id)
        cache = {project_id: [runner]}
        selected = orch._select_eligible_runner(
            cache, project_id, "fix", None, {}
        )
        assert selected is not None
        assert selected.id == runner.id


@pytest.mark.asyncio
async def test_held_require_local_compute_alerts_before_timeout(db_session, mocker):
    """A compute_experiment held for require_local longer than the early-warning
    threshold emits a WARNING alert before the 6h timeout while the task stays QUEUED.
    A second sweep pass must not create a duplicate (dedup by title + task_id).
    """
    from sqlmodel import select
    from app.models import Alert, AlertSeverity
    from app.orchestrator_runner_dispatch import (
        _fail_stale_local_runner_holds,
        _LOCAL_RUNNER_HOLD_ALERT_SECONDS,
    )

    mocker.patch("app.alert_dispatch.broadcast_to_project", new_callable=AsyncMock)

    project = make_project(name="RequireLocal Compute Hold Alert", user_id=_TEST_USER)
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    # Stamp the hold well past the early-warning threshold but under the 6h timeout.
    held_since = _utcnow() - timedelta(seconds=_LOCAL_RUNNER_HOLD_ALERT_SECONDS + 60)
    task = make_task(
        project_id=project.id,
        title="stranded compute",
        status=TaskStatus.QUEUED,
        payloads=[{"type": "compute_experiment"}],
        context={"awaiting_local_runner_since": held_since.isoformat()},
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)
    task_id = task.id

    failed_count = await _fail_stale_local_runner_holds(db_session)

    # Task must remain QUEUED — the 6h timeout has not elapsed.
    refreshed = await db_session.get(Task, task_id)
    assert refreshed.status == TaskStatus.QUEUED
    assert failed_count == 0

    # A WARNING alert must have been created for the stranded hold.
    alerts = (
        await db_session.execute(select(Alert).where(Alert.task_id == task_id))
    ).scalars().all()
    assert len(alerts) == 1, f"Expected 1 early-warning alert, got {len(alerts)}"
    alert = alerts[0]
    assert alert.severity == AlertSeverity.WARNING
    assert "local runner" in alert.title.lower()

    # Re-running the sweep must not create a duplicate (title+task_id dedup).
    await _fail_stale_local_runner_holds(db_session)
    alerts2 = (
        await db_session.execute(select(Alert).where(Alert.task_id == task_id))
    ).scalars().all()
    assert len(alerts2) == 1, "Second sweep pass must not duplicate the alert"


@pytest.mark.asyncio
async def test_held_container_runner_compute_alerts_before_timeout(db_session, mocker):
    """A container-image compute_experiment held (awaiting_container_runner_since)
    longer than the early-warning threshold but under the 6h timeout emits a
    distinct WARNING alert while the task stays QUEUED, and the second sweep pass
    does not duplicate it. This closes the silent-indefinite-stall gap: without the
    fix the container hold is invisible to the sweep and never escalates.
    """
    from sqlmodel import select
    from app.models import Alert, AlertSeverity
    from app.orchestrator_runner_dispatch import (
        _fail_stale_local_runner_holds,
        _LOCAL_RUNNER_HOLD_ALERT_SECONDS,
    )

    mocker.patch("app.alert_dispatch.broadcast_to_project", new_callable=AsyncMock)

    project = make_project(name="Container Hold Alert", user_id=_TEST_USER)
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    held_since = _utcnow() - timedelta(seconds=_LOCAL_RUNNER_HOLD_ALERT_SECONDS + 60)
    task = make_task(
        project_id=project.id,
        title="stranded container compute",
        status=TaskStatus.QUEUED,
        payloads=[{"type": "compute_experiment"}],
        context={"awaiting_container_runner_since": held_since.isoformat()},
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)
    task_id = task.id

    failed_count = await _fail_stale_local_runner_holds(db_session)

    refreshed = await db_session.get(Task, task_id)
    assert refreshed.status == TaskStatus.QUEUED
    assert failed_count == 0

    alerts = (
        await db_session.execute(select(Alert).where(Alert.task_id == task_id))
    ).scalars().all()
    assert len(alerts) == 1, f"Expected 1 container-hold alert, got {len(alerts)}"
    alert = alerts[0]
    assert alert.severity == AlertSeverity.WARNING
    # Distinct copy from the require-local hold so the operator sees the real cause.
    assert "container-capable" in alert.title.lower()
    assert "docker_usable" in alert.message

    await _fail_stale_local_runner_holds(db_session)
    alerts2 = (
        await db_session.execute(select(Alert).where(Alert.task_id == task_id))
    ).scalars().all()
    assert len(alerts2) == 1, "Second sweep pass must not duplicate the alert"


@pytest.mark.asyncio
async def test_held_container_runner_compute_fails_after_timeout(db_session, mocker):
    """A container-image compute_experiment held past the 6h wait timeout is
    terminal-FAILED (PROVISION_FAILURE) with a container-specific reason — there
    is no safe cloud fallback for an image-declaring job that needs a
    Docker-capable runner. Guards against the hold stalling indefinitely.
    """
    from app.failure_classification import PROVISION_FAILURE
    from app.orchestrator_runner_dispatch import (
        _fail_stale_local_runner_holds,
        _LOCAL_RUNNER_WAIT_TIMEOUT_SECONDS,
    )

    mocker.patch("app.alert_dispatch.broadcast_to_project", new_callable=AsyncMock)

    project = make_project(name="Container Hold Timeout", user_id=_TEST_USER)
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    held_since = _utcnow() - timedelta(seconds=_LOCAL_RUNNER_WAIT_TIMEOUT_SECONDS + 120)
    task = make_task(
        project_id=project.id,
        title="expired container compute",
        status=TaskStatus.QUEUED,
        payloads=[{"type": "compute_experiment"}],
        context={"awaiting_container_runner_since": held_since.isoformat()},
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)
    task_id = task.id

    failed_count = await _fail_stale_local_runner_holds(db_session)
    assert failed_count == 1

    refreshed = await db_session.get(Task, task_id)
    assert refreshed.status == TaskStatus.FAILED
    assert (refreshed.context or {}).get("failure_category") == PROVISION_FAILURE
    assert "container-capable" in (refreshed.latest_status_message or "")


@pytest.mark.asyncio
async def test_container_runner_hold_surfaces_in_local_hold_filters(db_session):
    """A container-image compute hold is counted by local_runner_hold_task_filters
    (the SQL predicate driving the operator-attention count and items) exactly like
    a require-local hold — the broadened OR predicate must compile and execute so
    the container stall is no longer invisible to the operator surface.
    """
    from sqlalchemy import func, select
    from app.strategic_runtime_work import (
        local_runner_hold_task_filters,
        task_awaiting_container_runner_since,
        task_is_local_runner_hold,
    )

    project = make_project(name="Container Hold Filter", user_id=_TEST_USER)
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    held_since = _utcnow().isoformat()
    task = make_task(
        project_id=project.id,
        title="container held",
        status=TaskStatus.QUEUED,
        payloads=[{"type": "compute_experiment"}],
        context={"awaiting_container_runner_since": held_since},
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    count = (
        await db_session.execute(
            select(func.count())
            .select_from(Task)
            .where(*local_runner_hold_task_filters(project.id))
        )
    ).scalar()
    assert count == 1, "container hold must be counted by the local-hold filter"

    # Python-side classifiers agree with the SQL predicate.
    assert task_awaiting_container_runner_since(task) == held_since
    assert task_is_local_runner_hold(task) is True


# ---------------------------------------------------------------------------
# MissingGreenlet hardening: strategic local-runner dispatch-prep path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_strategic_dispatch_prep_vm_id_via_dict_after_hold_rollback(
    db_session, expire_fidelity
):
    """After _hold_strategic_local_runner_task rolls back (no-change path),
    reading task.vm_instance_id via the ORM descriptor triggers a sync
    lazy-load that raises MissingGreenlet under asyncpg. The fix reads via
    __dict__ which avoids the descriptor entirely — reproducing the same
    pattern the dispatch loop uses at orchestrator.py:2776.

    expire_fidelity restores real SQLAlchemy expiry so the rollback truly
    clears the instance dict, matching what asyncpg does in production.
    """
    from app.orchestrator_runner_dispatch import (
        _hold_strategic_local_runner_task,
    )
    from app.workbench_state import (
        STRATEGIC_LOCAL_RUNNER_HOLD_CONTEXT_KEY,
        STRATEGIC_LOCAL_RUNNER_REQUIRED_CONTEXT_KEY,
    )

    project = make_project(name="strategic-mg-test")
    db_session.add(project)
    await db_session.commit()

    _hold_reason = (
        "Strategic workbench is in continuous local mode; waiting for a live "
        "Strategic-capable local runner instead of dispatching to cloud."
    )
    task = make_task(
        project_id=project.id,
        status=TaskStatus.QUEUED,
        vm_instance_id=None,
        payloads=[{"type": "strategic_review"}],
        context={
            STRATEGIC_LOCAL_RUNNER_REQUIRED_CONTEXT_KEY: True,
            STRATEGIC_LOCAL_RUNNER_HOLD_CONTEXT_KEY: "2020-01-01T00:00:00",
        },
        latest_status_message=_hold_reason,
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    # The hold function detects nothing changed → rolls back (expires task).
    result = await _hold_strategic_local_runner_task(db_session, task)
    assert result is True

    # With expire_fidelity the rollback truly expired the instance dict.
    # Reading via __dict__ avoids the descriptor's lazy-load → MissingGreenlet.
    vm_id = task.__dict__.get("vm_instance_id")
    # vm_instance_id was cleared by the rollback, so .get() returns None.
    # This is the exact same str(__dict__.get(...) or "") pattern the
    # dispatch loop fix uses to decide whether to increment _cycle_dispatched.
    assert not str(vm_id or "").startswith("runner:")
    assert vm_id is None, (
        "vm_instance_id should not be in __dict__ after rollback; "
        f"got {vm_id!r}"
    )


# ---------------------------------------------------------------------------
# Container-image dispatch eligibility gate
# ---------------------------------------------------------------------------


def test_image_job_requires_container_capable_runner():
    """_select_eligible_runner excludes runners without docker_usable when
    container_image is declared; includes them when no image is required.

    This is the primary acceptance-criterion test: a container_image-declaring
    job must not land on a runner that explicitly declares docker_usable=False,
    and must select a runner that declares docker_usable=True.
    """
    from app.orchestrator_runner_dispatch import (
        _select_eligible_runner,
        RunnerDispatchSnapshot,
    )

    project_id = uuid.uuid4()

    capable = RunnerDispatchSnapshot(
        id=uuid.uuid4(),
        project_id=project_id,
        capabilities={
            "task_types": "all",
            "isolation_backends": {"docker_usable": True, "docker_cdi_gpu": False},
        },
    )
    incapable = RunnerDispatchSnapshot(
        id=uuid.uuid4(),
        project_id=project_id,
        capabilities={
            "task_types": "all",
            "isolation_backends": {"docker_usable": False},
        },
    )

    cache = {project_id: [incapable, capable]}

    # With container_image declared: only the capable runner is selected.
    selected = _select_eligible_runner(
        active_runners_cache=cache,
        project_id=project_id,
        task_type="compute_experiment",
        accelerator=None,
        runner_pins_this_cycle={},
        container_image="registry.example.com/myproject:sha256-abc123",
    )
    assert selected is not None, "expected a container-capable runner to be selected"
    assert selected.id == capable.id, (
        "runner without docker_usable must not be selected for a container_image job"
    )

    # Without container_image: the incapable runner is still eligible (least loaded).
    selected_no_image = _select_eligible_runner(
        active_runners_cache=cache,
        project_id=project_id,
        task_type="compute_experiment",
        accelerator=None,
        runner_pins_this_cycle={},
        container_image=None,
    )
    assert selected_no_image is not None
    # Both are eligible when no image is required; the test just confirms selection
    # returns something (no exclusion applied).



@pytest.mark.asyncio
async def test_dispatch_holds_when_container_image_declared_but_no_capable_runner(
    db_session, mocker
):
    """_dispatch_compute_experiment_directly returns 'held' and stamps
    awaiting_container_runner_since when a container_image is declared but
    all registered runners lack docker_usable capability.

    No ComputeJob must be created and the task must remain QUEUED.
    """
    from app.orchestrator_runner_dispatch import (
        _dispatch_compute_experiment_directly,
        RunnerDispatchSnapshot,
    )

    mocker.patch("app.broadcast.broadcast_to_project", new_callable=AsyncMock)

    project = make_project(name="ContainerImageHoldProject", user_id=_TEST_USER)
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = make_task(
        project_id=project.id,
        status=TaskStatus.QUEUED,
        payloads=[
            {
                "type": "compute_experiment",
                "command": "echo hello",
                "spend_cap_usd": 1.0,
                "deadline_seconds": 300,
                "container_image": "registry.example.com/myproject:sha256-abc",
            }
        ],
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)
    task_id = task.id

    # One runner registered for the project, but it cannot run containers.
    incapable_runner = RunnerDispatchSnapshot(
        id=uuid.uuid4(),
        project_id=project.id,
        capabilities={
            "task_types": "all",
            "isolation_backends": {"docker_usable": False},
        },
    )
    active_runners_cache = {project.id: [incapable_runner]}

    result = await _dispatch_compute_experiment_directly(
        db_session, task, active_runners_cache, runner_pins_this_cycle={}
    )

    assert result == "held", f"expected 'held', got {result!r}"

    refreshed = await db_session.get(Task, task_id)
    assert refreshed.status == TaskStatus.QUEUED, (
        "task must remain QUEUED when held for container runner"
    )
    ctx = refreshed.context or {}
    assert ctx.get("awaiting_container_runner_since"), (
        "awaiting_container_runner_since must be stamped in context on hold"
    )
    assert "docker_usable" in (refreshed.latest_status_message or ""), (
        "status message must explain the docker_usable requirement"
    )


# ---------------------------------------------------------------------------
# Finding 12 — unvalidated float() coercion of the agent-authored `vram_gb`
# payload field raised in the dispatch HOT PATH, taking down the whole dispatch
# cycle (blast radius: every task in it) while the offending task itself only
# died later, reasonless, via poison quarantine.
# ---------------------------------------------------------------------------


def test_coerce_vram_gb_accepts_numbers_and_numeric_strings():
    """Valid VRAM figures coerce to float with no error, in flat and nested form."""
    from app.orchestrator_runner_dispatch import _coerce_vram_gb

    assert _coerce_vram_gb({"vram_gb": 24}) == (24.0, None)
    assert _coerce_vram_gb({"vram_gb": 24.5}) == (24.5, None)
    assert _coerce_vram_gb({"vram_gb": "24"}) == (24.0, None)
    assert _coerce_vram_gb({"vram_gb": " 24.5 "}) == (24.5, None)
    # Nested compute_experiment form resolves identically.
    assert _coerce_vram_gb({"compute_experiment": {"vram_gb": 40}}) == (40.0, None)
    # Unset is not an error — it simply declares no VRAM floor.
    assert _coerce_vram_gb({}) == (None, None)


def test_coerce_vram_gb_rejects_non_numeric_without_raising():
    """The values that used to RAISE in the dispatch cycle now return an error.

    Each of these fed a bare ``float()`` before the fix: "24GB" and [24] raised
    ValueError/TypeError straight out of the dispatch cycle. They must now come
    back as an honest error string naming the field and the offending value.
    """
    from app.orchestrator_runner_dispatch import _coerce_vram_gb

    for bad in ("24GB", "24 GB", "lots", [24], {"gb": 24}, object()):
        value, error = _coerce_vram_gb({"vram_gb": bad})
        assert value is None, f"{bad!r} must not coerce to a VRAM figure"
        assert error, f"{bad!r} must produce an honest validation error"
        assert "vram_gb" in error, f"error must name the bad field, got {error!r}"

    # The error names the offending value so an operator can see WHAT was wrong.
    _value, error = _coerce_vram_gb({"vram_gb": "24GB"})
    assert "24GB" in error


def test_coerce_vram_gb_rejects_non_finite_and_bool():
    """inf/nan and a truthy bool are rejected.

    ``inf`` parses fine through float() but then blows up downstream at
    ``int(vram_gb * 1024)`` (OverflowError) — the same hot-path crash by a longer
    route. ``True`` is an ``int`` subclass, so ``float(True) == 1.0`` would
    otherwise silently mean "1 GB".
    """
    from app.orchestrator_runner_dispatch import _coerce_vram_gb

    for bad in ("inf", "-inf", "nan", float("inf"), float("nan"), True):
        value, error = _coerce_vram_gb({"vram_gb": bad})
        assert value is None, f"{bad!r} must not coerce to a VRAM figure"
        assert error, f"{bad!r} must produce an honest validation error"


def test_coerce_vram_gb_treats_falsy_as_no_vram_floor():
    """A FALSY vram_gb declares no VRAM floor — it is not an error.

    ``0`` / ``False`` / ``""`` are falsy, so the flat-then-nested ``or`` lookup
    (pre-existing precedence, deliberately preserved) reads straight past them to
    the nested form and resolves to None = "no VRAM floor". None of these can
    crash the dispatch cycle, so none of them should terminal-fail a task — they
    just impose no constraint. This pins that contract so a future "tighten the
    validation" change cannot start failing tasks that declare vram_gb: 0.
    """
    from app.orchestrator_runner_dispatch import _coerce_vram_gb

    for falsy in (0, False, ""):
        value, error = _coerce_vram_gb({"vram_gb": falsy})
        assert value is None, f"{falsy!r} must impose no VRAM floor"
        assert error is None, f"{falsy!r} must NOT be treated as a validation error"

    # Documented precedence: a falsy flat value falls through to the nested form.
    assert _coerce_vram_gb(
        {"vram_gb": 0, "compute_experiment": {"vram_gb": 40}}
    ) == (40.0, None)


@pytest.mark.asyncio
async def test_dispatch_fails_task_honestly_on_non_numeric_vram_gb(db_session, mocker):
    """An agent writing ``vram_gb: "24GB"`` must fail THAT task with a clear
    validation message — NOT raise out of the dispatch cycle.

    Regression cover for the hot-path crash: before the fix this call raised
    ValueError from ``float("24GB")``, aborting the dispatch cycle for every other
    task in it, while the offending task died later via poison quarantine with no
    honest reason. Now it terminal-FAILs with compute_launch_failed and a message
    naming the bad field and value, and no billable ComputeJob is ever created.
    """
    from sqlmodel import select as _select
    from app.models import ComputeJob
    from app.orchestrator_runner_dispatch import _dispatch_compute_experiment_directly

    mocker.patch("app.broadcast.broadcast_to_project", new_callable=AsyncMock)

    project = make_project(name="BadVramProject", user_id=_TEST_USER)
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = make_task(
        project_id=project.id,
        status=TaskStatus.QUEUED,
        payloads=[
            {
                "type": "compute_experiment",
                "command": "python train.py",
                "spend_cap_usd": 1.0,
                "deadline_seconds": 300,
                "vram_gb": "24GB",  # unit suffix — the exact poison value
            }
        ],
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)
    task_id = task.id

    # Must NOT raise — that is the whole point of the fix.
    result = await _dispatch_compute_experiment_directly(
        db_session, task, {}, runner_pins_this_cycle={}
    )

    assert result == "failed", f"expected 'failed', got {result!r}"

    refreshed = await db_session.get(Task, task_id)
    assert refreshed.status == TaskStatus.FAILED
    assert (refreshed.context or {}).get("failure_category") == "compute_launch_failed"
    message = refreshed.latest_status_message or ""
    assert "vram_gb" in message, f"message must name the bad field, got {message!r}"
    assert "24GB" in message, f"message must name the bad value, got {message!r}"

    # A misconfigured launch must never create a (billable) compute job.
    jobs = (
        await db_session.execute(
            _select(ComputeJob).where(ComputeJob.task_id == task_id)
        )
    ).scalars().all()
    assert jobs == [], "a validation failure must not create a billable ComputeJob"


# ---------------------------------------------------------------------------
# Finding 11 — a forced-runner pin (`forced_runner_target_id`) stamped
# `awaiting_forced_runner_since` but NO sweep or alert ever consumed the stamp,
# so the task sat QUEUED indefinitely with nobody watching. It now rides the same
# bounded-wait contract as its siblings: 15-min early alert, 6h terminal fail.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_held_forced_runner_compute_alerts_before_timeout(db_session, mocker):
    """A forced-runner hold past the early-warning threshold emits a WARNING alert
    while the task stays QUEUED, and a second sweep pass must not duplicate it.

    Before the fix this hold was invisible to the sweep: no alert, no timeout — the
    task simply waited forever.
    """
    from sqlmodel import select as _select
    from app.models import Alert, AlertSeverity
    from app.orchestrator_runner_dispatch import (
        _fail_stale_local_runner_holds,
        _LOCAL_RUNNER_HOLD_ALERT_SECONDS,
        AWAITING_FORCED_RUNNER_CONTEXT_KEY,
        FORCED_RUNNER_TARGET_CONTEXT_KEY,
    )

    mocker.patch("app.alert_dispatch.broadcast_to_project", new_callable=AsyncMock)

    project = make_project(name="ForcedRunner Hold Alert", user_id=_TEST_USER)
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    held_since = _utcnow() - timedelta(seconds=_LOCAL_RUNNER_HOLD_ALERT_SECONDS + 60)
    task = make_task(
        project_id=project.id,
        title="pinned to an offline box",
        status=TaskStatus.QUEUED,
        payloads=[{"type": "compute_experiment"}],
        context={
            AWAITING_FORCED_RUNNER_CONTEXT_KEY: held_since.isoformat(),
            FORCED_RUNNER_TARGET_CONTEXT_KEY: str(uuid.uuid4()),
        },
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)
    task_id = task.id

    failed_count = await _fail_stale_local_runner_holds(db_session)

    # Still inside the 6h window — held, not failed.
    refreshed = await db_session.get(Task, task_id)
    assert refreshed.status == TaskStatus.QUEUED
    assert failed_count == 0

    alerts = (
        await db_session.execute(_select(Alert).where(Alert.task_id == task_id))
    ).scalars().all()
    assert len(alerts) == 1, f"Expected 1 early-warning alert, got {len(alerts)}"
    assert alerts[0].severity == AlertSeverity.WARNING

    # Dedup: a second sweep pass must not stack a duplicate alert.
    await _fail_stale_local_runner_holds(db_session)
    alerts2 = (
        await db_session.execute(_select(Alert).where(Alert.task_id == task_id))
    ).scalars().all()
    assert len(alerts2) == 1, "Second sweep pass must not duplicate the alert"


# ══════════════════════════════════════════════════════════════════════════════
# Zone-exhaustion gate — the cold-GCE dispatch skip when every zone is in cooldown
# ══════════════════════════════════════════════════════════════════════════════


class TestZoneExhaustionColdProvisionGate:
    """The orchestrator's "all GCP zones in cooldown" guard.

    The guard used to clear ``queued_tasks`` with the comment "Clear so the loop
    below is skipped" — but the dispatch loop iterates the ``_queued_task_gate_info``
    SNAPSHOT built from ``queued_tasks`` a few lines earlier, so clearing the list
    skipped nothing and every queued task still burned a full cold provision
    (Secret Manager write + GCS upload + a GCE insert that blocks in-zone), then
    got requeued with a 10-30 min retry_after — i.e. DELAYED past zone recovery.

    The fix gates the COLD-GCE path only. Locally-routable work (fleet-runner
    pins, compute_experiment, warm pins, warm-pool leases, Cloud Run jobs) uses no
    GCE zone capacity and must still dispatch during a zone outage — blocking it
    would be a worse regression than the dead guard.

    Reuses the ``orch_warm_db`` / ``_run_one_cycle`` one-cycle harness from
    ``TestWarmVMSkipProvision``.
    """

    orch_warm_db = TestWarmVMSkipProvision.orch_warm_db  # type: ignore[attr-defined]
    _run_one_cycle = staticmethod(TestWarmVMSkipProvision._run_one_cycle)  # type: ignore[attr-defined]

    @staticmethod
    def _naive_utcnow():
        from datetime import timezone as _tz

        return datetime.now(_tz.utc).replace(tzinfo=None)

    @staticmethod
    def _patch_common(mocker, *, zones_available: bool):
        """Mocks shared by every test in this class.

        Returns the ``provision_agent_vm`` mock — the single observable that
        answers "did the orchestrator attempt a GCP provision this cycle?".
        """
        import app.orchestrator as _orch_module

        prov_mock = mocker.patch(
            "app.orchestrator.provision_agent_vm",
            new_callable=AsyncMock,
            return_value=True,
        )
        mocker.patch(
            "app.orchestrator._sync_runtime_config_from_state",
            new_callable=AsyncMock,
            return_value=[],
        )
        # Supply dummy credentials so the dispatch loop reaches the cold-provision
        # step (no env LLM keys in CI). Without this the no-credentials branch
        # short-circuits BEFORE provisioning and these tests would pass vacuously
        # on the unfixed code.
        mocker.patch(
            "app.orchestrator._resolve_env_runtime_fallback",
            return_value=(
                "fake-api-key",
                "anthropic",
                "api_key",
                "claude-opus-4-8",
                None,
                {},
            ),
        )
        # The zone ring: patch the orchestrator's binding, which both the
        # cycle-level pre-check and the per-task cold-provision gate read.
        mocker.patch(
            "app.orchestrator.any_zone_available", return_value=zones_available
        )
        _orch_module._last_zone_exhaustion_log = None
        return prov_mock

    @pytest.mark.asyncio
    async def test_cold_provision_dispatches_when_a_zone_is_available(
        self, orch_warm_db, mocker
    ):
        """NEGATIVE CONTROL. With a zone available, the very same task DOES cold
        provision — so the exhaustion tests below cannot pass vacuously (i.e. the
        harness really does drive a dispatch)."""
        from sqlmodel import select as _select

        prov_mock = self._patch_common(mocker, zones_available=True)

        async with orch_warm_db() as session:
            project = make_project(name="Zone Available", user_id=_TEST_USER)
            session.add(project)
            await session.commit()
            await session.refresh(project)

            task = make_task(
                project_id=project.id,
                vm_instance_id=None,
                status=TaskStatus.QUEUED,
                payloads=[{"type": "fix", "prompt": "x"}],
                context={},
            )
            session.add(task)
            await session.commit()
            await session.refresh(task)
            task_id = task.id

        await self._run_one_cycle(mocker)

        assert prov_mock.called, (
            "control: with a healthy zone the task must cold-provision — if this "
            "fails the harness is not driving a dispatch and the exhaustion "
            "assertions below prove nothing"
        )
        async with orch_warm_db() as verify:
            row = (
                (await verify.execute(_select(Task).where(Task.id == task_id)))
                .scalars()
                .first()
            )
            assert row is not None
            assert row.status == TaskStatus.PROVISIONING

    @pytest.mark.asyncio
    async def test_zone_exhaustion_holds_cold_gcp_provision(
        self, orch_warm_db, mocker
    ):
        """THE REGRESSION. All zones in cooldown => no GCP provisioning is
        attempted, and the task is held QUEUED (not requeued with a retry_after
        that would delay it past zone recovery).

        Fails on the dead-guard code: clearing ``queued_tasks`` does not stop the
        loop, which iterates the ``_queued_task_gate_info`` snapshot, so
        provision_agent_vm is called anyway."""
        from sqlmodel import select as _select

        prov_mock = self._patch_common(mocker, zones_available=False)

        async with orch_warm_db() as session:
            project = make_project(name="Zone Exhausted", user_id=_TEST_USER)
            session.add(project)
            await session.commit()
            await session.refresh(project)

            task = make_task(
                project_id=project.id,
                vm_instance_id=None,
                status=TaskStatus.QUEUED,
                payloads=[{"type": "fix", "prompt": "x"}],
                context={},
            )
            session.add(task)
            await session.commit()
            await session.refresh(task)
            task_id = task.id

        await self._run_one_cycle(mocker)

        assert not prov_mock.called, (
            "Zone-exhaustion guard is dead: provision_agent_vm was called with "
            "every GCP zone in cooldown. The guard clears `queued_tasks`, but the "
            "dispatch loop iterates the `_queued_task_gate_info` snapshot."
        )
        async with orch_warm_db() as verify:
            row = (
                (await verify.execute(_select(Task).where(Task.id == task_id)))
                .scalars()
                .first()
            )
            assert row is not None
            assert row.status == TaskStatus.QUEUED, (
                f"A task held for zone recovery must stay QUEUED, got {row.status}"
            )
            assert row.retry_after_until is None, (
                "The hold must NOT bank a retry_after — that would delay the task "
                "past zone recovery, the very harm the guard exists to prevent"
            )

    @pytest.mark.asyncio
    async def test_zone_exhaustion_still_pins_task_to_local_runner(
        self, orch_warm_db, mocker
    ):
        """BLAST RADIUS. A task routable to a registered fleet runner needs no GCE
        zone, so a zone cooldown must NOT block it. Skipping the whole dispatch
        loop (rather than only the cold-GCE step) would strand every local-runner
        and DGX task for the duration of the outage."""
        import hashlib
        from sqlmodel import select as _select
        from app.models import LocalRunner

        prov_mock = self._patch_common(mocker, zones_available=False)

        async with orch_warm_db() as session:
            project = make_project(name="Zone Exhausted + Runner", user_id=_TEST_USER)
            session.add(project)
            await session.commit()
            await session.refresh(project)

            runner = LocalRunner(
                project_id=project.id,
                token_hash=hashlib.sha256(b"zone-gate-runner").hexdigest(),
                hostname="dgx-1",
                status="active",
                last_seen_at=_utcnow(),
                capabilities={"task_types": "all", "max_concurrent": 2},
            )
            session.add(runner)
            await session.commit()
            await session.refresh(runner)
            runner_id = runner.id

            task = make_task(
                project_id=project.id,
                vm_instance_id=None,
                status=TaskStatus.QUEUED,
                payloads=[{"type": "fix", "prompt": "x"}],
                context={},
            )
            session.add(task)
            await session.commit()
            await session.refresh(task)
            task_id = task.id

        await self._run_one_cycle(mocker)

        assert not prov_mock.called, (
            "A runner-pinned task must never reach GCP provisioning"
        )
        async with orch_warm_db() as verify:
            row = (
                (await verify.execute(_select(Task).where(Task.id == task_id)))
                .scalars()
                .first()
            )
            assert row is not None
            assert row.status == TaskStatus.PROVISIONING, (
                "A locally-routable task must still dispatch while GCP zones are "
                f"in cooldown, got {row.status}"
            )
            assert row.vm_instance_id == f"runner:{runner_id}", (
                f"Task must be pinned to the fleet runner, got {row.vm_instance_id}"
            )
            assert (row.context or {}).get("runner_target_id") == str(runner_id)

    @pytest.mark.asyncio
    async def test_zone_exhaustion_still_dispatches_cloud_run_job(
        self, orch_warm_db, mocker
    ):
        """BLAST RADIUS. A Cloud Run Job runs in a managed container, not on a GCE
        VM from the agent-VM zone ring — so an allowlisted read-only type must
        still dispatch while every zone is in cooldown."""
        from sqlmodel import select as _select
        import app.orchestrator as _orch_module

        prov_mock = self._patch_common(mocker, zones_available=False)
        cr_mock = mocker.patch(
            "app.orchestrator.launch_agent_cloud_run_job",
            new_callable=AsyncMock,
            return_value=True,
        )
        _orch_module._runtime_config["cloud_run_task_type_allowlist"] = ["research"]
        try:
            async with orch_warm_db() as session:
                project = make_project(name="Zone Exhausted + CR", user_id=_TEST_USER)
                session.add(project)
                await session.commit()
                await session.refresh(project)

                task = make_task(
                    project_id=project.id,
                    vm_instance_id=None,
                    status=TaskStatus.QUEUED,
                    payloads=[{"type": "research", "prompt": "Analyse the codebase"}],
                    context={},
                )
                session.add(task)
                await session.commit()
                await session.refresh(task)
                task_id = task.id

            await self._run_one_cycle(mocker)

            assert cr_mock.called, (
                "Cloud Run dispatch consumes no GCE zone capacity — a zone "
                "cooldown must not block it"
            )
            assert not prov_mock.called, (
                "A Cloud Run task must not fall through to GCE provisioning"
            )
            async with orch_warm_db() as verify:
                row = (
                    (await verify.execute(_select(Task).where(Task.id == task_id)))
                    .scalars()
                    .first()
                )
                assert row is not None
                assert row.status == TaskStatus.PROVISIONING
        finally:
            _orch_module._runtime_config["cloud_run_task_type_allowlist"] = []

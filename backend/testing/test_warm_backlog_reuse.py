"""Tests for warm-VM backlog reuse (efficiency direction d-91d8dede).

Covers the /api/agent/next-role backlog-claim branch and its module-level
eligibility/selection helpers:

  - claim path: an idle VM claims an eligible same-project QUEUED backlog task
    and the task is stamped warm (warm_start=True, warm_claim_origin=backlog_reuse)
  - no-double-claim: a task already grabbed (IN_PROGRESS/PROVISIONING) is never
    re-claimed; two idle VMs racing the same QUEUED task yield exactly one claim
  - project scoping: a VM never claims another project's backlog task
  - doomed-VM guard: a teardown-scheduled VM never wins a backlog claim
  - config gate: with the flag OFF behavior is unchanged (no backlog claim)
  - eligibility: dependency-bearing and file-conflicting tasks are skipped
  - telemetry: a backlog-reused task lifts vm_reuse_rate and adds no cold-start
    sample to the efficiency snapshot
"""

import time

import pytest
from httpx import AsyncClient

from app.auth import create_agent_token, verify_agent_jwt
from app.efficiency_snapshot import (
    AXIS_VM_COLD_START_P50_MS,
    AXIS_VM_REUSE_RATE,
    compute_current_axes,
)
from app.models import Project, Task, TaskStatus
from app.warm_backlog_reuse import (
    has_eligible_backlog_task,
    select_backlog_reuse_candidate,
)


async def _make_project(db_session, name="Backlog Reuse"):
    project = Project(name=name, user_id="test-user-123")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)
    return project


async def _make_backlog_task(db_session, project_id, **overrides):
    defaults = {
        "project_id": project_id,
        "title": "Queued backlog work",
        "payloads": [{"type": "create", "prompt": "do X", "base_branch": "automain"}],
        "status": TaskStatus.QUEUED,
        "context": {"scope": "small"},
    }
    defaults.update(overrides)
    task = Task(**defaults)
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)
    return task


def _patch_broadcast(mocker):
    mocker.patch(
        "app.routers.agent.broadcast_to_project", new_callable=mocker.AsyncMock
    )


class _FlagGuard:
    """Snapshot/restore runtime_config flags + the first-refusal registry."""

    def __init__(self, backlog=False, continuity=False):
        self.backlog = backlog
        self.continuity = continuity

    def __enter__(self):
        import app.shared_state as ss
        from app.shared_state import runtime_config as rc

        self._ss = ss
        self._rc = rc
        self._prior_backlog = rc.get("warm_backlog_reuse_enabled")
        self._prior_continuity = rc.get("warm_continuity_enabled")
        self._prior_refusal = dict(ss.warm_backlog_first_refusal)
        rc["warm_backlog_reuse_enabled"] = self.backlog
        rc["warm_continuity_enabled"] = self.continuity
        return self

    def __exit__(self, *exc):
        for key, prior in (
            ("warm_backlog_reuse_enabled", self._prior_backlog),
            ("warm_continuity_enabled", self._prior_continuity),
        ):
            if prior is None:
                self._rc.pop(key, None)
            else:
                self._rc[key] = prior
        self._ss.warm_backlog_first_refusal.clear()
        self._ss.warm_backlog_first_refusal.update(self._prior_refusal)
        # Reset circuit-breaker state so tests don't bleed into each other.
        self._ss.warm_backlog_reuse_failure_events.clear()
        self._ss.warm_backlog_reuse_suppressed_until = None
        return False


@pytest.mark.asyncio
async def test_backlog_reuse_claims_same_project_task(
    client: AsyncClient, db_session, mocker
):
    """An idle VM claims an eligible QUEUED backlog task in its project and the
    task is stamped warm with warm_claim_origin='backlog_reuse'."""
    _patch_broadcast(mocker)
    with _FlagGuard(backlog=True):
        project = await _make_project(db_session)
        vm_id = "agent-vm-backlog-001"
        prior = await _make_backlog_task(
            db_session, project.id, status=TaskStatus.COMPLETED, vm_instance_id=vm_id
        )
        backlog = await _make_backlog_task(db_session, project.id)

        token = create_agent_token(
            str(prior.id), vm_id, task_type="create", project_id=str(project.id)
        )
        resp = await client.post(
            "/api/agent/next-role",
            json={"vm_instance_id": vm_id, "prior_task_id": str(prior.id)},
            headers={"x-agent-token": token},
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["action"] == "claim"
        assert body["task_id"] == str(backlog.id)
        assert body["warm_start"] is True
        new_claims = verify_agent_jwt(body["agent_token"])
        assert new_claims["sub"] == str(backlog.id)
        assert new_claims["project_id"] == str(project.id)

        db_session.expire(backlog)
        await db_session.refresh(backlog)
        assert backlog.status == TaskStatus.IN_PROGRESS
        assert backlog.vm_instance_id == vm_id
        timing = (backlog.context or {}).get("_timing", {})
        assert timing.get("warm_start") is True
        assert timing.get("warm_claim_origin") == "backlog_reuse"
        # Both context flag and column must be synchronized.
        assert backlog.vm_warm_start is True
        # Warm claim must NOT stamp a cold-start sample.
        assert backlog.vm_cold_start_ms is None


@pytest.mark.asyncio
async def test_backlog_reuse_config_gate_off_is_unchanged(
    client: AsyncClient, db_session, mocker
):
    """With both warm flags OFF, an idle VM with no chain pin gets 204 and the
    backlog task is left untouched — behavior identical to pre-feature."""
    _patch_broadcast(mocker)
    with _FlagGuard(backlog=False, continuity=False):
        project = await _make_project(db_session, name="Gate Off")
        vm_id = "agent-vm-gate-off"
        prior = await _make_backlog_task(
            db_session, project.id, status=TaskStatus.COMPLETED, vm_instance_id=vm_id
        )
        backlog = await _make_backlog_task(db_session, project.id)

        token = create_agent_token(
            str(prior.id), vm_id, task_type="create", project_id=str(project.id)
        )
        resp = await client.post(
            "/api/agent/next-role",
            json={"vm_instance_id": vm_id, "prior_task_id": str(prior.id)},
            headers={"x-agent-token": token},
        )
        assert resp.status_code == 204

        db_session.expire(backlog)
        await db_session.refresh(backlog)
        assert backlog.status == TaskStatus.QUEUED
        assert backlog.vm_instance_id is None


@pytest.mark.asyncio
async def test_backlog_reuse_project_scoping(client: AsyncClient, db_session, mocker):
    """A VM never claims a backlog task belonging to a different project."""
    _patch_broadcast(mocker)
    with _FlagGuard(backlog=True):
        project_a = await _make_project(db_session, name="Project A")
        project_b = await _make_project(db_session, name="Project B")
        vm_id = "agent-vm-scoping"
        prior = await _make_backlog_task(
            db_session, project_a.id, status=TaskStatus.COMPLETED, vm_instance_id=vm_id
        )
        # Only project B has backlog work — the VM (scoped to A) must not take it.
        b_task = await _make_backlog_task(db_session, project_b.id)

        token = create_agent_token(
            str(prior.id), vm_id, task_type="create", project_id=str(project_a.id)
        )
        resp = await client.post(
            "/api/agent/next-role",
            json={"vm_instance_id": vm_id, "prior_task_id": str(prior.id)},
            headers={"x-agent-token": token},
        )
        assert resp.status_code == 204

        db_session.expire(b_task)
        await db_session.refresh(b_task)
        assert b_task.status == TaskStatus.QUEUED
        assert b_task.vm_instance_id is None


@pytest.mark.asyncio
async def test_backlog_reuse_doomed_vm_guard(client: AsyncClient, db_session, mocker):
    """A VM scheduled for orchestrator-initiated teardown never wins a backlog
    claim — the doomed-VM guard returns 204 before the backlog branch runs."""
    _patch_broadcast(mocker)
    import app.main as main

    with _FlagGuard(backlog=True):
        project = await _make_project(db_session, name="Doomed")
        vm_id = "agent-vm-doomed-backlog"
        prior = await _make_backlog_task(
            db_session, project.id, status=TaskStatus.COMPLETED, vm_instance_id=vm_id
        )
        backlog = await _make_backlog_task(db_session, project.id)

        main._orchestrator_deleted_vms[vm_id] = (time.time(), "test_doomed")
        try:
            token = create_agent_token(
                str(prior.id), vm_id, task_type="create", project_id=str(project.id)
            )
            resp = await client.post(
                "/api/agent/next-role",
                json={"vm_instance_id": vm_id, "prior_task_id": str(prior.id)},
                headers={"x-agent-token": token},
            )
            assert resp.status_code == 204
        finally:
            main._orchestrator_deleted_vms.pop(vm_id, None)

        db_session.expire(backlog)
        await db_session.refresh(backlog)
        assert backlog.status == TaskStatus.QUEUED


@pytest.mark.asyncio
async def test_backlog_reuse_no_double_claim_after_grabbed(
    client: AsyncClient, db_session, mocker
):
    """Once the orchestrator has grabbed a task (status != QUEUED), the backlog
    branch will not double-claim it — the VM gets 204."""
    _patch_broadcast(mocker)
    with _FlagGuard(backlog=True):
        project = await _make_project(db_session, name="No Double")
        vm_id = "agent-vm-no-double"
        prior = await _make_backlog_task(
            db_session, project.id, status=TaskStatus.COMPLETED, vm_instance_id=vm_id
        )
        # Simulate the orchestrator already cold-dispatching it.
        await _make_backlog_task(db_session, project.id, status=TaskStatus.PROVISIONING)

        token = create_agent_token(
            str(prior.id), vm_id, task_type="create", project_id=str(project.id)
        )
        resp = await client.post(
            "/api/agent/next-role",
            json={"vm_instance_id": vm_id, "prior_task_id": str(prior.id)},
            headers={"x-agent-token": token},
        )
        assert resp.status_code == 204


@pytest.mark.asyncio
async def test_backlog_reuse_two_vms_single_claim(
    client: AsyncClient, db_session, mocker
):
    """Two idle VMs in the same project, one QUEUED backlog task: exactly one
    claims it, the other gets 204 (the QUEUED->IN_PROGRESS flip is the race
    arbiter)."""
    _patch_broadcast(mocker)
    with _FlagGuard(backlog=True):
        project = await _make_project(db_session, name="Two VMs")
        backlog = await _make_backlog_task(db_session, project.id)

        results = []
        for vm_id in ("agent-vm-race-a", "agent-vm-race-b"):
            prior = await _make_backlog_task(
                db_session,
                project.id,
                status=TaskStatus.COMPLETED,
                vm_instance_id=vm_id,
            )
            token = create_agent_token(
                str(prior.id), vm_id, task_type="create", project_id=str(project.id)
            )
            resp = await client.post(
                "/api/agent/next-role",
                json={"vm_instance_id": vm_id, "prior_task_id": str(prior.id)},
                headers={"x-agent-token": token},
            )
            results.append(resp.status_code)

        assert sorted(results) == [200, 204]
        db_session.expire(backlog)
        await db_session.refresh(backlog)
        assert backlog.status == TaskStatus.IN_PROGRESS


@pytest.mark.asyncio
async def test_eligibility_skips_dependency_and_file_conflict(db_session):
    """select_backlog_reuse_candidate skips tasks with dependencies and tasks
    whose files overlap an IN_PROGRESS task in the same project."""
    project = await _make_project(db_session, name="Eligibility")
    vm_id = "agent-vm-eligibility"

    # IN_PROGRESS task editing src/app.py — file-conflict source.
    await _make_backlog_task(
        db_session,
        project.id,
        status=TaskStatus.IN_PROGRESS,
        context={"related_files": ["src/app.py"]},
    )
    # Candidate that conflicts on src/app.py -> ineligible.
    await _make_backlog_task(
        db_session, project.id, context={"related_files": ["src/app.py"]}
    )
    # Candidate with an unmet dependency -> ineligible.
    await _make_backlog_task(
        db_session, project.id, depends_on=["00000000-0000-0000-0000-000000000123"]
    )

    assert await has_eligible_backlog_task(db_session, project.id, vm_id) is False
    assert await select_backlog_reuse_candidate(db_session, project.id, vm_id) is None

    # Add a clean, dependency-free, non-conflicting candidate -> eligible.
    clean = await _make_backlog_task(
        db_session, project.id, context={"related_files": ["docs/readme.md"]}
    )
    assert await has_eligible_backlog_task(db_session, project.id, vm_id) is True
    picked = await select_backlog_reuse_candidate(db_session, project.id, vm_id)
    assert picked is not None and picked.id == clean.id


@pytest.mark.asyncio
async def test_eligibility_skips_other_vm_pin(db_session):
    """A task pinned to a different VM (chain pin) is not claimable as backlog
    by this VM."""
    project = await _make_project(db_session, name="Other Pin")
    vm_id = "agent-vm-mine"
    await _make_backlog_task(
        db_session,
        project.id,
        context={"warm_target_vm_id": "agent-vm-someone-else"},
    )
    assert await select_backlog_reuse_candidate(db_session, project.id, vm_id) is None


@pytest.mark.asyncio
async def test_in_progress_scan_computed_once_when_reused(db_session, mocker):
    """A caller pairing the eligibility gate with the claim can compute the
    project's IN_PROGRESS file set once and pass it to both, so the file-conflict
    scan runs a single time instead of once per call."""
    import app.warm_backlog_reuse as wbr

    project = await _make_project(db_session, name="Scan Reuse")
    vm_id = "agent-vm-scan"
    # One IN_PROGRESS task (file-conflict source) + one clean candidate.
    await _make_backlog_task(
        db_session,
        project.id,
        status=TaskStatus.IN_PROGRESS,
        context={"related_files": ["src/app.py"]},
    )
    clean = await _make_backlog_task(
        db_session, project.id, context={"related_files": ["docs/readme.md"]}
    )

    # Compute the IN_PROGRESS file set once, then reuse it across both calls.
    in_progress_files = await wbr._project_in_progress_files(db_session, project.id)
    spy = mocker.spy(wbr, "_project_in_progress_files")

    assert (
        await has_eligible_backlog_task(
            db_session, project.id, vm_id, in_progress_files=in_progress_files
        )
        is True
    )
    picked = await select_backlog_reuse_candidate(
        db_session, project.id, vm_id, in_progress_files=in_progress_files
    )
    assert picked is not None and picked.id == clean.id
    # Neither call re-scanned IN_PROGRESS — the precomputed set was reused.
    assert spy.call_count == 0


@pytest.mark.asyncio
async def test_in_progress_scan_runs_when_not_provided(db_session, mocker):
    """When no precomputed set is passed, the scan still runs (lazy default),
    preserving the standalone behavior of each helper."""
    import app.warm_backlog_reuse as wbr

    project = await _make_project(db_session, name="Scan Lazy")
    vm_id = "agent-vm-lazy"
    await _make_backlog_task(
        db_session, project.id, context={"related_files": ["docs/readme.md"]}
    )
    spy = mocker.spy(wbr, "_project_in_progress_files")
    assert await has_eligible_backlog_task(db_session, project.id, vm_id) is True
    assert spy.call_count == 1


@pytest.mark.asyncio
async def test_backlog_reused_task_lifts_efficiency_axes(
    client: AsyncClient, db_session, mocker
):
    """A backlog-reused task contributes to vm_reuse_rate (warm_start=True) and
    adds no cold-start sample (vm_cold_start_ms stays NULL), so the snapshot
    computation needs no change."""
    _patch_broadcast(mocker)
    from datetime import timedelta

    from testing.factories import _utcnow

    with _FlagGuard(backlog=True):
        project = await _make_project(db_session, name="Efficiency")
        vm_id = "agent-vm-eff"
        prior = await _make_backlog_task(
            db_session, project.id, status=TaskStatus.COMPLETED, vm_instance_id=vm_id
        )
        # A genuinely cold, merged task to populate the cold-start sample.
        now = _utcnow()
        cold = await _make_backlog_task(
            db_session,
            project.id,
            status=TaskStatus.COMPLETED,
            context={"pr_merged": True, "_timing": {"warm_start": False}},
            vm_provisioned_at=now - timedelta(minutes=5),
            vm_stopped_at=now,
            vm_cold_start_ms=12000,
        )
        # The backlog task to be warm-claimed.
        backlog = await _make_backlog_task(db_session, project.id)

        token = create_agent_token(
            str(prior.id), vm_id, task_type="create", project_id=str(project.id)
        )
        resp = await client.post(
            "/api/agent/next-role",
            json={"vm_instance_id": vm_id, "prior_task_id": str(prior.id)},
            headers={"x-agent-token": token},
        )
        assert resp.status_code == 200, resp.text

        # Mark the warm-claimed task merged so it participates as a warm sample.
        db_session.expire(backlog)
        await db_session.refresh(backlog)
        ctx = dict(backlog.context or {})
        ctx["pr_merged"] = True
        backlog.context = ctx
        backlog.status = TaskStatus.COMPLETED
        db_session.add(backlog)
        await db_session.commit()

        axes = await compute_current_axes(db_session, project.id)
        # cold (warm_start False) + backlog (warm_start True) participate -> 0.5.
        assert axes[AXIS_VM_REUSE_RATE] == 0.5
        # Only the cold task supplies a cold-start sample; the warm task is NULL.
        assert axes[AXIS_VM_COLD_START_P50_MS] == 12000.0
        assert cold.vm_cold_start_ms == 12000
        assert backlog.vm_cold_start_ms is None


# ---------------------------------------------------------------------------
# Circuit-breaker tests
# ---------------------------------------------------------------------------


def test_backlog_reuse_circuit_breaker_suppresses_after_threshold():
    """Injecting >= threshold failures trips the breaker and suppresses reuse."""
    import app.shared_state as ss

    ss.warm_backlog_reuse_failure_events.clear()
    ss.warm_backlog_reuse_suppressed_until = None
    threshold = ss.runtime_config.get("warm_backlog_reuse_failure_rate_threshold", 5)
    try:
        for _ in range(threshold):
            ss.record_backlog_reuse_failure("task_failed")
        assert ss.count_recent_backlog_reuse_failures() >= threshold
        assert ss.is_warm_backlog_reuse_suppressed() is True
        assert ss.warm_backlog_reuse_suppressed_until is not None
    finally:
        ss.warm_backlog_reuse_failure_events.clear()
        ss.warm_backlog_reuse_suppressed_until = None


def test_backlog_reuse_circuit_breaker_clears_after_cooldown():
    """With an expired suppression deadline the breaker clears on the next check."""
    from datetime import timedelta

    import app.shared_state as ss
    from app.time_utils import _utcnow

    ss.warm_backlog_reuse_failure_events.clear()
    # Set suppression deadline in the past so the breaker is "expired".
    ss.warm_backlog_reuse_suppressed_until = _utcnow() - timedelta(seconds=1)
    try:
        # No recent failures — below threshold — so breaker should clear.
        result = ss.is_warm_backlog_reuse_suppressed()
        assert result is False
        assert ss.warm_backlog_reuse_suppressed_until is None
    finally:
        ss.warm_backlog_reuse_failure_events.clear()
        ss.warm_backlog_reuse_suppressed_until = None


def test_backlog_reuse_finalize_error_records_failure():
    """record_backlog_reuse_failure increments the event count (used from finalize error path)."""
    import app.shared_state as ss

    ss.warm_backlog_reuse_failure_events.clear()
    ss.warm_backlog_reuse_suppressed_until = None
    try:
        assert ss.count_recent_backlog_reuse_failures() == 0
        ss.record_backlog_reuse_failure("finalize_error")
        assert ss.count_recent_backlog_reuse_failures() == 1
        ss.record_backlog_reuse_failure("task_failed")
        assert ss.count_recent_backlog_reuse_failures() == 2
    finally:
        ss.warm_backlog_reuse_failure_events.clear()
        ss.warm_backlog_reuse_suppressed_until = None


# ---------------------------------------------------------------------------
# Funnel observability counter tests
# ---------------------------------------------------------------------------


def _clear_funnel_events(wbr):
    wbr._vms_with_eligible_backlog_events.clear()
    wbr._teardown_defers_events.clear()
    wbr._next_role_polls_during_defer_events.clear()
    wbr._warm_claims_succeeded_events.clear()
    wbr._claim_skipped_events.clear()
    wbr._scan_skipped_depends_on_events.clear()
    wbr._scan_skipped_pinned_vm_events.clear()
    wbr._scan_skipped_file_conflict_events.clear()
    wbr._scan_saturated_events.clear()


def test_funnel_counters_and_snapshot():
    """Each record_* function increments its stage counter; get_reuse_funnel_snapshot
    returns accurate per-stage and per-reason totals."""
    import app.warm_backlog_reuse as wbr

    _clear_funnel_events(wbr)
    try:
        wbr.record_vm_with_eligible_backlog()
        wbr.record_vm_with_eligible_backlog()
        wbr.record_teardown_defer()
        wbr.record_next_role_poll_during_defer()
        wbr.record_warm_claim_succeeded()
        wbr.record_claim_skipped("no_eligible")
        wbr.record_claim_skipped("lost_claim_race")
        wbr.record_claim_skipped("no_eligible")

        snap = wbr.get_reuse_funnel_snapshot()
        assert snap["window_hours"] == 24
        assert snap["completed_vms_with_eligible_backlog"] == 2
        assert snap["teardown_defers_scheduled"] == 1
        assert snap["next_role_polls_during_defer"] == 1
        assert snap["warm_claims_succeeded"] == 1
        assert snap["claim_skipped_total"] == 3
        assert snap["claim_skipped_by_reason"]["no_eligible"] == 2
        assert snap["claim_skipped_by_reason"]["lost_claim_race"] == 1
    finally:
        _clear_funnel_events(wbr)


def test_funnel_claim_skipped_circuit_breaker():
    """record_claim_skipped('circuit_breaker_tripped') is tracked in the snapshot."""
    import app.warm_backlog_reuse as wbr

    _clear_funnel_events(wbr)
    try:
        wbr.record_claim_skipped("circuit_breaker_tripped")
        snap = wbr.get_reuse_funnel_snapshot()
        assert snap["claim_skipped_by_reason"].get("circuit_breaker_tripped") == 1
        assert snap["claim_skipped_total"] == 1
    finally:
        _clear_funnel_events(wbr)


@pytest.mark.asyncio
async def test_funnel_warm_claim_increments_succeeded(
    client: AsyncClient, db_session, mocker
):
    """A successful /next-role backlog claim increments warm_claims_succeeded
    and leaves claim_skipped_total at zero."""
    import app.warm_backlog_reuse as wbr

    _patch_broadcast(mocker)
    _clear_funnel_events(wbr)
    try:
        with _FlagGuard(backlog=True):
            project = await _make_project(db_session, name="Funnel Claim")
            vm_id = "agent-vm-funnel-claim"
            prior = await _make_backlog_task(
                db_session,
                project.id,
                status=TaskStatus.COMPLETED,
                vm_instance_id=vm_id,
            )
            await _make_backlog_task(db_session, project.id)
            token = create_agent_token(
                str(prior.id), vm_id, task_type="create", project_id=str(project.id)
            )
            resp = await client.post(
                "/api/agent/next-role",
                json={"vm_instance_id": vm_id, "prior_task_id": str(prior.id)},
                headers={"x-agent-token": token},
            )
            assert resp.status_code == 200, resp.text

        snap = wbr.get_reuse_funnel_snapshot()
        assert snap["warm_claims_succeeded"] >= 1
        assert snap["claim_skipped_total"] == 0
    finally:
        _clear_funnel_events(wbr)


@pytest.mark.asyncio
async def test_funnel_no_eligible_increments_skipped(
    client: AsyncClient, db_session, mocker
):
    """A /next-role poll with an empty backlog increments claim_skipped no_eligible."""
    import app.warm_backlog_reuse as wbr

    _patch_broadcast(mocker)
    _clear_funnel_events(wbr)
    try:
        with _FlagGuard(backlog=True):
            project = await _make_project(db_session, name="Funnel No Eligible")
            vm_id = "agent-vm-funnel-noelig"
            prior = await _make_backlog_task(
                db_session,
                project.id,
                status=TaskStatus.COMPLETED,
                vm_instance_id=vm_id,
            )
            # No QUEUED backlog task — queue is empty.
            token = create_agent_token(
                str(prior.id), vm_id, task_type="create", project_id=str(project.id)
            )
            resp = await client.post(
                "/api/agent/next-role",
                json={"vm_instance_id": vm_id, "prior_task_id": str(prior.id)},
                headers={"x-agent-token": token},
            )
            assert resp.status_code == 204

        snap = wbr.get_reuse_funnel_snapshot()
        assert snap["warm_claims_succeeded"] == 0
        assert snap["claim_skipped_by_reason"].get("no_eligible", 0) >= 1
    finally:
        _clear_funnel_events(wbr)


# ---------------------------------------------------------------------------
# Eligibility-scan skip-reason funnel tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_scan_skip_reasons_increment_each_stage_once(db_session):
    """A single scan that triggers each structural skip reason at least once
    increments each new snapshot field by exactly 1."""
    import app.warm_backlog_reuse as wbr

    _clear_funnel_events(wbr)
    try:
        project = await _make_project(db_session, name="Scan Mixed")
        vm_id = "agent-vm-scan-mixed"

        # IN_PROGRESS task editing src/app.py — file-conflict source.
        await _make_backlog_task(
            db_session,
            project.id,
            status=TaskStatus.IN_PROGRESS,
            context={"related_files": ["src/app.py"]},
        )
        # File-conflict candidate.
        await _make_backlog_task(
            db_session, project.id, context={"related_files": ["src/app.py"]}
        )
        # Depends_on candidate.
        await _make_backlog_task(
            db_session,
            project.id,
            depends_on=["00000000-0000-0000-0000-000000000123"],
        )
        # Pinned to a different VM.
        await _make_backlog_task(
            db_session,
            project.id,
            context={"warm_target_vm_id": "agent-vm-other"},
        )
        # One clean candidate so the scan still includes non-skip rows.
        await _make_backlog_task(
            db_session, project.id, context={"related_files": ["docs/readme.md"]}
        )

        await wbr._eligible_backlog_ids(db_session, project.id, vm_id)
        snap = wbr.get_reuse_funnel_snapshot()
        assert snap["scan_polls_with_depends_on_skip"] == 1
        assert snap["scan_polls_with_pinned_vm_skip"] == 1
        assert snap["scan_polls_with_file_conflict_skip"] == 1
        # 5 QUEUED rows fits under the default _backlog_scan_limit() (10).
        assert snap["scan_polls_saturated"] == 0
    finally:
        _clear_funnel_events(wbr)


@pytest.mark.asyncio
async def test_scan_skip_aggregates_per_poll_not_per_row(db_session):
    """A poll that skips multiple rows for the same reason increments the
    counter by 1 (per-poll aggregation), not by the row count."""
    import app.warm_backlog_reuse as wbr

    _clear_funnel_events(wbr)
    try:
        project = await _make_project(db_session, name="Scan Per Poll")
        vm_id = "agent-vm-scan-poll"

        # Three depends_on-skipped candidates.
        for _ in range(3):
            await _make_backlog_task(
                db_session,
                project.id,
                depends_on=["00000000-0000-0000-0000-000000000999"],
            )

        await wbr._eligible_backlog_ids(db_session, project.id, vm_id)
        snap = wbr.get_reuse_funnel_snapshot()
        # 3 skipped rows in one poll == exactly 1 event.
        assert snap["scan_polls_with_depends_on_skip"] == 1

        # A second poll with the same backlog: counter advances by exactly 1.
        await wbr._eligible_backlog_ids(db_session, project.id, vm_id)
        snap = wbr.get_reuse_funnel_snapshot()
        assert snap["scan_polls_with_depends_on_skip"] == 2
    finally:
        _clear_funnel_events(wbr)


@pytest.mark.asyncio
async def test_scan_saturated_increments_when_at_limit(db_session, monkeypatch):
    """When rows_fetched hits _backlog_scan_limit(), scan_polls_saturated increments."""
    import app.warm_backlog_reuse as wbr

    _clear_funnel_events(wbr)
    try:
        project = await _make_project(db_session, name="Scan Saturated")
        vm_id = "agent-vm-scan-sat"

        # Lower the scan limit so the test stays cheap.
        monkeypatch.setitem(wbr._runtime_config, "warm_backlog_scan_limit", 3)
        for _ in range(3):
            await _make_backlog_task(db_session, project.id)

        await wbr._eligible_backlog_ids(db_session, project.id, vm_id)
        snap = wbr.get_reuse_funnel_snapshot()
        assert snap["scan_polls_saturated"] == 1
    finally:
        _clear_funnel_events(wbr)


@pytest.mark.asyncio
async def test_scan_skip_no_events_when_all_eligible(db_session):
    """A scan where every candidate is eligible records zero skip events."""
    import app.warm_backlog_reuse as wbr

    _clear_funnel_events(wbr)
    try:
        project = await _make_project(db_session, name="Scan Clean")
        vm_id = "agent-vm-scan-clean"
        await _make_backlog_task(
            db_session, project.id, context={"related_files": ["docs/x.md"]}
        )

        await wbr._eligible_backlog_ids(db_session, project.id, vm_id)
        snap = wbr.get_reuse_funnel_snapshot()
        assert snap["scan_polls_with_depends_on_skip"] == 0
        assert snap["scan_polls_with_pinned_vm_skip"] == 0
        assert snap["scan_polls_with_file_conflict_skip"] == 0
        assert snap["scan_polls_saturated"] == 0
    finally:
        _clear_funnel_events(wbr)

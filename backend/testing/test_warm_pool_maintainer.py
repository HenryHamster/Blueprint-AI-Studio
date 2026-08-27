"""Seam tests for the warm-pool maintainer, GC survival, and idle reaper
(efficiency direction d-5fd69fe1).

Covers the assertions the task calls out:
  1. A registered (live) pool VM is spared by orphan cleanup and secret
     reconciliation (its id is in the in-use set), while a RETIRING member is
     NOT — so genuinely orphaned VMs are still reclaimed.
  2. Idle pool VMs do not inflate the dispatch slot count (they are excluded
     from the dispatch budget via count_idle_pool_vms).
  3. The idle reaper retires over-TTL / stale-image / zombie pool members and
     spares a fresh, heartbeating one.
Plus maintainer reconcile (drain over-target, provision below-target, recycle
stale image) and the pool circuit breaker.
"""

import uuid
from datetime import timedelta
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import update
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import async_sessionmaker
from sqlmodel import select

import app.shared_state as _shared_state
import app.warm_pool_maintainer as _wpm
from app.models import (
    ImprovementLane,
    ImprovementLaneStatus,
    PoolIdleCostLedger,
    PoolIdleSpan,
    PoolVm,
    PoolVmState,
    Project,
    Task,
    TaskStatus,
    _LANE_MAX_PHASE_REQUEUES,
)
from app.shared_state import runtime_config as _runtime_config
from app.time_utils import _utcnow
from app.warm_pool import (
    count_idle_pool_vms,
    count_live_pool_vms,
    clear_lane_warm_workspace_if_matching_member,
    get_live_pool_vm_ids,
    is_pool_vm_image_stale,
    lease_idle_pool_vm,
    register_pool_vm,
    resolve_project_warm_pool_target,
    try_pool_dispatch,
    warm_pool_project_target_map,
)
from app.warm_pool_maintainer import (
    evaluate_pool_cost_guard,
    evaluate_project_pool_cost_guard,
    reconcile_project_pool,
    retire_and_delete_pool_vm,
    run_pool_maintainer_cycle,
    sweep_exhausted_lane_budgets,
    sweep_idle_pool_vms,
    sweep_stranded_lanes,
)
from testing.conftest import USE_POSTGRES


class _NoCloseSession:
    """Wrap a test ``db_session`` so ``async with factory() as s`` yields it
    without closing it on exit (the fixture owns its lifecycle)."""

    def __init__(self, session):
        self._session = session

    async def __aenter__(self):
        return self._session

    async def __aexit__(self, *exc):
        return False


def _factory_for(session):
    return lambda: _NoCloseSession(session)


async def _make_project(db_session, name="Pool Maint"):
    project = Project(name=name, user_id="u-pool")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)
    return project


async def _register(db_session, project, vm_id, state, **kw):
    member = await register_pool_vm(
        db_session,
        vm_instance_id=vm_id,
        project_id=project.id,
        account_id=kw.get("account_id", "acct-1"),
        provider=kw.get("provider", "anthropic"),
        model=kw.get("model", "claude-opus-4-8"),
        state=state,
    )
    await db_session.commit()
    return member


@pytest.fixture
def warm_pool_on():
    """Enable warm_pool_enabled for the test and restore prior state + breaker."""
    prior = dict(_runtime_config)
    prior_deadline = _shared_state.warm_pool_suppressed_until
    _runtime_config["warm_pool_enabled"] = True
    _shared_state.warm_pool_suppressed_until = None
    _shared_state.warm_pool_claim_failure_events.clear()
    try:
        yield
    finally:
        for k in (
            "warm_pool_enabled",
            "warm_pool_target_size",
            "warm_pool_target_size_by_project",
            "warm_pool_max_vms",
            "warm_pool_idle_ttl_seconds",
            "warm_pool_heartbeat_stale_seconds",
            "warm_pool_failure_rate_threshold",
            "warm_pool_demand_window_minutes",
            "warm_pool_project_allowlist",
            "max_concurrent_vms",
        ):
            if k in prior:
                _runtime_config[k] = prior[k]
            else:
                _runtime_config.pop(k, None)
        _shared_state.warm_pool_suppressed_until = prior_deadline
        _shared_state.warm_pool_claim_failure_events.clear()


# ---------------------------------------------------------------------------
# Deliverable 2: GC + secret-reconciliation survival
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_live_pool_vms_are_spared_retiring_is_not(db_session):
    project = await _make_project(db_session)
    await _register(db_session, project, "agent-vm-idle", PoolVmState.IDLE)
    await _register(db_session, project, "agent-vm-prov", PoolVmState.PROVISIONING)
    await _register(db_session, project, "agent-vm-leased", PoolVmState.LEASED)
    await _register(db_session, project, "agent-vm-retiring", PoolVmState.RETIRING)

    spare = await get_live_pool_vm_ids(db_session)
    # Live members (PROVISIONING/IDLE/LEASED) are spared from cleanup...
    assert "agent-vm-idle" in spare
    assert "agent-vm-prov" in spare
    assert "agent-vm-leased" in spare
    # ...but a RETIRING member is NOT — so genuinely retiring/orphaned VMs are
    # still reclaimed by the generic cleanup. (Precision check.)
    assert "agent-vm-retiring" not in spare


# ---------------------------------------------------------------------------
# Deliverable 3: slot accounting
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_idle_pool_vms_excluded_from_dispatch_slot_count(db_session):
    project = await _make_project(db_session)
    await _register(db_session, project, "agent-vm-i1", PoolVmState.IDLE)
    await _register(db_session, project, "agent-vm-i2", PoolVmState.IDLE)
    await _register(db_session, project, "agent-vm-p1", PoolVmState.PROVISIONING)
    # LEASED member is task-bound — its task already consumes a dispatch slot, so
    # it must NOT be double-counted as idle inventory.
    await _register(db_session, project, "agent-vm-l1", PoolVmState.LEASED)
    await _register(db_session, project, "agent-vm-r1", PoolVmState.RETIRING)

    # Slot accounting subtracts non-task-bound (PROVISIONING + IDLE) members.
    assert await count_idle_pool_vms(db_session) == 3
    # Live count (used for the dedicated pool budget) excludes only RETIRING.
    assert await count_live_pool_vms(db_session) == 4


# ---------------------------------------------------------------------------
# Deliverable 4: idle-TTL / zombie / stale-image reaper
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_idle_reaper_retires_over_ttl_member(db_session, warm_pool_on, mocker):
    mock_del = mocker.patch(
        "app.vm_lifecycle._ensure_vm_deleted", new_callable=mocker.AsyncMock
    )
    mocker.patch("app.compute.get_stale_image_vms", return_value=[])
    _runtime_config["warm_pool_idle_ttl_seconds"] = 1800

    project = await _make_project(db_session)
    member = await _register(db_session, project, "agent-vm-ttl", PoolVmState.IDLE)
    member.idle_since = _utcnow() - timedelta(seconds=4000)  # well over TTL
    db_session.add(member)
    await db_session.commit()
    # Recent heartbeat so the ONLY reason it dies is the idle-TTL.
    _shared_state.record_vm_heartbeat("agent-vm-ttl")

    retired = await sweep_idle_pool_vms(
        db_session, _utcnow(), runtime_config=_runtime_config
    )
    assert retired == 1
    mock_del.assert_awaited()
    assert await db_session.get(PoolVm, member.id) is None


@pytest.mark.asyncio
async def test_idle_reaper_retires_stale_image_member(db_session, warm_pool_on, mocker):
    mocker.patch("app.vm_lifecycle._ensure_vm_deleted", new_callable=mocker.AsyncMock)
    project = await _make_project(db_session)
    member = await _register(db_session, project, "agent-vm-stale", PoolVmState.IDLE)
    member.idle_since = _utcnow()  # fresh idle — not over TTL
    db_session.add(member)
    await db_session.commit()
    _shared_state.record_vm_heartbeat("agent-vm-stale")  # alive — isolate stale reason
    # The existing image-staleness detector reports this VM as stale.
    mocker.patch(
        "app.compute.get_stale_image_vms",
        return_value=[{"vm_id": "agent-vm-stale"}],
    )

    retired = await sweep_idle_pool_vms(
        db_session, _utcnow(), runtime_config=_runtime_config
    )
    assert retired == 1
    assert await db_session.get(PoolVm, member.id) is None


@pytest.mark.asyncio
@pytest.mark.skipif(
    not USE_POSTGRES,
    reason="the except-handler read after session.rollback() only expires (and so "
    "raises MissingGreenlet) on asyncpg; aiosqlite no-ops InstanceState._expire.",
)
async def test_idle_reaper_continues_after_retire_failure(
    db_session, warm_pool_on, mocker
):
    """A per-member retire failure rolls back, logs, and the sweep CONTINUES to
    the next member — it must not crash with a MissingGreenlet reading an
    instance the rollback expired (regression for the 2026-06-04 incident triage:
    the except handler read member.vm_instance_id after session.rollback(),
    masking the real error and aborting the whole sweep)."""
    mocker.patch("app.vm_lifecycle._ensure_vm_deleted", new_callable=mocker.AsyncMock)
    mocker.patch("app.compute.get_stale_image_vms", return_value=[])
    _runtime_config["warm_pool_idle_ttl_seconds"] = 1800

    project = await _make_project(db_session)
    # created_at order = scan/iteration order: the failing member is reaped first.
    m_fail = await _register(db_session, project, "agent-vm-fail", PoolVmState.IDLE)
    m_ok = await _register(db_session, project, "agent-vm-ok", PoolVmState.IDLE)
    for m in (m_fail, m_ok):
        m.idle_since = _utcnow() - timedelta(seconds=4000)  # over TTL
        db_session.add(m)
        _shared_state.record_vm_heartbeat(m.vm_instance_id)  # alive → idle-TTL only
    await db_session.commit()

    real_retire = _wpm.retire_and_delete_pool_vm
    attempted: list[str] = []

    async def flaky_retire(session, member, **kw):
        attempted.append(member.vm_instance_id)
        if member.vm_instance_id == "agent-vm-fail":
            raise SQLAlchemyError("simulated lock timeout")
        return await real_retire(session, member, **kw)

    mocker.patch("app.warm_pool_maintainer.retire_and_delete_pool_vm", new=flaky_retire)

    # Snapshot ids before the sweep: its internal rollback (on the simulated
    # failure) expires the test's instances, so reading m_fail.id afterwards
    # would itself lazy-load and raise MissingGreenlet on asyncpg.
    m_fail_id = m_fail.id
    m_ok_id = m_ok.id

    # Must not raise (no MissingGreenlet), and the healthy member is still reaped.
    retired = await sweep_idle_pool_vms(
        db_session, _utcnow(), runtime_config=_runtime_config
    )
    assert retired == 1
    # Both candidates were attempted in order — the failure did not abort the loop.
    assert attempted == ["agent-vm-fail", "agent-vm-ok"]
    assert await db_session.get(PoolVm, m_ok_id) is None  # the OK one was deleted
    assert await db_session.get(PoolVm, m_fail_id) is not None  # failed one survives


@pytest.mark.asyncio
async def test_idle_reaper_retires_zombie_member(db_session, warm_pool_on, mocker):
    mocker.patch("app.vm_lifecycle._ensure_vm_deleted", new_callable=mocker.AsyncMock)
    mocker.patch("app.compute.get_stale_image_vms", return_value=[])
    _runtime_config["warm_pool_heartbeat_stale_seconds"] = 300
    # Simulate a long-lived process so the zombie-teardown warmup gate is armed
    # (a fresh process must NOT reap on an empty in-memory heartbeat map — that
    # is the deploy/failover false-positive covered by the warmup test below).
    mocker.patch.object(_wpm, "_PROCESS_START_TS", _utcnow() - timedelta(seconds=900))

    project = await _make_project(db_session)
    # PROVISIONING member that never heartbeated and has aged past the liveness
    # window → zombie.
    member = await _register(
        db_session, project, "agent-vm-zombie", PoolVmState.PROVISIONING
    )
    member.created_at = _utcnow() - timedelta(seconds=900)
    db_session.add(member)
    await db_session.commit()

    retired = await sweep_idle_pool_vms(
        db_session, _utcnow(), runtime_config=_runtime_config
    )
    assert retired == 1
    assert await db_session.get(PoolVm, member.id) is None


@pytest.mark.asyncio
async def test_idle_reaper_spares_zombie_during_warmup(
    db_session, warm_pool_on, mocker
):
    """A fresh leader (process just started) must NOT tear down an established
    pool member solely because its in-memory heartbeat map is empty. This is the
    deploy/crash/failover false-positive: the heartbeat map is process-local and
    never hydrated at startup, so the zombie teardown is suppressed until the
    process has been up past the warmup window."""
    mock_del = mocker.patch(
        "app.vm_lifecycle._ensure_vm_deleted", new_callable=mocker.AsyncMock
    )
    mocker.patch("app.compute.get_stale_image_vms", return_value=[])
    _runtime_config["warm_pool_heartbeat_stale_seconds"] = 300
    # Process started "just now" — warmup window has NOT elapsed.
    mocker.patch.object(_wpm, "_PROCESS_START_TS", _utcnow() - timedelta(seconds=5))

    project = await _make_project(db_session)
    # Established IDLE member, aged well past the staleness window, with NO
    # heartbeat in the (empty) in-memory map — exactly what a member looks like
    # to a fresh leader right after a changeover. It must survive.
    member = await _register(
        db_session, project, "agent-vm-established", PoolVmState.IDLE
    )
    member.idle_since = _utcnow() - timedelta(seconds=900)
    db_session.add(member)
    await db_session.commit()

    retired = await sweep_idle_pool_vms(
        db_session, _utcnow(), runtime_config=_runtime_config
    )
    assert retired == 0
    mock_del.assert_not_awaited()
    assert await db_session.get(PoolVm, member.id) is not None


@pytest.mark.asyncio
async def test_retire_carries_banked_idle_cost_to_ledger(
    db_session, warm_pool_on, mocker
):
    """Tearing down a pool member must NOT erase its banked idle spend: the
    banked cost is carried into the durable PoolIdleCostLedger in the same
    transaction that deletes the registry row, so compute_pool_idle_cost still
    sees it after the row is gone."""
    from app.efficiency_snapshot import compute_pool_idle_cost

    mocker.patch("app.vm_lifecycle._ensure_vm_deleted", new_callable=mocker.AsyncMock)

    project = await _make_project(db_session)
    member = await _register(db_session, project, "agent-vm-banked", PoolVmState.IDLE)
    member.idle_cost_banked = 1.25
    member.idle_since = None  # already settled; no further accrual on teardown
    db_session.add(member)
    await db_session.commit()

    ok = await retire_and_delete_pool_vm(
        db_session, member, reason="pool_idle_ttl", claim_grace_seconds=300
    )
    assert ok is True
    # Registry row is gone...
    assert await db_session.get(PoolVm, member.id) is None
    # ...but the banked spend survived in the ledger and is still counted.
    ledger = (await db_session.execute(select(PoolIdleCostLedger))).scalars().all()
    assert len(ledger) == 1
    assert ledger[0].vm_instance_id == "agent-vm-banked"
    assert ledger[0].idle_cost_usd == pytest.approx(1.25)
    cost = await compute_pool_idle_cost(db_session, project.id)
    assert cost["retired_idle_cost_usd"] == pytest.approx(1.25)
    assert cost["settled_idle_cost_usd"] == pytest.approx(1.25)


@pytest.mark.asyncio
async def test_retire_writes_pool_idle_span_for_final_accrual(
    db_session, warm_pool_on, mocker
):
    """retire_and_delete_pool_vm must write a PoolIdleSpan row for the final
    idle interval so _compute_windowed_idle_cost can attribute the spend to
    the exact span end time rather than the member's retirement timestamp."""
    from app.efficiency_snapshot import _compute_windowed_idle_cost

    mocker.patch("app.vm_lifecycle._ensure_vm_deleted", new_callable=mocker.AsyncMock)

    project = await _make_project(db_session)
    member = await _register(db_session, project, "agent-vm-span", PoolVmState.IDLE)
    now = _utcnow()
    # Member has been idle for 2 hours — this span must become a PoolIdleSpan row.
    member.idle_since = now - timedelta(hours=2)
    db_session.add(member)
    await db_session.commit()

    ok = await retire_and_delete_pool_vm(
        db_session, member, reason="pool_idle_ttl", claim_grace_seconds=300
    )
    assert ok is True

    # A PoolIdleSpan row must have been written for the 2-hour span.
    spans = (await db_session.execute(select(PoolIdleSpan))).scalars().all()
    assert len(spans) == 1, f"expected 1 PoolIdleSpan row, got {len(spans)}"
    span = spans[0]
    assert span.vm_instance_id == "agent-vm-span"
    assert span.cost_usd > 0
    assert span.reason == "pool_idle_ttl"
    # The span's settled_at must be recent (within a few seconds).
    span_age = (now - span.settled_at).total_seconds()
    assert abs(span_age) < 10, f"settled_at too far from now: {span_age}s"

    # The span must appear in the windowed idle cost (window covers the last 24h).
    cutoff = now - timedelta(hours=24)
    windowed = await _compute_windowed_idle_cost(db_session, project.id, cutoff)
    assert windowed["total"] == pytest.approx(span.cost_usd, rel=0.01), (
        f"windowed total {windowed['total']} should equal the span cost {span.cost_usd}"
    )


@pytest.mark.asyncio
async def test_idle_reaper_spares_fresh_alive_member(db_session, warm_pool_on, mocker):
    mock_del = mocker.patch(
        "app.vm_lifecycle._ensure_vm_deleted", new_callable=mocker.AsyncMock
    )
    mocker.patch("app.compute.get_stale_image_vms", return_value=[])
    _runtime_config["warm_pool_idle_ttl_seconds"] = 1800

    project = await _make_project(db_session)
    member = await _register(db_session, project, "agent-vm-fresh", PoolVmState.IDLE)
    member.idle_since = _utcnow()  # just went idle
    db_session.add(member)
    await db_session.commit()
    _shared_state.record_vm_heartbeat("agent-vm-fresh")

    retired = await sweep_idle_pool_vms(
        db_session, _utcnow(), runtime_config=_runtime_config
    )
    assert retired == 0
    mock_del.assert_not_awaited()
    assert await db_session.get(PoolVm, member.id) is not None


@pytest.mark.asyncio
async def test_idle_reaper_inert_when_disabled(db_session, mocker):
    mock_del = mocker.patch(
        "app.vm_lifecycle._ensure_vm_deleted", new_callable=mocker.AsyncMock
    )
    _runtime_config["warm_pool_enabled"] = False
    try:
        project = await _make_project(db_session)
        member = await _register(db_session, project, "agent-vm-off", PoolVmState.IDLE)
        member.idle_since = _utcnow() - timedelta(seconds=99999)
        db_session.add(member)
        await db_session.commit()

        retired = await sweep_idle_pool_vms(
            db_session, _utcnow(), runtime_config=_runtime_config
        )
        assert retired == 0
        mock_del.assert_not_awaited()
        assert await db_session.get(PoolVm, member.id) is not None
    finally:
        _runtime_config.pop("warm_pool_enabled", None)


# ---------------------------------------------------------------------------
# Deliverable 1: maintainer reconcile (drain / provision / recycle)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_reconcile_drains_excess_idle(db_session, warm_pool_on, mocker):
    mocker.patch("app.vm_lifecycle._ensure_vm_deleted", new_callable=mocker.AsyncMock)
    _runtime_config["warm_pool_target_size"] = 1

    project = await _make_project(db_session)
    m_old = await _register(db_session, project, "agent-vm-old", PoolVmState.IDLE)
    m_old.idle_since = _utcnow() - timedelta(seconds=500)
    m_new = await _register(db_session, project, "agent-vm-new", PoolVmState.IDLE)
    m_new.idle_since = _utcnow()
    db_session.add_all([m_old, m_new])
    await db_session.commit()

    async def _no_provision(session, project_id):
        raise AssertionError("must not provision when over target")

    summary = await reconcile_project_pool(
        db_session,
        project.id,
        runtime_config=_runtime_config,
        provision_fn=_no_provision,
        dispatch_slots_remaining=10,
        pool_budget_remaining=10,
        stale_vm_ids=set(),
        suppressed=False,
    )
    assert summary["drained"] == 1
    # The oldest-idle member is drained; the freshest survives.
    assert await db_session.get(PoolVm, m_old.id) is None
    assert await db_session.get(PoolVm, m_new.id) is not None


@pytest.mark.asyncio
async def test_reconcile_provisions_deficit_within_budgets(db_session, warm_pool_on):
    _runtime_config["warm_pool_target_size"] = 3
    project = await _make_project(db_session)
    await _register(db_session, project, "agent-vm-seed", PoolVmState.IDLE)

    calls = {"n": 0}

    async def _stub_provision(session, project_id):
        calls["n"] += 1
        return True

    # Deficit is 2 (target 3, have 1) but only 1 dispatch slot is free → cap at 1.
    summary = await reconcile_project_pool(
        db_session,
        project.id,
        runtime_config=_runtime_config,
        provision_fn=_stub_provision,
        dispatch_slots_remaining=1,
        pool_budget_remaining=10,
        stale_vm_ids=set(),
        suppressed=False,
    )
    assert summary["provisioned"] == 1
    assert calls["n"] == 1


@pytest.mark.asyncio
async def test_reconcile_recycles_stale_image(db_session, warm_pool_on, mocker):
    mock_del = mocker.patch(
        "app.vm_lifecycle._ensure_vm_deleted", new_callable=mocker.AsyncMock
    )
    _runtime_config["warm_pool_target_size"] = 1
    project = await _make_project(db_session)
    stale = await _register(db_session, project, "agent-vm-stale2", PoolVmState.IDLE)

    async def _no_provision(session, project_id):
        return False

    summary = await reconcile_project_pool(
        db_session,
        project.id,
        runtime_config=_runtime_config,
        provision_fn=_no_provision,
        dispatch_slots_remaining=10,
        pool_budget_remaining=10,
        stale_vm_ids={"agent-vm-stale2"},
        suppressed=False,
    )
    assert summary["recycled"] == 1
    mock_del.assert_awaited()
    assert await db_session.get(PoolVm, stale.id) is None


@pytest.mark.asyncio
async def test_reconcile_skips_provision_when_suppressed(db_session, warm_pool_on):
    _runtime_config["warm_pool_target_size"] = 3
    project = await _make_project(db_session)
    await _register(db_session, project, "agent-vm-seed2", PoolVmState.IDLE)

    async def _no_provision(session, project_id):
        raise AssertionError("circuit breaker tripped — must not provision")

    summary = await reconcile_project_pool(
        db_session,
        project.id,
        runtime_config=_runtime_config,
        provision_fn=_no_provision,
        dispatch_slots_remaining=10,
        pool_budget_remaining=10,
        stale_vm_ids=set(),
        suppressed=True,
    )
    assert summary["provisioned"] == 0


@pytest.mark.asyncio
async def test_cost_guard_drains_idle_and_skips_provision(
    db_session, warm_pool_on, mocker
):
    """When the cost guard is engaged, reconcile forces target 0 — every IDLE
    member is drained and no provisioning happens, regardless of the configured
    target."""
    mocker.patch("app.vm_lifecycle._ensure_vm_deleted", new_callable=mocker.AsyncMock)
    _runtime_config["warm_pool_target_size"] = 3
    project = await _make_project(db_session)
    idle = await _register(db_session, project, "agent-vm-guard", PoolVmState.IDLE)
    idle.idle_since = _utcnow() - timedelta(seconds=300)
    db_session.add(idle)
    await db_session.commit()

    async def _no_provision(session, project_id):
        raise AssertionError("cost guard engaged — must not provision")

    summary = await reconcile_project_pool(
        db_session,
        project.id,
        runtime_config=_runtime_config,
        provision_fn=_no_provision,
        dispatch_slots_remaining=10,
        pool_budget_remaining=10,
        stale_vm_ids=set(),
        suppressed=False,
        cost_guard_engaged=True,
    )
    assert summary["drained"] == 1
    assert summary["provisioned"] == 0
    assert await db_session.get(PoolVm, idle.id) is None


@pytest.mark.asyncio
async def test_scoped_project_is_reconciled_to_target(db_session, warm_pool_on):
    """A project IN the warm-pool allowlist is reconciled up to its configured
    target — scoping does not suppress provisioning for the scoped project."""
    _runtime_config["warm_pool_target_size"] = 2
    project = await _make_project(db_session)
    _runtime_config["warm_pool_project_allowlist"] = [str(project.id)]

    calls = {"n": 0}

    async def _stub_provision(session, project_id):
        calls["n"] += 1
        return True

    summary = await reconcile_project_pool(
        db_session,
        project.id,
        runtime_config=_runtime_config,
        provision_fn=_stub_provision,
        dispatch_slots_remaining=10,
        pool_budget_remaining=10,
        stale_vm_ids=set(),
        suppressed=False,
    )
    # In scope + target 2 + zero members → provision the full deficit.
    assert summary["provisioned"] == 2
    assert calls["n"] == 2


@pytest.mark.asyncio
async def test_unscoped_demand_active_project_provisions_zero(db_session, warm_pool_on):
    """With a non-empty allowlist, a demand-active project that is NOT listed
    provisions zero — every other project stays on the cold path while the pool
    is rolled out to the scoped project(s) only."""
    _runtime_config["warm_pool_target_size"] = 2
    _runtime_config["warm_pool_max_vms"] = 20
    _runtime_config["max_concurrent_vms"] = 10
    _runtime_config["warm_pool_demand_window_minutes"] = 30
    project = await _make_project(db_session)
    # Real dispatch demand, but the allowlist scopes the pool to a DIFFERENT project.
    await _make_task(db_session, project, TaskStatus.QUEUED)
    _runtime_config["warm_pool_project_allowlist"] = [str(uuid.uuid4())]

    async def _no_provision(session, project_id):
        raise AssertionError("unscoped project must not provision")

    totals = await run_pool_maintainer_cycle(
        runtime_config=_runtime_config,
        provision_fn=_no_provision,
        session_factory=_factory_for(db_session),
    )
    assert totals["provisioned"] == 0


@pytest.mark.asyncio
async def test_scoped_project_seeded_members_not_drained(
    db_session, warm_pool_on, mocker
):
    """A scoped project's manually-seeded members are held at target, NOT drained
    — the whole point of scoping (vs. the global-target-0 workaround that drains
    seeded members on the next tick)."""
    mocker.patch("app.vm_lifecycle._ensure_vm_deleted", new_callable=mocker.AsyncMock)
    _runtime_config["warm_pool_target_size"] = 2
    project = await _make_project(db_session)
    _runtime_config["warm_pool_project_allowlist"] = [str(project.id)]
    seeded = await _register(db_session, project, "agent-vm-seeded", PoolVmState.IDLE)
    seeded.idle_since = _utcnow() - timedelta(seconds=500)
    db_session.add(seeded)
    await db_session.commit()

    async def _no_provision(session, project_id):
        return False

    summary = await reconcile_project_pool(
        db_session,
        project.id,
        runtime_config=_runtime_config,
        provision_fn=_no_provision,
        dispatch_slots_remaining=10,
        pool_budget_remaining=10,
        stale_vm_ids=set(),
        suppressed=False,
    )
    # In scope, 1 idle <= target 2 → no drain; the seeded member survives.
    assert summary["drained"] == 0
    assert await db_session.get(PoolVm, seeded.id) is not None


@pytest.mark.asyncio
async def test_unscoped_project_idle_members_are_drained(
    db_session, warm_pool_on, mocker
):
    """The contrast to the scoped case: an UNSCOPED project's idle members are
    drained (effective target forced to 0) so narrowing the scope winds down
    out-of-scope pools instead of leaking idle cost."""
    mocker.patch("app.vm_lifecycle._ensure_vm_deleted", new_callable=mocker.AsyncMock)
    _runtime_config["warm_pool_target_size"] = 2
    project = await _make_project(db_session)
    _runtime_config["warm_pool_project_allowlist"] = [str(uuid.uuid4())]
    stray = await _register(db_session, project, "agent-vm-stray", PoolVmState.IDLE)
    stray.idle_since = _utcnow() - timedelta(seconds=500)
    db_session.add(stray)
    await db_session.commit()

    async def _no_provision(session, project_id):
        raise AssertionError("unscoped project must not provision")

    summary = await reconcile_project_pool(
        db_session,
        project.id,
        runtime_config=_runtime_config,
        provision_fn=_no_provision,
        dispatch_slots_remaining=10,
        pool_budget_remaining=10,
        stale_vm_ids=set(),
        suppressed=False,
    )
    # Out of scope → target forced to 0 → the idle member is drained.
    assert summary["drained"] == 1
    assert await db_session.get(PoolVm, stray.id) is None


@pytest.mark.asyncio
async def test_evaluate_cost_guard_engages_on_low_hit_rate(
    db_session, warm_pool_on, mocker
):
    """Guard engages when there is real idle spend, enough ELIGIBLE dispatches,
    and an eligible hit rate below the floor — and disengages once it recovers.
    The guard gates on the eligible rate (hits + pool-serviceable misses), NOT
    the raw fleet-wide rate, so a deliberately raw-low value here is paired with
    a matching eligible value to drive engagement."""
    mocker.patch(
        "app.efficiency_snapshot._compute_windowed_idle_cost",
        new_callable=mocker.AsyncMock,
        return_value={"total": 5.0, "by_account": {}},
    )
    snap = mocker.patch("app.warm_backlog_reuse.get_pool_funnel_snapshot")

    # Low eligible hit rate (2%) over 50 eligible dispatches with $5 idle → engage.
    snap.return_value = {
        "eligible_hit_rate": 0.02,
        "eligible_dispatches": 50,
        "pool_hit_rate": 0.02,
        "total_dispatches": 50,
    }
    engaged = await evaluate_pool_cost_guard(db_session, runtime_config=_runtime_config)
    assert engaged["engaged"] is True
    assert "below" in engaged["reason"]
    # The diagnostic carries the floors the panel renders the current values
    # against (default config: 10% hit-rate floor, $1 idle-spend floor).
    assert engaged["min_hit_rate"] == pytest.approx(0.10)
    assert engaged["min_idle_cost_usd"] == pytest.approx(1.0)
    # window_hours is the rolling window idle_cost_usd was computed over; the
    # panel labels the windowed figure so it cannot be misread as lifetime spend.
    assert engaged["window_hours"] == 24

    # Healthy eligible hit rate (40%) → disengage, provisioning resumes next tick.
    snap.return_value = {
        "eligible_hit_rate": 0.40,
        "eligible_dispatches": 50,
        "pool_hit_rate": 0.40,
        "total_dispatches": 50,
    }
    recovered = await evaluate_pool_cost_guard(
        db_session, runtime_config=_runtime_config
    )
    assert recovered["engaged"] is False


@pytest.mark.asyncio
async def test_evaluate_cost_guard_holds_below_min_dispatches(
    db_session, warm_pool_on, mocker
):
    """Guard stays disengaged on a thin sample even at a 0% eligible hit rate — it
    must not drain on noise before enough ELIGIBLE dispatches accumulate. A large
    raw fleet-wide volume must NOT substitute for eligible volume here."""
    mocker.patch(
        "app.efficiency_snapshot._compute_windowed_idle_cost",
        new_callable=mocker.AsyncMock,
        return_value={"total": 5.0, "by_account": {}},
    )
    mocker.patch(
        "app.warm_backlog_reuse.get_pool_funnel_snapshot",
        return_value={
            "eligible_hit_rate": 0.0,
            "eligible_dispatches": 3,
            # Raw fleet-wide volume is well over the floor, but it's mostly
            # out-of-scope noise — the guard must ignore it and hold.
            "pool_hit_rate": 0.0,
            "total_dispatches": 50,
        },
    )
    result = await evaluate_pool_cost_guard(db_session, runtime_config=_runtime_config)
    assert result["engaged"] is False


@pytest.mark.asyncio
async def test_evaluate_cost_guard_ignores_out_of_scope_noise(db_session, warm_pool_on):
    """The live regression: a pool scoped to one project sees its RAW fleet-wide
    hit rate crushed by other projects' dispatches, but its ELIGIBLE rate is
    healthy. The guard must read the eligible rate and stay disengaged — not
    drain a productive pool on out-of-scope noise. Exercises the real funnel
    end-to-end (no snapshot mock)."""
    from app.warm_backlog_reuse import (
        _pool_cold_dispatch_events,
        _pool_lease_hit_events,
        _pool_lease_miss_events,
        record_pool_cold_dispatch,
        record_pool_lease_hit,
        record_pool_lease_miss,
    )
    from app.vm_cost_rates import idle_cost_usd as _idle_cost_usd

    _pool_lease_hit_events.clear()
    _pool_cold_dispatch_events.clear()
    _pool_lease_miss_events.clear()

    project = await _make_project(db_session)
    member = await _register(db_session, project, "agent-vm-noise", PoolVmState.IDLE)
    now = _utcnow()
    member.idle_since = now - timedelta(days=10)
    # A settled span from 2 hours ago (within the 24h guard window).
    db_session.add(
        PoolIdleSpan(
            project_id=project.id,
            vm_instance_id="agent-vm-inwindow",
            started_at=now - timedelta(hours=3),
            settled_at=now - timedelta(hours=2),
            cost_usd=0.5,
        )
    )
    db_session.add(member)
    await db_session.commit()

    # 9 in-scope warm hits + 1 serviceable miss → eligible rate 90% (healthy).
    for _ in range(9):
        record_pool_lease_hit("acct-in-scope")
    record_pool_lease_miss("no_idle_member", "acct-in-scope")
    # 100 out-of-scope dispatches (other projects) crush the RAW rate to ~8%
    # (9 / 109), well below the 10% floor, while the eligible rate stays 90%.
    for _ in range(100):
        record_pool_lease_miss("out_of_scope", "acct-other")
        record_pool_cold_dispatch("acct-other")

    _runtime_config["warm_pool_cost_guard_min_dispatches"] = 20
    _runtime_config["warm_pool_cost_guard_min_hit_rate"] = 0.10
    _runtime_config["warm_pool_cost_guard_min_idle_cost_usd"] = 0.0

    guard = await evaluate_pool_cost_guard(db_session, runtime_config=_runtime_config)
    assert guard["raw_hit_rate"] is not None and guard["raw_hit_rate"] < 0.10
    assert guard["hit_rate"] == 0.9
    assert guard["engaged"] is False
    assert guard["idle_cost_usd"] == pytest.approx(
        0.5 + _idle_cost_usd(24 * 3600), rel=0.05
    )


@pytest.mark.asyncio
async def test_reconcile_provisions_despite_leased_members(db_session, warm_pool_on):
    """LEASED members are busy serving tasks and must NOT count against the IDLE
    target — otherwise the pool stops keeping warm capacity ready under load."""
    _runtime_config["warm_pool_target_size"] = 2
    project = await _make_project(db_session)
    # Two LEASED members (busy), zero IDLE. Pre-fix: live=2==target → deficit 0.
    await _register(db_session, project, "agent-vm-lease-a", PoolVmState.LEASED)
    await _register(db_session, project, "agent-vm-lease-b", PoolVmState.LEASED)

    calls = {"n": 0}

    async def _stub_provision(session, project_id):
        calls["n"] += 1
        return True

    summary = await reconcile_project_pool(
        db_session,
        project.id,
        runtime_config=_runtime_config,
        provision_fn=_stub_provision,
        dispatch_slots_remaining=10,
        pool_budget_remaining=10,
        stale_vm_ids=set(),
        suppressed=False,
    )
    # warm idle capacity is 0, target is 2 → replenish 2 warm VMs.
    assert summary["provisioned"] == 2
    assert calls["n"] == 2


@pytest.mark.asyncio
async def test_reconcile_keeps_last_idle_when_leased_coexists(
    db_session, warm_pool_on, mocker
):
    """A LEASED member coexisting with the last IDLE member must not push live
    over target and drain that IDLE member (pre-fix bug)."""
    mocker.patch("app.vm_lifecycle._ensure_vm_deleted", new_callable=mocker.AsyncMock)
    _runtime_config["warm_pool_target_size"] = 1
    project = await _make_project(db_session)
    idle = await _register(db_session, project, "agent-vm-keep-idle", PoolVmState.IDLE)
    idle.idle_since = _utcnow()
    db_session.add(idle)
    await db_session.commit()
    await _register(db_session, project, "agent-vm-keep-lease", PoolVmState.LEASED)

    async def _no_provision(session, project_id):
        return False

    summary = await reconcile_project_pool(
        db_session,
        project.id,
        runtime_config=_runtime_config,
        provision_fn=_no_provision,
        dispatch_slots_remaining=10,
        pool_budget_remaining=10,
        stale_vm_ids=set(),
        suppressed=False,
    )
    # warm idle capacity (1) == target (1): no drain, no provision.
    assert summary["drained"] == 0
    assert await db_session.get(PoolVm, idle.id) is not None


@pytest.mark.asyncio
async def test_run_pool_maintainer_cycle_provisions_within_budgets(
    db_session, warm_pool_on
):
    """run_pool_maintainer_cycle reconciles every project with members, capping
    provisioning by the fleet dispatch-slot + pool budget arithmetic it owns."""
    _runtime_config["warm_pool_target_size"] = 3
    _runtime_config["warm_pool_max_vms"] = 20
    _runtime_config["max_concurrent_vms"] = 10
    project = await _make_project(db_session)
    await _register(db_session, project, "agent-vm-cycle-seed", PoolVmState.IDLE)

    calls = {"n": 0}

    async def _stub_provision(session, project_id):
        calls["n"] += 1
        return True

    totals = await run_pool_maintainer_cycle(
        runtime_config=_runtime_config,
        provision_fn=_stub_provision,
        session_factory=_factory_for(db_session),
    )
    # Deficit is 2 (target 3, 1 idle seed); slot/pool budgets are ample → 2 provisioned.
    assert totals["provisioned"] == 2
    assert totals["projects"] == 1
    assert calls["n"] == 2


@pytest.mark.asyncio
async def test_run_pool_maintainer_cycle_inert_when_disabled(db_session):
    """The cycle early-returns zero totals when warm_pool_enabled is off."""
    _runtime_config["warm_pool_enabled"] = False
    try:

        async def _no_provision(session, project_id):
            raise AssertionError("must not provision when disabled")

        totals = await run_pool_maintainer_cycle(
            runtime_config=_runtime_config,
            provision_fn=_no_provision,
            session_factory=_factory_for(db_session),
        )
        assert totals == {
            "recycled": 0,
            "drained": 0,
            "provisioned": 0,
            "projects": 0,
        }
    finally:
        _runtime_config.pop("warm_pool_enabled", None)


# ---------------------------------------------------------------------------
# Bootstrap-from-zero: a demand-active project with ZERO live members forms a
# pool (the maintainer no longer reconciles ONLY members-bearing projects).
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_pool_maintainer_cycle_bootstraps_zero_member_queued_demand(
    db_session, warm_pool_on
):
    """A project with QUEUED backlog and ZERO pool members is bootstrapped up to
    target through the normal reconcile path (deficit = target - 0)."""
    _runtime_config["warm_pool_target_size"] = 2
    _runtime_config["warm_pool_max_vms"] = 20
    _runtime_config["max_concurrent_vms"] = 10
    _runtime_config["warm_pool_demand_window_minutes"] = 30
    project = await _make_project(db_session)
    # Real dispatch demand (QUEUED task), but no pool member exists yet.
    await _make_task(db_session, project, TaskStatus.QUEUED)

    calls = {"n": 0}

    async def _stub_provision(session, project_id):
        calls["n"] += 1
        return True

    totals = await run_pool_maintainer_cycle(
        runtime_config=_runtime_config,
        provision_fn=_stub_provision,
        session_factory=_factory_for(db_session),
    )
    # Zero members + target 2 → bootstrap the full target within ample budgets.
    assert totals["provisioned"] == 2
    assert totals["projects"] == 1
    assert calls["n"] == 2


@pytest.mark.asyncio
async def test_run_pool_maintainer_cycle_bootstraps_on_recent_cold_dispatch(
    db_session, warm_pool_on
):
    """A recent COLD dispatch (vm_dispatched_at within the window) is itself a
    demand signal — even with no QUEUED backlog and zero members."""
    _runtime_config["warm_pool_target_size"] = 2
    _runtime_config["warm_pool_max_vms"] = 20
    _runtime_config["max_concurrent_vms"] = 10
    _runtime_config["warm_pool_demand_window_minutes"] = 30
    project = await _make_project(db_session)
    # Terminal task whose VM was dispatched 5 min ago — within the demand window.
    task = await _make_task(db_session, project, TaskStatus.COMPLETED)
    task.vm_dispatched_at = _utcnow() - timedelta(minutes=5)
    db_session.add(task)
    await db_session.commit()

    calls = {"n": 0}

    async def _stub_provision(session, project_id):
        calls["n"] += 1
        return True

    totals = await run_pool_maintainer_cycle(
        runtime_config=_runtime_config,
        provision_fn=_stub_provision,
        session_factory=_factory_for(db_session),
    )
    assert totals["provisioned"] == 2
    assert totals["projects"] == 1


@pytest.mark.asyncio
async def test_run_pool_maintainer_cycle_skips_dormant_zero_member_project(
    db_session, warm_pool_on
):
    """A DORMANT project (no QUEUED backlog, dispatch outside the window, no
    members) must NEVER be seeded — blanket-seeding would explode spend."""
    _runtime_config["warm_pool_target_size"] = 2
    _runtime_config["warm_pool_demand_window_minutes"] = 30
    project = await _make_project(db_session)
    # Terminal task dispatched 2h ago — well outside the 30-min demand window.
    task = await _make_task(db_session, project, TaskStatus.COMPLETED)
    task.vm_dispatched_at = _utcnow() - timedelta(minutes=120)
    db_session.add(task)
    await db_session.commit()

    async def _no_provision(session, project_id):
        raise AssertionError("dormant zero-member project must not be seeded")

    totals = await run_pool_maintainer_cycle(
        runtime_config=_runtime_config,
        provision_fn=_no_provision,
        session_factory=_factory_for(db_session),
    )
    assert totals == {
        "recycled": 0,
        "drained": 0,
        "provisioned": 0,
        "projects": 0,
    }


@pytest.mark.asyncio
async def test_bootstrap_respects_dispatch_slot_budget(db_session, warm_pool_on):
    """Bootstrapping from zero still yields to the dispatch-slot headroom: a
    demand-active project never provisions into slots real dispatch needs."""
    _runtime_config["warm_pool_target_size"] = 3
    _runtime_config["warm_pool_max_vms"] = 20
    # Only 1 dispatch slot free fleet-wide (1 IN_PROGRESS task fills the other).
    _runtime_config["max_concurrent_vms"] = 2
    _runtime_config["warm_pool_demand_window_minutes"] = 30
    project = await _make_project(db_session)
    await _make_task(db_session, project, TaskStatus.QUEUED)  # demand
    await _make_task(db_session, project, TaskStatus.IN_PROGRESS)  # consumes a slot

    calls = {"n": 0}

    async def _stub_provision(session, project_id):
        calls["n"] += 1
        return True

    totals = await run_pool_maintainer_cycle(
        runtime_config=_runtime_config,
        provision_fn=_stub_provision,
        session_factory=_factory_for(db_session),
    )
    # max_concurrent 2 - 1 active task - 0 idle pool = 1 dispatch slot -> cap at 1.
    assert totals["provisioned"] == 1
    assert calls["n"] == 1


@pytest.mark.asyncio
async def test_bootstrap_respects_pool_max_budget(db_session, warm_pool_on):
    """The fleet-wide warm_pool_max_vms ceiling bounds bootstrap provisioning."""
    _runtime_config["warm_pool_target_size"] = 3
    _runtime_config["warm_pool_max_vms"] = 1  # only 1 live pool member allowed
    _runtime_config["max_concurrent_vms"] = 10
    _runtime_config["warm_pool_demand_window_minutes"] = 30
    project = await _make_project(db_session)
    await _make_task(db_session, project, TaskStatus.QUEUED)

    calls = {"n": 0}

    async def _stub_provision(session, project_id):
        calls["n"] += 1
        return True

    totals = await run_pool_maintainer_cycle(
        runtime_config=_runtime_config,
        provision_fn=_stub_provision,
        session_factory=_factory_for(db_session),
    )
    # pool budget = 1 - 0 live = 1 -> cap at 1 even though deficit is 3.
    assert totals["provisioned"] == 1


@pytest.mark.asyncio
async def test_bootstrap_counts_pending_leased_pool_vms_against_headroom(
    db_session, warm_pool_on
):
    """LEASED pool VMs with a QUEUED task must count against dispatch-slot headroom.

    The dispatch-to-claim handoff window creates pool VMs whose task remains
    QUEUED while the agent claims it via /next-role. These VMs occupy a real
    GCP slot but are invisible to both ``active_tasks`` (PROVISIONING/IN_PROGRESS
    only) and ``idle_pool`` (PROVISIONING/IDLE only). Without counting them, the
    maintainer can provision past max_concurrent_vms during a burst of pool hits.
    """
    _runtime_config["warm_pool_target_size"] = 3
    _runtime_config["warm_pool_max_vms"] = 20
    # Two slots total: one consumed by an IDLE pool member, one by a LEASED member
    # whose task is still QUEUED (in the handoff window).
    _runtime_config["max_concurrent_vms"] = 2
    _runtime_config["warm_pool_demand_window_minutes"] = 30

    project = await _make_project(db_session)
    # Demand signal so the maintainer considers this project.
    await _make_task(db_session, project, TaskStatus.QUEUED)

    # An IDLE member already holds one dispatch slot in the headroom formula.
    await _register(db_session, project, "agent-vm-idle-1", PoolVmState.IDLE)

    # A LEASED member in the handoff window: its task is QUEUED (not yet claimed).
    queued_task = await _make_task(db_session, project, TaskStatus.QUEUED)
    leased_member = await _register(
        db_session, project, "agent-vm-leased-1", PoolVmState.LEASED
    )
    leased_member.leased_task_id = queued_task.id
    db_session.add(leased_member)
    await db_session.commit()

    calls = {"n": 0}

    async def _stub_provision(session, project_id, **kw):
        calls["n"] += 1
        return True

    totals = await run_pool_maintainer_cycle(
        runtime_config=_runtime_config,
        provision_fn=_stub_provision,
        session_factory=_factory_for(db_session),
    )
    # max_concurrent 2 - 0 active tasks - 1 idle pool - 1 pending lease = 0 slots.
    # The maintainer must NOT provision any new members.
    assert totals["provisioned"] == 0
    assert calls["n"] == 0


@pytest.mark.asyncio
async def test_run_pool_maintainer_cycle_inert_with_demand_when_disabled(
    db_session,
):
    """Byte-identical inertness when warm_pool_enabled is off: even with live
    demand, the cycle early-returns zero totals and never queries/provisions."""
    _runtime_config["warm_pool_enabled"] = False
    try:
        project = await _make_project(db_session)
        await _make_task(db_session, project, TaskStatus.QUEUED)

        async def _no_provision(session, project_id):
            raise AssertionError("must not provision when warm pool disabled")

        totals = await run_pool_maintainer_cycle(
            runtime_config=_runtime_config,
            provision_fn=_no_provision,
            session_factory=_factory_for(db_session),
        )
        assert totals == {
            "recycled": 0,
            "drained": 0,
            "provisioned": 0,
            "projects": 0,
        }
    finally:
        _runtime_config.pop("warm_pool_enabled", None)


@pytest.mark.asyncio
async def test_demand_and_member_projects_both_reconcile_in_one_cycle(
    db_session, warm_pool_on
):
    """The union reconciles BOTH a demand-active zero-member project and a
    member-bearing (idle) project in a single tick, with no double-counting."""
    _runtime_config["warm_pool_target_size"] = 2
    _runtime_config["warm_pool_max_vms"] = 20
    _runtime_config["max_concurrent_vms"] = 10
    _runtime_config["warm_pool_demand_window_minutes"] = 30

    # Project A: zero members, QUEUED demand → bootstrap to target (2).
    project_a = await _make_project(db_session, name="Pool A")
    await _make_task(db_session, project_a, TaskStatus.QUEUED)
    # Project B: one existing idle member, no tasks → top up to target (1 more).
    project_b = await _make_project(db_session, name="Pool B")
    await _register(db_session, project_b, "agent-vm-b-seed", PoolVmState.IDLE)

    per_project = {}

    async def _stub_provision(session, project_id):
        per_project[project_id] = per_project.get(project_id, 0) + 1
        return True

    totals = await run_pool_maintainer_cycle(
        runtime_config=_runtime_config,
        provision_fn=_stub_provision,
        session_factory=_factory_for(db_session),
    )
    assert totals["projects"] == 2
    assert totals["provisioned"] == 3  # A: 2 (0→2), B: 1 (1→2)
    assert per_project[project_a.id] == 2
    assert per_project[project_b.id] == 1


@pytest.mark.asyncio
async def test_bootstrapped_member_idle_cost_is_observable(db_session, warm_pool_on):
    """A bootstrapped member flows through the normal register→idle lifecycle, so
    its idle cost is observable through the standard compute_pool_idle_cost path
    (no special bootstrap cost path)."""
    from app.efficiency_snapshot import compute_pool_idle_cost

    _runtime_config["warm_pool_target_size"] = 1
    _runtime_config["warm_pool_max_vms"] = 20
    _runtime_config["max_concurrent_vms"] = 10
    _runtime_config["warm_pool_demand_window_minutes"] = 30
    project = await _make_project(db_session)
    await _make_task(db_session, project, TaskStatus.QUEUED)  # demand, zero members

    async def _registering_provision(session, project_id):
        member = await register_pool_vm(
            session,
            vm_instance_id=f"agent-vm-boot-{uuid.uuid4().hex}",
            project_id=project_id,
            account_id="acct-1",
            provider="anthropic",
            model="claude-opus-4-8",
            state=PoolVmState.IDLE,
        )
        member.idle_since = _utcnow() - timedelta(seconds=600)
        session.add(member)
        return True

    totals = await run_pool_maintainer_cycle(
        runtime_config=_runtime_config,
        provision_fn=_registering_provision,
        session_factory=_factory_for(db_session),
    )
    assert totals["provisioned"] == 1
    # The bootstrapped member is a normal pool member: idle accrual shows up.
    cost = await compute_pool_idle_cost(db_session, project.id)
    assert cost["pool_size"] == 1
    assert cost["idle_count"] == 1
    assert cost["live_idle_cost_usd"] > 0


# ---------------------------------------------------------------------------
# Deliverable 5: pool circuit breaker
# ---------------------------------------------------------------------------


def test_pool_circuit_breaker_trips_after_threshold(warm_pool_on):
    from app.shared_state import (
        is_warm_pool_suppressed,
        record_pool_claim_failure,
    )

    _runtime_config["warm_pool_failure_rate_threshold"] = 3
    assert is_warm_pool_suppressed() is False
    for _ in range(3):
        record_pool_claim_failure("task_failed")
    assert is_warm_pool_suppressed() is True
    assert _shared_state.warm_pool_suppressed_until is not None


def test_all_warm_breakers_trip_and_reset_identically(warm_pool_on):
    """Verify all three warm-* suppression breakers trip and reset identically
    through the shared canonical helper given equal event rates and thresholds."""
    from app.shared_state import (
        is_warm_continuity_suppressed,
        is_warm_backlog_reuse_suppressed,
        is_warm_pool_suppressed,
        record_warm_pin_clear,
        record_backlog_reuse_failure,
        record_pool_claim_failure,
    )

    threshold = 3
    _runtime_config["warm_pin_clear_rate_threshold"] = threshold
    _runtime_config["warm_backlog_reuse_failure_rate_threshold"] = threshold
    _runtime_config["warm_pool_failure_rate_threshold"] = threshold
    _runtime_config["warm_continuity_cooldown_seconds"] = 1800
    _runtime_config["warm_backlog_reuse_cooldown_seconds"] = 1800
    _runtime_config["warm_pool_cooldown_seconds"] = 1800

    # All three breakers start unsuppressed
    assert is_warm_continuity_suppressed() is False
    assert is_warm_backlog_reuse_suppressed() is False
    assert is_warm_pool_suppressed() is False

    # Trip all three breakers with identical event counts
    for i in range(threshold):
        record_warm_pin_clear(f"vm-{i}", "test_reason")
        record_backlog_reuse_failure("test_reason")
        record_pool_claim_failure("test_reason")

    # All three trip simultaneously
    assert is_warm_continuity_suppressed() is True
    assert is_warm_backlog_reuse_suppressed() is True
    assert is_warm_pool_suppressed() is True

    # All three have deadlines set
    assert _shared_state.warm_continuity_suppressed_until is not None
    assert _shared_state.warm_backlog_reuse_suppressed_until is not None
    assert _shared_state.warm_pool_suppressed_until is not None

    # Deadlines are approximately equal (within a second)
    deadlines = [
        _shared_state.warm_continuity_suppressed_until,
        _shared_state.warm_backlog_reuse_suppressed_until,
        _shared_state.warm_pool_suppressed_until,
    ]
    deadline_diffs = [
        abs((deadlines[i] - deadlines[0]).total_seconds())
        for i in range(1, len(deadlines))
    ]
    assert all(diff < 1 for diff in deadline_diffs), (
        f"Deadline drift detected: {deadline_diffs}"
    )

    # Subsequent checks return suppressed (deadline not yet expired)
    assert is_warm_continuity_suppressed() is True
    assert is_warm_backlog_reuse_suppressed() is True
    assert is_warm_pool_suppressed() is True

    # Manually advance time past deadlines to test reset
    now = _utcnow()
    future = now + timedelta(seconds=2000)
    _shared_state.warm_continuity_suppressed_until = future
    _shared_state.warm_backlog_reuse_suppressed_until = future
    _shared_state.warm_pool_suppressed_until = future

    # All still suppressed while deadline not reached
    assert is_warm_continuity_suppressed() is True
    assert is_warm_backlog_reuse_suppressed() is True
    assert is_warm_pool_suppressed() is True

    # Clear event history so they don't trip again when we check
    _shared_state.warm_pin_clear_events.clear()
    _shared_state.warm_backlog_reuse_failure_events.clear()
    _shared_state.warm_pool_claim_failure_events.clear()

    # Manually set deadlines to past to simulate expiry
    past = _utcnow() - timedelta(seconds=1)
    _shared_state.warm_continuity_suppressed_until = past
    _shared_state.warm_backlog_reuse_suppressed_until = past
    _shared_state.warm_pool_suppressed_until = past

    # All reset to unsuppressed after deadline passes
    assert is_warm_continuity_suppressed() is False
    assert is_warm_backlog_reuse_suppressed() is False
    assert is_warm_pool_suppressed() is False

    # Deadlines are cleared
    assert _shared_state.warm_continuity_suppressed_until is None
    assert _shared_state.warm_backlog_reuse_suppressed_until is None
    assert _shared_state.warm_pool_suppressed_until is None


def test_is_pool_vm_image_stale_helper():
    member = PoolVm(vm_instance_id="agent-vm-x", project_id=uuid.uuid4())
    assert is_pool_vm_image_stale(member, {"agent-vm-x"}) is True
    assert is_pool_vm_image_stale(member, {"agent-vm-y"}) is False


# ---------------------------------------------------------------------------
# Per-project target resolution
# ---------------------------------------------------------------------------


def test_resolve_project_warm_pool_target_falls_back_to_global():
    """With no per-project entry the resolver returns the global target."""
    pid = uuid.uuid4()
    _runtime_config["warm_pool_target_size"] = 4
    _runtime_config["warm_pool_target_size_by_project"] = {}
    assert resolve_project_warm_pool_target(pid) == 4


def test_resolve_project_warm_pool_target_uses_explicit_value():
    """An explicit per-project entry overrides the global target."""
    pid = uuid.uuid4()
    _runtime_config["warm_pool_target_size"] = 4
    _runtime_config["warm_pool_target_size_by_project"] = {str(pid): 1}
    assert resolve_project_warm_pool_target(pid) == 1
    # Explicit 0 is honored (not treated as "unset").
    _runtime_config["warm_pool_target_size_by_project"] = {str(pid): 0}
    assert resolve_project_warm_pool_target(pid) == 0


def test_warm_pool_project_target_map_normalizes_and_clamps():
    """The map helper trims keys, coerces ints, clamps to 0..50, drops garbage."""
    _runtime_config["warm_pool_target_size_by_project"] = {
        " p-a ": "3",
        "p-b": 99,  # clamped to 50
        "p-c": -5,  # clamped to 0
        "": 2,  # dropped (blank key)
        "p-d": "nope",  # dropped (non-int)
    }
    result = warm_pool_project_target_map()
    assert result == {"p-a": 3, "p-b": 50, "p-c": 0}


@pytest.mark.asyncio
async def test_run_pool_maintainer_cycle_reconciles_projects_to_different_targets(
    db_session, warm_pool_on
):
    """In ONE cycle, two projects reconcile toward DIFFERENT targets: one from the
    explicit per-project map, the other from the global fallback."""
    _runtime_config["warm_pool_target_size"] = 1  # global fallback for project B
    _runtime_config["warm_pool_max_vms"] = 20
    _runtime_config["max_concurrent_vms"] = 10
    _runtime_config["warm_pool_demand_window_minutes"] = 30

    project_a = await _make_project(db_session, name="Pool A")
    project_b = await _make_project(db_session, name="Pool B")
    # Project A: explicit per-project target of 3; Project B: no entry → global 1.
    _runtime_config["warm_pool_target_size_by_project"] = {str(project_a.id): 3}
    # Both demand-active (QUEUED backlog), both start with zero members.
    await _make_task(db_session, project_a, TaskStatus.QUEUED)
    await _make_task(db_session, project_b, TaskStatus.QUEUED)

    per_project = {}

    async def _stub_provision(session, project_id, account_id=None):
        per_project[project_id] = per_project.get(project_id, 0) + 1
        return True

    totals = await run_pool_maintainer_cycle(
        runtime_config=_runtime_config,
        provision_fn=_stub_provision,
        session_factory=_factory_for(db_session),
    )
    assert totals["projects"] == 2
    # Project A reconciles to its explicit target 3; B to the global fallback 1.
    assert per_project[project_a.id] == 3
    assert per_project[project_b.id] == 1
    assert totals["provisioned"] == 4


# ---------------------------------------------------------------------------
# Concurrency: atomic compare-and-swap claim (no double-teardown)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_retire_skips_member_leased_after_scan(db_session, warm_pool_on, mocker):
    """A member a dispatcher LEASED between the candidate scan and the teardown
    must NOT be torn down — the atomic claim's state guard skips it."""
    mock_del = mocker.patch(
        "app.vm_lifecycle._ensure_vm_deleted", new_callable=mocker.AsyncMock
    )
    project = await _make_project(db_session)
    member = await _register(db_session, project, "agent-vm-raced", PoolVmState.IDLE)

    # Simulate the race: a peer leases the member (IDLE → LEASED) after the
    # reaper scanned it as an idle-TTL candidate.
    member.state = PoolVmState.LEASED
    db_session.add(member)
    await db_session.commit()

    won = await retire_and_delete_pool_vm(db_session, member, reason="pool_idle_ttl")
    assert won is False
    mock_del.assert_not_awaited()
    surviving = await db_session.get(PoolVm, member.id)
    assert surviving is not None and surviving.state == PoolVmState.LEASED


# ---------------------------------------------------------------------------
# Stranded-lease reclaim: a LEASED member whose task finished must be released
# (else the pool self-exhausts — LEASED rows count as live forever).
# ---------------------------------------------------------------------------


async def _make_task(db_session, project, status):
    task = Task(
        project_id=project.id,
        title="pool lease",
        payloads=[{"type": "create", "prompt": "x", "base_branch": "automain"}],
        status=status,
        context={},
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)
    return task


@pytest.mark.asyncio
async def test_idle_reaper_reclaims_orphaned_lease(db_session, warm_pool_on, mocker):
    """A LEASED member whose lease expired, whose task is terminal, and whose VM
    is no longer heartbeating is reclaimed so the pool stops self-exhausting."""
    mock_del = mocker.patch(
        "app.vm_lifecycle._ensure_vm_deleted", new_callable=mocker.AsyncMock
    )
    mocker.patch("app.compute.get_stale_image_vms", return_value=[])
    _runtime_config["warm_pool_heartbeat_stale_seconds"] = 300

    project = await _make_project(db_session)
    task = await _make_task(db_session, project, TaskStatus.COMPLETED)
    member = await _register(db_session, project, "agent-vm-orphan", PoolVmState.LEASED)
    member.leased_task_id = task.id
    member.lease_expires_at = _utcnow() - timedelta(seconds=10)  # handoff window passed
    db_session.add(member)
    await db_session.commit()
    # No heartbeat recorded → VM is not alive (torn down on completion).

    retired = await sweep_idle_pool_vms(
        db_session, _utcnow(), runtime_config=_runtime_config
    )
    assert retired == 1
    mock_del.assert_awaited()
    assert await db_session.get(PoolVm, member.id) is None


@pytest.mark.asyncio
async def test_idle_reaper_spares_lease_with_running_task(
    db_session, warm_pool_on, mocker
):
    """A LEASED member whose task is still IN_PROGRESS (lease expired but the agent
    claimed it and is running) must NOT be torn down mid-task."""
    mock_del = mocker.patch(
        "app.vm_lifecycle._ensure_vm_deleted", new_callable=mocker.AsyncMock
    )
    mocker.patch("app.compute.get_stale_image_vms", return_value=[])

    project = await _make_project(db_session)
    task = await _make_task(db_session, project, TaskStatus.IN_PROGRESS)
    member = await _register(db_session, project, "agent-vm-busy", PoolVmState.LEASED)
    member.leased_task_id = task.id
    member.lease_expires_at = _utcnow() - timedelta(seconds=10)
    db_session.add(member)
    await db_session.commit()

    retired = await sweep_idle_pool_vms(
        db_session, _utcnow(), runtime_config=_runtime_config
    )
    assert retired == 0
    mock_del.assert_not_awaited()
    survivor = await db_session.get(PoolVm, member.id)
    assert survivor is not None and survivor.state == PoolVmState.LEASED


@pytest.mark.asyncio
async def test_idle_reaper_spares_lease_with_live_vm(db_session, warm_pool_on, mocker):
    """A LEASED member whose task is terminal but whose VM is STILL heartbeating
    (deferred + reused for backlog work) must NOT be torn down."""
    mock_del = mocker.patch(
        "app.vm_lifecycle._ensure_vm_deleted", new_callable=mocker.AsyncMock
    )
    mocker.patch("app.compute.get_stale_image_vms", return_value=[])
    _runtime_config["warm_pool_heartbeat_stale_seconds"] = 300

    project = await _make_project(db_session)
    task = await _make_task(db_session, project, TaskStatus.COMPLETED)
    member = await _register(db_session, project, "agent-vm-reused", PoolVmState.LEASED)
    member.leased_task_id = task.id
    member.lease_expires_at = _utcnow() - timedelta(seconds=10)
    db_session.add(member)
    await db_session.commit()
    _shared_state.record_vm_heartbeat("agent-vm-reused")  # VM is alive → spared

    retired = await sweep_idle_pool_vms(
        db_session, _utcnow(), runtime_config=_runtime_config
    )
    assert retired == 0
    mock_del.assert_not_awaited()
    assert await db_session.get(PoolVm, member.id) is not None


@pytest.mark.asyncio
async def test_retire_defers_to_peer_fresh_retiring(db_session, warm_pool_on, mocker):
    """A RETIRING row freshly claimed by a live peer (recent updated_at) is NOT
    adopted by a cleanup pass — no duplicate ~240s _ensure_vm_deleted."""
    mock_del = mocker.patch(
        "app.vm_lifecycle._ensure_vm_deleted", new_callable=mocker.AsyncMock
    )
    project = await _make_project(db_session)
    member = await _register(
        db_session, project, "agent-vm-peerretire", PoolVmState.IDLE
    )
    # Peer just flipped it to RETIRING (fresh updated_at).
    member.state = PoolVmState.RETIRING
    member.updated_at = _utcnow()
    db_session.add(member)
    await db_session.commit()

    won = await retire_and_delete_pool_vm(
        db_session,
        member,
        reason="pool_retiring_cleanup",
        claim_grace_seconds=300,
    )
    assert won is False
    mock_del.assert_not_awaited()
    assert await db_session.get(PoolVm, member.id) is not None


@pytest.mark.asyncio
async def test_retire_adopts_stuck_retiring_row(db_session, warm_pool_on, mocker):
    """A RETIRING row left stuck by a crashed prior teardown (stale updated_at)
    IS adopted and finished by the cleanup pass."""
    mock_del = mocker.patch(
        "app.vm_lifecycle._ensure_vm_deleted", new_callable=mocker.AsyncMock
    )
    project = await _make_project(db_session)
    member = await _register(db_session, project, "agent-vm-stuck", PoolVmState.IDLE)
    member.state = PoolVmState.RETIRING
    member.updated_at = _utcnow() - timedelta(seconds=600)  # older than grace
    db_session.add(member)
    await db_session.commit()

    won = await retire_and_delete_pool_vm(
        db_session,
        member,
        reason="pool_retiring_cleanup",
        claim_grace_seconds=300,
    )
    assert won is True
    mock_del.assert_awaited()
    assert await db_session.get(PoolVm, member.id) is None


# ---------------------------------------------------------------------------
# Demand-weighted provisioning across a project's account mix: the same small
# idle pool must SPREAD across the accounts that have recent demand rather than
# piling every member onto the first eligible account.
# ---------------------------------------------------------------------------


class _PoolAccounts:
    """Seed several in-memory max-plan accounts under the test project owner
    (``u-pool``, matching ``_make_project``) and tear them down afterward."""

    def __init__(self, accounts):
        # accounts: list of dicts merged onto a ready Anthropic-OAuth default.
        self._specs = accounts

    async def __aenter__(self):
        from app.max_plan_pool import (
            _account_by_id,
            _max_plan_pool_lock,
            _max_plan_pools,
        )

        self.accounts = []
        async with _max_plan_pool_lock:
            for spec in self._specs:
                acct = {
                    "user_id": "u-pool",
                    "name": spec["id"],
                    "enabled": True,
                    "oauth_token": "tok",  # makes _account_runtime_ready True
                    "provider": "anthropic",
                    "auth_mode": "oauth",
                    "active_tasks": 0,
                    **spec,
                }
                _max_plan_pools.setdefault("u-pool", []).append(acct)
                _account_by_id[str(acct["id"])] = acct
                self.accounts.append(acct)
        return self.accounts

    async def __aexit__(self, *exc):
        from app.max_plan_pool import (
            _account_by_id,
            _max_plan_pool_lock,
            _max_plan_pools,
        )

        async with _max_plan_pool_lock:
            for acct in self.accounts:
                _account_by_id.pop(str(acct["id"]), None)
            _max_plan_pools.pop("u-pool", None)
        return False


async def _make_task_for_account(
    db_session, project, account_id, status=TaskStatus.QUEUED
):
    """A demand-signal task whose context attributes it to ``account_id``."""
    task = Task(
        project_id=project.id,
        title="demand",
        payloads=[{"type": "create", "prompt": "x", "base_branch": "automain"}],
        status=status,
        context={"llm_account_id": account_id, "max_plan_account_id": account_id},
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)
    return task


@pytest.mark.asyncio
async def test_reconcile_spreads_provision_across_demand_accounts(
    db_session, warm_pool_on
):
    """Given QUEUED demand across three accounts and zero idle members, the
    deficit is distributed across all three accounts — not piled onto one."""
    _runtime_config["warm_pool_target_size"] = 3
    _runtime_config["warm_pool_demand_window_minutes"] = 30
    project = await _make_project(db_session)
    for acct_id in ("acct-a", "acct-b", "acct-c"):
        await _make_task_for_account(db_session, project, acct_id)

    booted: dict = {}

    async def _stub_provision(session, project_id, *, account_id=None):
        booted[account_id] = booted.get(account_id, 0) + 1
        return True

    async with _PoolAccounts([{"id": "acct-a"}, {"id": "acct-b"}, {"id": "acct-c"}]):
        summary = await reconcile_project_pool(
            db_session,
            project.id,
            runtime_config=_runtime_config,
            provision_fn=_stub_provision,
            dispatch_slots_remaining=10,
            pool_budget_remaining=10,
            stale_vm_ids=set(),
            suppressed=False,
        )
    # Total boots unchanged (deficit = target 3 - 0 idle), spread one per account.
    assert summary["provisioned"] == 3
    assert booted == {"acct-a": 1, "acct-b": 1, "acct-c": 1}


@pytest.mark.asyncio
async def test_reconcile_never_boots_account_without_demand(db_session, warm_pool_on):
    """An eligible account with NO recent demand is never booted; the deficit goes
    to the demand-active account even when other accounts are ready."""
    _runtime_config["warm_pool_target_size"] = 2
    _runtime_config["warm_pool_demand_window_minutes"] = 30
    project = await _make_project(db_session)
    # Only acct-a has demand; acct-b is ready but idle in the mix.
    await _make_task_for_account(db_session, project, "acct-a")

    booted: dict = {}

    async def _stub_provision(session, project_id, *, account_id=None):
        booted[account_id] = booted.get(account_id, 0) + 1
        return True

    async with _PoolAccounts([{"id": "acct-a"}, {"id": "acct-b"}]):
        summary = await reconcile_project_pool(
            db_session,
            project.id,
            runtime_config=_runtime_config,
            provision_fn=_stub_provision,
            dispatch_slots_remaining=10,
            pool_budget_remaining=10,
            stale_vm_ids=set(),
            suppressed=False,
        )
    assert summary["provisioned"] == 2
    # All boots go to the only demand-active account; acct-b (no demand) is never chosen.
    assert booted == {"acct-a": 2}


@pytest.mark.asyncio
async def test_reconcile_deprioritizes_over_represented_account(
    db_session, warm_pool_on
):
    """An account already over-represented among current idle members is
    deprioritized so the next boot broadens coverage to the under-served account."""
    _runtime_config["warm_pool_target_size"] = 3
    _runtime_config["warm_pool_demand_window_minutes"] = 30
    project = await _make_project(db_session)
    # Equal demand on both accounts...
    await _make_task_for_account(db_session, project, "acct-a")
    await _make_task_for_account(db_session, project, "acct-b")
    # ...but acct-a already has 2 idle members → only 1 deficit slot, and it must
    # go to the under-served acct-b.
    await _register(
        db_session, project, "agent-vm-a1", PoolVmState.IDLE, account_id="acct-a"
    )
    await _register(
        db_session, project, "agent-vm-a2", PoolVmState.IDLE, account_id="acct-a"
    )

    booted: dict = {}

    async def _stub_provision(session, project_id, *, account_id=None):
        booted[account_id] = booted.get(account_id, 0) + 1
        return True

    async with _PoolAccounts([{"id": "acct-a"}, {"id": "acct-b"}]):
        summary = await reconcile_project_pool(
            db_session,
            project.id,
            runtime_config=_runtime_config,
            provision_fn=_stub_provision,
            dispatch_slots_remaining=10,
            pool_budget_remaining=10,
            stale_vm_ids=set(),
            suppressed=False,
        )
    # Deficit is target 3 - 2 idle = 1, and it broadens to the under-served account.
    assert summary["provisioned"] == 1
    assert booted == {"acct-b": 1}


@pytest.mark.asyncio
async def test_reconcile_skips_at_cap_and_cooldown_accounts(db_session, warm_pool_on):
    """At-cap and rate-limit-cooldown accounts are excluded from distribution; the
    next eligible demand-active account is chosen instead."""
    _runtime_config["warm_pool_target_size"] = 2
    _runtime_config["warm_pool_demand_window_minutes"] = 30
    project = await _make_project(db_session)
    for acct_id in ("acct-cap", "acct-cool", "acct-ok"):
        await _make_task_for_account(db_session, project, acct_id)

    booted: dict = {}

    async def _stub_provision(session, project_id, *, account_id=None):
        booted[account_id] = booted.get(account_id, 0) + 1
        return True

    # acct-cap is at its concurrent cap; acct-cool is in rate-limit cooldown.
    async with _PoolAccounts(
        [
            {"id": "acct-cap", "max_concurrent_tasks": 1, "active_tasks": 1},
            {
                "id": "acct-cool",
                "disabled_models": {"claude-opus-4-8": {"at": _utcnow()}},
            },
            {"id": "acct-ok"},
        ]
    ):
        summary = await reconcile_project_pool(
            db_session,
            project.id,
            runtime_config=_runtime_config,
            provision_fn=_stub_provision,
            dispatch_slots_remaining=10,
            pool_budget_remaining=10,
            stale_vm_ids=set(),
            suppressed=False,
        )
    assert summary["provisioned"] == 2
    # Only the one bootable account is chosen — capped/cooldown accounts excluded.
    assert booted == {"acct-ok": 2}
    assert "acct-cap" not in booted and "acct-cool" not in booted


@pytest.mark.asyncio
async def test_reconcile_falls_back_to_greedy_without_account_context(
    db_session, warm_pool_on
):
    """When demand carries no resolvable account (no llm_account_id in context),
    reconcile falls back to the greedy provision call (no account_id kwarg)."""
    _runtime_config["warm_pool_target_size"] = 2
    _runtime_config["warm_pool_demand_window_minutes"] = 30
    project = await _make_project(db_session)
    # QUEUED demand but NO account attribution in context.
    await _make_task(db_session, project, TaskStatus.QUEUED)

    seen_account_kwarg = []

    async def _stub_provision(session, project_id, *, account_id=None):
        seen_account_kwarg.append(account_id)
        return True

    async with _PoolAccounts([{"id": "acct-a"}]):
        summary = await reconcile_project_pool(
            db_session,
            project.id,
            runtime_config=_runtime_config,
            provision_fn=_stub_provision,
            dispatch_slots_remaining=10,
            pool_budget_remaining=10,
            stale_vm_ids=set(),
            suppressed=False,
        )
    assert summary["provisioned"] == 2
    # Greedy fallback: account_id was never steered (all None).
    assert seen_account_kwarg == [None, None]


@pytest.mark.asyncio
async def test_demand_weighting_never_exceeds_global_target(db_session, warm_pool_on):
    """Demand-weighting redistributes WHICH accounts warm, never HOW many: total
    boots stay bounded by the global per-project target regardless of how much
    multi-account demand exists."""
    _runtime_config["warm_pool_target_size"] = 2
    _runtime_config["warm_pool_demand_window_minutes"] = 30
    project = await _make_project(db_session)
    # Heavy demand across many accounts, far exceeding the target.
    for acct_id in ("acct-a", "acct-b", "acct-c", "acct-d"):
        await _make_task_for_account(db_session, project, acct_id)
        await _make_task_for_account(db_session, project, acct_id)

    total = {"n": 0}

    async def _stub_provision(session, project_id, *, account_id=None):
        total["n"] += 1
        return True

    async with _PoolAccounts(
        [{"id": "acct-a"}, {"id": "acct-b"}, {"id": "acct-c"}, {"id": "acct-d"}]
    ):
        summary = await reconcile_project_pool(
            db_session,
            project.id,
            runtime_config=_runtime_config,
            provision_fn=_stub_provision,
            dispatch_slots_remaining=10,
            pool_budget_remaining=10,
            stale_vm_ids=set(),
            suppressed=False,
        )
    # Bounded by target (2), not by total demand (8).
    assert summary["provisioned"] == 2
    assert total["n"] == 2


# ---------------------------------------------------------------------------
# Durable pool liveness + QUEUED-pin lease reclaim
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_reaper_uses_durable_pool_liveness_and_reclaims_expired_queued_lease(
    db_session, warm_pool_on, mocker
):
    """Dual assertion:

    (a) Durable liveness: a LEASED member with a recent PoolVm.last_heartbeat_at
        but NO in-memory heartbeat is spared — a process restart (empty cache)
        must not destroy a live pool member whose durable stamp is within the
        staleness window.

    (b) QUEUED-pin reclaim: a LEASED member with an expired lease, a dead VM (no
        in-memory heartbeat AND stale last_heartbeat_at), and a QUEUED task whose
        context carries warm_pool_pin is reclaimed AND the task's warm_target_vm_id
        / warm_pool_pin context keys are cleared so the orchestrator can cold-
        dispatch it instead of waiting indefinitely for the dead VM.
    """
    from app.warm_pool import WARM_POOL_PIN_KEY

    mock_del = mocker.patch(
        "app.vm_lifecycle._ensure_vm_deleted", new_callable=mocker.AsyncMock
    )
    mocker.patch("app.compute.get_stale_image_vms", return_value=[])
    _runtime_config["warm_pool_heartbeat_stale_seconds"] = 300

    project = await _make_project(db_session)

    # (a) Durable-live member: recent last_heartbeat_at (60s ago, within 300s
    # window) but NO in-memory heartbeat — simulates a fresh leader whose cache
    # is empty. The durable stamp alone must keep this member alive.
    live_task = await _make_task(db_session, project, TaskStatus.COMPLETED)
    live_member = await _register(
        db_session, project, "agent-vm-durable-live", PoolVmState.LEASED
    )
    live_member.leased_task_id = live_task.id
    live_member.lease_expires_at = _utcnow() - timedelta(seconds=10)
    live_member.last_heartbeat_at = _utcnow() - timedelta(seconds=60)  # recent durable
    db_session.add(live_member)

    # (b) Dead QUEUED-pin member: stale last_heartbeat_at (600s > 300s window),
    # no in-memory heartbeat → VM is dead. Task is still QUEUED with warm_pool_pin
    # pointing at this dead member. Must be reclaimed and the task context cleared.
    pin_task = Task(
        project_id=project.id,
        title="pool pin",
        payloads=[{"type": "create", "prompt": "x", "base_branch": "automain"}],
        status=TaskStatus.QUEUED,
        context={
            "warm_target_vm_id": "agent-vm-dead-lease",
            WARM_POOL_PIN_KEY: True,
        },
    )
    db_session.add(pin_task)
    await db_session.commit()
    await db_session.refresh(pin_task)

    dead_member = await _register(
        db_session, project, "agent-vm-dead-lease", PoolVmState.LEASED
    )
    dead_member.leased_task_id = pin_task.id
    dead_member.lease_expires_at = _utcnow() - timedelta(seconds=10)
    dead_member.last_heartbeat_at = _utcnow() - timedelta(seconds=600)  # stale
    db_session.add(dead_member)
    await db_session.commit()
    # No in-memory heartbeat for either VM — only the durable stamp distinguishes them.

    retired = await sweep_idle_pool_vms(
        db_session, _utcnow(), runtime_config=_runtime_config
    )

    # (a) Durable-live member must be spared — durable heartbeat is within window.
    assert await db_session.get(PoolVm, live_member.id) is not None, (
        "member with recent last_heartbeat_at must be spared (durable liveness)"
    )

    # (b) Dead QUEUED-pin member must be reclaimed (only 1 retire expected).
    assert retired == 1, f"expected exactly 1 retired member, got {retired}"
    assert await db_session.get(PoolVm, dead_member.id) is None, (
        "LEASED member with stale durable hb and QUEUED-pin task must be reclaimed"
    )
    mock_del.assert_awaited_once()

    # (b) QUEUED task must stay QUEUED (not terminated) and have its warm-pool
    # pin fields cleared so the orchestrator can cold-dispatch it.
    await db_session.refresh(pin_task)
    assert pin_task.status == TaskStatus.QUEUED, (
        "reclaimed QUEUED task must remain QUEUED — cold dispatch picks it up"
    )
    task_ctx = pin_task.context or {}
    assert "warm_target_vm_id" not in task_ctx, (
        "warm_target_vm_id must be cleared from the QUEUED task context after reclaim"
    )
    assert WARM_POOL_PIN_KEY not in task_ctx, (
        "warm_pool_pin must be cleared from the QUEUED task context after reclaim"
    )


@pytest.mark.asyncio
async def test_reaper_orphan_cleanup_reverts_owned_pool_reservation_once(
    db_session, warm_pool_on, mocker
):
    """Maintainer reclaim owns pin, lease, and reservation abandonment together."""
    from app.max_plan_pool import reserve_max_plan_account
    from app.warm_pool import (
        WARM_POOL_PIN_KEY,
        WARM_POOL_RESERVATION_ACCOUNT_KEY,
    )

    mocker.patch(
        "app.vm_lifecycle._ensure_vm_deleted", new_callable=mocker.AsyncMock
    )
    mocker.patch("app.compute.get_stale_image_vms", return_value=[])
    _runtime_config["warm_pool_heartbeat_stale_seconds"] = 300
    project = await _make_project(db_session, name="Owned Orphan Reservation")

    async with _PoolAccounts(
        [
            {
                "id": "acct-owned-orphan",
                "model": "claude-opus-4-8",
                "daily_task_count": 0,
                "project_state": {},
                "disabled_models": {},
            }
        ]
    ) as accounts:
        account = accounts[0]
        assert await reserve_max_plan_account(
            account["id"],
            user_id=project.user_id,
            project_id=str(project.id),
            model="claude-opus-4-8",
            count_daily_task=True,
        )
        task = Task(
            project_id=project.id,
            title="owned orphan pin",
            payloads=[{"type": "create", "prompt": "x"}],
            status=TaskStatus.QUEUED,
            context={
                "warm_target_vm_id": "agent-vm-owned-orphan",
                WARM_POOL_PIN_KEY: True,
                WARM_POOL_RESERVATION_ACCOUNT_KEY: account["id"],
            },
        )
        db_session.add(task)
        await db_session.commit()
        await db_session.refresh(task)

        member = await _register(
            db_session,
            project,
            "agent-vm-owned-orphan",
            PoolVmState.LEASED,
            account_id=account["id"],
        )
        member.leased_task_id = task.id
        member.lease_expires_at = _utcnow() - timedelta(seconds=10)
        member.last_heartbeat_at = _utcnow() - timedelta(seconds=600)
        db_session.add(member)
        await db_session.commit()

        assert await sweep_idle_pool_vms(
            db_session, _utcnow(), runtime_config=_runtime_config
        ) == 1
        await db_session.refresh(task)
        assert task.status == TaskStatus.QUEUED
        assert task.vm_instance_id is None
        assert (task.context or {}).get(WARM_POOL_PIN_KEY) is None
        assert (
            task.context or {}
        ).get(WARM_POOL_RESERVATION_ACCOUNT_KEY) is None
        assert account["active_tasks"] == 0
        assert account["daily_task_count"] == 0
        assert account["project_state"][str(project.id)] == {
            "active_tasks": 0,
            "daily_task_count": 0,
        }
        mutations = list(account["_pending_usage_cap_mutations"])
        assert [item["delta"] for item in mutations] == [1, -1]
        assert [item["project_delta"] for item in mutations] == [1, -1]

        # The reclaimed row is gone; another sweep cannot release the marker
        # again because the first cleanup committed its removal.
        assert await sweep_idle_pool_vms(
            db_session, _utcnow(), runtime_config=_runtime_config
        ) == 0
        assert account["_pending_usage_cap_mutations"] == mutations


@pytest.mark.asyncio
async def test_default_provision_fn_honors_requested_account(
    db_session, warm_pool_on, mocker
):
    """_default_pool_provision_fn boots the REQUESTED account when account_id is
    given, and skips when that account is not eligible."""
    boot_calls = []

    async def _fake_provision_pool_vm(session, vm_instance_id, project_id, **kwargs):
        boot_calls.append(kwargs.get("account_id"))

        class _Member:
            pass

        return _Member()

    mocker.patch("app.compute.provision_pool_vm", side_effect=_fake_provision_pool_vm)
    project = await _make_project(db_session)

    async with _PoolAccounts([{"id": "acct-a"}, {"id": "acct-b"}]):
        ok = await _wpm._default_pool_provision_fn(
            db_session, project.id, account_id="acct-b"
        )
    assert ok is True
    assert boot_calls == ["acct-b"]

    # Requesting an account that is not in the pool → no boot, returns False.
    async with _PoolAccounts([{"id": "acct-a"}]):
        ok2 = await _wpm._default_pool_provision_fn(
            db_session, project.id, account_id="acct-missing"
        )
    assert ok2 is False


@pytest.mark.asyncio
async def test_default_provision_fn_serializes_taskless_codex_refresh_probe(
    db_session, warm_pool_on, mocker
):
    provision = mocker.patch(
        "app.compute.provision_pool_vm", new=AsyncMock(return_value=object())
    )
    project = await _make_project(db_session)
    other_project = await _make_project(db_session)
    existing = await _register(
        db_session, other_project, "agent-vm-codex-refresh-probe", PoolVmState.IDLE,
        account_id="acct-codex", provider="openai", model="gpt-5.6-terra",
    )
    async with _PoolAccounts([{
        "id": "acct-codex", "provider": "openai", "auth_mode": "oauth",
        "runtime_ready": True,
        "runtime_secret": '{"tokens":{"access_token":"opaque","refresh_token":"rt"}}',
    }]):
        blocked = await _wpm._default_pool_provision_fn(
            db_session, project.id, account_id="acct-codex"
        )
        assert blocked is False
        provision.assert_not_awaited()
        await db_session.delete(existing)
        await db_session.commit()
        first_probe = await _wpm._default_pool_provision_fn(
            db_session, project.id, account_id="acct-codex"
        )
        assert first_probe is True
        provision.assert_awaited_once()


@pytest.mark.asyncio
async def test_stale_pool_image_ids_unions_outdated(mocker):
    """_stale_pool_image_ids unions temporal-staleness with pre-promotion
    outdatedness (a newer valid image already exists), so the fleet converges
    on the promoted image instead of lingering on old boots."""
    from app.warm_pool_maintainer import _stale_pool_image_ids

    mocker.patch(
        "app.compute.get_stale_image_vms",
        return_value=[{"vm_id": "vm-temporal-stale"}],
    )
    mocker.patch(
        "app.compute.get_outdated_image_vms",
        return_value=[{"vm_id": "vm-pre-promotion"}],
    )
    ids = await _stale_pool_image_ids()
    assert ids == {"vm-temporal-stale", "vm-pre-promotion"}


@pytest.mark.asyncio
async def test_idle_reaper_recycles_pre_promotion_member(
    db_session, warm_pool_on, mocker
):
    """A warm member booted from a pre-promotion image is recycled once a newer
    valid image exists — even when it is NOT temporally stale and is alive."""
    mocker.patch("app.vm_lifecycle._ensure_vm_deleted", new_callable=mocker.AsyncMock)
    # Not temporally stale, but a newer family-target image exists → outdated.
    mocker.patch("app.compute.get_stale_image_vms", return_value=[])
    mocker.patch(
        "app.compute.get_outdated_image_vms",
        return_value=[{"vm_id": "agent-vm-pre-promo"}],
    )
    project = await _make_project(db_session)
    member = await _register(
        db_session, project, "agent-vm-pre-promo", PoolVmState.IDLE
    )
    member.idle_since = _utcnow()  # fresh idle — not over TTL
    db_session.add(member)
    await db_session.commit()
    _shared_state.record_vm_heartbeat("agent-vm-pre-promo")  # alive — isolate reason

    retired = await sweep_idle_pool_vms(
        db_session, _utcnow(), runtime_config=_runtime_config
    )
    assert retired == 1
    assert await db_session.get(PoolVm, member.id) is None


# ---------------------------------------------------------------------------
# Per-project cost guard: targeted drain without collateral damage
# ---------------------------------------------------------------------------


async def _make_dispatched_task(
    db_session, project, warm_pool_hit: bool, *, minutes_ago: int = 5
):
    """A terminal task dispatched recently. ``warm_pool_hit`` sets the timing origin."""
    from app.warm_pool import WARM_POOL_ORIGIN

    timing = {"warm_start": warm_pool_hit}
    if warm_pool_hit:
        timing["warm_claim_origin"] = WARM_POOL_ORIGIN
    task = Task(
        project_id=project.id,
        title="dispatched",
        payloads=[{"type": "create", "prompt": "x", "base_branch": "automain"}],
        status=TaskStatus.COMPLETED,
        context={"_timing": timing},
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)
    task.vm_dispatched_at = _utcnow() - timedelta(minutes=minutes_ago)
    db_session.add(task)
    await db_session.commit()
    return task


@pytest.mark.asyncio
async def test_per_project_guard_drains_only_failing_project(
    db_session, warm_pool_on, mocker
):
    """A project with high idle cost and low eligible hit rate has its pool drained
    independently while a co-tenant project with good hit rate is unaffected.

    Project A: all dispatches are warm-pool hits (eligible hit rate 100% — guard off).
    Project B: no warm-pool hits, all eligible misses (eligible hit rate 0% — guard engages).
    Only B's idle member should be drained; A's must survive."""
    from app.warm_backlog_reuse import (
        _pool_lease_hit_events,
        _pool_lease_miss_events,
        record_pool_lease_hit,
        record_pool_lease_miss,
    )

    _pool_lease_hit_events.clear()
    _pool_lease_miss_events.clear()

    mocker.patch("app.vm_lifecycle._ensure_vm_deleted", new_callable=mocker.AsyncMock)
    mocker.patch(
        "app.efficiency_snapshot._compute_windowed_idle_cost",
        new_callable=mocker.AsyncMock,
        side_effect=lambda session, pid, cutoff: (
            {"total": 5.0, "by_account": {}}  # both projects have $5 idle
        ),
    )
    _runtime_config["warm_pool_target_size"] = 2
    _runtime_config["warm_pool_max_vms"] = 20
    _runtime_config["max_concurrent_vms"] = 20
    _runtime_config["warm_pool_cost_guard_min_dispatches"] = 5
    _runtime_config["warm_pool_cost_guard_min_idle_cost_usd"] = 1.0
    _runtime_config["warm_pool_cost_guard_min_hit_rate"] = 0.10

    project_a = await _make_project(db_session, name="Guard A")
    project_b = await _make_project(db_session, name="Guard B")

    # Project A: 10 eligible hits in the in-memory funnel → 100% eligible hit rate.
    for _ in range(10):
        record_pool_lease_hit(project_id=project_a.id)
    idle_a = await _register(
        db_session, project_a, "agent-vm-guard-a", PoolVmState.IDLE
    )
    idle_a.idle_since = _utcnow() - timedelta(seconds=300)
    db_session.add(idle_a)

    # Project B: 10 eligible misses, no hits → 0% eligible hit rate + $5 idle.
    for _ in range(10):
        record_pool_lease_miss("no_idle_member", project_id=project_b.id)
    idle_b = await _register(
        db_session, project_b, "agent-vm-guard-b", PoolVmState.IDLE
    )
    idle_b.idle_since = _utcnow() - timedelta(seconds=300)
    db_session.add(idle_b)
    await db_session.commit()

    async def _no_provision(session, project_id, **kw):
        return False

    totals = await run_pool_maintainer_cycle(
        runtime_config=_runtime_config,
        provision_fn=_no_provision,
        session_factory=_factory_for(db_session),
    )

    # Project A (healthy) survives; Project B (low eligible hit rate) is drained.
    assert await db_session.get(PoolVm, idle_a.id) is not None, (
        "project A's idle member must NOT be drained (guard is off for it)"
    )
    assert await db_session.get(PoolVm, idle_b.id) is None, (
        "project B's idle member must be drained (guard engaged for it)"
    )
    assert totals["drained"] == 1


@pytest.mark.asyncio
async def test_per_project_guard_does_not_drain_on_insufficient_dispatches(
    db_session, warm_pool_on, mocker
):
    """A project with low hit rate but fewer than min_dispatches eligible dispatches
    is NOT drained — thin-sample protection mirrors the fleet guard's behavior."""
    from app.warm_backlog_reuse import (
        _pool_lease_hit_events,
        _pool_lease_miss_events,
        record_pool_lease_miss,
    )

    _pool_lease_hit_events.clear()
    _pool_lease_miss_events.clear()

    mocker.patch("app.vm_lifecycle._ensure_vm_deleted", new_callable=mocker.AsyncMock)
    mocker.patch(
        "app.efficiency_snapshot._compute_windowed_idle_cost",
        new_callable=mocker.AsyncMock,
        return_value={"total": 5.0, "by_account": {}},
    )
    _runtime_config["warm_pool_target_size"] = 2
    _runtime_config["warm_pool_max_vms"] = 20
    _runtime_config["max_concurrent_vms"] = 20
    _runtime_config["warm_pool_cost_guard_min_dispatches"] = 20  # need 20
    _runtime_config["warm_pool_cost_guard_min_idle_cost_usd"] = 1.0
    _runtime_config["warm_pool_cost_guard_min_hit_rate"] = 0.10

    project = await _make_project(db_session, name="Thin Sample")
    # Only 3 eligible misses — below the min_dispatches floor.
    for _ in range(3):
        record_pool_lease_miss("no_idle_member", project_id=project.id)
    idle = await _register(db_session, project, "agent-vm-thin", PoolVmState.IDLE)
    idle.idle_since = _utcnow() - timedelta(seconds=300)
    db_session.add(idle)
    await db_session.commit()

    guard = await evaluate_project_pool_cost_guard(
        db_session, project.id, runtime_config=_runtime_config
    )
    assert guard["engaged"] is False, (
        "guard must not engage on a thin sample (< min_dispatches)"
    )
    assert guard["total_dispatches"] == 3
    assert await db_session.get(PoolVm, idle.id) is not None


@pytest.mark.asyncio
async def test_project_cost_guard_ignores_never_consulted_pool_work(
    db_session, warm_pool_on, mocker
):
    """Never-consulted cold dispatches (recorded without project_id attribution,
    or with ineligible miss reasons) must NOT count toward a project's eligible
    denominator — so the project guard cannot be tripped by work the pool was
    never asked to serve.

    Scenario: a project has 5 eligible hits and 5 eligible misses in its per-project
    funnel (eligible hit rate = 50%).  Additionally, 100 unattributed cold dispatches
    (no project_id) and 50 suppressed/cost_guard misses for the same project are
    recorded — these represent work that never consulted the pool and structurally
    ineligible demand.  The guard must read eligible_hit_rate = 50% (well above the
    10% floor) and stay disengaged, ignoring the ineligible noise."""
    from app.warm_backlog_reuse import (
        _pool_cold_dispatch_events,
        _pool_lease_hit_events,
        _pool_lease_miss_events,
        record_pool_cold_dispatch,
        record_pool_lease_hit,
        record_pool_lease_miss,
    )

    _pool_lease_hit_events.clear()
    _pool_cold_dispatch_events.clear()
    _pool_lease_miss_events.clear()

    mocker.patch("app.vm_lifecycle._ensure_vm_deleted", new_callable=mocker.AsyncMock)
    mocker.patch(
        "app.efficiency_snapshot._compute_windowed_idle_cost",
        new_callable=mocker.AsyncMock,
        return_value={"total": 5.0, "by_account": {}},
    )
    _runtime_config["warm_pool_cost_guard_min_dispatches"] = 5
    _runtime_config["warm_pool_cost_guard_min_idle_cost_usd"] = 1.0
    _runtime_config["warm_pool_cost_guard_min_hit_rate"] = 0.10

    project = await _make_project(db_session, name="Never Consulted")

    # 5 genuine eligible hits + 5 genuine eligible misses for this project.
    # eligible_hit_rate = 5 / (5 + 5) = 50% — well above the 10% floor.
    for _ in range(5):
        record_pool_lease_hit(project_id=project.id)
    for _ in range(5):
        record_pool_lease_miss("no_idle_member", project_id=project.id)

    # 100 cold dispatches with NO project_id — work that never consulted the pool.
    # These must NOT count toward this project's eligible denominator.
    for _ in range(100):
        record_pool_cold_dispatch()

    # 50 ineligible miss reasons for this project (suppressed + cost_guard).
    # These are structurally non-eligible and must be excluded from the denominator.
    for _ in range(25):
        record_pool_lease_miss("suppressed", project_id=project.id)
    for _ in range(25):
        record_pool_lease_miss("cost_guard", project_id=project.id)

    guard = await evaluate_project_pool_cost_guard(
        db_session, project.id, runtime_config=_runtime_config
    )

    # eligible_dispatches = 5 hits + 5 eligible misses = 10; rate = 50%.
    assert guard["total_dispatches"] == 10, (
        f"eligible_dispatches must be 10 (hits + eligible misses only), got {guard['total_dispatches']}"
    )
    assert guard["hit_rate"] == 0.5, (
        f"eligible_hit_rate must be 0.5, got {guard['hit_rate']}"
    )
    assert guard["engaged"] is False, (
        "guard must NOT engage when eligible hit rate (50%) is above the floor"
    )


@pytest.mark.asyncio
async def test_fleet_guard_still_drains_all_as_safety_valve(
    db_session, warm_pool_on, mocker
):
    """When the fleet-level guard engages, ALL projects drain regardless of
    individual per-project hit rates — it acts as a global safety valve."""
    mocker.patch("app.vm_lifecycle._ensure_vm_deleted", new_callable=mocker.AsyncMock)
    _runtime_config["warm_pool_target_size"] = 2
    _runtime_config["warm_pool_max_vms"] = 20
    _runtime_config["max_concurrent_vms"] = 20

    project_a = await _make_project(db_session, name="Fleet A")
    project_b = await _make_project(db_session, name="Fleet B")
    idle_a = await _register(
        db_session, project_a, "agent-vm-fleet-a", PoolVmState.IDLE
    )
    idle_a.idle_since = _utcnow() - timedelta(seconds=300)
    idle_b = await _register(
        db_session, project_b, "agent-vm-fleet-b", PoolVmState.IDLE
    )
    idle_b.idle_since = _utcnow() - timedelta(seconds=300)
    db_session.add_all([idle_a, idle_b])
    await db_session.commit()

    # Patch the fleet-level guard to engage unconditionally.
    mocker.patch(
        "app.warm_pool_maintainer.evaluate_pool_cost_guard",
        new_callable=mocker.AsyncMock,
        return_value={
            "engaged": True,
            "reason": "fleet guard engaged for test",
            "hit_rate": 0.0,
            "total_dispatches": 100,
            "raw_hit_rate": 0.0,
            "raw_total_dispatches": 100,
            "idle_cost_usd": 10.0,
            "min_hit_rate": 0.10,
            "min_idle_cost_usd": 1.0,
            "window_hours": 24,
        },
    )

    async def _no_provision(session, project_id, **kw):
        return False

    totals = await run_pool_maintainer_cycle(
        runtime_config=_runtime_config,
        provision_fn=_no_provision,
        session_factory=_factory_for(db_session),
    )

    # Fleet guard engaged → both projects drained, even though per-project hit
    # rates were not individually evaluated.
    assert await db_session.get(PoolVm, idle_a.id) is None, (
        "project A must be drained when fleet guard engages"
    )
    assert await db_session.get(PoolVm, idle_b.id) is None, (
        "project B must be drained when fleet guard engages"
    )
    assert totals["drained"] == 2


# ---------------------------------------------------------------------------
# Lane-scoped workspace reservation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_lane_reserved_member_skipped_by_non_lane_request(
    db_session, warm_pool_on
):
    """A non-lane (or different-lane) lease request skips a member with an active
    lane reservation; the matching lane request claims it."""
    project = await _make_project(db_session, name="Reserve Skip")
    lane_a = uuid.uuid4()
    lane_b = uuid.uuid4()

    member = await _register(db_session, project, "agent-vm-res-skip", PoolVmState.IDLE)
    member.reserved_lane_id = lane_a
    member.reservation_expires_at = _utcnow() + timedelta(seconds=900)
    member.idle_since = _utcnow() - timedelta(seconds=60)
    db_session.add(member)
    await db_session.commit()

    # No lane_id: must skip the reserved member.
    result = await lease_idle_pool_vm(
        db_session,
        project_id=project.id,
        leased_task_id=uuid.uuid4(),
    )
    assert result is None, "Non-lane request must skip a reserved member"

    # Different lane_id: must also skip.
    result = await lease_idle_pool_vm(
        db_session,
        project_id=project.id,
        leased_task_id=uuid.uuid4(),
        lane_id=str(lane_b),
    )
    assert result is None, "Different-lane request must skip a reserved member"

    # Member is still IDLE.
    await db_session.refresh(member)
    assert member.state == PoolVmState.IDLE

    # Matching lane_id: must claim it.
    leased = await lease_idle_pool_vm(
        db_session,
        project_id=project.id,
        leased_task_id=uuid.uuid4(),
        lane_id=str(lane_a),
    )
    assert leased is not None, "Matching-lane request must claim the reserved member"
    assert leased.vm_instance_id == "agent-vm-res-skip"
    assert leased.state == PoolVmState.LEASED
    # Reservation cleared on lease.
    assert leased.reserved_lane_id is None
    assert leased.reservation_expires_at is None
    await db_session.commit()


@pytest.mark.asyncio
async def test_lane_workspace_correction_stays_project_scoped(db_session, warm_pool_on):
    """A task must not be able to downgrade or rewrite another project's lane
    workspace just by copying its lane UUID into task context."""
    prior_lanes = _runtime_config.get("improvement_lanes_enabled")
    _runtime_config["improvement_lanes_enabled"] = True
    try:
        project_a = await _make_project(db_session, name="Lane Scope A")
        project_b = await _make_project(db_session, name="Lane Scope B")
        foreign_lane = ImprovementLane(
            project_id=project_b.id,
            title="foreign lane",
            goal="foreign goal",
            branch_name="foreign-branch",
            base_branch="main",
            workspace_state="WARM_RESERVED",
            workspace_vm_instance_id="foreign-vm",
        )
        db_session.add(foreign_lane)
        await db_session.commit()
        await db_session.refresh(foreign_lane)

        task = await _make_task(db_session, project_a, TaskStatus.QUEUED)
        task.context = {"improvement_lane_id": str(foreign_lane.id)}
        db_session.add(task)
        await db_session.commit()

        # No compatible pool member: the lane-correction miss path must not
        # touch the foreign lane.
        result = await try_pool_dispatch(
            db_session,
            task,
            required_provider="anthropic",
            required_model="claude-opus-4-8",
            required_account="acct-1",
        )
        assert result is None
        await db_session.refresh(foreign_lane)
        assert foreign_lane.workspace_state == "WARM_RESERVED"
        assert foreign_lane.workspace_vm_instance_id == "foreign-vm"

        # Add a compatible member and retry: the success path must also leave
        # the foreign lane untouched.
        await _register(
            db_session,
            project_a,
            "agent-vm-scope-local",
            PoolVmState.IDLE,
            account_id="acct-1",
            provider="anthropic",
            model="claude-opus-4-8",
        )
        result = await try_pool_dispatch(
            db_session,
            task,
            required_provider="anthropic",
            required_model="claude-opus-4-8",
            required_account="acct-1",
        )
        assert result is not None
        await db_session.refresh(foreign_lane)
        assert foreign_lane.workspace_state == "WARM_RESERVED"
        assert foreign_lane.workspace_vm_instance_id == "foreign-vm"
    finally:
        if prior_lanes is None:
            _runtime_config.pop("improvement_lanes_enabled", None)
        else:
            _runtime_config["improvement_lanes_enabled"] = prior_lanes


@pytest.mark.asyncio
async def test_expired_reservation_claimable_by_any_request(db_session, warm_pool_on):
    """After the reservation TTL expires, the member is claimable by any request
    (no lane_id required)."""
    project = await _make_project(db_session, name="Reserve Expiry")
    lane_id = uuid.uuid4()

    member = await _register(
        db_session, project, "agent-vm-res-expired", PoolVmState.IDLE
    )
    member.reserved_lane_id = lane_id
    member.reservation_expires_at = _utcnow() - timedelta(seconds=10)  # past
    member.idle_since = _utcnow() - timedelta(seconds=100)
    db_session.add(member)
    await db_session.commit()

    # Non-lane request should now succeed (expired reservation is not a blocker).
    leased = await lease_idle_pool_vm(
        db_session,
        project_id=project.id,
        leased_task_id=uuid.uuid4(),
    )
    assert leased is not None, "Expired reservation must not block a general lease"
    assert leased.vm_instance_id == "agent-vm-res-expired"
    await db_session.commit()


@pytest.mark.asyncio
async def test_expired_reservation_claim_by_lane_clears_lane_workspace(
    db_session, warm_pool_on
):
    """Claiming an expired lane reservation must downgrade the lane to COLD so
    the row no longer advertises a warm-resume workspace that no longer exists."""
    prior_lanes = _runtime_config.get("improvement_lanes_enabled")
    _runtime_config["improvement_lanes_enabled"] = True
    try:
        project = await _make_project(db_session, name="Reserve Claim Lane")
        lane = ImprovementLane(
            project_id=project.id,
            title="claim lane",
            goal="claim goal",
            branch_name="claim-branch",
            base_branch="main",
            workspace_state="WARM_RESERVED",
            workspace_vm_instance_id="agent-vm-res-claimed",
        )
        db_session.add(lane)
        await db_session.commit()
        await db_session.refresh(lane)

        member = await _register(
            db_session, project, "agent-vm-res-claimed", PoolVmState.IDLE
        )
        member.idle_since = _utcnow() - timedelta(seconds=100)
        member.reserved_lane_id = lane.id
        member.reservation_expires_at = _utcnow() - timedelta(seconds=10)
        db_session.add(member)
        await db_session.commit()

        leased = await lease_idle_pool_vm(
            db_session,
            project_id=project.id,
            leased_task_id=uuid.uuid4(),
            lane_id=str(lane.id),
        )

        assert leased is not None
        assert leased.vm_instance_id == "agent-vm-res-claimed"
        await db_session.refresh(lane)
        assert lane.workspace_state == "COLD"
        assert lane.workspace_vm_instance_id is None
    finally:
        if prior_lanes is None:
            _runtime_config.pop("improvement_lanes_enabled", None)
        else:
            _runtime_config["improvement_lanes_enabled"] = prior_lanes


@pytest.mark.asyncio
async def test_maintainer_clears_expired_reservation_banks_lane_cost(
    db_session, warm_pool_on
):
    """The maintainer sweep clears an expired lane reservation on an IDLE member,
    settles the reserved-idle cost to the fleet ledger (PoolIdleSpan), and
    attributes it to the lane (ImprovementLane.reserved_idle_cost_usd_total)."""
    prior_lanes = _runtime_config.get("improvement_lanes_enabled")
    _runtime_config["improvement_lanes_enabled"] = True
    try:
        project = await _make_project(db_session, name="Reserve Expire Lane")

        lane = ImprovementLane(
            project_id=project.id,
            title="test lane",
            goal="test goal",
            branch_name="test-branch",
            base_branch="main",
            workspace_state="WARM_RESERVED",
            workspace_vm_instance_id="agent-vm-ws-cold",
            reserved_idle_cost_usd_total=0.0,
        )
        db_session.add(lane)
        await db_session.commit()
        await db_session.refresh(lane)

        # Seed an expired reservation on the member pointing to the lane.
        member = await _register(
            db_session, project, "agent-vm-ws-cold", PoolVmState.IDLE
        )
        member.idle_since = _utcnow() - timedelta(seconds=120)
        member.reserved_lane_id = lane.id
        member.reservation_expires_at = _utcnow() - timedelta(seconds=30)
        db_session.add(member)
        await db_session.commit()

        await sweep_idle_pool_vms(db_session, _utcnow(), runtime_config=_runtime_config)

        # Member reservation cleared — still IDLE, not reaped.
        await db_session.refresh(member)
        assert member.state == PoolVmState.IDLE, "Member must not be reaped"
        assert member.reserved_lane_id is None, "reserved_lane_id must be cleared"
        assert member.reservation_expires_at is None, (
            "reservation_expires_at must be cleared"
        )

        # Lane workspace state must be corrected to COLD.
        await db_session.refresh(lane)
        assert lane.workspace_state == "COLD", (
            f"Lane workspace_state must be COLD after reservation expiry, "
            f"got {lane.workspace_state!r}"
        )
        assert lane.workspace_vm_instance_id is None, (
            "Lane workspace_vm_instance_id must be cleared after reservation expiry"
        )
    finally:
        if prior_lanes is None:
            _runtime_config.pop("improvement_lanes_enabled", None)
        else:
            _runtime_config["improvement_lanes_enabled"] = prior_lanes


@pytest.mark.asyncio
async def test_maintainer_clears_expired_reservation_even_with_zero_accrued_cost(
    db_session, warm_pool_on, mocker
):
    """Even when idle-cost settlement returns 0, the expired reservation must
    still downgrade the lane to COLD and clear its workspace VM pointer."""
    prior_lanes = _runtime_config.get("improvement_lanes_enabled")
    _runtime_config["improvement_lanes_enabled"] = True
    mocker.patch("app.efficiency_snapshot.settle_pool_vm_idle_cost", return_value=0)
    try:
        project = await _make_project(db_session, name="Reserve Zero Cost")
        lane = ImprovementLane(
            project_id=project.id,
            title="zero cost lane",
            goal="zero cost goal",
            branch_name="zero-cost-branch",
            base_branch="main",
            workspace_state="WARM_RESERVED",
            workspace_vm_instance_id="agent-vm-ws-zero",
            reserved_idle_cost_usd_total=0.0,
        )
        db_session.add(lane)
        await db_session.commit()
        await db_session.refresh(lane)

        member = await _register(
            db_session, project, "agent-vm-ws-zero", PoolVmState.IDLE
        )
        member.idle_since = _utcnow()
        member.reserved_lane_id = lane.id
        member.reservation_expires_at = _utcnow() - timedelta(seconds=5)
        db_session.add(member)
        await db_session.commit()

        await sweep_idle_pool_vms(db_session, _utcnow(), runtime_config=_runtime_config)

        await db_session.refresh(lane)
        assert lane.workspace_state == "COLD"
        assert lane.workspace_vm_instance_id is None
    finally:
        if prior_lanes is None:
            _runtime_config.pop("improvement_lanes_enabled", None)
        else:
            _runtime_config["improvement_lanes_enabled"] = prior_lanes


@pytest.mark.asyncio
async def test_clear_lane_warm_workspace_if_matching_member_is_atomic_against_racing_reserve(
    db_session,
):
    """A concurrent re-reservation must win over a stale clear attempt."""
    project = await _make_project(db_session, name="Racing Reserve Lane")
    lane = ImprovementLane(
        project_id=project.id,
        title="race lane",
        goal="race goal",
        branch_name="race-branch",
        base_branch="main",
        workspace_state="WARM_RESERVED",
        workspace_vm_instance_id="agent-vm-race-old",
    )
    db_session.add(lane)
    await db_session.commit()
    await db_session.refresh(lane)

    original_execute = db_session.execute
    racing_session_factory = async_sessionmaker(
        bind=db_session.bind, expire_on_commit=False
    )

    async def raced_execute(statement, *args, **kwargs):
        async with racing_session_factory() as racing_session:
            racing_lane = await racing_session.get(ImprovementLane, lane.id)
            racing_lane.workspace_state = "WARM_RESERVED"
            racing_lane.workspace_vm_instance_id = "agent-vm-race-new"
            racing_session.add(racing_lane)
            await racing_session.commit()
        return await original_execute(statement, *args, **kwargs)

    with patch.object(
        db_session,
        "execute",
        new=AsyncMock(side_effect=raced_execute),
    ):
        cleared = await clear_lane_warm_workspace_if_matching_member(
            db_session,
            lane.id,
            "agent-vm-race-old",
        )

    assert cleared is False
    await db_session.refresh(lane)
    assert lane.workspace_state == "WARM_RESERVED"
    assert lane.workspace_vm_instance_id == "agent-vm-race-new"


@pytest.mark.asyncio
async def test_maintainer_skips_expired_reservation_for_other_vm_instance(
    db_session, warm_pool_on, mocker
):
    """A stale reservation must not clear a lane that has already moved to a different VM."""
    prior_lanes = _runtime_config.get("improvement_lanes_enabled")
    _runtime_config["improvement_lanes_enabled"] = True
    mocker.patch("app.efficiency_snapshot.settle_pool_vm_idle_cost", return_value=0)
    try:
        project = await _make_project(db_session, name="Reserve Other Vm")
        # "live-vm" is the pool VM that already holds the lane's workspace.
        # It must exist in the DB so the periodic orphan-reclaim sweep does not
        # incorrectly treat the lane as abandoned.
        live_pool_vm = PoolVm(
            vm_instance_id="live-vm",
            state=PoolVmState.IDLE,
            project_id=project.id,
        )
        db_session.add(live_pool_vm)
        lane = ImprovementLane(
            project_id=project.id,
            title="other vm lane",
            goal="other vm goal",
            branch_name="other-vm-branch",
            base_branch="main",
            workspace_state="WARM_RESERVED",
            workspace_vm_instance_id="live-vm",
            reserved_idle_cost_usd_total=0.0,
        )
        db_session.add(lane)
        await db_session.commit()
        await db_session.refresh(lane)

        member = await _register(
            db_session, project, "agent-vm-stale-other", PoolVmState.IDLE
        )
        member.idle_since = _utcnow()
        member.reserved_lane_id = lane.id
        member.reservation_expires_at = _utcnow() - timedelta(seconds=5)
        db_session.add(member)
        await db_session.commit()

        await sweep_idle_pool_vms(db_session, _utcnow(), runtime_config=_runtime_config)

        await db_session.refresh(member)
        await db_session.refresh(lane)
        assert member.reserved_lane_id is None
        assert member.reservation_expires_at is None
        assert lane.workspace_state == "WARM_RESERVED"
        assert lane.workspace_vm_instance_id == "live-vm"
    finally:
        if prior_lanes is None:
            _runtime_config.pop("improvement_lanes_enabled", None)
        else:
            _runtime_config["improvement_lanes_enabled"] = prior_lanes


@pytest.mark.asyncio
async def test_maintainer_skips_live_lane_workspace_for_stale_expired_reservation(
    db_session, warm_pool_on, mocker
):
    """An expired reservation should not downgrade a lane that already has a live workspace."""
    prior_lanes = _runtime_config.get("improvement_lanes_enabled")
    _runtime_config["improvement_lanes_enabled"] = True
    mocker.patch("app.efficiency_snapshot.settle_pool_vm_idle_cost", return_value=0)
    try:
        project = await _make_project(db_session, name="Reserve Live Lane Maint")
        # "live-vm" represents the leased pool VM actually running the workspace.
        # It must exist in the DB so the periodic orphan-reclaim sweep does not
        # incorrectly treat the still-active lane as abandoned.
        live_pool_vm = PoolVm(
            vm_instance_id="live-vm",
            state=PoolVmState.LEASED,
            project_id=project.id,
        )
        db_session.add(live_pool_vm)
        lane = ImprovementLane(
            project_id=project.id,
            title="live lane",
            goal="live goal",
            branch_name="live-branch",
            base_branch="main",
            workspace_state="LEASED",
            workspace_vm_instance_id="live-vm",
            reserved_idle_cost_usd_total=0.0,
        )
        db_session.add(lane)
        await db_session.commit()
        await db_session.refresh(lane)

        member = await _register(
            db_session, project, "agent-vm-live-stale", PoolVmState.IDLE
        )
        member.idle_since = _utcnow()
        member.reserved_lane_id = lane.id
        member.reservation_expires_at = _utcnow() - timedelta(seconds=5)
        db_session.add(member)
        await db_session.commit()

        await sweep_idle_pool_vms(db_session, _utcnow(), runtime_config=_runtime_config)

        await db_session.refresh(member)
        await db_session.refresh(lane)
        assert member.reserved_lane_id is None
        assert member.reservation_expires_at is None
        assert lane.workspace_state == "LEASED"
        assert lane.workspace_vm_instance_id == "live-vm"
    finally:
        if prior_lanes is None:
            _runtime_config.pop("improvement_lanes_enabled", None)
        else:
            _runtime_config["improvement_lanes_enabled"] = prior_lanes


# ---------------------------------------------------------------------------
# Stranded / stuck improvement-lane reconciliation (sweep_stranded_lanes).
#
# Detection for lane strands that NEVER reach the pilot's checkpoint seam:
#   * an ACTIVE lane whose bound phase task is terminal / vanished / unbound
#     (operator/admin cancel, reaper kill, agent crash before any checkpoint),
#     routed through the lane's BOUNDED recovery (mark_checkpoint_missing ->
#     requeue-from-checkpoint while under budget, KILLED once exhausted);
#   * a stale PROPOSED lane whose activation never completed, aged to KILLED.
# Sibling of the in-seam "recover checkpoint-missing lanes" work — this owns
# DETECTION for the out-of-seam cases.
# ---------------------------------------------------------------------------


@pytest.fixture
def lanes_on():
    """Enable improvement_lanes_enabled for the test and restore prior state."""
    prior = _runtime_config.get("improvement_lanes_enabled")
    prior_stale = _runtime_config.get("improvement_lane_proposed_stale_seconds")
    _runtime_config["improvement_lanes_enabled"] = True
    try:
        yield
    finally:
        if prior is None:
            _runtime_config.pop("improvement_lanes_enabled", None)
        else:
            _runtime_config["improvement_lanes_enabled"] = prior
        if prior_stale is None:
            _runtime_config.pop("improvement_lane_proposed_stale_seconds", None)
        else:
            _runtime_config["improvement_lane_proposed_stale_seconds"] = prior_stale


async def _make_lane(
    db_session,
    project,
    *,
    status=ImprovementLaneStatus.ACTIVE,
    current_task_id=None,
    phase_requeues=0,
    created_at=None,
    parked_at=None,
    name="lane",
):
    lane = ImprovementLane(
        project_id=project.id,
        title=f"{name} title",
        goal=f"{name} goal",
        branch_name=f"lane/{name}",
        base_branch="automain",
        status=status.value,
        current_task_id=current_task_id,
        phase_requeues=phase_requeues,
    )
    db_session.add(lane)
    await db_session.commit()
    await db_session.refresh(lane)
    if created_at is not None:
        # created_at has a server_default; override after insert for age tests.
        lane.created_at = created_at
        db_session.add(lane)
        await db_session.commit()
        await db_session.refresh(lane)
    if parked_at is not None:
        lane.parked_at = parked_at
        db_session.add(lane)
        await db_session.commit()
        await db_session.refresh(lane)
    return lane


async def _make_task_with_pr(db_session, project, status, *, pr_number=42):
    """Create a terminal task carrying PR dispatch context (pr_number, branch,
    base_branch) so sweep_stranded_lanes can spawn a recovery task for it."""
    task = Task(
        project_id=project.id,
        title="pr fix",
        payloads=[
            {
                "type": "fix",
                "pr_number": pr_number,
                "branch": f"feature/pr-{pr_number}",
                "base_branch": "automain",
                "prompt": "fix it",
                "original_task_id": "orig-task-id",
            }
        ],
        context={"_review_fix_lane_id": "some-lane-id"},
        status=status,
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)
    return task


@pytest.mark.asyncio
async def test_sweep_reconciles_stranded_active_lane_and_ages_stale_proposed(
    db_session, lanes_on
):
    """Acceptance seam: the sweep reconciles an ACTIVE lane whose bound task was
    cancelled externally (no PR context -> KILLED deterministically) AND ages a
    stale PROPOSED lane to KILLED, while leaving a fresh PROPOSED lane untouched
    — and a re-run (concurrent tick) is a safe no-op."""
    project = await _make_project(db_session, name="Strand Recon")

    cancelled = await _make_task(db_session, project, TaskStatus.CANCELLED)
    active_lane = await _make_lane(
        db_session,
        project,
        status=ImprovementLaneStatus.ACTIVE,
        current_task_id=cancelled.id,
        name="active-cancelled",
    )
    stale_proposed = await _make_lane(
        db_session,
        project,
        status=ImprovementLaneStatus.PROPOSED,
        created_at=_utcnow() - timedelta(seconds=4000),
        name="stale-proposed",
    )
    fresh_proposed = await _make_lane(
        db_session,
        project,
        status=ImprovementLaneStatus.PROPOSED,
        created_at=_utcnow() - timedelta(seconds=10),
        name="fresh-proposed",
    )

    now = _utcnow()
    transitioned = await sweep_stranded_lanes(
        db_session, now, runtime_config=_runtime_config
    )
    assert transitioned == 2, "one stranded ACTIVE + one stale PROPOSED"

    await db_session.refresh(active_lane)
    assert active_lane.status == ImprovementLaneStatus.KILLED.value, (
        "stranded lane with no PR context is killed deterministically (not parked inert)"
    )
    assert active_lane.retired_at is not None
    assert active_lane.phase_requeues == 1, "mark_checkpoint_missing bumped the counter"

    await db_session.refresh(stale_proposed)
    assert stale_proposed.status == ImprovementLaneStatus.KILLED.value
    assert stale_proposed.retired_at is not None

    await db_session.refresh(fresh_proposed)
    assert fresh_proposed.status == ImprovementLaneStatus.PROPOSED.value, (
        "a PROPOSED lane inside the staleness window must be left untouched"
    )

    # Concurrent / repeated tick: the lanes are no longer ACTIVE/stale-PROPOSED,
    # so every CAS misses and the second pass is a no-op (loser never re-kills).
    second = await sweep_stranded_lanes(db_session, now, runtime_config=_runtime_config)
    assert second == 0, "re-run finds nothing reconcilable — exactly one transition won"


@pytest.mark.asyncio
async def test_sweep_kills_stranded_active_lane_when_budget_exhausted(
    db_session, lanes_on
):
    """An ACTIVE lane whose bound task is FAILED and whose phase_requeues budget
    is already at the cap is KILLED (not requeued) — bounded recovery terminates."""
    project = await _make_project(db_session, name="Strand Exhausted")
    failed = await _make_task(db_session, project, TaskStatus.FAILED)
    lane = await _make_lane(
        db_session,
        project,
        status=ImprovementLaneStatus.ACTIVE,
        current_task_id=failed.id,
        phase_requeues=_LANE_MAX_PHASE_REQUEUES,
        name="exhausted",
    )

    transitioned = await sweep_stranded_lanes(
        db_session, _utcnow(), runtime_config=_runtime_config
    )
    assert transitioned == 1

    await db_session.refresh(lane)
    assert lane.status == ImprovementLaneStatus.KILLED.value
    assert lane.current_task_id is None
    assert lane.retired_at is not None


@pytest.mark.asyncio
async def test_sweep_reconciles_vanished_and_unbound_active_lanes(db_session, lanes_on):
    """An ACTIVE lane whose current_task_id points at a deleted Task (vanished),
    and one with no bound task at all, are both killed deterministically —
    no dispatch context means no recovery task can be created."""
    project = await _make_project(db_session, name="Strand Vanished")

    vanished_lane = await _make_lane(
        db_session,
        project,
        status=ImprovementLaneStatus.ACTIVE,
        current_task_id=uuid.uuid4(),  # references a Task row that never existed
        name="vanished",
    )
    unbound_lane = await _make_lane(
        db_session,
        project,
        status=ImprovementLaneStatus.ACTIVE,
        current_task_id=None,
        name="unbound",
    )

    transitioned = await sweep_stranded_lanes(
        db_session, _utcnow(), runtime_config=_runtime_config
    )
    assert transitioned == 2

    for lane in (vanished_lane, unbound_lane):
        await db_session.refresh(lane)
        assert lane.status == ImprovementLaneStatus.KILLED.value
        assert lane.current_task_id is None


@pytest.mark.asyncio
async def test_sweep_spares_active_lane_with_live_task(db_session, lanes_on):
    """An ACTIVE lane whose bound task is still in flight (IN_PROGRESS or the
    children-waiting EVALUATING state) must NOT be reconciled as stranded."""
    project = await _make_project(db_session, name="Strand Live")

    in_progress = await _make_task(db_session, project, TaskStatus.IN_PROGRESS)
    live_lane = await _make_lane(
        db_session,
        project,
        status=ImprovementLaneStatus.ACTIVE,
        current_task_id=in_progress.id,
        name="live",
    )
    evaluating = await _make_task(db_session, project, TaskStatus.EVALUATING)
    eval_lane = await _make_lane(
        db_session,
        project,
        status=ImprovementLaneStatus.ACTIVE,
        current_task_id=evaluating.id,
        name="evaluating",
    )

    transitioned = await sweep_stranded_lanes(
        db_session, _utcnow(), runtime_config=_runtime_config
    )
    assert transitioned == 0

    for lane in (live_lane, eval_lane):
        await db_session.refresh(lane)
        assert lane.status == ImprovementLaneStatus.ACTIVE.value
    assert live_lane.current_task_id == in_progress.id
    assert eval_lane.current_task_id == evaluating.id


@pytest.mark.asyncio
async def test_sweep_skips_already_parked_lane_checkpoint_won_the_race(
    db_session, lanes_on
):
    """External cancel racing a checkpoint write: if the checkpoint already moved
    the lane ACTIVE->PARKED, the sweep's ACTIVE-expected CAS misses and no-ops —
    the lane is NOT double-handled even though its (now-historical) task is
    terminal."""
    project = await _make_project(db_session, name="Strand Race")
    cancelled = await _make_task(db_session, project, TaskStatus.CANCELLED)
    parked_lane = await _make_lane(
        db_session,
        project,
        status=ImprovementLaneStatus.PARKED,
        current_task_id=cancelled.id,
        name="checkpoint-won",
    )

    transitioned = await sweep_stranded_lanes(
        db_session, _utcnow(), runtime_config=_runtime_config
    )
    assert transitioned == 0

    await db_session.refresh(parked_lane)
    assert parked_lane.status == ImprovementLaneStatus.PARKED.value


@pytest.mark.asyncio
async def test_sweep_stranded_lanes_inert_when_disabled(db_session):
    """With improvement_lanes_enabled False the sweep is fully inert — a stranded
    ACTIVE lane and a stale PROPOSED lane are both left untouched."""
    prior = _runtime_config.get("improvement_lanes_enabled")
    _runtime_config["improvement_lanes_enabled"] = False
    try:
        project = await _make_project(db_session, name="Strand Disabled")
        cancelled = await _make_task(db_session, project, TaskStatus.CANCELLED)
        active_lane = await _make_lane(
            db_session,
            project,
            status=ImprovementLaneStatus.ACTIVE,
            current_task_id=cancelled.id,
            name="disabled-active",
        )
        stale_proposed = await _make_lane(
            db_session,
            project,
            status=ImprovementLaneStatus.PROPOSED,
            created_at=_utcnow() - timedelta(seconds=4000),
            name="disabled-proposed",
        )

        transitioned = await sweep_stranded_lanes(
            db_session, _utcnow(), runtime_config=_runtime_config
        )
        assert transitioned == 0

        await db_session.refresh(active_lane)
        assert active_lane.status == ImprovementLaneStatus.ACTIVE.value
        await db_session.refresh(stale_proposed)
        assert stale_proposed.status == ImprovementLaneStatus.PROPOSED.value
    finally:
        if prior is None:
            _runtime_config.pop("improvement_lanes_enabled", None)
        else:
            _runtime_config["improvement_lanes_enabled"] = prior


@pytest.mark.asyncio
async def test_sweep_idle_pool_vms_invokes_stranded_lane_sweep(
    db_session, lanes_on, mocker
):
    """sweep_idle_pool_vms drives the stranded-lane reconciliation independently
    of warm_pool_enabled (shadow-lane mode still reconciles)."""
    spy = mocker.spy(_wpm, "sweep_stranded_lanes")
    # warm_pool_enabled stays False (no warm_pool_on fixture) — sweep must still run.
    project = await _make_project(db_session, name="Strand Wired")
    cancelled = await _make_task(db_session, project, TaskStatus.CANCELLED)
    lane = await _make_lane(
        db_session,
        project,
        status=ImprovementLaneStatus.ACTIVE,
        current_task_id=cancelled.id,
        name="wired",
    )

    await sweep_idle_pool_vms(db_session, _utcnow(), runtime_config=_runtime_config)

    assert spy.call_count == 1, "sweep_idle_pool_vms must invoke sweep_stranded_lanes"
    await db_session.refresh(lane)
    assert lane.status == ImprovementLaneStatus.KILLED.value, (
        "the wired sweep killed the stranded lane (no PR context for recovery)"
    )


@pytest.mark.asyncio
async def test_sweep_exhausted_budget_clears_pool_vm_reservation(db_session, lanes_on):
    """sweep_exhausted_lane_budgets clears PoolVm reservation fields after
    killing an over-budget lane so the VM rejoins the general pool immediately."""
    project = await _make_project(db_session, name="Exhausted Budget Res")
    lane = await _make_lane(
        db_session,
        project,
        status=ImprovementLaneStatus.ACTIVE,
        phase_requeues=_LANE_MAX_PHASE_REQUEUES + 1,
        name="over-budget",
    )
    member = await _register(db_session, project, "agent-vm-exh-res", PoolVmState.IDLE)
    member.reserved_lane_id = lane.id
    member.reservation_expires_at = _utcnow() + timedelta(hours=1)
    member.reservation_hard_expires_at = _utcnow() + timedelta(hours=2)
    db_session.add(member)
    await db_session.commit()

    killed = await sweep_exhausted_lane_budgets(db_session)
    assert killed == 1

    await db_session.refresh(lane)
    assert lane.status == ImprovementLaneStatus.KILLED.value

    await db_session.refresh(member)
    assert member.reserved_lane_id is None, "reservation must be cleared after KILLED"
    assert member.reservation_expires_at is None
    assert member.reservation_hard_expires_at is None


@pytest.mark.asyncio
async def test_sweep_stranded_lane_kill_clears_pool_vm_reservation(
    db_session, lanes_on
):
    """sweep_stranded_lanes clears PoolVm reservation fields when it KILLs a
    budget-exhausted stranded lane."""
    project = await _make_project(db_session, name="Stranded Kill Res")
    failed = await _make_task(db_session, project, TaskStatus.FAILED)
    lane = await _make_lane(
        db_session,
        project,
        status=ImprovementLaneStatus.ACTIVE,
        current_task_id=failed.id,
        phase_requeues=_LANE_MAX_PHASE_REQUEUES,
        name="stranded-exhausted",
    )
    member = await _register(
        db_session, project, "agent-vm-strand-res", PoolVmState.IDLE
    )
    member.reserved_lane_id = lane.id
    member.reservation_expires_at = _utcnow() + timedelta(hours=1)
    member.reservation_hard_expires_at = _utcnow() + timedelta(hours=2)
    db_session.add(member)
    await db_session.commit()

    transitioned = await sweep_stranded_lanes(
        db_session, _utcnow(), runtime_config=_runtime_config
    )
    assert transitioned == 1

    await db_session.refresh(lane)
    assert lane.status == ImprovementLaneStatus.KILLED.value

    await db_session.refresh(member)
    assert member.reserved_lane_id is None, "reservation must be cleared after KILLED"
    assert member.reservation_expires_at is None
    assert member.reservation_hard_expires_at is None


@pytest.mark.asyncio
async def test_sweep_stranded_lane_no_context_kills_and_clears_reservation(
    db_session, lanes_on
):
    """sweep_stranded_lanes kills an under-budget stranded lane that has no PR
    dispatch context and clears the PoolVm reservation — the lane cannot be
    recovered without a pr_number so it is terminated deterministically."""
    project = await _make_project(db_session, name="Stranded No-ctx Res")
    cancelled = await _make_task(db_session, project, TaskStatus.CANCELLED)
    lane = await _make_lane(
        db_session,
        project,
        status=ImprovementLaneStatus.ACTIVE,
        current_task_id=cancelled.id,
        phase_requeues=0,
        name="stranded-no-context",
    )
    member = await _register(
        db_session, project, "agent-vm-nokill-res", PoolVmState.IDLE
    )
    member.reserved_lane_id = lane.id
    member.reservation_expires_at = _utcnow() + timedelta(hours=1)
    member.reservation_hard_expires_at = _utcnow() + timedelta(hours=2)
    db_session.add(member)
    await db_session.commit()

    transitioned = await sweep_stranded_lanes(
        db_session, _utcnow(), runtime_config=_runtime_config
    )
    assert transitioned == 1

    await db_session.refresh(lane)
    assert lane.status == ImprovementLaneStatus.KILLED.value, (
        "no-context stranded lane must be killed, not parked into a dead state"
    )

    await db_session.refresh(member)
    assert member.reserved_lane_id is None, (
        "reservation must be cleared after deterministic kill"
    )
    assert member.reservation_expires_at is None
    assert member.reservation_hard_expires_at is None


@pytest.mark.asyncio
async def test_sweep_stranded_lanes_creates_recovery_task_when_context_available(
    db_session, lanes_on, mocker
):
    """Acceptance proof: a stranded ACTIVE lane whose terminal task carries PR
    dispatch context (pr_number / branch / base_branch) is re-dispatched with a
    live QUEUED recovery task rather than parked inert until retirement.

    The lane stays ACTIVE and current_task_id is rebound to the new recovery
    task, so chain continuity is maintained and the 'requeue-from-checkpoint'
    promise is real (selector: backend/testing/test_warm_pool_maintainer.py)."""
    from app.models import Task as _Task

    project = await _make_project(db_session, name="Strand Recovery")
    cancelled = await _make_task_with_pr(
        db_session, project, TaskStatus.CANCELLED, pr_number=99
    )
    lane = await _make_lane(
        db_session,
        project,
        status=ImprovementLaneStatus.ACTIVE,
        current_task_id=cancelled.id,
        phase_requeues=0,
        name="stranded-with-context",
    )

    mocker.patch(
        "app.github_token_resolver.get_github_token_for_task",
        new=AsyncMock(return_value=("token", "owner", "repo")),
    )
    mocker.patch(
        "app.github.get_pull_request",
        new=AsyncMock(
            return_value={
                "state": "open",
                "merged": False,
                "base": {"sha": "base-a"},
                "head": {"sha": "head-a"},
            }
        ),
    )

    transitioned = await sweep_stranded_lanes(
        db_session, _utcnow(), runtime_config=_runtime_config
    )
    assert transitioned == 1

    await db_session.refresh(lane)
    assert lane.status == ImprovementLaneStatus.ACTIVE.value, (
        "lane with PR context stays ACTIVE — bound to the recovery task"
    )
    assert lane.phase_requeues == 1, "mark_checkpoint_missing bumped the counter"
    assert lane.current_task_id is not None, "lane must be bound to recovery task"
    assert lane.current_task_id != cancelled.id, "old stale binding must be replaced"

    # The recovery task must exist and be QUEUED.
    recovery = await db_session.get(_Task, lane.current_task_id)
    assert recovery is not None, "recovery task must exist in the DB"
    assert recovery.status == TaskStatus.QUEUED, "recovery task must be QUEUED"
    assert recovery.project_id == project.id
    payload = recovery.first_payload or {}
    assert payload.get("pr_number") == 99, "recovery payload carries the PR number"
    assert payload.get("type") == "fix"

    # A second sweep tick must not re-process the lane (now ACTIVE with a live task).
    second = await sweep_stranded_lanes(
        db_session, _utcnow(), runtime_config=_runtime_config
    )
    assert second == 0, "live-task lane is not stranded — re-run is a no-op"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "terminal_status",
    [TaskStatus.CANCELLED, TaskStatus.FAILED, TaskStatus.COMPLETED],
)
async def test_recovery_prompt_states_the_observed_strand_cause(
    db_session, lanes_on, mocker, terminal_status
):
    """The recovery worker is TOLD why it was spawned, not left to guess.

    The sweep observes exactly which task, in exactly which terminal status,
    stranded the lane — and used to throw all of it away, handing the worker a
    prompt that asserted the task "was cancelled or killed". That is a guess,
    and for two of the three terminal statuses in
    ``_LANE_TERMINAL_TASK_STATUSES`` it is a WRONG guess: a COMPLETED task that
    never wrote a checkpoint is neither cancelled nor killed.

    Parametrizing over all three statuses is what makes this a real pin: a
    hardcoded string cannot satisfy three different expected substrings.
    """
    from app.models import Task as _Task
    from app.review_fix_lane_pilot import (
        LANE_RECOVERY_ATTEMPT_KEY,
        LANE_RECOVERY_REASON_KEY,
    )

    project = await _make_project(db_session, name="Strand Cause")
    stranded_task = await _make_task_with_pr(
        db_session, project, terminal_status, pr_number=77
    )
    lane = await _make_lane(
        db_session,
        project,
        status=ImprovementLaneStatus.ACTIVE,
        current_task_id=stranded_task.id,
        phase_requeues=0,
        name="strand-cause",
    )

    mocker.patch(
        "app.github_token_resolver.get_github_token_for_task",
        new=AsyncMock(return_value=("token", "owner", "repo")),
    )
    mocker.patch(
        "app.github.get_pull_request",
        new=AsyncMock(
            return_value={
                "state": "open",
                "merged": False,
                "base": {"sha": "base-a"},
                "head": {"sha": "head-a"},
            }
        ),
    )

    assert (
        await sweep_stranded_lanes(
            db_session, _utcnow(), runtime_config=_runtime_config
        )
        == 1
    )

    await db_session.refresh(lane)
    recovery = await db_session.get(_Task, lane.current_task_id)
    assert recovery is not None
    prompt = (recovery.first_payload or {}).get("prompt") or ""

    # The observed cause, verbatim: which task, which terminal status, and the
    # fact that no post-commit continuation marker was left behind.
    assert str(stranded_task.id) in prompt, (
        "the prompt must name the task that actually stranded the lane"
    )
    assert str(terminal_status.value) in prompt, (
        "the prompt must name the OBSERVED terminal status, not a guess"
    )
    assert "without writing a checkpoint" in prompt
    assert "post-commit continuation marker" in prompt
    assert "was cancelled or killed" not in prompt, (
        "the old guess is wrong for COMPLETED and for FAILED"
    )
    # The description shown to a human carries the same statement.
    assert recovery.description == prompt

    # The categorical bucket that lane_baseline_snapshot._compute_checkpoint_
    # recovery_waste groups on is UNCHANGED — the free-text cause rides
    # alongside it, it does not replace it.
    ctx = recovery.context or {}
    assert ctx.get(LANE_RECOVERY_REASON_KEY) == "stranded_terminal_task"
    assert ctx.get(LANE_RECOVERY_ATTEMPT_KEY) == 1
    assert str(stranded_task.id) in (ctx.get("_lane_strand_detail") or "")



@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("case", "denial"),
    [
        ("lookup_failure", "retryable"),
        ("unknown_live_state", "retryable"),
        ("existing_owner", "occupied"),
    ],
)
async def test_sweep_stranded_lane_defers_nonterminal_fix_denial(
    db_session, lanes_on, mocker, case, denial
):
    from app.fix_admission import FixAdmissionDecision, FixAdmissionDenial

    project = await _make_project(db_session, name=f"Strand defer {case}")
    failed = await _make_task_with_pr(
        db_session, project, TaskStatus.FAILED, pr_number=100
    )
    lane = await _make_lane(
        db_session,
        project,
        status=ImprovementLaneStatus.ACTIVE,
        current_task_id=failed.id,
        phase_requeues=0,
        name=f"stranded-{case}",
    )
    mocker.patch(
        "app.github_token_resolver.get_github_token_for_task",
        new_callable=AsyncMock,
        return_value=("token", "owner", "repo"),
    )
    denial_kind = FixAdmissionDenial(denial)
    mocker.patch(
        "app.fix_admission.prepare_direct_fix_admission",
        new_callable=AsyncMock,
        return_value=FixAdmissionDecision(
            False,
            f"{case} admission",
            denial=denial_kind,
        ),
    )

    transitioned = await sweep_stranded_lanes(
        db_session, _utcnow(), runtime_config=_runtime_config
    )

    assert transitioned == 0
    await db_session.refresh(lane)
    assert lane.status == ImprovementLaneStatus.ACTIVE.value
    assert lane.current_task_id == failed.id
    assert lane.phase_requeues == 0


@pytest.mark.asyncio
async def test_sweep_stranded_lane_kills_only_terminal_fix_denial(
    db_session, lanes_on, mocker
):
    from app.fix_admission import FixAdmissionDecision, FixAdmissionDenial

    project = await _make_project(db_session, name="Strand terminal denial")
    failed = await _make_task_with_pr(
        db_session, project, TaskStatus.FAILED, pr_number=101
    )
    lane = await _make_lane(
        db_session,
        project,
        status=ImprovementLaneStatus.ACTIVE,
        current_task_id=failed.id,
        phase_requeues=0,
        name="stranded-terminal",
    )
    mocker.patch(
        "app.github_token_resolver.get_github_token_for_task",
        new_callable=AsyncMock,
        return_value=("token", "owner", "repo"),
    )
    mocker.patch(
        "app.fix_admission.prepare_direct_fix_admission",
        new_callable=AsyncMock,
        return_value=FixAdmissionDecision(
            False,
            "PR is no longer open",
            denial=FixAdmissionDenial.TERMINAL,
        ),
    )

    transitioned = await sweep_stranded_lanes(
        db_session, _utcnow(), runtime_config=_runtime_config
    )

    assert transitioned == 1
    await db_session.refresh(lane)
    assert lane.status == ImprovementLaneStatus.KILLED.value


@pytest.mark.asyncio
async def test_sweep_stranded_lane_preserves_pending_terminal_owner(
    db_session, lanes_on, mocker
):
    project = await _make_project(db_session, name="Strand pending owner")
    completed = await _make_task_with_pr(
        db_session, project, TaskStatus.COMPLETED, pr_number=102
    )
    completed.post_commit_pending = True
    db_session.add(completed)
    await db_session.commit()
    lane = await _make_lane(
        db_session,
        project,
        status=ImprovementLaneStatus.ACTIVE,
        current_task_id=completed.id,
        phase_requeues=0,
        name="stranded-pending",
    )
    token_mock = mocker.patch(
        "app.github_token_resolver.get_github_token_for_task",
        new_callable=AsyncMock,
    )
    admission_mock = mocker.patch(
        "app.fix_admission.prepare_direct_fix_admission",
        new_callable=AsyncMock,
    )

    transitioned = await sweep_stranded_lanes(
        db_session, _utcnow(), runtime_config=_runtime_config
    )

    assert transitioned == 0
    token_mock.assert_not_awaited()
    admission_mock.assert_not_awaited()
    await db_session.refresh(lane)
    assert lane.status == ImprovementLaneStatus.ACTIVE.value
    assert lane.current_task_id == completed.id
    assert lane.phase_requeues == 0


@pytest.mark.asyncio
async def test_sweep_stranded_lanes_continues_after_non_cas_exception(
    db_session, lanes_on, mocker
):
    """sweep_stranded_lanes must not abort when a general (non-LaneCASError)
    exception is raised while processing the first stranded lane — the second
    lane must still be processed."""
    project = await _make_project(db_session, name="Non-CAS Continue")

    cancelled1 = await _make_task(db_session, project, TaskStatus.CANCELLED)
    cancelled2 = await _make_task(db_session, project, TaskStatus.CANCELLED)

    lane1 = await _make_lane(
        db_session,
        project,
        status=ImprovementLaneStatus.ACTIVE,
        current_task_id=cancelled1.id,
        name="lane-fail",
    )
    lane2 = await _make_lane(
        db_session,
        project,
        status=ImprovementLaneStatus.ACTIVE,
        current_task_id=cancelled2.id,
        name="lane-ok",
    )

    call_count = 0
    real_mcm = None
    # Snapshot the id: the sweep's rollback after the simulated error expires
    # lane1, and reading lane1.id inside the second _patched_mcm call would
    # itself lazy-load and raise MissingGreenlet on asyncpg.
    lane1_id = lane1.id

    async def _patched_mcm(session, lane_id, **kwargs):
        nonlocal call_count, real_mcm
        call_count += 1
        if lane_id == lane1_id:
            raise RuntimeError("simulated unexpected error on lane1")
        return await real_mcm(session, lane_id, **kwargs)

    from app.improvement_lane_ops import mark_checkpoint_missing as _real_mcm

    real_mcm = _real_mcm
    mocker.patch("app.improvement_lane_ops.mark_checkpoint_missing", new=_patched_mcm)

    transitioned = await sweep_stranded_lanes(
        db_session, _utcnow(), runtime_config=_runtime_config
    )

    # lane1 errored — not counted; lane2 was processed (KILLED, no PR context).
    assert transitioned == 1, "second lane must still be processed after non-CAS error"
    assert call_count == 2, "mark_checkpoint_missing was attempted for both lanes"

    await db_session.refresh(lane2)
    assert lane2.status == ImprovementLaneStatus.KILLED.value, (
        "lane2 must be KILLED despite lane1 raising a non-CAS exception"
    )


@pytest.mark.asyncio
async def test_expired_reservation_sweep_skips_concurrently_leased_member(
    db_session, warm_pool_on, mocker
):
    """If a member is leased after the unlocked expiry scan, the sweep must not
    clear fields or settle cost unless it can lock/revalidate the same IDLE
    reservation."""
    prior_lanes = _runtime_config.get("improvement_lanes_enabled")
    _runtime_config["improvement_lanes_enabled"] = True
    try:
        project = await _make_project(db_session, name="Reserve Expire Race")
        lane = ImprovementLane(
            project_id=project.id,
            title="race lane",
            goal="race goal",
            branch_name="race-branch",
            base_branch="main",
            workspace_state="WARM_RESERVED",
            reserved_idle_cost_usd_total=0.0,
        )
        db_session.add(lane)
        await db_session.commit()
        await db_session.refresh(lane)

        member = await _register(
            db_session, project, "agent-vm-lane-race", PoolVmState.IDLE
        )
        member.idle_since = _utcnow() - timedelta(seconds=120)
        member.reserved_lane_id = lane.id
        member.reservation_expires_at = _utcnow() - timedelta(seconds=30)
        db_session.add(member)
        await db_session.commit()
        await db_session.refresh(member)

        racing_task_id = uuid.uuid4()
        original_execute = db_session.execute
        race_committed = False
        fresh_session_factory = async_sessionmaker(
            bind=db_session.bind, expire_on_commit=False
        )

        async def _execute_with_concurrent_lease(*args, **kwargs):
            nonlocal race_committed
            statement = args[0] if args else kwargs.get("statement")
            result = await original_execute(*args, **kwargs)
            statement_text = str(statement).lower()
            if (
                not race_committed
                and "poolvm" in statement_text
                and "reservation_expires_at" in statement_text
                and "reserved_lane_id" in statement_text
                and "for update" not in statement_text
            ):
                race_committed = True
                async with fresh_session_factory() as fresh_session:
                    leased = await lease_idle_pool_vm(
                        fresh_session,
                        project_id=project.id,
                        leased_task_id=racing_task_id,
                    )
                    assert leased is not None
                    assert leased.vm_instance_id == "agent-vm-lane-race"
                    await fresh_session.commit()
            return result

        mocker.patch.object(
            db_session, "execute", side_effect=_execute_with_concurrent_lease
        )

        await sweep_idle_pool_vms(db_session, _utcnow(), runtime_config=_runtime_config)

        assert race_committed is True
        await db_session.refresh(member)
        assert member.state == PoolVmState.LEASED
        assert member.leased_task_id == racing_task_id
        assert member.idle_since is None
        assert member.reserved_lane_id is None
        assert member.reservation_expires_at is None

        spans = list(
            (
                await original_execute(
                    select(PoolIdleSpan).where(
                        PoolIdleSpan.vm_instance_id == "agent-vm-lane-race"
                    )
                )
            )
            .scalars()
            .all()
        )
        assert len(spans) == 1, "sweep must not double-bank a racing lease span"

        await db_session.refresh(lane)
        assert lane.reserved_idle_cost_usd_total == 0.0
    finally:
        if prior_lanes is None:
            _runtime_config.pop("improvement_lanes_enabled", None)
        else:
            _runtime_config["improvement_lanes_enabled"] = prior_lanes


# ---------------------------------------------------------------------------
# Lane reservation survives a spanning review phase (hard ceiling + telemetry)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_lane_reservation_survives_long_review_then_fix_claims(
    db_session, warm_pool_on
):
    """implement -> long review (soft TTL elapsed while the lane is still PARKED
    awaiting its fix, hard ceiling NOT elapsed) -> fix.

    The maintainer sweep must EXTEND the reservation rather than clear it, so the
    fix STILL claims the reserved member warm (warm-resume receipt path reachable)
    and the claimed-by-fix telemetry records the success.
    """
    prior_lanes = _runtime_config.get("improvement_lanes_enabled")
    _runtime_config["improvement_lanes_enabled"] = True
    try:
        project = await _make_project(db_session, name="Survive Long Review")
        lane = ImprovementLane(
            project_id=project.id,
            title="long review lane",
            goal="span the review",
            branch_name="lane-branch",
            base_branch="main",
            status=ImprovementLaneStatus.PARKED.value,
            workspace_state="WARM_RESERVED",
            workspace_vm_instance_id="agent-vm-long-review",
        )
        db_session.add(lane)
        await db_session.commit()
        await db_session.refresh(lane)

        member = await _register(
            db_session, project, "agent-vm-long-review", PoolVmState.IDLE
        )
        member.idle_since = _utcnow() - timedelta(seconds=120)
        member.reserved_lane_id = lane.id
        # SOFT TTL already lapsed (a long cold review is in flight) ...
        member.reservation_expires_at = _utcnow() - timedelta(seconds=30)
        # ... but the HARD ceiling still has plenty of headroom.
        member.reservation_hard_expires_at = _utcnow() + timedelta(seconds=3600)
        db_session.add(member)
        await db_session.commit()

        # Sweep must NOT clear the reservation — it extends the soft TTL forward.
        await sweep_idle_pool_vms(db_session, _utcnow(), runtime_config=_runtime_config)

        await db_session.refresh(member)
        assert member.state == PoolVmState.IDLE, "member must not be reaped"
        assert member.reserved_lane_id == lane.id, (
            "reservation must survive the long review (not cleared by the sweep)"
        )
        assert (
            member.reservation_expires_at is not None
            and member.reservation_expires_at > _utcnow()
        ), "soft TTL must be re-stamped forward so the member stays protected"
        assert member.reservation_hard_expires_at is not None, (
            "hard ceiling anchor must be preserved across extension"
        )

        await db_session.refresh(lane)
        assert lane.workspace_state == "WARM_RESERVED", (
            "lane must stay WARM_RESERVED while the reservation is extended"
        )

        # Now the fix phase claims its reserved member warm.
        leased = await lease_idle_pool_vm(
            db_session,
            project_id=project.id,
            account_id="acct-1",
            provider="anthropic",
            model="claude-opus-4-8",
            leased_task_id=uuid.uuid4(),
            lane_id=str(lane.id),
        )
        await db_session.commit()
        assert leased is not None, "fix must STILL claim the reserved member"
        assert leased.vm_instance_id == "agent-vm-long-review"
        assert leased.state == PoolVmState.LEASED

        await db_session.refresh(lane)
        assert lane.reservation_claimed_by_fix_count == 1, (
            "claimed-by-fix telemetry must record the warm resume"
        )
        assert lane.reservation_expired_before_fix_count == 0
        assert lane.reservation_force_expired_count == 0
    finally:
        if prior_lanes is None:
            _runtime_config.pop("improvement_lanes_enabled", None)
        else:
            _runtime_config["improvement_lanes_enabled"] = prior_lanes


@pytest.mark.asyncio
async def test_lane_reservation_hard_ceiling_frees_pinned_member(
    db_session, warm_pool_on
):
    """The HARD absolute ceiling ALWAYS frees a reserved member — even while the
    owning lane still looks alive (PARKED) — so a stuck/zombie lane can never pin a
    warm VM indefinitely. Recorded as force-expired."""
    prior_lanes = _runtime_config.get("improvement_lanes_enabled")
    _runtime_config["improvement_lanes_enabled"] = True
    try:
        project = await _make_project(db_session, name="Hard Ceiling Frees")
        lane = ImprovementLane(
            project_id=project.id,
            title="pinned lane",
            goal="never finishes",
            branch_name="pinned-branch",
            base_branch="main",
            status=ImprovementLaneStatus.PARKED.value,
            workspace_state="WARM_RESERVED",
            workspace_vm_instance_id="agent-vm-pinned",
        )
        db_session.add(lane)
        await db_session.commit()
        await db_session.refresh(lane)

        member = await _register(
            db_session, project, "agent-vm-pinned", PoolVmState.IDLE
        )
        member.idle_since = _utcnow() - timedelta(seconds=120)
        member.reserved_lane_id = lane.id
        # Both the soft TTL and the HARD ceiling have elapsed — capacity protection
        # must win even though the lane is still PARKED.
        member.reservation_expires_at = _utcnow() - timedelta(seconds=600)
        member.reservation_hard_expires_at = _utcnow() - timedelta(seconds=30)
        db_session.add(member)
        await db_session.commit()

        await sweep_idle_pool_vms(db_session, _utcnow(), runtime_config=_runtime_config)

        await db_session.refresh(member)
        assert member.state == PoolVmState.IDLE, "member stays IDLE (not reaped)"
        assert member.reserved_lane_id is None, (
            "hard ceiling must free the member even while the lane looks alive"
        )
        assert member.reservation_expires_at is None
        assert member.reservation_hard_expires_at is None

        # A general (non-lane) request can now claim the freed member.
        leased = await lease_idle_pool_vm(
            db_session, project_id=project.id, leased_task_id=uuid.uuid4()
        )
        await db_session.commit()
        assert leased is not None and leased.vm_instance_id == "agent-vm-pinned", (
            "freed member must be generally claimable (pool not pinned)"
        )

        await db_session.refresh(lane)
        assert lane.reservation_force_expired_count == 1, (
            "hard-ceiling release must record force-expired"
        )
        assert lane.reservation_claimed_by_fix_count == 0
        assert lane.reservation_expired_before_fix_count == 0
    finally:
        if prior_lanes is None:
            _runtime_config.pop("improvement_lanes_enabled", None)
        else:
            _runtime_config["improvement_lanes_enabled"] = prior_lanes


@pytest.mark.asyncio
async def test_lane_reservation_expired_before_fix_when_chain_dies(
    db_session, warm_pool_on
):
    """When the chain dies mid-review (the lane is no longer PARKED-awaiting-fix)
    before the hard ceiling, the sweep frees the member and records
    expired-before-fix — the diagnostic signal for low warm_resume_fraction."""
    prior_lanes = _runtime_config.get("improvement_lanes_enabled")
    _runtime_config["improvement_lanes_enabled"] = True
    try:
        project = await _make_project(db_session, name="Chain Dies Mid Review")
        lane = ImprovementLane(
            project_id=project.id,
            title="dead chain lane",
            goal="review failed",
            branch_name="dead-branch",
            base_branch="main",
            # Chain died: the lane is terminal (KILLED), no longer awaiting a fix.
            status=ImprovementLaneStatus.KILLED.value,
            workspace_state="WARM_RESERVED",
            workspace_vm_instance_id="agent-vm-dead-chain",
        )
        db_session.add(lane)
        await db_session.commit()
        await db_session.refresh(lane)

        member = await _register(
            db_session, project, "agent-vm-dead-chain", PoolVmState.IDLE
        )
        member.idle_since = _utcnow() - timedelta(seconds=120)
        member.reserved_lane_id = lane.id
        member.reservation_expires_at = _utcnow() - timedelta(seconds=30)
        # Hard ceiling NOT elapsed: the clear is driven by chain death, not the cap.
        member.reservation_hard_expires_at = _utcnow() + timedelta(seconds=3600)
        db_session.add(member)
        await db_session.commit()

        await sweep_idle_pool_vms(db_session, _utcnow(), runtime_config=_runtime_config)

        await db_session.refresh(member)
        assert member.state == PoolVmState.IDLE
        assert member.reserved_lane_id is None, (
            "a dead chain's reservation must be freed (pool not pinned)"
        )
        assert member.reservation_expires_at is None
        assert member.reservation_hard_expires_at is None

        await db_session.refresh(lane)
        assert lane.reservation_expired_before_fix_count == 1, (
            "chain-death clear must record expired-before-fix"
        )
        assert lane.reservation_force_expired_count == 0
        assert lane.reservation_claimed_by_fix_count == 0
    finally:
        if prior_lanes is None:
            _runtime_config.pop("improvement_lanes_enabled", None)
        else:
            _runtime_config["improvement_lanes_enabled"] = prior_lanes


@pytest.mark.asyncio
async def test_actively_reserved_member_not_idle_ttl_reaped(
    db_session, warm_pool_on, mocker
):
    """An actively-reserved member older than the idle TTL must NOT be reaped while
    its reservation is still live — otherwise a long review loses the warm member
    out from under the reservation."""
    prior_lanes = _runtime_config.get("improvement_lanes_enabled")
    _runtime_config["improvement_lanes_enabled"] = True
    _runtime_config["warm_pool_idle_ttl_seconds"] = 60
    # Keep the VM "alive" and arm the zombie gate so the ONLY reaper that could
    # fire on this aged-idle member is the idle-TTL path — isolating the
    # reservation guard under test.
    mocker.patch("app.warm_vm_state.is_pool_vm_alive", return_value=True)
    mocker.patch.object(_wpm, "_PROCESS_START_TS", _utcnow() - timedelta(seconds=900))
    try:
        project = await _make_project(db_session, name="Reserved Not Idle Reaped")
        lane = ImprovementLane(
            project_id=project.id,
            title="reserved lane",
            goal="goal",
            branch_name="rsv-branch",
            base_branch="main",
            status=ImprovementLaneStatus.PARKED.value,
            workspace_state="WARM_RESERVED",
            workspace_vm_instance_id="agent-vm-reserved-old",
        )
        db_session.add(lane)
        await db_session.commit()
        await db_session.refresh(lane)

        member = await _register(
            db_session, project, "agent-vm-reserved-old", PoolVmState.IDLE
        )
        # Idle far longer than the idle TTL, but the reservation is still LIVE
        # (soft TTL not yet expired) and within the hard ceiling.
        member.idle_since = _utcnow() - timedelta(seconds=600)
        member.reserved_lane_id = lane.id
        member.reservation_expires_at = _utcnow() + timedelta(seconds=300)
        member.reservation_hard_expires_at = _utcnow() + timedelta(seconds=3600)
        db_session.add(member)
        await db_session.commit()

        await sweep_idle_pool_vms(db_session, _utcnow(), runtime_config=_runtime_config)

        # Member must survive: still IDLE and still reserved to the lane.
        refreshed = await db_session.get(PoolVm, member.id)
        assert refreshed is not None, "reserved member must not be idle-TTL reaped"
        assert refreshed.state == PoolVmState.IDLE
        assert refreshed.reserved_lane_id == lane.id
    finally:
        if prior_lanes is None:
            _runtime_config.pop("improvement_lanes_enabled", None)
        else:
            _runtime_config["improvement_lanes_enabled"] = prior_lanes


# ---------------------------------------------------------------------------
# Forced drain: operator drain/disable and global disable retire LEASED members
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_drain_tears_down_leased_member(db_session, warm_pool_on, mocker):
    """drain_global_warm_pool must retire LEASED members, not silently skip them.

    This is the acceptance-gate test named in the task.  Before the fix the else
    branch of retire_and_delete_pool_vm excluded PoolVmState.LEASED from the
    atomic claim predicate, so rowcount==0 and the member was left LEASED -- a
    true leak because the pool stayed enabled and the orphan cleanup exempted
    live pool members.  force=True now includes LEASED in the claim predicate.
    """
    mock_del = mocker.patch(
        "app.vm_lifecycle._ensure_vm_deleted", new_callable=mocker.AsyncMock
    )
    mocker.patch("app.compute.get_stale_image_vms", return_value=[])
    _runtime_config["warm_pool_enabled"] = True

    project = await _make_project(db_session)
    vm_id = "agent-vm-leased-drain"
    pid = str(project.id)
    async with _PoolAccounts(
        [
            {
                "id": "acct-global-drain",
                "active_tasks": 1,
                "daily_task_count": 1,
                "project_state": {
                    pid: {"active_tasks": 1, "daily_task_count": 1}
                },
            }
        ]
    ) as accounts:
        account = accounts[0]
        task = await _make_task_for_account(db_session, project, account["id"])
        task.vm_instance_id = vm_id
        task.context = {
            **(task.context or {}),
            "warm_target_vm_id": vm_id,
            "warm_pool_pin": True,
            "warm_handoff_deadline": "2099-01-01T00:00:00",
            "_warm_pool_reservation_account_id": account["id"],
        }
        member = await _register(
            db_session,
            project,
            vm_id,
            PoolVmState.LEASED,
            account_id=account["id"],
        )
        member.leased_task_id = task.id
        db_session.add_all([task, member])
        await db_session.commit()
        member_id = member.id

        retired = await _wpm.drain_global_warm_pool(
            session_factory=_factory_for(db_session)
        )
        assert retired == 1, f"expected 1 retired, got {retired}"
        mock_del.assert_awaited_once()
        assert await db_session.get(PoolVm, member_id) is None
        await db_session.refresh(task)
        assert task.vm_instance_id is None
        assert (task.context or {}).get("warm_pool_pin") is None
        assert (task.context or {}).get("warm_target_vm_id") is None
        assert (
            task.context or {}
        ).get("_warm_pool_reservation_account_id") is None
        assert account["active_tasks"] == 0
        assert account["daily_task_count"] == 0
        assert account["project_state"][pid] == {
            "active_tasks": 0,
            "daily_task_count": 0,
        }
        assert [
            (m["delta"], m["project_delta"])
            for m in account["_pending_usage_cap_mutations"]
        ] == [(-1, -1)]


@pytest.mark.asyncio
async def test_drain_project_pool_operator_drain_retires_leased_member(
    db_session, warm_pool_on, mocker
):
    """_drain_project_pool with reason='operator_drain' must retire LEASED members."""
    from app.routers.admin_warm_pool import _drain_project_pool

    mocker.patch("app.vm_lifecycle._ensure_vm_deleted", new_callable=mocker.AsyncMock)
    mocker.patch("app.compute.get_stale_image_vms", return_value=[])

    project = await _make_project(db_session)
    vm_id = "agent-vm-leased-opdr"
    pid = str(project.id)
    async with _PoolAccounts(
        [
            {
                "id": "acct-project-drain",
                "active_tasks": 1,
                "daily_task_count": 1,
                "project_state": {
                    pid: {"active_tasks": 1, "daily_task_count": 1}
                },
            }
        ]
    ) as accounts:
        account = accounts[0]
        task = await _make_task_for_account(db_session, project, account["id"])
        task.vm_instance_id = vm_id
        task.context = {
            **(task.context or {}),
            "warm_target_vm_id": vm_id,
            "warm_pool_pin": True,
            "warm_handoff_deadline": "2099-01-01T00:00:00",
            "_warm_pool_reservation_account_id": account["id"],
        }
        member = await _register(
            db_session,
            project,
            vm_id,
            PoolVmState.LEASED,
            account_id=account["id"],
        )
        member.leased_task_id = task.id
        db_session.add_all([task, member])
        await db_session.commit()
        member_id = member.id

        drained = await _drain_project_pool(
            db_session, project.id, "u-test", reason="operator_drain"
        )
        assert drained == 1, f"expected 1 drained, got {drained}"
        assert await db_session.get(PoolVm, member_id) is None
        await db_session.refresh(task)
        assert task.vm_instance_id is None
        assert (task.context or {}).get("warm_pool_pin") is None
        assert (task.context or {}).get("warm_target_vm_id") is None
        assert (
            task.context or {}
        ).get("_warm_pool_reservation_account_id") is None
        assert account["active_tasks"] == 0
        assert account["daily_task_count"] == 0
        assert account["project_state"][pid] == {
            "active_tasks": 0,
            "daily_task_count": 0,
        }
        assert [
            (m["delta"], m["project_delta"])
            for m in account["_pending_usage_cap_mutations"]
        ] == [(-1, -1)]


@pytest.mark.asyncio
async def test_forced_drain_idle_scan_uses_current_leased_task_from_cas(
    db_session, warm_pool_on, mocker
):
    """An IDLE scan that races a lease must abandon the newly-current task."""
    from app.max_plan_pool import reserve_max_plan_account

    mocker.patch("app.vm_lifecycle._ensure_vm_deleted", new_callable=mocker.AsyncMock)
    project = await _make_project(db_session, name="Forced drain IDLE lease race")
    vm_id = "agent-vm-drain-idle-race"
    stale_member = await _register(
        db_session, project, vm_id, PoolVmState.IDLE, account_id="acct-idle-race"
    )
    member_id = stale_member.id
    pid = str(project.id)

    async with _PoolAccounts(
        [
            {
                "id": "acct-idle-race",
                "daily_task_count": 0,
                "project_state": {},
                "disabled_models": {},
            }
        ]
    ) as accounts:
        account = accounts[0]
        assert await reserve_max_plan_account(
            account["id"],
            user_id=project.user_id,
            project_id=pid,
            model="claude-opus-4-8",
            count_daily_task=True,
        )
        task = await _make_task_for_account(db_session, project, account["id"])
        task.vm_instance_id = vm_id
        task.context = {
            **(task.context or {}),
            "warm_target_vm_id": vm_id,
            "warm_pool_pin": True,
            "_warm_pool_reservation_account_id": account["id"],
        }
        db_session.add(task)
        await db_session.commit()

        # Simulate a peer's committed lease with a Core UPDATE that deliberately
        # does not synchronize this session's already-scanned ORM member.
        await db_session.execute(
            update(PoolVm)
            .where(PoolVm.id == member_id)
            .values(
                state=PoolVmState.LEASED,
                leased_task_id=task.id,
                lease_expires_at=_utcnow() + timedelta(seconds=240),
                idle_since=None,
            )
            .execution_options(synchronize_session=False)
        )
        await db_session.commit()
        assert stale_member.state == PoolVmState.IDLE

        assert await retire_and_delete_pool_vm(
            db_session,
            stale_member,
            reason="operator_drain",
            force=True,
            abandon_leased_handoff=True,
        )
        await db_session.refresh(task)
        assert await db_session.get(PoolVm, member_id) is None
        assert task.vm_instance_id is None
        assert (task.context or {}).get("warm_pool_pin") is None
        assert (
            task.context or {}
        ).get("_warm_pool_reservation_account_id") is None
        assert account["active_tasks"] == 0
        assert account["daily_task_count"] == 0
        assert account["project_state"][pid] == {
            "active_tasks": 0,
            "daily_task_count": 0,
        }


@pytest.mark.asyncio
async def test_forced_drain_released_task_a_released_to_task_b_uses_cas_owner(
    db_session, warm_pool_on, mocker
):
    """A stale A lease scan must abandon current task B, never stale task A."""
    mocker.patch("app.vm_lifecycle._ensure_vm_deleted", new_callable=mocker.AsyncMock)
    project = await _make_project(db_session, name="Forced drain A to B race")
    vm_id = "agent-vm-drain-a-b-race"
    pid = str(project.id)

    async with _PoolAccounts(
        [
            {
                "id": "acct-drain-a",
                "active_tasks": 1,
                "daily_task_count": 1,
                "project_state": {
                    pid: {"active_tasks": 1, "daily_task_count": 1}
                },
            },
            {
                "id": "acct-drain-b",
                "active_tasks": 1,
                "daily_task_count": 1,
                "project_state": {
                    pid: {"active_tasks": 1, "daily_task_count": 1}
                },
            },
        ]
    ) as accounts:
        account_a, account_b = accounts
        task_a = await _make_task_for_account(
            db_session, project, account_a["id"]
        )
        task_b = await _make_task_for_account(
            db_session, project, account_b["id"]
        )
        for task, account in ((task_a, account_a), (task_b, account_b)):
            task.vm_instance_id = vm_id
            task.context = {
                **(task.context or {}),
                "warm_target_vm_id": vm_id,
                "warm_pool_pin": True,
                "_warm_pool_reservation_account_id": account["id"],
            }
        stale_member = await _register(
            db_session,
            project,
            vm_id,
            PoolVmState.LEASED,
            account_id=account_a["id"],
        )
        stale_member.leased_task_id = task_a.id
        db_session.add_all([task_a, task_b, stale_member])
        await db_session.commit()
        member_id = stale_member.id

        await db_session.execute(
            update(PoolVm)
            .where(PoolVm.id == member_id)
            .values(
                leased_task_id=task_b.id,
                account_id=account_b["id"],
                lease_expires_at=_utcnow() + timedelta(seconds=240),
            )
            .execution_options(synchronize_session=False)
        )
        await db_session.commit()
        assert stale_member.leased_task_id == task_a.id

        assert await retire_and_delete_pool_vm(
            db_session,
            stale_member,
            reason="operator_drain",
            force=True,
            abandon_leased_handoff=True,
        )
        await db_session.refresh(task_a)
        await db_session.refresh(task_b)
        assert await db_session.get(PoolVm, member_id) is None

        # The stale task A ownership is untouched; the CAS-returned current task
        # B is the one canonically abandoned and released.
        assert (task_a.context or {}).get("warm_pool_pin") is True
        assert (
            task_a.context or {}
        ).get("_warm_pool_reservation_account_id") == account_a["id"]
        assert account_a["active_tasks"] == 1
        assert account_a["daily_task_count"] == 1
        assert task_b.vm_instance_id is None
        assert (task_b.context or {}).get("warm_pool_pin") is None
        assert (
            task_b.context or {}
        ).get("_warm_pool_reservation_account_id") is None
        assert account_b["active_tasks"] == 0
        assert account_b["daily_task_count"] == 0
        assert account_b["project_state"][pid] == {
            "active_tasks": 0,
            "daily_task_count": 0,
        }


@pytest.mark.asyncio
async def test_forced_drain_does_not_unclaim_already_in_progress_task(
    db_session, warm_pool_on, mocker
):
    """A claim committed before the drain's Task lock owns task/account state."""
    mocker.patch("app.vm_lifecycle._ensure_vm_deleted", new_callable=mocker.AsyncMock)
    project = await _make_project(db_session, name="Forced drain after claim")
    vm_id = "agent-vm-drain-after-claim"
    pid = str(project.id)

    async with _PoolAccounts(
        [
            {
                "id": "acct-drain-after-claim",
                "active_tasks": 1,
                "daily_task_count": 1,
                "project_state": {
                    pid: {"active_tasks": 1, "daily_task_count": 1}
                },
            }
        ]
    ) as accounts:
        account = accounts[0]
        task = await _make_task_for_account(db_session, project, account["id"])
        task.status = TaskStatus.IN_PROGRESS
        task.vm_instance_id = vm_id
        task.context = {
            **(task.context or {}),
            "warm_target_vm_id": vm_id,
            "warm_pool_pin": True,
            # Deliberately retain the marker to prove status ownership alone
            # prevents a stale drain from releasing the running task's slot.
            "_warm_pool_reservation_account_id": account["id"],
        }
        member = await _register(
            db_session,
            project,
            vm_id,
            PoolVmState.LEASED,
            account_id=account["id"],
        )
        member.leased_task_id = task.id
        db_session.add_all([task, member])
        await db_session.commit()
        member_id = member.id

        assert await retire_and_delete_pool_vm(
            db_session,
            member,
            reason="operator_drain",
            force=True,
            abandon_leased_handoff=True,
        )
        await db_session.refresh(task)
        assert await db_session.get(PoolVm, member_id) is None
        assert task.status == TaskStatus.IN_PROGRESS
        assert task.vm_instance_id == vm_id
        assert (task.context or {}).get("warm_target_vm_id") == vm_id
        assert (task.context or {}).get("warm_pool_pin") is True
        assert (
            task.context or {}
        ).get("_warm_pool_reservation_account_id") == account["id"]
        assert account["active_tasks"] == 1
        assert account["daily_task_count"] == 1
        assert account["project_state"][pid] == {
            "active_tasks": 1,
            "daily_task_count": 1,
        }


@pytest.mark.asyncio
async def test_drain_project_pool_operator_disable_retires_leased_member(
    db_session, warm_pool_on, mocker
):
    """_drain_project_pool with reason='operator_disable' must retire LEASED members."""
    from app.routers.admin_warm_pool import _drain_project_pool

    mocker.patch("app.vm_lifecycle._ensure_vm_deleted", new_callable=mocker.AsyncMock)
    mocker.patch("app.compute.get_stale_image_vms", return_value=[])

    project = await _make_project(db_session)
    member = await _register(
        db_session, project, "agent-vm-leased-opds", PoolVmState.LEASED
    )
    member_id = member.id

    drained = await _drain_project_pool(
        db_session, project.id, "u-test", reason="operator_disable"
    )
    assert drained == 1, f"expected 1 drained, got {drained}"
    assert await db_session.get(PoolVm, member_id) is None


@pytest.mark.asyncio
async def test_routine_over_target_drain_spares_leased_member(
    db_session, warm_pool_on, mocker
):
    """The routine over-target drain path (non-forced) must NOT tear down a LEASED
    member -- LEASED-protection stays in place for all paths that do not pass
    force=True (i.e. reconcile_project_pool / sweep_idle_pool_vms).

    This verifies that adding force=True to the forced-drain paths did NOT weaken
    the protection on the routine reaping path.
    """
    mocker.patch("app.vm_lifecycle._ensure_vm_deleted", new_callable=mocker.AsyncMock)
    mocker.patch("app.compute.get_stale_image_vms", return_value=[])
    # Target 0, with 1 IDLE + 1 LEASED.  The routine drain triggers (IDLE count 1
    # > target 0) and removes the IDLE member, but must leave the LEASED member
    # untouched — reconcile only drains IDLE members, not LEASED ones.
    _runtime_config["warm_pool_target_size"] = 0
    project = await _make_project(db_session)
    idle_member = await _register(
        db_session, project, "agent-vm-idle-extra", PoolVmState.IDLE
    )
    idle_member.idle_since = _utcnow() - timedelta(seconds=300)
    leased_member = await _register(
        db_session, project, "agent-vm-leased-safe", PoolVmState.LEASED
    )
    db_session.add_all([idle_member, leased_member])
    await db_session.commit()

    async def _no_provision(session, project_id, **kw):
        raise AssertionError("must not provision at target 0")

    summary = await reconcile_project_pool(
        db_session,
        project.id,
        runtime_config=_runtime_config,
        provision_fn=_no_provision,
        dispatch_slots_remaining=10,
        pool_budget_remaining=10,
        stale_vm_ids=set(),
        suppressed=False,
    )
    # The IDLE member is over-target and gets drained; the LEASED member is spared.
    assert summary["drained"] == 1
    assert await db_session.get(PoolVm, idle_member.id) is None
    surviving = await db_session.get(PoolVm, leased_member.id)
    assert surviving is not None and surviving.state == PoolVmState.LEASED


@pytest.mark.asyncio
async def test_global_disable_drain_records_force_expired_cause(
    db_session, warm_pool_on, mocker
):
    """drain_global_warm_pool must record force_expired reservation cause for any
    lane whose warm workspace is backed by a drained VM.

    Before this fix, retire_and_delete_pool_vm cleared the lane workspace via
    clear_lanes_for_torn_down_vm but never called record_lane_reservation_cause,
    so global-disable drain, cost-guard teardown, and stale-image recycle all
    degraded lanes to COLD without incrementing reservation_force_expired_count.
    """
    mocker.patch("app.vm_lifecycle._ensure_vm_deleted", new_callable=mocker.AsyncMock)
    mocker.patch("app.compute.get_stale_image_vms", return_value=[])

    project = await _make_project(db_session, name="Force Expired Cause")
    member = await _register(
        db_session, project, "agent-vm-drain-cause", PoolVmState.IDLE
    )

    lane = ImprovementLane(
        project_id=project.id,
        title="drain cause lane",
        goal="test",
        branch_name="drain-cause-branch",
        base_branch="main",
        status=ImprovementLaneStatus.PARKED.value,
        workspace_state="WARM_RESERVED",
        workspace_vm_instance_id="agent-vm-drain-cause",
    )
    db_session.add(lane)
    await db_session.commit()
    await db_session.refresh(lane)

    assert lane.reservation_force_expired_count == 0

    retired = await _wpm.drain_global_warm_pool(
        session_factory=_factory_for(db_session)
    )
    assert retired == 1, f"expected 1 retired, got {retired}"
    assert await db_session.get(PoolVm, member.id) is None

    await db_session.refresh(lane)
    assert lane.workspace_state == "COLD", "lane must be degraded to COLD after drain"
    assert lane.reservation_force_expired_count == 1, (
        "global-disable drain must record force_expired cause on the cleared lane"
    )


# ---------------------------------------------------------------------------
# Per-project cost guard: lease-side honor
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_lease_blocked_when_per_project_cost_guard_engaged(
    db_session, warm_pool_on, mocker
):
    """When the fleet guard is disengaged but one project's per-project cost guard
    is engaged, try_pool_dispatch must immediately return None and record a
    'cost_guard' lease-miss for that project — without waiting for the maintainer
    tick to drain idle members."""
    from app.warm_backlog_reuse import (
        _pool_lease_hit_events,
        _pool_lease_miss_events,
        get_project_pool_funnel_snapshot,
        record_pool_lease_miss,
    )

    _pool_lease_hit_events.clear()
    _pool_lease_miss_events.clear()

    mocker.patch(
        "app.efficiency_snapshot._compute_windowed_idle_cost",
        new_callable=mocker.AsyncMock,
        side_effect=lambda session, pid, cutoff: {"total": 5.0, "by_account": {}},
    )

    _runtime_config["warm_pool_cost_guard_min_dispatches"] = 5
    _runtime_config["warm_pool_cost_guard_min_idle_cost_usd"] = 1.0
    _runtime_config["warm_pool_cost_guard_min_hit_rate"] = 0.10

    # Fleet guard is disengaged: patch it to return engaged=False.
    mocker.patch(
        "app.warm_pool_maintainer.evaluate_pool_cost_guard",
        new_callable=mocker.AsyncMock,
        return_value={
            "engaged": False,
            "reason": None,
            "hit_rate": 1.0,
            "total_dispatches": 10,
            "idle_cost_usd": 0.0,
            "min_hit_rate": 0.10,
            "min_idle_cost_usd": 1.0,
            "window_hours": 24,
        },
    )

    project = await _make_project(db_session, name="Per-Project Guard Lease")
    # Drive 10 eligible misses so the per-project hit rate is 0% — guard engages.
    for _ in range(10):
        record_pool_lease_miss("no_idle_member", project_id=project.id)

    # Register an IDLE member that would otherwise be leaseable.
    await _register(
        db_session,
        project,
        "agent-vm-per-proj-guard",
        PoolVmState.IDLE,
        account_id="acct-1",
        provider="anthropic",
        model="claude-opus-4-8",
    )

    task = await _make_task(db_session, project, TaskStatus.QUEUED)

    # Pre-check: the per-project guard evaluates as engaged for this project.
    per_guard = await evaluate_project_pool_cost_guard(
        db_session, project.id, runtime_config=_runtime_config
    )
    assert per_guard["engaged"], (
        "pre-condition: per-project guard must be engaged with 10 eligible misses"
    )

    # Record how many cost_guard misses exist before the dispatch call.
    snap_before = get_project_pool_funnel_snapshot(project.id, 24)
    cost_guard_before = snap_before["lease_miss_by_reason"].get("cost_guard", 0)

    result = await try_pool_dispatch(
        db_session,
        task,
        required_provider="anthropic",
        required_model="claude-opus-4-8",
        required_account="acct-1",
    )

    assert result is None, (
        "try_pool_dispatch must return None when per-project cost guard is engaged"
    )

    snap_after = get_project_pool_funnel_snapshot(project.id, 24)
    assert (
        snap_after["lease_miss_by_reason"].get("cost_guard", 0) > cost_guard_before
    ), "a cost_guard lease-miss must be recorded for the project"


@pytest.mark.asyncio
async def test_fleet_guard_engaged_skips_per_project_check(
    db_session, warm_pool_on, mocker
):
    """When the fleet guard is already engaged, the per-project guard must NOT be
    evaluated (short-circuit) — fleet guard acts as a global safety valve."""
    mocker.patch(
        "app.efficiency_snapshot._compute_windowed_idle_cost",
        new_callable=mocker.AsyncMock,
        return_value={"total": 5.0, "by_account": {}},
    )

    # Fleet guard engaged.
    mocker.patch(
        "app.warm_pool_maintainer.evaluate_pool_cost_guard",
        new_callable=mocker.AsyncMock,
        return_value={
            "engaged": True,
            "reason": "fleet guard test",
            "hit_rate": 0.0,
            "total_dispatches": 100,
            "idle_cost_usd": 10.0,
            "min_hit_rate": 0.10,
            "min_idle_cost_usd": 1.0,
            "window_hours": 24,
        },
    )
    per_project_spy = mocker.patch(
        "app.warm_pool_maintainer.evaluate_project_pool_cost_guard",
        new_callable=mocker.AsyncMock,
        return_value={"engaged": False},
    )

    project = await _make_project(db_session, name="Fleet Guard Short-circuit")
    await _register(
        db_session,
        project,
        "agent-vm-fleet-guard-sc",
        PoolVmState.IDLE,
        account_id="acct-1",
        provider="anthropic",
        model="claude-opus-4-8",
    )

    task = await _make_task(db_session, project, TaskStatus.QUEUED)
    result = await try_pool_dispatch(
        db_session,
        task,
        required_provider="anthropic",
        required_model="claude-opus-4-8",
        required_account="acct-1",
    )

    assert result is None, "fleet guard must block the lease"
    per_project_spy.assert_not_called()


# ---------------------------------------------------------------------------
# Acceptance proof: lane kill emits deduplicated operator message
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_lane_kill_emits_operator_message(db_session, lanes_on):
    """Acceptance proof: killing a lane for exhausted checkpoint recovery via
    sweep_exhausted_lane_budgets emits a deduplicated WARNING ProjectMessage
    naming the lane and cause, and a second sweep does NOT emit a duplicate."""
    from sqlmodel import select as sm_select

    from app.models import ProjectMessage

    project = await _make_project(db_session, name="Kill Operator Msg")
    lane = await _make_lane(
        db_session,
        project,
        status=ImprovementLaneStatus.ACTIVE,
        phase_requeues=_LANE_MAX_PHASE_REQUEUES + 1,
        name="op-msg-lane",
    )

    killed = await sweep_exhausted_lane_budgets(db_session)
    assert killed == 1

    await db_session.refresh(lane)
    assert lane.status == ImprovementLaneStatus.KILLED.value

    msgs = (
        (
            await db_session.execute(
                sm_select(ProjectMessage).where(
                    ProjectMessage.project_id == project.id,
                    ProjectMessage.status == "open",
                )
            )
        )
        .scalars()
        .all()
    )
    lane_msgs = [
        m
        for m in msgs
        if (m.context or {}).get("event_type") == "lane_killed_exhausted"
    ]
    assert len(lane_msgs) == 1, "exactly one operator message should be emitted"
    assert str(lane.id) in (lane_msgs[0].context or {}).get("lane_id", ""), (
        "message context must name the killed lane"
    )
    assert "op-msg-lane title" in lane_msgs[0].content, (
        "message content must name the lane title"
    )

    # Re-insert a fresh over-budget lane and sweep again — the dedup key is
    # per-lane so the second lane gets its own message, but the FIRST lane
    # must NOT produce a duplicate.
    lane2 = await _make_lane(
        db_session,
        project,
        status=ImprovementLaneStatus.ACTIVE,
        phase_requeues=_LANE_MAX_PHASE_REQUEUES + 1,
        name="op-msg-lane-2",
    )
    killed2 = await sweep_exhausted_lane_budgets(db_session)
    assert killed2 == 1

    msgs_after = (
        (
            await db_session.execute(
                sm_select(ProjectMessage).where(
                    ProjectMessage.project_id == project.id,
                    ProjectMessage.status == "open",
                )
            )
        )
        .scalars()
        .all()
    )
    lane_msgs_after = [
        m
        for m in msgs_after
        if (m.context or {}).get("event_type") == "lane_killed_exhausted"
    ]
    assert len(lane_msgs_after) == 2, (
        "second sweep should emit one new message for the new lane only (no dup for lane 1)"
    )
    lane_ids_in_msgs = {(m.context or {}).get("lane_id") for m in lane_msgs_after}
    assert str(lane.id) in lane_ids_in_msgs
    assert str(lane2.id) in lane_ids_in_msgs


# ── sweep_idle_pool_vms: LIMIT 500 on candidate scan ────────────────────────


@pytest.mark.asyncio
async def test_sweep_idle_pool_vms_candidate_scan_carries_limit(db_session, mocker):
    """Pass-2 PoolVm candidate scan must include LIMIT 500 to cap per-cycle cost."""
    captured_queries: list = []
    _orig_execute = db_session.execute

    async def _spy_execute(stmt, *a, **kw):
        captured_queries.append(stmt)
        return await _orig_execute(stmt, *a, **kw)

    mocker.patch.object(db_session, "execute", side_effect=_spy_execute)
    mocker.patch(
        "app.warm_pool_maintainer.is_warm_pool_enabled",
        return_value=True,
    )
    mocker.patch(
        "app.warm_pool_maintainer.retire_and_delete_pool_vm",
        new_callable=AsyncMock,
        return_value=True,
    )
    mocker.patch(
        "app.warm_pool_maintainer.sweep_stranded_lanes",
        new_callable=AsyncMock,
        return_value=0,
    )
    mocker.patch(
        "app.warm_pool_maintainer.sweep_exhausted_lane_budgets",
        new_callable=AsyncMock,
        return_value=0,
    )

    await sweep_idle_pool_vms(db_session, _utcnow(), runtime_config=_runtime_config)

    import sqlalchemy.dialects.sqlite

    dialect = sqlalchemy.dialects.sqlite.dialect()
    pool_vm_scans = [
        str(q.compile(dialect=dialect)).upper()
        for q in captured_queries
        if hasattr(q, "compile") and "POOLVM" in str(q.compile(dialect=dialect)).upper()
    ]
    assert pool_vm_scans, "expected at least one pool_vm scan"
    assert any("LIMIT" in s for s in pool_vm_scans), (
        "sweep_idle_pool_vms must apply LIMIT 500 to the PoolVm candidate scan"
    )


# ---------------------------------------------------------------------------
# Transient SHA verification failures must NOT burn phase_requeues budget.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "exc_or_none",
    [
        Exception("rate limit hit"),
        None,
    ],
    ids=["exception", "none"],
)
async def test_transient_is_ancestor_of_does_not_burn_phase_requeues(
    db_session, lanes_on, mocker, exc_or_none
):
    """A transient is_ancestor_of failure (exception OR None return) must return
    False from checkpoint_and_reserve_on_phase_complete WITHOUT incrementing
    lane.phase_requeues — three transient hiccups must not permanently close the lane.

    Selector: backend/testing/test_warm_pool_maintainer.py
    """
    from app.improvement_lane_ops import cas_transition, create_lane
    from app.models import ImprovementLaneStatus
    from app.review_fix_lane_pilot import checkpoint_and_reserve_on_phase_complete

    project = await _make_project(db_session, name="Transient SHA Test")
    project.is_self_repo = True
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    lane = await create_lane(
        db_session,
        project_id=project.id,
        direction_id=None,
        title="transient-sha-test",
        goal="verify transient does not burn budget",
        branch_name="feat/transient-sha",
        base_branch="main",
        pilot_arm="lane",
    )
    await db_session.flush()
    await cas_transition(
        db_session,
        lane.id,
        ImprovementLaneStatus.PROPOSED,
        ImprovementLaneStatus.ACTIVE,
        current_task_id=None,
    )

    source_task = Task(
        project_id=project.id,
        title="fix task",
        payloads=[{"type": "fix", "prompt": "fix", "base_branch": "main"}],
        context={"head_sha": "deadbeef1234"},
        status=TaskStatus.EVALUATING,
    )
    db_session.add(source_task)
    await db_session.commit()
    await db_session.refresh(source_task)
    lane.current_task_id = source_task.id
    db_session.add(lane)
    await db_session.commit()
    await db_session.refresh(lane)

    if exc_or_none is None:
        mocker.patch(
            "app.github_token_resolver.get_github_token_for_task",
            new_callable=AsyncMock,
            return_value=("gh-token", "owner", "repo"),
        )
        mocker.patch(
            "app.github_refs.is_ancestor_of",
            new_callable=AsyncMock,
            return_value=None,
        )
    else:
        mocker.patch(
            "app.github_token_resolver.get_github_token_for_task",
            new_callable=AsyncMock,
            side_effect=exc_or_none,
        )

    initial_requeues = lane.phase_requeues or 0

    result = await checkpoint_and_reserve_on_phase_complete(
        db_session,
        source_task,
        lane,
        sha="deadbeef1234",
        summary="phase summary",
    )

    assert result is False, "transient failure must return False"
    await db_session.refresh(lane)
    assert (lane.phase_requeues or 0) == initial_requeues, (
        "transient is_ancestor_of failure must NOT increment phase_requeues"
    )


# ---------------------------------------------------------------------------
# sweep_lane_pilot_kill_verdict — reaper-owned KILL sweep
# ---------------------------------------------------------------------------


@pytest.fixture
def pilot_on(lanes_on):
    """Enable both improvement_lanes_enabled and review_fix_lane_pilot_enabled."""
    prior = _runtime_config.get("review_fix_lane_pilot_enabled")
    _runtime_config["review_fix_lane_pilot_enabled"] = True
    try:
        yield
    finally:
        if prior is None:
            _runtime_config.pop("review_fix_lane_pilot_enabled", None)
        else:
            _runtime_config["review_fix_lane_pilot_enabled"] = prior


@pytest.mark.asyncio
async def test_sweep_lane_pilot_kill_verdict_noop_when_pilot_disabled(db_session):
    """sweep_lane_pilot_kill_verdict returns 0 immediately when the pilot is off,
    without querying ImprovementLane at all."""
    prior = _runtime_config.get("review_fix_lane_pilot_enabled")
    _runtime_config["review_fix_lane_pilot_enabled"] = False
    try:
        from app.review_fix_lane_pilot import sweep_lane_pilot_kill_verdict

        result = await sweep_lane_pilot_kill_verdict(db_session)
        assert result == 0
    finally:
        if prior is None:
            _runtime_config.pop("review_fix_lane_pilot_enabled", None)
        else:
            _runtime_config["review_fix_lane_pilot_enabled"] = prior


@pytest.mark.asyncio
async def test_sweep_lane_pilot_kill_verdict_skips_already_killed(
    db_session, pilot_on, mocker
):
    """When a project already has a durable KILL verdict recorded in
    capability_adoption_verdicts, the sweep is a no-op for that project."""
    from app.capability_trial_verdict import LANE_PILOT_CAP_NAME
    from app.review_fix_lane_pilot import sweep_lane_pilot_kill_verdict

    project = await _make_project(db_session, name="Already Killed")
    project.capability_adoption_verdicts = {
        LANE_PILOT_CAP_NAME: {"verdict": "KILL", "verdict_reason": "prior_kill"}
    }
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    await _make_lane(
        db_session, project, status=ImprovementLaneStatus.ACTIVE, name="killed-lane"
    )

    kill_fn = mocker.patch(
        "app.review_fix_lane_pilot.apply_lane_pilot_kill_in_transaction",
        new_callable=AsyncMock,
    )

    result = await sweep_lane_pilot_kill_verdict(db_session)
    assert result == 0
    kill_fn.assert_not_awaited()


@pytest.mark.asyncio
async def test_sweep_lane_pilot_kill_verdict_applies_kill(db_session, pilot_on, mocker):
    """Acceptance seam: sweep_lane_pilot_kill_verdict applies a KILL when
    evaluate_lane_pilot_verdict returns KILL (gate-integrity violations >= 3).

    Verifies:
    - return value >= 1
    - apply_lane_pilot_kill_in_transaction is called with reason='reaper_sweep'
    - alert_detail is set because immediate_alert=True from gate violations
    """
    from app.review_fix_lane_pilot import sweep_lane_pilot_kill_verdict

    project = await _make_project(db_session, name="Kill Candidate")
    project.is_self_repo = True
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    await _make_lane(
        db_session, project, status=ImprovementLaneStatus.ACTIVE, name="kill-lane"
    )

    # Aggregates with 3 gate-integrity violations trigger an immediate-alert KILL.
    kill_aggregates = {
        "n_lane": 0,
        "n_control": 0,
        "n_shadow": 0,
        "lane_merge_rate": None,
        "control_merge_rate": None,
        "lane_cost_per_merged": None,
        "control_cost_per_merged": None,
        "lane_runtime_cost_per_merged": None,
        "control_runtime_cost_per_merged": None,
        "lane_reserved_idle_cost_per_merged": None,
        "control_reserved_idle_cost_per_merged": None,
        "gate_integrity_violations": 3,
        "control_warm_resume_count": 0,
        "checkpoint_loss_rate": None,
        "checkpoint_attempts_total": 0,
        "checkpoint_requeues_total": 0,
        "warm_resume_fraction": None,
        "checkpoint_fraction": None,
        "lane_fix_tasks_sampled": 0,
    }

    mocker.patch(
        "app.review_fix_lane_pilot.compute_lane_pilot_aggregates",
        new_callable=AsyncMock,
        return_value=kill_aggregates,
    )
    kill_fn = mocker.patch(
        "app.review_fix_lane_pilot.apply_lane_pilot_kill_in_transaction",
        new_callable=AsyncMock,
        return_value={
            "flag_disabled": True,
            "reservations_cleared": 0,
            "lanes_killed": 1,
        },
    )

    result = await sweep_lane_pilot_kill_verdict(db_session)

    assert result >= 1, "sweep must return >= 1 when KILL is applied"
    kill_fn.assert_awaited_once()
    call_kwargs = kill_fn.await_args
    assert call_kwargs.kwargs["reason"] == "reaper_sweep"
    # Gate violations set immediate_alert=True → alert_detail must be non-None.
    assert call_kwargs.kwargs["alert_detail"] is not None


@pytest.mark.asyncio
async def test_leader_startup_reclaims_orphaned_lane_workspaces(db_session):
    """reclaim_orphaned_lane_workspace_states resets workspace_state to COLD for lanes
    whose backing pool VM no longer exists, and leaves intact lanes whose VM is present.

    This test exercises the production sweep that lifecycle.py calls at leader startup
    to recover workspace state orphaned by out-of-process pool VM deletions.
    """
    from app.improvement_lane_ops import reclaim_orphaned_lane_workspace_states

    project = await _make_project(db_session, name="Reclaim Test")

    # Lane whose backing VM is gone — should be reclaimed.
    orphaned_lane = ImprovementLane(
        project_id=project.id,
        title="orphaned lane",
        goal="orphaned goal",
        branch_name="lane/orphaned",
        base_branch="automain",
        status=ImprovementLaneStatus.ACTIVE.value,
        workspace_state="WARM_RESERVED",
        workspace_vm_instance_id="vm-gone-from-db",
    )
    db_session.add(orphaned_lane)

    live_lane = ImprovementLane(
        project_id=project.id,
        title="live lane",
        goal="live goal",
        branch_name="lane/live",
        base_branch="automain",
        status=ImprovementLaneStatus.ACTIVE.value,
        workspace_state="WARM_RESERVED",
        workspace_vm_instance_id="vm-still-alive",
    )
    # Create a real matching reservation so the second lane keeps its workspace_state.
    live_vm = PoolVm(
        vm_instance_id="vm-still-alive",
        state=PoolVmState.IDLE,
        project_id=project.id,
        reserved_lane_id=live_lane.id,
        reservation_expires_at=_utcnow() + timedelta(minutes=30),
        reservation_hard_expires_at=_utcnow() + timedelta(hours=1),
    )
    db_session.add_all([live_lane, live_vm])

    # Lane already COLD — should be untouched.
    cold_lane = ImprovementLane(
        project_id=project.id,
        title="cold lane",
        goal="cold goal",
        branch_name="lane/cold",
        base_branch="automain",
        status=ImprovementLaneStatus.ACTIVE.value,
        workspace_state="COLD",
        workspace_vm_instance_id=None,
    )
    db_session.add(cold_lane)

    await db_session.commit()
    await db_session.refresh(orphaned_lane)
    await db_session.refresh(live_lane)
    await db_session.refresh(cold_lane)

    reclaimed = await reclaim_orphaned_lane_workspace_states(db_session)
    await db_session.commit()
    await db_session.refresh(orphaned_lane)
    await db_session.refresh(live_lane)
    await db_session.refresh(cold_lane)

    assert reclaimed == 1, f"expected 1 reclaimed lane, got {reclaimed}"
    assert orphaned_lane.workspace_state == "COLD"
    assert orphaned_lane.workspace_vm_instance_id is None
    assert live_lane.workspace_state == "WARM_RESERVED"
    assert live_lane.workspace_vm_instance_id == "vm-still-alive"
    assert cold_lane.workspace_state == "COLD"


@pytest.mark.asyncio
async def test_periodic_reclaim_clears_orphaned_workspace(
    db_session, warm_pool_on, mocker
):
    """sweep_idle_pool_vms runs reclaim_orphaned_lane_workspace_states as a
    periodic defense-in-depth pass.  A lane whose backing VM was removed
    out-of-band (GCP console, crash during retire) should have its
    workspace_state reset to COLD by the end of the sweep, without requiring
    an orchestrator restart.
    """
    project = await _make_project(db_session, name="Periodic Reclaim Test")

    live_lane = ImprovementLane(
        project_id=project.id,
        title="live lane",
        goal="live goal",
        branch_name="lane/periodic-live",
        base_branch="automain",
        status=ImprovementLaneStatus.ACTIVE.value,
        workspace_state="WARM_RESERVED",
        workspace_vm_instance_id="vm-periodic-alive",
    )
    # Lane whose VM exists in the DB with a valid reservation — workspace_state
    # should survive.
    live_vm = PoolVm(
        vm_instance_id="vm-periodic-alive",
        state=PoolVmState.IDLE,
        project_id=project.id,
        reserved_lane_id=live_lane.id,
        reservation_expires_at=_utcnow() + timedelta(minutes=30),
        reservation_hard_expires_at=_utcnow() + timedelta(hours=1),
    )
    db_session.add_all([live_lane, live_vm])

    # Lane whose VM is absent — should be cleared by the periodic sweep.
    orphaned_lane = ImprovementLane(
        project_id=project.id,
        title="orphaned lane",
        goal="orphaned goal",
        branch_name="lane/periodic-orphan",
        base_branch="automain",
        status=ImprovementLaneStatus.ACTIVE.value,
        workspace_state="LEASED",
        workspace_vm_instance_id="vm-periodic-gone",
    )
    db_session.add(orphaned_lane)

    await db_session.commit()
    await db_session.refresh(live_lane)
    await db_session.refresh(orphaned_lane)

    now = _utcnow()
    mocker.patch("app.warm_pool_maintainer._PROCESS_START_TS", now - timedelta(hours=1))
    mocker.patch(
        "app.warm_pool_maintainer._stale_pool_image_ids",
        new=AsyncMock(return_value=set()),
    )
    mocker.patch("app.warm_vm_state.is_pool_vm_alive", return_value=True)

    runtime_config = {}
    await sweep_idle_pool_vms(db_session, now, runtime_config=runtime_config)

    await db_session.refresh(live_lane)
    await db_session.refresh(orphaned_lane)

    assert orphaned_lane.workspace_state == "COLD", (
        f"orphaned lane should be COLD after sweep, got {orphaned_lane.workspace_state}"
    )
    assert orphaned_lane.workspace_vm_instance_id is None
    assert live_lane.workspace_state == "WARM_RESERVED", (
        f"live lane should remain WARM_RESERVED, got {live_lane.workspace_state}"
    )


@pytest.mark.asyncio
async def test_lane_workspace_reconciliation_dry_run_is_structured_and_idempotent(
    db_session,
):
    from app.improvement_lane_ops import reconcile_lane_workspaces

    project = await _make_project(db_session, name="Workspace Dry Run")
    lane = ImprovementLane(
        project_id=project.id,
        title="dry-run orphan",
        goal="dry-run goal",
        branch_name="lane/dry-run",
        base_branch="automain",
        status=ImprovementLaneStatus.ACTIVE.value,
        workspace_state="WARM_RESERVED",
        workspace_vm_instance_id="vm-dry-run-missing",
    )
    db_session.add(lane)
    await db_session.commit()

    dry = await reconcile_lane_workspaces(db_session, dry_run=True)
    assert dry["dry_run"] is True
    assert dry["missing_backing_vm"] == 1
    assert dry["lanes_would_clear"] == 1
    assert dry["lanes_cleared"] == 0

    await db_session.refresh(lane)
    assert lane.workspace_state == "WARM_RESERVED"
    assert lane.workspace_vm_instance_id == "vm-dry-run-missing"

    repaired = await reconcile_lane_workspaces(db_session)
    await db_session.commit()
    assert repaired["missing_backing_vm"] == 1
    assert repaired["lanes_cleared"] == 1

    second = await reconcile_lane_workspaces(db_session)
    assert second["lanes_cleared"] == 0
    assert second["lanes_would_clear"] == 0


@pytest.mark.asyncio
async def test_lane_workspace_reconciliation_clears_terminal_and_missing_leased_task(
    db_session,
):
    from app.improvement_lane_ops import reconcile_lane_workspaces

    project = await _make_project(db_session, name="Leased Task Reconcile")
    terminal_task = await _make_task(db_session, project, TaskStatus.COMPLETED)

    terminal_lane = ImprovementLane(
        project_id=project.id,
        title="terminal leased lane",
        goal="terminal goal",
        branch_name="lane/terminal-leased",
        base_branch="automain",
        status=ImprovementLaneStatus.ACTIVE.value,
        current_task_id=terminal_task.id,
        workspace_state="LEASED",
        workspace_vm_instance_id="vm-terminal-task",
    )
    missing_lane = ImprovementLane(
        project_id=project.id,
        title="missing leased lane",
        goal="missing goal",
        branch_name="lane/missing-leased",
        base_branch="automain",
        status=ImprovementLaneStatus.ACTIVE.value,
        current_task_id=uuid.uuid4(),
        workspace_state="LEASED",
        workspace_vm_instance_id="vm-missing-task",
    )
    db_session.add_all(
        [
            terminal_lane,
            missing_lane,
            PoolVm(
                vm_instance_id="vm-terminal-task",
                project_id=project.id,
                state=PoolVmState.LEASED,
                leased_task_id=terminal_task.id,
            ),
            PoolVm(
                vm_instance_id="vm-missing-task",
                project_id=project.id,
                state=PoolVmState.LEASED,
                leased_task_id=uuid.uuid4(),
            ),
        ]
    )
    await db_session.commit()

    result = await reconcile_lane_workspaces(db_session)
    await db_session.commit()

    assert result["terminal_or_missing_leased_task"] == 2
    assert result["lanes_cleared"] == 2
    await db_session.refresh(terminal_lane)
    await db_session.refresh(missing_lane)
    assert terminal_lane.workspace_state == "COLD"
    assert missing_lane.workspace_state == "COLD"


@pytest.mark.asyncio
async def test_lane_workspace_reconciliation_project_filter_isolates_repairs(
    db_session,
):
    from app.improvement_lane_ops import reconcile_lane_workspaces

    project_a = await _make_project(db_session, name="Project A Reconcile")
    project_b = await _make_project(db_session, name="Project B Reconcile")
    lane_b = ImprovementLane(
        project_id=project_b.id,
        title="valid b lane",
        goal="valid b goal",
        branch_name="lane/project-b",
        base_branch="automain",
        status=ImprovementLaneStatus.PARKED.value,
        workspace_state="WARM_RESERVED",
        workspace_vm_instance_id="vm-cross-project",
    )
    lane_a = ImprovementLane(
        project_id=project_a.id,
        title="cross project lane",
        goal="cross project goal",
        branch_name="lane/project-a",
        base_branch="automain",
        status=ImprovementLaneStatus.PARKED.value,
        workspace_state="WARM_RESERVED",
        workspace_vm_instance_id="vm-cross-project",
    )
    member_b = PoolVm(
        vm_instance_id="vm-cross-project",
        project_id=project_b.id,
        state=PoolVmState.IDLE,
        reserved_lane_id=lane_b.id,
        reservation_expires_at=_utcnow() + timedelta(minutes=30),
        reservation_hard_expires_at=_utcnow() + timedelta(hours=1),
    )
    db_session.add_all([lane_a, lane_b, member_b])
    await db_session.commit()

    result = await reconcile_lane_workspaces(db_session, project_id=project_a.id)
    await db_session.commit()

    assert result["project_mismatch"] == 1
    assert result["lanes_cleared"] == 1
    assert result["reservations_cleared"] == 0
    await db_session.refresh(lane_a)
    await db_session.refresh(lane_b)
    await db_session.refresh(member_b)
    assert lane_a.workspace_state == "COLD"
    assert lane_b.workspace_state == "WARM_RESERVED"
    assert member_b.reserved_lane_id == lane_b.id


@pytest.mark.asyncio
async def test_lane_workspace_reconciliation_clears_stale_terminal_reservation(
    db_session,
):
    from app.improvement_lane_ops import reconcile_lane_workspaces

    project = await _make_project(db_session, name="Stale Reservation")
    lane = ImprovementLane(
        project_id=project.id,
        title="terminal reserved lane",
        goal="terminal reserved goal",
        branch_name="lane/stale-reservation",
        base_branch="automain",
        status=ImprovementLaneStatus.KILLED.value,
        workspace_state="WARM_RESERVED",
        workspace_vm_instance_id="vm-stale-reservation",
    )
    member = PoolVm(
        vm_instance_id="vm-stale-reservation",
        project_id=project.id,
        state=PoolVmState.IDLE,
        reserved_lane_id=lane.id,
        reservation_expires_at=_utcnow() + timedelta(minutes=30),
        reservation_hard_expires_at=_utcnow() + timedelta(hours=1),
    )
    db_session.add_all([lane, member])
    await db_session.commit()

    result = await reconcile_lane_workspaces(db_session)
    await db_session.commit()

    assert result["terminal_lane_workspace"] == 1
    assert result["terminal_reservation_lane"] == 1
    assert result["lanes_cleared"] == 1
    assert result["reservations_cleared"] == 1
    await db_session.refresh(lane)
    await db_session.refresh(member)
    assert lane.workspace_state == "COLD"
    assert member.reserved_lane_id is None
    assert member.reservation_expires_at is None
    assert lane.reservation_force_expired_count == 1


@pytest.mark.asyncio
async def test_lane_workspace_reconciliation_clears_non_idle_reservation(
    db_session,
):
    from app.improvement_lane_ops import reconcile_lane_workspaces

    project = await _make_project(db_session, name="Non Idle Reservation")
    lane = ImprovementLane(
        project_id=project.id,
        title="non-idle reserved lane",
        goal="non-idle reserved goal",
        branch_name="lane/non-idle-reservation",
        base_branch="automain",
        status=ImprovementLaneStatus.PARKED.value,
        workspace_state="WARM_RESERVED",
        workspace_vm_instance_id="vm-non-idle-reservation",
    )
    member = PoolVm(
        vm_instance_id="vm-non-idle-reservation",
        project_id=project.id,
        state=PoolVmState.LEASED,
        reserved_lane_id=lane.id,
        reservation_expires_at=_utcnow() + timedelta(minutes=30),
        reservation_hard_expires_at=_utcnow() + timedelta(hours=1),
    )
    db_session.add_all([lane, member])
    await db_session.commit()

    result = await reconcile_lane_workspaces(db_session)
    await db_session.commit()

    assert result["stale_workspace_reservation"] == 1
    assert result["non_idle_reservations"] == 1
    assert result["lanes_cleared"] == 1
    assert result["reservations_cleared"] == 1
    await db_session.refresh(lane)
    await db_session.refresh(member)
    assert lane.workspace_state == "COLD"
    assert member.reserved_lane_id is None
    assert member.reservation_expires_at is None
    assert lane.reservation_force_expired_count == 1


@pytest.mark.asyncio
async def test_lane_workspace_reconciliation_race_safe_noop(db_session, mocker):
    from app import improvement_lane_ops as ops

    project = await _make_project(db_session, name="Race Reconcile")
    lane = ImprovementLane(
        project_id=project.id,
        title="race lane",
        goal="race goal",
        branch_name="lane/race",
        base_branch="automain",
        status=ImprovementLaneStatus.ACTIVE.value,
        workspace_state="WARM_RESERVED",
        workspace_vm_instance_id="vm-race-old",
    )
    db_session.add(lane)
    await db_session.commit()
    await db_session.refresh(lane)

    async def raced_clear(session, scanned_lane):
        from sqlalchemy import update

        old_state = scanned_lane.workspace_state
        old_vm = scanned_lane.workspace_vm_instance_id
        await session.execute(
            update(ImprovementLane)
            .where(ImprovementLane.id == scanned_lane.id)
            .values(
                workspace_state="WARM_RESERVED",
                workspace_vm_instance_id="vm-race-new",
            )
            .execution_options(synchronize_session=False)
        )
        result = await session.execute(
            update(ImprovementLane)
            .where(
                ImprovementLane.id == scanned_lane.id,
                ImprovementLane.workspace_state == old_state,
                ImprovementLane.workspace_vm_instance_id == old_vm,
            )
            .values(workspace_state="COLD", workspace_vm_instance_id=None)
            .execution_options(synchronize_session=False)
        )
        return bool(result.rowcount)

    mocker.patch.object(ops, "_clear_lane_workspace_if_snapshot_matches", raced_clear)

    result = await ops.reconcile_lane_workspaces(db_session)
    await db_session.commit()

    assert result["missing_backing_vm"] == 1
    assert result["lanes_cleared"] == 0
    assert result["race_noops"] == 1
    await db_session.refresh(lane)
    assert lane.workspace_state == "WARM_RESERVED"
    assert lane.workspace_vm_instance_id == "vm-race-new"


# ---------------------------------------------------------------------------
# Stale PARKED lane retirement (Class 3 in sweep_stranded_lanes)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_retire_stale_parked_lane_no_live_tasks(db_session, lanes_on):
    """A PARKED lane older than the threshold with all tasks terminal is retired."""
    project = await _make_project(db_session, name="Retire Parked")
    stale_cutoff = _utcnow() - timedelta(seconds=7200)
    lane = await _make_lane(
        db_session,
        project,
        status=ImprovementLaneStatus.PARKED,
        parked_at=stale_cutoff,
        name="retire-parked",
    )
    _runtime_config["improvement_lane_parked_terminal_stale_seconds"] = 3600

    transitioned = await sweep_stranded_lanes(
        db_session, _utcnow(), runtime_config=_runtime_config
    )
    assert transitioned == 1

    await db_session.refresh(lane)
    assert lane.status == ImprovementLaneStatus.RETIRED.value


@pytest.mark.asyncio
async def test_stale_parked_lane_spared_when_live_task_references_it(
    db_session, lanes_on
):
    """A PARKED lane is NOT retired when a non-terminal task references its ID."""
    project = await _make_project(db_session, name="Spare Parked Live")
    stale_cutoff = _utcnow() - timedelta(seconds=7200)
    lane = await _make_lane(
        db_session,
        project,
        status=ImprovementLaneStatus.PARKED,
        parked_at=stale_cutoff,
        name="spare-parked-live",
    )
    live_task = Task(
        project_id=project.id,
        title="chain task",
        payloads=[{"type": "fix", "prompt": "x", "base_branch": "automain"}],
        status=TaskStatus.IN_PROGRESS,
        context={"_review_fix_lane_id": str(lane.id)},
    )
    db_session.add(live_task)
    await db_session.commit()
    _runtime_config["improvement_lane_parked_terminal_stale_seconds"] = 3600

    transitioned = await sweep_stranded_lanes(
        db_session, _utcnow(), runtime_config=_runtime_config
    )
    assert transitioned == 0

    await db_session.refresh(lane)
    assert lane.status == ImprovementLaneStatus.PARKED.value


@pytest.mark.asyncio
async def test_multiple_stale_parked_lanes_same_project_both_retired(
    db_session, lanes_on
):
    """Two stale PARKED lanes in the same project with no live chain tasks are
    both retired in a single sweep call (correctness of the batched path)."""
    project = await _make_project(db_session, name="Batch Parked")
    stale_cutoff = _utcnow() - timedelta(seconds=7200)
    lane_a = await _make_lane(
        db_session,
        project,
        status=ImprovementLaneStatus.PARKED,
        parked_at=stale_cutoff,
        name="batch-lane-a",
    )
    lane_b = await _make_lane(
        db_session,
        project,
        status=ImprovementLaneStatus.PARKED,
        parked_at=stale_cutoff,
        name="batch-lane-b",
    )
    _runtime_config["improvement_lane_parked_terminal_stale_seconds"] = 3600

    transitioned = await sweep_stranded_lanes(
        db_session, _utcnow(), runtime_config=_runtime_config
    )
    assert transitioned == 2, "both stale PARKED lanes with no live chain should retire"

    await db_session.refresh(lane_a)
    await db_session.refresh(lane_b)
    assert lane_a.status == ImprovementLaneStatus.RETIRED.value
    assert lane_b.status == ImprovementLaneStatus.RETIRED.value


@pytest.mark.asyncio
async def test_stale_parked_lane_live_task_prevents_only_its_own_lane(
    db_session, lanes_on
):
    """With two stale PARKED lanes: lane A has a live chain task, lane B does not.
    Only lane B is retired; lane A is spared. Both lanes are in the same project."""
    project = await _make_project(db_session, name="Selective Retire")
    stale_cutoff = _utcnow() - timedelta(seconds=7200)

    lane_a = await _make_lane(
        db_session,
        project,
        status=ImprovementLaneStatus.PARKED,
        parked_at=stale_cutoff,
        name="has-live-task",
    )
    lane_b = await _make_lane(
        db_session,
        project,
        status=ImprovementLaneStatus.PARKED,
        parked_at=stale_cutoff,
        name="no-live-task",
    )

    # Live task references lane_a only.
    live_task = Task(
        project_id=project.id,
        title="chain task for a",
        payloads=[{"type": "fix", "prompt": "x", "base_branch": "automain"}],
        status=TaskStatus.QUEUED,
        context={"_review_fix_lane_id": str(lane_a.id)},
    )
    db_session.add(live_task)
    await db_session.commit()
    _runtime_config["improvement_lane_parked_terminal_stale_seconds"] = 3600

    transitioned = await sweep_stranded_lanes(
        db_session, _utcnow(), runtime_config=_runtime_config
    )
    assert transitioned == 1, "only lane B (no live chain task) should be retired"

    await db_session.refresh(lane_a)
    await db_session.refresh(lane_b)
    assert lane_a.status == ImprovementLaneStatus.PARKED.value, (
        "lane A has a live task — must not be retired"
    )
    assert lane_b.status == ImprovementLaneStatus.RETIRED.value, (
        "lane B has no live task — must be retired"
    )

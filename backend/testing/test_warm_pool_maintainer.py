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

import pytest
from sqlalchemy.exc import SQLAlchemyError
from sqlmodel import select

import app.shared_state as _shared_state
import app.warm_pool_maintainer as _wpm
from app.models import (
    ImprovementLane,
    PoolIdleCostLedger,
    PoolIdleSpan,
    PoolVm,
    PoolVmState,
    Project,
    Task,
    TaskStatus,
)
from app.shared_state import runtime_config as _runtime_config
from app.time_utils import _utcnow
from app.warm_pool import (
    count_idle_pool_vms,
    count_live_pool_vms,
    get_live_pool_vm_ids,
    is_pool_vm_image_stale,
    lease_idle_pool_vm,
    register_pool_vm,
    resolve_project_warm_pool_target,
    warm_pool_project_target_map,
)
from app.warm_pool_maintainer import (
    evaluate_pool_cost_guard,
    evaluate_project_pool_cost_guard,
    reconcile_project_pool,
    retire_and_delete_pool_vm,
    run_pool_maintainer_cycle,
    sweep_idle_pool_vms,
)


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

    # Must not raise (no MissingGreenlet), and the healthy member is still reaped.
    retired = await sweep_idle_pool_vms(
        db_session, _utcnow(), runtime_config=_runtime_config
    )
    assert retired == 1
    # Both candidates were attempted in order — the failure did not abort the loop.
    assert attempted == ["agent-vm-fail", "agent-vm-ok"]
    assert await db_session.get(PoolVm, m_ok.id) is None  # the OK one was deleted
    assert await db_session.get(PoolVm, m_fail.id) is not None  # failed one survives


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
    """A project with high idle cost and low hit rate has its pool drained
    independently while a co-tenant project with good hit rate is unaffected.

    Project A: all dispatches are warm-pool hits (hit rate 100% — guard off).
    Project B: no warm-pool hits, high idle cost (hit rate 0% — guard engages).
    Only B's idle member should be drained; A's must survive."""
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

    # Project A: 10 warm-pool hits → 100% hit rate → guard stays off.
    for _ in range(10):
        await _make_dispatched_task(db_session, project_a, warm_pool_hit=True)
    idle_a = await _register(
        db_session, project_a, "agent-vm-guard-a", PoolVmState.IDLE
    )
    idle_a.idle_since = _utcnow() - timedelta(seconds=300)
    db_session.add(idle_a)

    # Project B: 10 cold dispatches, no hits → 0% hit rate + $5 idle → guard engages.
    for _ in range(10):
        await _make_dispatched_task(db_session, project_b, warm_pool_hit=False)
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

    # Project A (healthy) survives; Project B (low hit rate) is drained.
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
    """A project with low hit rate but fewer than min_dispatches dispatches is NOT
    drained — thin-sample protection mirrors the fleet guard's behavior."""
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
    # Only 3 cold dispatches — below the min_dispatches floor.
    for _ in range(3):
        await _make_dispatched_task(db_session, project, warm_pool_hit=False)
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
            reserved_idle_cost_usd_total=0.0,
        )
        db_session.add(lane)
        await db_session.commit()
        await db_session.refresh(lane)

        member = await _register(
            db_session, project, "agent-vm-lane-expire", PoolVmState.IDLE
        )
        idle_start = _utcnow() - timedelta(seconds=120)
        member.idle_since = idle_start
        member.reserved_lane_id = lane.id
        member.reservation_expires_at = _utcnow() - timedelta(seconds=30)
        db_session.add(member)
        await db_session.commit()

        await sweep_idle_pool_vms(db_session, _utcnow(), runtime_config=_runtime_config)

        # Member still exists and is IDLE — reservation clear, not reaped.
        await db_session.refresh(member)
        assert member.state == PoolVmState.IDLE, (
            "Member must not be reaped, only cleared"
        )
        assert member.reserved_lane_id is None, "reserved_lane_id must be cleared"
        assert member.reservation_expires_at is None, (
            "reservation_expires_at must be cleared"
        )

        # Fleet ledger: PoolIdleSpan row written.
        spans = list(
            (
                await db_session.execute(
                    select(PoolIdleSpan).where(
                        PoolIdleSpan.vm_instance_id == "agent-vm-lane-expire"
                    )
                )
            )
            .scalars()
            .all()
        )
        assert len(spans) >= 1, "settle_pool_vm_idle_cost must write a PoolIdleSpan"
        span_cost = sum(s.cost_usd for s in spans)
        assert span_cost > 0, "Settled idle cost must be positive"

        # Lane attribution: reserved_idle_cost_usd_total must match the settled cost.
        await db_session.refresh(lane)
        assert lane.reserved_idle_cost_usd_total > 0, (
            "Lane reserved_idle_cost_usd_total must be incremented"
        )
        assert abs(lane.reserved_idle_cost_usd_total - span_cost) < 1e-9, (
            "Lane attribution must equal the fleet-ledger settled cost"
        )
    finally:
        if prior_lanes is None:
            _runtime_config.pop("improvement_lanes_enabled", None)
        else:
            _runtime_config["improvement_lanes_enabled"] = prior_lanes

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
from sqlmodel import select

import app.shared_state as _shared_state
import app.warm_pool_maintainer as _wpm
from app.models import (
    PoolIdleCostLedger,
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
    register_pool_vm,
)
from app.warm_pool_maintainer import (
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
            "warm_pool_max_vms",
            "warm_pool_idle_ttl_seconds",
            "warm_pool_heartbeat_stale_seconds",
            "warm_pool_failure_rate_threshold",
        ):
            if k in prior:
                _runtime_config[k] = prior[k]
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

    retired = await sweep_idle_pool_vms(db_session, _utcnow(), runtime_config=_runtime_config)
    assert retired == 1
    mock_del.assert_awaited()
    assert await db_session.get(PoolVm, member.id) is None


@pytest.mark.asyncio
async def test_idle_reaper_retires_stale_image_member(db_session, warm_pool_on, mocker):
    mocker.patch(
        "app.vm_lifecycle._ensure_vm_deleted", new_callable=mocker.AsyncMock
    )
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

    retired = await sweep_idle_pool_vms(db_session, _utcnow(), runtime_config=_runtime_config)
    assert retired == 1
    assert await db_session.get(PoolVm, member.id) is None


@pytest.mark.asyncio
async def test_idle_reaper_retires_zombie_member(db_session, warm_pool_on, mocker):
    mocker.patch(
        "app.vm_lifecycle._ensure_vm_deleted", new_callable=mocker.AsyncMock
    )
    mocker.patch("app.compute.get_stale_image_vms", return_value=[])
    _runtime_config["warm_pool_heartbeat_stale_seconds"] = 300
    # Simulate a long-lived process so the zombie-teardown warmup gate is armed
    # (a fresh process must NOT reap on an empty in-memory heartbeat map — that
    # is the deploy/failover false-positive covered by the warmup test below).
    mocker.patch.object(_wpm, "_PROCESS_START_TS", _utcnow() - timedelta(seconds=900))

    project = await _make_project(db_session)
    # PROVISIONING member that never heartbeated and has aged past the liveness
    # window → zombie.
    member = await _register(db_session, project, "agent-vm-zombie", PoolVmState.PROVISIONING)
    member.created_at = _utcnow() - timedelta(seconds=900)
    db_session.add(member)
    await db_session.commit()

    retired = await sweep_idle_pool_vms(db_session, _utcnow(), runtime_config=_runtime_config)
    assert retired == 1
    assert await db_session.get(PoolVm, member.id) is None


@pytest.mark.asyncio
async def test_idle_reaper_spares_zombie_during_warmup(db_session, warm_pool_on, mocker):
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
    member = await _register(db_session, project, "agent-vm-established", PoolVmState.IDLE)
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
async def test_retire_carries_banked_idle_cost_to_ledger(db_session, warm_pool_on, mocker):
    """Tearing down a pool member must NOT erase its banked idle spend: the
    banked cost is carried into the durable PoolIdleCostLedger in the same
    transaction that deletes the registry row, so compute_pool_idle_cost still
    sees it after the row is gone."""
    from app.efficiency_snapshot import compute_pool_idle_cost

    mocker.patch(
        "app.vm_lifecycle._ensure_vm_deleted", new_callable=mocker.AsyncMock
    )

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
    ledger = (
        (await db_session.execute(select(PoolIdleCostLedger))).scalars().all()
    )
    assert len(ledger) == 1
    assert ledger[0].vm_instance_id == "agent-vm-banked"
    assert ledger[0].idle_cost_usd == pytest.approx(1.25)
    cost = await compute_pool_idle_cost(db_session, project.id)
    assert cost["retired_idle_cost_usd"] == pytest.approx(1.25)
    assert cost["settled_idle_cost_usd"] == pytest.approx(1.25)


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

    retired = await sweep_idle_pool_vms(db_session, _utcnow(), runtime_config=_runtime_config)
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
    mocker.patch(
        "app.vm_lifecycle._ensure_vm_deleted", new_callable=mocker.AsyncMock
    )
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
async def test_reconcile_keeps_last_idle_when_leased_coexists(db_session, warm_pool_on, mocker):
    """A LEASED member coexisting with the last IDLE member must not push live
    over target and drain that IDLE member (pre-fix bug)."""
    mocker.patch(
        "app.vm_lifecycle._ensure_vm_deleted", new_callable=mocker.AsyncMock
    )
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
async def test_run_pool_maintainer_cycle_provisions_within_budgets(db_session, warm_pool_on):
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

    won = await retire_and_delete_pool_vm(
        db_session, member, reason="pool_idle_ttl"
    )
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

    retired = await sweep_idle_pool_vms(db_session, _utcnow(), runtime_config=_runtime_config)
    assert retired == 1
    mock_del.assert_awaited()
    assert await db_session.get(PoolVm, member.id) is None


@pytest.mark.asyncio
async def test_idle_reaper_spares_lease_with_running_task(db_session, warm_pool_on, mocker):
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

    retired = await sweep_idle_pool_vms(db_session, _utcnow(), runtime_config=_runtime_config)
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

    retired = await sweep_idle_pool_vms(db_session, _utcnow(), runtime_config=_runtime_config)
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
    member = await _register(db_session, project, "agent-vm-peerretire", PoolVmState.IDLE)
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

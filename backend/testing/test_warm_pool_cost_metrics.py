"""Tests for warm-pool cost accounting + dispatch funnel (direction d-5fd69fe1).

Covers the three seams this task adds:
  1. The pool-hit-vs-cold-dispatch funnel snapshot shape + rollup math.
  2. ``compute_pool_idle_cost`` summing banked + live idle accrual, and
     ``settle_pool_vm_idle_cost`` banking elapsed idle onto the registry row.
  3. ``compute_current_axes`` surfacing the idle-cost companion metric.
"""

from datetime import timedelta

import pytest

from app.efficiency_snapshot import (
    compute_current_axes,
    compute_pool_idle_cost,
    settle_pool_vm_idle_cost,
)
from app.models import PoolVm, PoolVmState, Project, _utcnow
from app.vm_cost_rates import get_fleet_vm_rate_per_second
from app.warm_backlog_reuse import (
    get_pool_funnel_snapshot,
    record_pool_cold_dispatch,
    record_pool_lease_hit,
    record_pool_lease_miss,
    _pool_cold_dispatch_events,
    _pool_lease_hit_events,
    _pool_lease_miss_events,
)


async def _make_project(db_session, name="Warm Pool Cost"):
    project = Project(name=name, user_id="test-user-pool-cost")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)
    return project


# ---------------------------------------------------------------------------
# Pool dispatch funnel
# ---------------------------------------------------------------------------


def test_pool_funnel_snapshot_shape_and_math():
    """Snapshot exposes the documented keys and computes hit_rate over hits+cold."""
    _pool_lease_hit_events.clear()
    _pool_cold_dispatch_events.clear()
    _pool_lease_miss_events.clear()

    # Empty state: hit rate is None, all counters zero.
    snap = get_pool_funnel_snapshot()
    for key in (
        "window_hours",
        "pool_lease_hits",
        "cold_dispatches",
        "total_dispatches",
        "pool_hit_rate",
        "lease_miss_by_reason",
        "lease_miss_total",
    ):
        assert key in snap, f"missing {key}"
    assert snap["pool_hit_rate"] is None
    assert snap["total_dispatches"] == 0

    # 3 hits, 1 cold → 75% hit rate; one miss reason rolled up.
    for _ in range(3):
        record_pool_lease_hit()
    record_pool_cold_dispatch()
    record_pool_lease_miss("no_idle_member")
    record_pool_lease_miss("no_idle_member")

    snap = get_pool_funnel_snapshot()
    assert snap["pool_lease_hits"] == 3
    assert snap["cold_dispatches"] == 1
    assert snap["total_dispatches"] == 4
    assert snap["pool_hit_rate"] == 0.75
    assert snap["lease_miss_by_reason"] == {"no_idle_member": 2}
    assert snap["lease_miss_total"] == 2


# ---------------------------------------------------------------------------
# Idle-cost accounting
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_compute_pool_idle_cost_banked_plus_live(db_session):
    """Banked idle cost and live accrual of an IDLE member both count; a
    RETIRING member is excluded from size but its banked cost still sums."""
    project = await _make_project(db_session)
    now = _utcnow()
    rate = get_fleet_vm_rate_per_second()

    # IDLE for 600s, nothing banked yet → live accrual only.
    idle = PoolVm(
        vm_instance_id="vm-idle-1",
        project_id=project.id,
        state=PoolVmState.IDLE,
        idle_since=now - timedelta(seconds=600),
    )
    # LEASED member carries a banked cost from a prior idle stint.
    leased = PoolVm(
        vm_instance_id="vm-leased-1",
        project_id=project.id,
        state=PoolVmState.LEASED,
        idle_cost_banked=0.05,
    )
    # RETIRING member: excluded from pool_size, banked cost still counted.
    retiring = PoolVm(
        vm_instance_id="vm-retiring-1",
        project_id=project.id,
        state=PoolVmState.RETIRING,
        idle_cost_banked=0.02,
    )
    db_session.add_all([idle, leased, retiring])
    await db_session.commit()

    result = await compute_pool_idle_cost(db_session, project.id)
    # idle + leased are live members; retiring excluded.
    assert result["pool_size"] == 2
    assert result["idle_count"] == 1
    assert result["settled_idle_cost_usd"] == pytest.approx(0.07, abs=1e-6)
    assert result["live_idle_cost_usd"] == pytest.approx(600 * rate, rel=1e-3)
    assert result["total_idle_cost_usd"] == pytest.approx(
        0.07 + 600 * rate, rel=1e-3
    )


@pytest.mark.asyncio
async def test_settle_pool_vm_idle_cost_banks_and_advances(db_session):
    """settle banks elapsed idle onto idle_cost_banked and advances idle_since."""
    project = await _make_project(db_session)
    now = _utcnow()
    member = PoolVm(
        vm_instance_id="vm-settle-1",
        project_id=project.id,
        state=PoolVmState.IDLE,
        idle_since=now - timedelta(seconds=300),
    )
    db_session.add(member)
    await db_session.commit()

    banked = settle_pool_vm_idle_cost(member, now=now)
    rate = get_fleet_vm_rate_per_second()
    assert banked == pytest.approx(300 * rate, rel=1e-3)
    assert member.idle_cost_banked == pytest.approx(300 * rate, rel=1e-3)
    # idle_since advanced to now → a second settle at the same instant is a no-op.
    assert member.idle_since == now
    assert settle_pool_vm_idle_cost(member, now=now) == 0.0


@pytest.mark.asyncio
async def test_settle_adds_onto_existing_banked_cost(db_session):
    """settle ADDS the new span onto a non-None idle_cost_banked (a member that
    already banked cost from a prior idle stint), never overwriting it."""
    project = await _make_project(db_session)
    now = _utcnow()
    rate = get_fleet_vm_rate_per_second()
    prior = 0.05
    member = PoolVm(
        vm_instance_id="vm-settle-add",
        project_id=project.id,
        state=PoolVmState.IDLE,
        idle_since=now - timedelta(seconds=300),
        idle_cost_banked=prior,
    )
    db_session.add(member)
    await db_session.commit()

    banked = settle_pool_vm_idle_cost(member, now=now)
    assert banked == pytest.approx(300 * rate, rel=1e-3)
    # The new span is ADDED to the pre-existing balance, not replacing it.
    assert member.idle_cost_banked == pytest.approx(prior + 300 * rate, rel=1e-3)


@pytest.mark.asyncio
async def test_settle_no_op_when_not_idle(db_session):
    """A member with no idle_since (e.g. LEASED) banks nothing."""
    project = await _make_project(db_session)
    member = PoolVm(
        vm_instance_id="vm-settle-2",
        project_id=project.id,
        state=PoolVmState.LEASED,
        idle_since=None,
    )
    db_session.add(member)
    await db_session.commit()
    assert settle_pool_vm_idle_cost(member) == 0.0
    assert member.idle_cost_banked is None


@pytest.mark.asyncio
async def test_lease_banks_accrued_idle_cost(db_session):
    """Leasing an IDLE member banks its accrued idle cost BEFORE clearing
    idle_since, so the IDLE->LEASED transition never erases measured spend."""
    from app.warm_pool import lease_idle_pool_vm

    project = await _make_project(db_session)
    now = _utcnow()
    member = PoolVm(
        vm_instance_id="vm-lease-bank",
        project_id=project.id,
        account_id="acct-1",
        provider="anthropic",
        model="claude-opus-4-8",
        state=PoolVmState.IDLE,
        idle_since=now - timedelta(seconds=300),
    )
    db_session.add(member)
    await db_session.commit()

    leased = await lease_idle_pool_vm(
        db_session,
        project_id=project.id,
        account_id="acct-1",
        provider="anthropic",
        model="claude-opus-4-8",
    )
    await db_session.commit()
    rate = get_fleet_vm_rate_per_second()
    assert leased is not None
    assert leased.state == PoolVmState.LEASED
    assert leased.idle_since is None
    assert leased.idle_cost_banked == pytest.approx(300 * rate, rel=1e-2)


@pytest.mark.asyncio
async def test_mark_retiring_banks_accrued_idle_cost(db_session):
    """Retiring an IDLE member banks its accrued idle cost before clearing
    idle_since, so a RETIRING member's measured spend is still summed."""
    from app.warm_pool import mark_pool_vm_retiring

    project = await _make_project(db_session)
    now = _utcnow()
    member = PoolVm(
        vm_instance_id="vm-retiring-bank",
        project_id=project.id,
        state=PoolVmState.IDLE,
        idle_since=now - timedelta(seconds=200),
    )
    db_session.add(member)
    await db_session.commit()

    retired = await mark_pool_vm_retiring(db_session, "vm-retiring-bank")
    await db_session.commit()
    rate = get_fleet_vm_rate_per_second()
    assert retired is not None
    assert retired.state == PoolVmState.RETIRING
    assert retired.idle_since is None
    assert retired.idle_cost_banked == pytest.approx(200 * rate, rel=1e-2)


@pytest.mark.asyncio
async def test_compute_current_axes_includes_pool_idle_companion(db_session):
    """compute_current_axes surfaces pool idle cost + the net-cost companion
    without mutating the immutable cost_per_merged_pr baseline axis."""
    project = await _make_project(db_session)
    now = _utcnow()
    member = PoolVm(
        vm_instance_id="vm-axes-1",
        project_id=project.id,
        state=PoolVmState.IDLE,
        idle_since=now - timedelta(seconds=3600),
    )
    db_session.add(member)
    await db_session.commit()

    axes = await compute_current_axes(db_session, project.id)
    assert "pool_idle_cost_usd" in axes
    assert "pool_metrics" in axes
    assert "cost_per_merged_pr_incl_pool_idle" in axes
    assert axes["pool_idle_cost_usd"] > 0
    assert axes["pool_metrics"]["idle_count"] == 1
    # No merged tasks in this project → both cost metrics are None (no division).
    assert axes["cost_per_merged_pr"] is None
    assert axes["cost_per_merged_pr_incl_pool_idle"] is None

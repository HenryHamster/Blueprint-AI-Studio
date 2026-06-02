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
    compute_pool_cost_amortization_by_account,
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
    # No account passed → everything buckets under "unattributed".
    assert set(snap["by_account"]) == {"unattributed"}
    assert snap["by_account"]["unattributed"]["pool_hit_rate"] == 0.75


def test_pool_funnel_per_account_breakdown():
    """by_account attributes hits/cold/miss + a per-account hit rate, so an
    operator can read warm-vs-cold and the hit rate per account during a
    controlled enable; the fleet-wide totals still aggregate across accounts."""
    _pool_lease_hit_events.clear()
    _pool_cold_dispatch_events.clear()
    _pool_lease_miss_events.clear()

    # acct-a: 3 warm hits, 1 cold → 75% per-account hit rate.
    for _ in range(3):
        record_pool_lease_hit("acct-a")
    record_pool_cold_dispatch("acct-a")
    # acct-b: 0 hits, 2 cold → 0% hit rate, plus a lease miss attributed to it.
    record_pool_cold_dispatch("acct-b")
    record_pool_cold_dispatch("acct-b")
    record_pool_lease_miss("no_idle_member", "acct-b")
    # legacy call site with no account → "unattributed" bucket.
    record_pool_lease_hit()

    snap = get_pool_funnel_snapshot()
    # Fleet-wide totals aggregate across every account + the unattributed bucket.
    assert snap["pool_lease_hits"] == 4
    assert snap["cold_dispatches"] == 3
    assert snap["total_dispatches"] == 7

    by_account = snap["by_account"]
    assert set(by_account) == {"acct-a", "acct-b", "unattributed"}

    a = by_account["acct-a"]
    assert a["pool_lease_hits"] == 3
    assert a["cold_dispatches"] == 1
    assert a["total_dispatches"] == 4
    assert a["pool_hit_rate"] == 0.75

    b = by_account["acct-b"]
    assert b["pool_lease_hits"] == 0
    assert b["cold_dispatches"] == 2
    assert b["pool_hit_rate"] == 0.0
    assert b["lease_miss_by_reason"] == {"no_idle_member": 1}
    assert b["lease_miss_total"] == 1

    u = by_account["unattributed"]
    assert u["pool_lease_hits"] == 1
    assert u["pool_hit_rate"] == 1.0


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


# ---------------------------------------------------------------------------
# Per-account-mix idle cost + amortization (telemetry cluster, d-5fd69fe1)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_compute_pool_idle_cost_by_account(db_session):
    """by_account attributes live-member idle cost (banked + live accrual) to the
    member's pinned account, so an operator can see which account's idle members
    drive cost. A member with no account_id buckets under 'unattributed'."""
    project = await _make_project(db_session)
    now = _utcnow()
    rate = get_fleet_vm_rate_per_second()

    # acct-a: one IDLE member accruing live, one LEASED with banked cost.
    db_session.add_all(
        [
            PoolVm(
                vm_instance_id="vm-a-idle",
                project_id=project.id,
                account_id="acct-a",
                state=PoolVmState.IDLE,
                idle_since=now - timedelta(seconds=600),
            ),
            PoolVm(
                vm_instance_id="vm-a-leased",
                project_id=project.id,
                account_id="acct-a",
                state=PoolVmState.LEASED,
                idle_cost_banked=0.05,
            ),
            # acct-b: a single IDLE member.
            PoolVm(
                vm_instance_id="vm-b-idle",
                project_id=project.id,
                account_id="acct-b",
                state=PoolVmState.IDLE,
                idle_since=now - timedelta(seconds=300),
            ),
            # no account_id → unattributed bucket.
            PoolVm(
                vm_instance_id="vm-none",
                project_id=project.id,
                state=PoolVmState.LEASED,
                idle_cost_banked=0.01,
            ),
        ]
    )
    await db_session.commit()

    result = await compute_pool_idle_cost(db_session, project.id)
    by_account = result["by_account"]
    assert set(by_account) == {"acct-a", "acct-b", "unattributed"}

    a = by_account["acct-a"]
    assert a["member_count"] == 2
    assert a["idle_count"] == 1
    assert a["settled_idle_cost_usd"] == pytest.approx(0.05, abs=1e-6)
    assert a["live_idle_cost_usd"] == pytest.approx(600 * rate, rel=1e-3)
    assert a["total_idle_cost_usd"] == pytest.approx(0.05 + 600 * rate, rel=1e-3)

    b = by_account["acct-b"]
    assert b["member_count"] == 1
    assert b["settled_idle_cost_usd"] == 0.0
    assert b["live_idle_cost_usd"] == pytest.approx(300 * rate, rel=1e-3)

    u = by_account["unattributed"]
    assert u["settled_idle_cost_usd"] == pytest.approx(0.01, abs=1e-6)

    # Per-account live totals reconcile with the fleet-wide live figure.
    fleet_live = sum(v["live_idle_cost_usd"] for v in by_account.values())
    assert fleet_live == pytest.approx(result["live_idle_cost_usd"], rel=1e-3)


def test_compute_pool_cost_amortization_by_account_joins_idle_and_funnel():
    """The join pairs per-account idle cost with per-account funnel hits so an
    operator gets warm-vs-cold, hit rate, idle cost, and cost-per-warm-start per
    account. Accounts present on only one side are still emitted."""
    idle_by_account = {
        "acct-a": {"total_idle_cost_usd": 6.0},
        # acct-c has idle members but no dispatch landed for it yet.
        "acct-c": {"total_idle_cost_usd": 2.0},
    }
    funnel = {
        "by_account": {
            "acct-a": {
                "pool_lease_hits": 4,
                "cold_dispatches": 1,
                "total_dispatches": 5,
                "pool_hit_rate": 0.8,
            },
            # acct-b had dispatches but no live idle member at snapshot time.
            "acct-b": {
                "pool_lease_hits": 0,
                "cold_dispatches": 3,
                "total_dispatches": 3,
                "pool_hit_rate": 0.0,
            },
        }
    }

    out = compute_pool_cost_amortization_by_account(idle_by_account, funnel)
    assert set(out) == {"acct-a", "acct-b", "acct-c"}

    a = out["acct-a"]
    assert a["pool_idle_cost_usd"] == 6.0
    assert a["pool_lease_hits"] == 4
    assert a["pool_hit_rate"] == 0.8
    # 6.0 idle USD / 4 warm starts = 1.5 per hit.
    assert a["cost_per_pool_hit_usd"] == 1.5

    # Dispatches but no idle cost recorded → zero idle, hits present.
    b = out["acct-b"]
    assert b["pool_idle_cost_usd"] == 0.0
    assert b["cost_per_pool_hit_usd"] is None
    assert b["total_dispatches"] == 3

    # Idle cost but no warm hit yet → spend not amortized.
    c = out["acct-c"]
    assert c["pool_idle_cost_usd"] == 2.0
    assert c["pool_lease_hits"] == 0
    assert c["cost_per_pool_hit_usd"] is None

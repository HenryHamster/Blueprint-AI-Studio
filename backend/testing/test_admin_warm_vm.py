"""Tests for the warm-VM continuity admin endpoint (/api/admin/warm-vm-state).

Surfaces operator visibility for the soft warm-VM lifecycle. The endpoint
exposes which VMs are heartbeating, which task each is running, queued
warm-pinned tasks per VM, and the warm-pin clear-rate (a leading indicator
of warm-VM instability).
"""

from datetime import datetime, timedelta

import pytest
from httpx import AsyncClient, ASGITransport

from app.main import app
from app.models import Project, Task, TaskStatus
from app.auth import get_current_user, require_admin
import app.shared_state as _shared_state


@pytest.mark.asyncio
async def test_warm_vm_state_empty(client: AsyncClient, db_session):
    """Endpoint returns 200 + empty active_vms when no warm activity exists."""
    # Snapshot + clear in-memory state so other tests don't leak in.
    saved_hb = dict(_shared_state.vm_last_heartbeat)
    saved_clears = list(_shared_state.warm_pin_clear_events)
    _shared_state.vm_last_heartbeat.clear()
    _shared_state.warm_pin_clear_events.clear()
    try:
        resp = await client.get("/api/admin/warm-vm-state")
        assert resp.status_code == 200
        body = resp.json()
        assert body["active_vms"] == []
        assert body["warm_pin_metrics"]["clears_last_hour"] == 0
        assert body["warm_pin_metrics"]["clears_last_24h"] == 0
        assert body["warm_pin_metrics"]["clears_by_reason_last_hour"] == {}
        assert body["warm_pin_metrics"]["clears_by_reason_last_24h"] == {}
        assert body["warm_pin_metrics"]["active_pinned_queued_total"] == 0
        assert body["warm_pin_metrics"]["active_warm_chains"] == 0
        assert body["warm_pin_metrics"]["active_backlog_reuse_claims"] == 0
        assert body["warm_pin_metrics"]["active_chain_continuity_claims"] == 0
        assert "warm_continuity_enabled" in body
        assert "warm_backlog_reuse_enabled" in body
        assert isinstance(body["warm_backlog_reuse_enabled"], bool)
        assert isinstance(body["next_role_poll_timeout_secs"], int)
        funnel = body.get("chain_continuity_funnel", {})
        assert isinstance(funnel, dict)
        for key in ("poll_arrivals", "chain_claimed", "no_pinned_task", "another_vm_owns_pr", "doomed_vm", "skipped_total"):
            assert key in funnel, f"missing key {key} in chain_continuity_funnel"
        spawn_funnel = body.get("spawn_pin_funnel", {})
        assert isinstance(spawn_funnel, dict)
        for key in ("window_hours", "spawn_attempts", "pin_set", "skipped_by_reason", "skipped_total"):
            assert key in spawn_funnel, f"missing key {key} in spawn_pin_funnel"
        assert isinstance(spawn_funnel["skipped_by_reason"], dict)
        backlog_funnel = body.get("backlog_reuse_funnel", {})
        assert isinstance(backlog_funnel, dict)
        for key in (
            "window_hours",
            "completed_vms_with_eligible_backlog",
            "teardown_defers_scheduled",
            "next_role_polls_during_defer",
            "warm_claims_succeeded",
            "claim_skipped_by_reason",
            "claim_skipped_total",
        ):
            assert key in backlog_funnel, f"missing key {key} in backlog_reuse_funnel"
        assert isinstance(backlog_funnel["claim_skipped_by_reason"], dict)
    finally:
        _shared_state.vm_last_heartbeat.clear()
        _shared_state.vm_last_heartbeat.update(saved_hb)
        _shared_state.warm_pin_clear_events.clear()
        _shared_state.warm_pin_clear_events.extend(saved_clears)


@pytest.mark.asyncio
async def test_warm_vm_state_active_chain(client: AsyncClient, db_session):
    """A live warm chain shows up: heartbeating VM, IN_PROGRESS warm-start task,
    pinned QUEUED follow-up, and prior completion in the recent list."""
    saved_hb = dict(_shared_state.vm_last_heartbeat)
    saved_clears = list(_shared_state.warm_pin_clear_events)
    _shared_state.vm_last_heartbeat.clear()
    _shared_state.warm_pin_clear_events.clear()

    project = Project(name="Warm-VM Test", user_id="test-user-123")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    vm_id = "agent-vm-warm-test"
    _shared_state.record_vm_heartbeat(vm_id)

    # Prior task completed within the last hour, recent_completions should
    # surface it.
    prior = Task(
        project_id=project.id,
        title="Review PR #42",
        payloads=[{"type": "review", "pr_number": 42}],
        vm_instance_id=vm_id,
        status=TaskStatus.COMPLETED,
        last_heartbeat=datetime.utcnow() - timedelta(minutes=5),
    )
    db_session.add(prior)

    # IN_PROGRESS warm-start task — should be the current_task.
    current = Task(
        project_id=project.id,
        title="Fix PR #42",
        payloads=[{"type": "fix", "pr_number": 42}],
        vm_instance_id=vm_id,
        status=TaskStatus.IN_PROGRESS,
        started_at=datetime.utcnow(),
        last_heartbeat=datetime.utcnow(),
        context={
            "_timing": {
                "warm_start": True,
                "prior_role_vm_id": vm_id,
                "setup_secs": 4.2,
            }
        },
    )
    db_session.add(current)

    # QUEUED warm-pinned follow-up — should appear in warm_pinned_queued.
    deadline = (datetime.utcnow() + timedelta(seconds=240)).isoformat()
    pinned = Task(
        project_id=project.id,
        title="Re-review PR #42",
        payloads=[{"type": "review", "pr_number": 42}],
        vm_instance_id=None,
        status=TaskStatus.QUEUED,
        context={
            "warm_target_vm_id": vm_id,
            "warm_target_pr": 42,
            "warm_handoff_deadline": deadline,
        },
    )
    db_session.add(pinned)

    # A clear-out event from a different VM — should bump the clears metric.
    _shared_state.record_warm_pin_clear("agent-vm-other", "deadline_expired")

    await db_session.commit()
    try:
        resp = await client.get("/api/admin/warm-vm-state")
        assert resp.status_code == 200
        body = resp.json()
        # The endpoint surfaces our test VM at minimum (other tests may share
        # state, so we look it up by id rather than asserting count).
        ours = next(
            (v for v in body["active_vms"] if v["vm_instance_id"] == vm_id), None
        )
        assert ours is not None, f"vm {vm_id} missing from {body['active_vms']}"
        assert ours["is_alive"] is True
        assert ours["last_heartbeat"] is not None

        cur = ours["current_task"]
        assert cur is not None
        assert cur["task_type"] == "fix"
        assert cur["pr_number"] == 42
        assert cur["warm_start"] is True
        assert cur["prior_role_vm_id"] == vm_id
        assert cur["setup_secs"] == 4.2

        assert len(ours["warm_pinned_queued"]) == 1
        pinned_out = ours["warm_pinned_queued"][0]
        assert pinned_out["task_type"] == "review"
        assert pinned_out["warm_target_pr"] == 42
        assert pinned_out["warm_handoff_deadline"] == deadline

        assert any(c["task_type"] == "review" for c in ours["recent_completions"])

        assert body["warm_pin_metrics"]["active_pinned_queued_total"] >= 1
        assert body["warm_pin_metrics"]["active_warm_chains"] >= 1
        assert body["warm_pin_metrics"]["clears_last_hour"] >= 1
    finally:
        _shared_state.vm_last_heartbeat.clear()
        _shared_state.vm_last_heartbeat.update(saved_hb)
        _shared_state.warm_pin_clear_events.clear()
        _shared_state.warm_pin_clear_events.extend(saved_clears)


@pytest.mark.asyncio
async def test_warm_vm_state_backlog_reuse_attribution(client: AsyncClient, db_session):
    """A backlog-reuse warm-start claim is attributed distinctly from chain
    continuity: it counts toward active_backlog_reuse_claims (not chain), and
    the per-VM current_task exposes warm_claim_origin == 'backlog_reuse'."""
    saved_hb = dict(_shared_state.vm_last_heartbeat)
    _shared_state.vm_last_heartbeat.clear()

    project = Project(name="Backlog-Reuse Test", user_id="test-user-456")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    vm_id = "agent-vm-backlog-reuse"
    _shared_state.record_vm_heartbeat(vm_id)

    current = Task(
        project_id=project.id,
        title="feat: a backlog-reused task",
        payloads=[{"type": "create"}],
        vm_instance_id=vm_id,
        status=TaskStatus.IN_PROGRESS,
        started_at=datetime.utcnow(),
        last_heartbeat=datetime.utcnow(),
        context={
            "_timing": {
                "warm_start": True,
                "warm_claim_origin": "backlog_reuse",
                "prior_role_vm_id": vm_id,
                "setup_secs": 2.1,
            }
        },
    )
    db_session.add(current)
    await db_session.commit()
    try:
        resp = await client.get("/api/admin/warm-vm-state")
        assert resp.status_code == 200
        body = resp.json()
        ours = next(
            (v for v in body["active_vms"] if v["vm_instance_id"] == vm_id), None
        )
        assert ours is not None, f"vm {vm_id} missing from {body['active_vms']}"
        cur = ours["current_task"]
        assert cur is not None
        assert cur["warm_start"] is True
        assert cur["warm_claim_origin"] == "backlog_reuse"

        metrics = body["warm_pin_metrics"]
        assert metrics["active_backlog_reuse_claims"] >= 1
        # This warm-start task is backlog reuse, so it must NOT inflate the
        # chain-continuity or pool-origin counts, and the three partitions must
        # always sum to exactly the total warm-chain population.
        assert (
            metrics["active_backlog_reuse_claims"]
            + metrics["active_pool_origin_claims"]
            + metrics["active_chain_continuity_claims"]
            == metrics["active_warm_chains"]
        )
    finally:
        _shared_state.vm_last_heartbeat.clear()
        _shared_state.vm_last_heartbeat.update(saved_hb)


@pytest.mark.asyncio
async def test_warm_vm_state_pool_origin_attribution(client: AsyncClient, db_session):
    """A warm-pool-origin warm-start claim is attributed to its OWN bucket
    (active_pool_origin_claims), not folded into chain continuity, and the
    per-VM current_task exposes warm_claim_origin == 'warm_pool'."""
    saved_hb = dict(_shared_state.vm_last_heartbeat)
    _shared_state.vm_last_heartbeat.clear()

    project = Project(name="Pool-Origin Test", user_id="test-user-789")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    vm_id = "agent-vm-pool-origin"
    _shared_state.record_vm_heartbeat(vm_id)

    current = Task(
        project_id=project.id,
        title="feat: a pool-served task",
        payloads=[{"type": "create"}],
        vm_instance_id=vm_id,
        status=TaskStatus.IN_PROGRESS,
        started_at=datetime.utcnow(),
        last_heartbeat=datetime.utcnow(),
        context={
            "_timing": {
                "warm_start": True,
                "warm_claim_origin": "warm_pool",
                "prior_role_vm_id": vm_id,
                "setup_secs": 1.4,
            }
        },
    )
    db_session.add(current)
    await db_session.commit()
    try:
        resp = await client.get("/api/admin/warm-vm-state")
        assert resp.status_code == 200
        body = resp.json()
        ours = next(
            (v for v in body["active_vms"] if v["vm_instance_id"] == vm_id), None
        )
        assert ours is not None, f"vm {vm_id} missing from {body['active_vms']}"
        cur = ours["current_task"]
        assert cur is not None
        assert cur["warm_start"] is True
        assert cur["warm_claim_origin"] == "warm_pool"

        metrics = body["warm_pin_metrics"]
        assert metrics["active_pool_origin_claims"] >= 1
        # Pool-origin claims must NOT be counted as chain continuity, and the
        # three origin buckets must always sum to the total warm-chain count.
        assert (
            metrics["active_backlog_reuse_claims"]
            + metrics["active_pool_origin_claims"]
            + metrics["active_chain_continuity_claims"]
            == metrics["active_warm_chains"]
        )
        # pool_origin_share is the pool fraction of all active warm chains.
        assert metrics["pool_origin_share"] is not None
        assert 0.0 <= metrics["pool_origin_share"] <= 1.0
    finally:
        _shared_state.vm_last_heartbeat.clear()
        _shared_state.vm_last_heartbeat.update(saved_hb)


@pytest.mark.asyncio
async def test_warm_vm_state_stale_heartbeat(client: AsyncClient, db_session):
    """A VM with a stale heartbeat is reported as not-alive."""
    saved_hb = dict(_shared_state.vm_last_heartbeat)
    _shared_state.vm_last_heartbeat.clear()
    try:
        vm_id = "agent-vm-stale"
        # Heartbeat 5 minutes ago — well past the 90s freshness threshold.
        _shared_state.vm_last_heartbeat[vm_id] = datetime.utcnow() - timedelta(
            minutes=5
        )

        resp = await client.get("/api/admin/warm-vm-state")
        assert resp.status_code == 200
        ours = next(
            v for v in resp.json()["active_vms"] if v["vm_instance_id"] == vm_id
        )
        assert ours["is_alive"] is False
        assert ours["last_heartbeat"] is not None
    finally:
        _shared_state.vm_last_heartbeat.clear()
        _shared_state.vm_last_heartbeat.update(saved_hb)


@pytest.mark.asyncio
async def test_warm_vm_state_requires_admin(override_get_session, db_session):
    """Non-admin user is rejected with 403."""
    from fastapi import HTTPException

    async def non_admin():
        return {"user_id": "other", "email": "other@example.com", "role": "user"}

    async def reject_admin():
        raise HTTPException(status_code=403, detail="Admin privileges required")

    app.dependency_overrides[get_current_user] = non_admin
    app.dependency_overrides[require_admin] = reject_admin
    try:
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as ac:
            resp = await ac.get("/api/admin/warm-vm-state")
        assert resp.status_code == 403
    finally:
        app.dependency_overrides.pop(get_current_user, None)
        app.dependency_overrides.pop(require_admin, None)


@pytest.mark.asyncio
async def test_warm_vm_state_clears_by_reason_histogram(client: AsyncClient):
    """Per-reason histogram surfaces each reason with the correct count and
    sums back to the existing clears_last_hour total."""
    saved_clears = list(_shared_state.warm_pin_clear_events)
    _shared_state.warm_pin_clear_events.clear()
    try:
        # Two deadline_expired, one vm_unresponsive — plus a legacy entry with
        # an empty reason that must be coerced to "unknown" so the sum-of-values
        # invariant holds.
        _shared_state.record_warm_pin_clear("vm-a", "deadline_expired")
        _shared_state.record_warm_pin_clear("vm-b", "deadline_expired")
        _shared_state.record_warm_pin_clear("vm-c", "vm_unresponsive")
        _shared_state.record_warm_pin_clear("vm-d", "")

        resp = await client.get("/api/admin/warm-vm-state")
        assert resp.status_code == 200
        metrics = resp.json()["warm_pin_metrics"]

        hist_1h = metrics["clears_by_reason_last_hour"]
        assert isinstance(hist_1h, dict)
        assert hist_1h.get("deadline_expired") == 2
        assert hist_1h.get("vm_unresponsive") == 1
        assert hist_1h.get("unknown") == 1
        assert sum(hist_1h.values()) == metrics["clears_last_hour"]

        # 24h field is present and at least as big as 1h (same events fit
        # in both windows).
        hist_24h = metrics["clears_by_reason_last_24h"]
        assert isinstance(hist_24h, dict)
        assert hist_24h.get("deadline_expired") == 2
        assert hist_24h.get("vm_unresponsive") == 1
        assert sum(hist_24h.values()) == metrics["clears_last_24h"]
    finally:
        _shared_state.warm_pin_clear_events.clear()
        _shared_state.warm_pin_clear_events.extend(saved_clears)


@pytest.mark.asyncio
async def test_warm_vm_state_includes_pool_metrics(client: AsyncClient, db_session):
    """The warm-vm-state payload exposes a pool_metrics block: enabled flag,
    target/current size, idle count, projected idle cost, and the dispatch
    funnel — sourced from the PoolVm registry + runtime config."""
    from app.models import PoolVm, PoolVmState, Project, _utcnow
    from datetime import timedelta

    project = Project(name="Pool Metrics", user_id="test-user-pm")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    member = PoolVm(
        vm_instance_id="vm-pm-idle-1",
        project_id=project.id,
        state=PoolVmState.IDLE,
        idle_since=_utcnow() - timedelta(seconds=120),
    )
    db_session.add(member)
    await db_session.commit()

    resp = await client.get("/api/admin/warm-vm-state")
    assert resp.status_code == 200
    pool = resp.json()["pool_metrics"]
    assert isinstance(pool, dict)
    for key in (
        "enabled",
        "target_size",
        "idle_ttl_secs",
        "current_size",
        "idle_count",
        "projected_idle_cost_usd",
        "funnel",
        "idle_cost_by_account",
        "cost_amortization_by_account",
    ):
        assert key in pool, f"missing pool_metrics.{key}"
    assert isinstance(pool["enabled"], bool)
    # The idle member we just inserted is counted and accruing live cost.
    assert pool["current_size"] >= 1
    assert pool["idle_count"] >= 1
    assert pool["projected_idle_cost_usd"] > 0
    assert "pool_hit_rate" in pool["funnel"]
    assert "by_account" in pool["funnel"]
    # Per-account idle cost attributes the unpinned idle member to the
    # 'unattributed' bucket, and the amortization view emits a matching key so an
    # operator can read per-account idle cost + hit rate together.
    assert isinstance(pool["idle_cost_by_account"], dict)
    assert "unattributed" in pool["idle_cost_by_account"]
    assert "unattributed" in pool["cost_amortization_by_account"]
    amort = pool["cost_amortization_by_account"]["unattributed"]
    assert amort["pool_idle_cost_usd"] > 0
    assert "pool_hit_rate" in amort

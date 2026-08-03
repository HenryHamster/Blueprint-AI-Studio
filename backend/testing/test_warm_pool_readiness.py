"""Warm-pool PROVISIONING -> IDLE readiness signal (the pool's load-bearing wire).

A taskless pool VM is registered in PROVISIONING by ``compute.provision_pool_vm``,
but nothing in production transitioned it to IDLE — and ``lease_idle_pool_vm`` only
ever selects IDLE members, so a booted member was never leasable, the pool served
zero tasks, and the billed VM sat in PROVISIONING forever.

The agent on a taskless pool VM fires a VM-scoped ``/api/agent/vm-heartbeat`` on
boot (before it begins polling /next-role). These tests assert that heartbeat now
promotes the member PROVISIONING -> IDLE, while leaving non-pool VMs, LEASED
members, and the flag-off case untouched. The first test is the full end-to-end:
provision (PROVISIONING) -> boot heartbeat -> IDLE -> lease -> warm claim
(origin "warm_pool") -> non-zero pool hit rate in the dispatch funnel.

Reads that must observe a commit made on the ENDPOINT's own session/connection use
a FRESH session (``TestingSessionLocal``) — exactly as production does, one session
per request — because the long-lived test ``db_session`` holds a transaction
snapshot that does not reliably see another connection's commit.
"""

import uuid
from contextlib import contextmanager

import pytest
from httpx import AsyncClient

from app.auth import create_agent_token, create_vm_scoped_token
from app.models import Project, PoolVmState, Task, TaskStatus
from testing.conftest import TestingSessionLocal


@contextmanager
def _warm_pool_enabled():
    """Enable warm_pool_enabled, clear the pool circuit breaker, and reset the
    dispatch funnel deques so a neighbour's state can't suppress the lease or
    leak counts into our snapshot. runtime_config keys are restored by the autouse
    reset_runtime_config fixture; the module-level deques/scalars are restored here.
    """
    import app.shared_state as ss
    import app.warm_backlog_reuse as wbr
    from app.shared_state import runtime_config as rc

    prior_failures = list(ss.warm_pool_claim_failure_events)
    prior_suppressed = ss.warm_pool_suppressed_until
    rc["warm_pool_enabled"] = True
    ss.warm_pool_claim_failure_events.clear()
    ss.warm_pool_suppressed_until = None
    wbr._pool_lease_hit_events.clear()
    wbr._pool_cold_dispatch_events.clear()
    wbr._pool_lease_miss_events.clear()
    try:
        yield ss
    finally:
        ss.warm_pool_claim_failure_events.clear()
        ss.warm_pool_claim_failure_events.extend(prior_failures)
        ss.warm_pool_suppressed_until = prior_suppressed
        wbr._pool_lease_hit_events.clear()
        wbr._pool_cold_dispatch_events.clear()
        wbr._pool_lease_miss_events.clear()


async def _fresh_pool_vm(vm_instance_id: str):
    """Read a PoolVm row through a brand-new session so a cross-connection commit
    (made by an endpoint's own session) is reliably visible."""
    from app.warm_pool import get_pool_vm

    async with TestingSessionLocal() as s:
        return await get_pool_vm(s, vm_instance_id)


@pytest.mark.asyncio
async def test_boot_heartbeat_promotes_provisioning_member_then_leases_warm(
    client: AsyncClient, db_session, mocker
):
    """End-to-end: a PROVISIONING pool member becomes leasable via its boot
    heartbeat, then the dispatch hook leases it and the member claims a queued
    task warm (origin "warm_pool", non-zero pool hit rate)."""
    from app.warm_pool import WARM_POOL_PIN_KEY, register_pool_vm, try_pool_dispatch
    from app.warm_backlog_reuse import get_pool_funnel_snapshot, record_pool_lease_hit

    mocker.patch(
        "app.routers.agent_warm_continuity.broadcast_to_project",
        new_callable=mocker.AsyncMock,
    )

    with _warm_pool_enabled():
        project = Project(name="Pool Readiness E2E", user_id="test-user-123")
        db_session.add(project)
        await db_session.commit()
        await db_session.refresh(project)

        acct, provider, model = "acct-ready-1", "anthropic", "claude-opus-4-8"
        pool_vm_id = f"agent-vm-ready-{uuid.uuid4().hex}"

        # Provisioner registers a taskless member in PROVISIONING (the default).
        member = await register_pool_vm(
            db_session,
            vm_instance_id=pool_vm_id,
            project_id=project.id,
            account_id=acct,
            provider=provider,
            model=model,
        )
        await db_session.commit()
        assert member.state == PoolVmState.PROVISIONING
        assert member.idle_since is None

        # ── Boot readiness signal: the taskless VM fires /vm-heartbeat with its
        #    VM-SCOPED token (no TASK_ID) before it begins polling /next-role. ──
        token = create_vm_scoped_token(pool_vm_id, str(project.id))
        hb = await client.post(
            "/api/agent/vm-heartbeat",
            json={"vm_instance_id": pool_vm_id},
            headers={"x-agent-token": token},
        )
        assert hb.status_code == 204

        # The member is now IDLE (leasable) with idle_since stamped.
        promoted = await _fresh_pool_vm(pool_vm_id)
        assert promoted.state == PoolVmState.IDLE
        assert promoted.idle_since is not None

        # ── Dispatch hook leases the now-IDLE member for a queued same-tenant
        #    task. Lease + pin on a FRESH session (the orchestrator's own), so it
        #    sees the endpoint-committed IDLE state. ──
        task = Task(
            project_id=project.id,
            payloads=[{"type": "create", "prompt": "x", "base_branch": "automain"}],
            status=TaskStatus.QUEUED,
            context={"scope": "small"},
        )
        db_session.add(task)
        await db_session.commit()
        task_id = task.id

        from app import max_plan_pool as max_plan_pool_module
        from app.max_plan_pool import reserve_max_plan_account

        account = {
            "id": acct,
            "user_id": project.user_id,
            "name": acct,
            "enabled": True,
            "oauth_token": "tok",
            "provider": provider,
            "auth_mode": "oauth",
            "model": model,
            "active_tasks": 0,
            "daily_task_count": 0,
            "project_state": {},
            "disabled_models": {},
        }
        mocker.patch.object(
            max_plan_pool_module,
            "_max_plan_pools",
            {project.user_id: [account]},
        )
        mocker.patch.object(
            max_plan_pool_module,
            "_account_by_id",
            {acct: account},
        )
        assert await reserve_max_plan_account(
            acct,
            user_id=project.user_id,
            project_id=str(project.id),
            model=model,
            count_daily_task=True,
        )

        async with TestingSessionLocal() as s:
            task_in_s = await s.get(Task, task_id)
            leased = await try_pool_dispatch(
                s,
                task_in_s,
                required_provider=provider,
                required_model=model,
                required_account=acct,
                reserved_account_id=acct,
            )
            assert leased is not None and leased.vm_instance_id == pool_vm_id
            assert leased.state == PoolVmState.LEASED
            await s.commit()
        record_pool_lease_hit()

        # ── The member's agent claims the task warm via /next-role. ─────────────
        nr = await client.post(
            "/api/agent/next-role",
            json={"vm_instance_id": pool_vm_id},
            headers={"x-agent-token": token},
        )
        assert nr.status_code == 200, nr.text
        body = nr.json()
        assert body["action"] == "claim"
        assert body["task_id"] == str(task_id)
        assert body["warm_start"] is True

        # Warm telemetry stamped; task bound IN_PROGRESS to the VM (fresh read).
        async with TestingSessionLocal() as s:
            claimed = await s.get(Task, task_id)
            assert claimed.status == TaskStatus.IN_PROGRESS
            assert claimed.vm_instance_id == pool_vm_id
            assert (claimed.context or {}).get(WARM_POOL_PIN_KEY) is True
            timing = (claimed.context or {}).get("_timing", {})
            assert timing.get("warm_claim_origin") == "warm_pool"

        # The dispatch funnel reflects the warm start (non-zero pool hit rate).
        funnel = get_pool_funnel_snapshot()
        assert funnel["pool_lease_hits"] == 1
        assert funnel["pool_hit_rate"] > 0.0


@pytest.mark.asyncio
async def test_repeated_boot_heartbeat_does_not_reset_idle_since(
    client: AsyncClient, db_session
):
    """Promotion is idempotent: once a member is IDLE, a later heartbeat does NOT
    re-stamp ``idle_since`` (which would defeat the idle reaper / cost accounting)."""
    from app.warm_pool import register_pool_vm

    with _warm_pool_enabled():
        project = Project(name="Pool Readiness Idempotent", user_id="test-user-123")
        db_session.add(project)
        await db_session.commit()
        await db_session.refresh(project)

        pool_vm_id = f"agent-vm-idem-{uuid.uuid4().hex}"
        await register_pool_vm(
            db_session, vm_instance_id=pool_vm_id, project_id=project.id
        )
        await db_session.commit()

        token = create_vm_scoped_token(pool_vm_id, str(project.id))
        assert (
            await client.post(
                "/api/agent/vm-heartbeat",
                json={"vm_instance_id": pool_vm_id},
                headers={"x-agent-token": token},
            )
        ).status_code == 204

        member = await _fresh_pool_vm(pool_vm_id)
        assert member.state == PoolVmState.IDLE
        first_idle_since = member.idle_since
        assert first_idle_since is not None

        # A second heartbeat must NOT re-promote (state is already IDLE), so
        # idle_since is unchanged.
        assert (
            await client.post(
                "/api/agent/vm-heartbeat",
                json={"vm_instance_id": pool_vm_id},
                headers={"x-agent-token": token},
            )
        ).status_code == 204
        member = await _fresh_pool_vm(pool_vm_id)
        assert member.idle_since == first_idle_since


@pytest.mark.asyncio
async def test_boot_heartbeat_never_demotes_leased_member(
    client: AsyncClient, db_session
):
    """A LEASED member (pinned to a task during the dispatch->claim handoff) is
    NEVER demoted back to IDLE by a heartbeat — that would strand the task."""
    from app.warm_pool import register_pool_vm

    with _warm_pool_enabled():
        project = Project(name="Pool Readiness Leased", user_id="test-user-123")
        db_session.add(project)
        await db_session.commit()
        await db_session.refresh(project)

        pool_vm_id = f"agent-vm-leased-{uuid.uuid4().hex}"
        member = await register_pool_vm(
            db_session,
            vm_instance_id=pool_vm_id,
            project_id=project.id,
            state=PoolVmState.LEASED,
        )
        leased_task_id = uuid.uuid4()
        member.leased_task_id = leased_task_id
        db_session.add(member)
        await db_session.commit()

        token = create_vm_scoped_token(pool_vm_id, str(project.id))
        assert (
            await client.post(
                "/api/agent/vm-heartbeat",
                json={"vm_instance_id": pool_vm_id},
                headers={"x-agent-token": token},
            )
        ).status_code == 204

        member = await _fresh_pool_vm(pool_vm_id)
        assert member.state == PoolVmState.LEASED
        assert member.leased_task_id == leased_task_id


@pytest.mark.asyncio
async def test_boot_heartbeat_no_pool_row_is_noop(client: AsyncClient, db_session):
    """A normal task-bound VM (no PoolVm row) is completely unaffected, and the
    heartbeat still succeeds (204)."""
    with _warm_pool_enabled():
        project = Project(name="Pool Readiness Non-Member", user_id="test-user-123")
        db_session.add(project)
        await db_session.commit()
        await db_session.refresh(project)

        vm_id = f"agent-vm-nonmember-{uuid.uuid4().hex}"
        task = Task(
            project_id=project.id,
            payloads=[{"type": "fix"}],
            vm_instance_id=vm_id,
            status=TaskStatus.IN_PROGRESS,
        )
        db_session.add(task)
        await db_session.commit()
        await db_session.refresh(task)

        token = create_agent_token(str(task.id), vm_id)
        assert (
            await client.post(
                "/api/agent/vm-heartbeat",
                json={"vm_instance_id": vm_id},
                headers={"x-agent-token": token},
            )
        ).status_code == 204

        assert await _fresh_pool_vm(vm_id) is None


@pytest.mark.asyncio
async def test_boot_heartbeat_inert_when_pool_disabled(
    client: AsyncClient, db_session
):
    """With warm_pool_enabled=False the promotion is inert: a PROVISIONING member
    stays PROVISIONING (the pool's inertness guarantee)."""
    from app.warm_pool import register_pool_vm

    # warm_pool_enabled defaults False; do NOT enter _warm_pool_enabled().
    project = Project(name="Pool Readiness Disabled", user_id="test-user-123")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    pool_vm_id = f"agent-vm-disabled-{uuid.uuid4().hex}"
    await register_pool_vm(
        db_session, vm_instance_id=pool_vm_id, project_id=project.id
    )
    await db_session.commit()

    token = create_vm_scoped_token(pool_vm_id, str(project.id))
    assert (
        await client.post(
            "/api/agent/vm-heartbeat",
            json={"vm_instance_id": pool_vm_id},
            headers={"x-agent-token": token},
        )
    ).status_code == 204

    member = await _fresh_pool_vm(pool_vm_id)
    assert member.state == PoolVmState.PROVISIONING


@pytest.mark.asyncio
async def test_pre_claim_failure_heartbeat_retires_not_promotes(
    client: AsyncClient, db_session
):
    """A taskless pool member that dies in early boot validation reports a single
    /vm-heartbeat carrying ``pre_claim_failure`` (then process.exit(1)). That
    heartbeat must NEVER promote the credential-broken member to IDLE — a leasable
    dead VM would be handed to a real task that can never be claimed, turning an
    explained death into a downstream task timeout. Instead the member is RETIRED
    so ``lease_idle_pool_vm`` (IDLE-only) can never select it."""
    from app.warm_pool import register_pool_vm

    with _warm_pool_enabled():
        project = Project(name="Pool Pre-Claim Failure", user_id="test-user-123")
        db_session.add(project)
        await db_session.commit()
        await db_session.refresh(project)

        pool_vm_id = f"agent-vm-preclaim-{uuid.uuid4().hex}"
        member = await register_pool_vm(
            db_session, vm_instance_id=pool_vm_id, project_id=project.id
        )
        await db_session.commit()
        assert member.state == PoolVmState.PROVISIONING

        token = create_vm_scoped_token(pool_vm_id, str(project.id))
        hb = await client.post(
            "/api/agent/vm-heartbeat",
            json={"vm_instance_id": pool_vm_id, "pre_claim_failure": "bad creds"},
            headers={"x-agent-token": token},
        )
        assert hb.status_code == 204

        # The dying member is RETIRING (never leasable), NOT IDLE.
        retired = await _fresh_pool_vm(pool_vm_id)
        assert retired.state == PoolVmState.RETIRING
        assert retired.idle_since is None

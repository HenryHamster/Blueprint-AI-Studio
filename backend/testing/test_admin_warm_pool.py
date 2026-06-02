"""Integration tests for the admin warm-pool seed/drain control endpoints
(``backend/app/routers/admin_warm_pool.py``, efficiency direction d-5fd69fe1).

Covers the task's deliverables:
  * auth/project-access enforced on both routes (non-member → 404);
  * SEED returns a clear 409 no-op when ``warm_pool_enabled`` is off;
  * SEED provisions taskless members up to the per-project target, routed
    through the production provisioner path, respecting the budget ceiling;
  * SEED provisions zero while the claim circuit breaker is tripped;
  * DRAIN retires every live member through the teardown path (rows removed,
    idle cost banked to the ledger).
"""

import uuid
from datetime import timedelta

import pytest
from httpx import AsyncClient
from sqlmodel import select

import app.shared_state as _shared_state
from app.models import (
    PoolIdleCostLedger,
    PoolVm,
    PoolVmState,
    Project,
)
from app.shared_state import runtime_config as _runtime_config
from app.time_utils import _utcnow
from app.warm_pool import register_pool_vm
from testing.factories import make_membership


@pytest.fixture
async def project_owner(db_session):
    """A project with test-user-123 (the client fixture's user) as owner."""
    project = Project(name="Warm Pool Ctl", user_id="test-user-123")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)
    db_session.add(make_membership(project.id, user_id="test-user-123", role="owner"))
    await db_session.commit()
    await db_session.refresh(project)
    return project


@pytest.fixture
async def foreign_project(db_session):
    """A project test-user-123 is NOT a member of (access must be denied)."""
    project = Project(name="Someone Else", user_id="other-user")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)
    return project


@pytest.fixture
def warm_pool_enabled():
    """Enable the master switch and clear the circuit breaker for the test.

    runtime_config is snapshot/restored by the autouse reset_runtime_config
    fixture; the suppression deadline is process-global so we reset it here.
    """
    prior_deadline = _shared_state.warm_pool_suppressed_until
    _runtime_config["warm_pool_enabled"] = True
    _shared_state.warm_pool_suppressed_until = None
    _shared_state.warm_pool_claim_failure_events.clear()
    try:
        yield
    finally:
        _shared_state.warm_pool_suppressed_until = prior_deadline
        _shared_state.warm_pool_claim_failure_events.clear()


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


# ---------------------------------------------------------------------------
# Auth / project-access enforcement
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_seed_denied_for_non_member_project(
    client: AsyncClient, db_session, foreign_project, warm_pool_enabled
):
    resp = await client.post(
        f"/api/admin/projects/{foreign_project.id}/warm-pool/seed"
    )
    assert resp.status_code == 404, resp.text


@pytest.mark.asyncio
async def test_drain_denied_for_non_member_project(
    client: AsyncClient, db_session, foreign_project
):
    resp = await client.post(
        f"/api/admin/projects/{foreign_project.id}/warm-pool/drain"
    )
    assert resp.status_code == 404, resp.text


# ---------------------------------------------------------------------------
# SEED
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_seed_returns_409_when_pool_disabled(
    client: AsyncClient, db_session, project_owner
):
    """warm_pool_enabled off → clear 409 no-op, no PoolVm rows registered."""
    _runtime_config["warm_pool_enabled"] = False
    resp = await client.post(
        f"/api/admin/projects/{project_owner.id}/warm-pool/seed"
    )
    assert resp.status_code == 409, resp.text
    rows = (await db_session.execute(select(PoolVm))).scalars().all()
    assert rows == []


@pytest.mark.asyncio
async def test_seed_provisions_up_to_target(
    client: AsyncClient, db_session, project_owner, warm_pool_enabled, mocker
):
    """SEED provisions taskless members up to warm_pool_target_size via the
    production provisioner path, within the dispatch/pool budget ceilings."""
    _runtime_config["warm_pool_target_size"] = 2
    _runtime_config["warm_pool_max_vms"] = 20
    _runtime_config["max_concurrent_vms"] = 10

    async def _registering_provision(session, project_id):
        member = await register_pool_vm(
            session,
            vm_instance_id=f"agent-vm-seed-{uuid.uuid4().hex}",
            project_id=project_id,
            account_id="acct-1",
            provider="anthropic",
            model="claude-opus-4-8",
            state=PoolVmState.IDLE,
        )
        session.add(member)
        await session.commit()
        return True

    # The route delegates provisioning to the module-level production provisioner;
    # patch it with a registering stub so no real GCP boot happens but real PoolVm
    # rows land through the normal registry path.
    mocker.patch(
        "app.routers.admin_warm_pool._default_pool_provision_fn",
        new=_registering_provision,
    )

    resp = await client.post(
        f"/api/admin/projects/{project_owner.id}/warm-pool/seed"
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["seeded"] == 2
    assert data["project_id"] == str(project_owner.id)
    rows = (
        await db_session.execute(
            select(PoolVm).where(PoolVm.project_id == project_owner.id)
        )
    ).scalars().all()
    assert len(rows) == 2


@pytest.mark.asyncio
async def test_seed_returns_409_when_project_not_in_scope(
    client: AsyncClient, db_session, project_owner, warm_pool_enabled
):
    """A non-empty allowlist that excludes the project → clear 409, no provision.

    The maintainer forces an unscoped project's target to 0 and would drain
    anything seeded, so the endpoint refuses rather than seeding into a drain.
    """
    _runtime_config["warm_pool_target_size"] = 2
    _runtime_config["warm_pool_max_vms"] = 20
    _runtime_config["max_concurrent_vms"] = 10
    # Scope the pool to some OTHER project so project_owner is out of scope.
    _runtime_config["warm_pool_project_allowlist"] = [str(uuid.uuid4())]

    resp = await client.post(
        f"/api/admin/projects/{project_owner.id}/warm-pool/seed"
    )
    assert resp.status_code == 409, resp.text
    rows = (
        await db_session.execute(
            select(PoolVm).where(PoolVm.project_id == project_owner.id)
        )
    ).scalars().all()
    assert rows == []


@pytest.mark.asyncio
async def test_seed_provisions_when_project_in_scope(
    client: AsyncClient, db_session, project_owner, warm_pool_enabled, mocker
):
    """A project explicitly listed in the allowlist seeds up to target normally."""
    _runtime_config["warm_pool_target_size"] = 1
    _runtime_config["warm_pool_max_vms"] = 20
    _runtime_config["max_concurrent_vms"] = 10
    _runtime_config["warm_pool_project_allowlist"] = [str(project_owner.id)]

    async def _registering_provision(session, project_id):
        member = await register_pool_vm(
            session,
            vm_instance_id=f"agent-vm-scoped-{uuid.uuid4().hex}",
            project_id=project_id,
            account_id="acct-1",
            provider="anthropic",
            model="claude-opus-4-8",
            state=PoolVmState.IDLE,
        )
        session.add(member)
        await session.commit()
        return True

    mocker.patch(
        "app.routers.admin_warm_pool._default_pool_provision_fn",
        new=_registering_provision,
    )

    resp = await client.post(
        f"/api/admin/projects/{project_owner.id}/warm-pool/seed"
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["seeded"] == 1


@pytest.mark.asyncio
async def test_seed_respects_pool_budget_ceiling(
    client: AsyncClient, db_session, project_owner, warm_pool_enabled, mocker
):
    """warm_pool_max_vms caps the seed even when the per-project target is higher."""
    _runtime_config["warm_pool_target_size"] = 5
    _runtime_config["warm_pool_max_vms"] = 1  # fleet budget allows only 1 more
    _runtime_config["max_concurrent_vms"] = 10

    calls = {"n": 0}

    async def _registering_provision(session, project_id):
        calls["n"] += 1
        member = await register_pool_vm(
            session,
            vm_instance_id=f"agent-vm-cap-{uuid.uuid4().hex}",
            project_id=project_id,
            account_id="acct-1",
            provider="anthropic",
            model="claude-opus-4-8",
            state=PoolVmState.IDLE,
        )
        session.add(member)
        await session.commit()
        return True

    mocker.patch(
        "app.routers.admin_warm_pool._default_pool_provision_fn",
        new=_registering_provision,
    )

    resp = await client.post(
        f"/api/admin/projects/{project_owner.id}/warm-pool/seed"
    )
    assert resp.status_code == 200, resp.text
    # pool_budget_remaining = max_vms(1) - live(0) = 1 → exactly one provisioned.
    assert resp.json()["seeded"] == 1
    assert calls["n"] == 1


@pytest.mark.asyncio
async def test_seed_provisions_zero_when_breaker_tripped(
    client: AsyncClient, db_session, project_owner, warm_pool_enabled, mocker
):
    """A tripped claim circuit breaker suppresses all provisioning."""
    _runtime_config["warm_pool_target_size"] = 3
    _runtime_config["warm_pool_max_vms"] = 20
    _runtime_config["max_concurrent_vms"] = 10
    # Trip the breaker: suppressed until a future instant.
    _shared_state.warm_pool_suppressed_until = _utcnow() + timedelta(minutes=10)

    async def _must_not_provision(session, project_id):
        raise AssertionError("must not provision while suppressed")

    mocker.patch(
        "app.routers.admin_warm_pool._default_pool_provision_fn",
        new=_must_not_provision,
    )

    resp = await client.post(
        f"/api/admin/projects/{project_owner.id}/warm-pool/seed"
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["seeded"] == 0
    rows = (
        await db_session.execute(
            select(PoolVm).where(PoolVm.project_id == project_owner.id)
        )
    ).scalars().all()
    assert rows == []


# ---------------------------------------------------------------------------
# DRAIN
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_drain_retires_live_members_and_banks_idle_cost(
    client: AsyncClient, db_session, project_owner, warm_pool_enabled, mocker
):
    """DRAIN retires every live member; rows removed and idle cost banked to the
    ledger via the existing retire_and_delete_pool_vm path."""
    mocker.patch(
        "app.vm_lifecycle._ensure_vm_deleted", new_callable=mocker.AsyncMock
    )
    mocker.patch("app.compute.get_stale_image_vms", return_value=[])

    idle = await _register(db_session, project_owner, "agent-vm-d1", PoolVmState.IDLE)
    idle.idle_since = _utcnow() - timedelta(seconds=600)  # accrue measurable cost
    db_session.add(idle)
    await db_session.commit()
    await _register(db_session, project_owner, "agent-vm-d2", PoolVmState.PROVISIONING)
    # A RETIRING member is not "live" and must not be counted/double-torn-down.
    await _register(db_session, project_owner, "agent-vm-d3", PoolVmState.RETIRING)

    resp = await client.post(
        f"/api/admin/projects/{project_owner.id}/warm-pool/drain"
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["drained"] == 2

    remaining = (
        await db_session.execute(
            select(PoolVm).where(
                PoolVm.project_id == project_owner.id,
                PoolVm.state.in_(
                    (PoolVmState.IDLE, PoolVmState.PROVISIONING, PoolVmState.LEASED)
                ),
            )
        )
    ).scalars().all()
    assert remaining == []

    # The idle member's accrued cost was carried into the durable ledger.
    ledger = (
        await db_session.execute(
            select(PoolIdleCostLedger).where(
                PoolIdleCostLedger.project_id == project_owner.id
            )
        )
    ).scalars().all()
    assert any(row.idle_cost_usd > 0 for row in ledger)


@pytest.mark.asyncio
async def test_drain_empty_pool_is_noop(
    client: AsyncClient, db_session, project_owner, warm_pool_enabled
):
    resp = await client.post(
        f"/api/admin/projects/{project_owner.id}/warm-pool/drain"
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["drained"] == 0

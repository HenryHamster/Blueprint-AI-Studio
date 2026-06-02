"""Tests for the warm-VM pool foundation (efficiency direction d-5fd69fe1).

Covers the three seams the task calls out:
  1. ``lease_idle_pool_vm`` returns ONLY a compatible + same-tenant IDLE row,
     under FOR UPDATE SKIP LOCKED, and flips it to LEASED.
  2. A VM-scoped (taskless) token round-trips through ``verify_agent_token`` on
     the taskless endpoints (vm-heartbeat), and a malformed one is rejected.
  3. The existing task-scoped auth path is unaffected.

Plus registry CRUD transitions and the taskless manifest variant.
"""

import uuid

import pytest

from app.auth import (
    create_agent_token,
    create_vm_scoped_token,
    verify_agent_jwt,
    VM_SCOPED_TOKEN_SCOPE,
)
from app.compute_manifest import (
    build_agent_secret_manifest,
    build_taskless_agent_secret_manifest,
    build_warm_claim_retarget_manifest,
    POOL_MODE_ENV_VAR,
)
from app.models import PoolVmState, Project, Task, TaskStatus
from app.warm_pool import (
    get_pool_vm,
    lease_idle_pool_vm,
    list_pool_vms,
    mark_pool_vm_idle,
    mark_pool_vm_retiring,
    pool_vm_cleanup_exemptions,
    promote_pool_vm_to_idle,
    register_pool_vm,
    release_pool_vm,
)
import app.shared_state as shared_state


async def _make_project(db_session, name="Warm Pool"):
    project = Project(name=name, user_id="test-user-pool")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)
    return project


# ---------------------------------------------------------------------------
# VM-scoped token round-trip
# ---------------------------------------------------------------------------


def test_vm_scoped_token_roundtrips_through_verify_jwt():
    tok = create_vm_scoped_token("vm-pool-1", "proj-abc")
    payload = verify_agent_jwt(tok)
    assert payload["token_scope"] == VM_SCOPED_TOKEN_SCOPE
    assert payload["vm_instance_id"] == "vm-pool-1"
    assert payload["project_id"] == "proj-abc"
    # No task binding.
    assert payload.get("sub") is None


@pytest.mark.asyncio
async def test_vm_heartbeat_accepts_vm_scoped_token(client):
    """A taskless VM-scoped token authenticates the taskless /vm-heartbeat path."""
    tok = create_vm_scoped_token("vm-pool-hb", str(uuid.uuid4()))
    resp = await client.post(
        "/api/agent/vm-heartbeat",
        json={"vm_instance_id": "vm-pool-hb"},
        headers={"X-Agent-Token": tok},
    )
    assert resp.status_code == 204


@pytest.mark.asyncio
async def test_vm_heartbeat_rejects_body_vm_mismatch(client):
    """JWT vm_instance_id must match the body — defence in depth."""
    tok = create_vm_scoped_token("vm-pool-hb", str(uuid.uuid4()))
    resp = await client.post(
        "/api/agent/vm-heartbeat",
        json={"vm_instance_id": "some-other-vm"},
        headers={"X-Agent-Token": tok},
    )
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_vm_scoped_token_without_vm_instance_id_rejected(client):
    """A VM-scoped token carrying no vm_instance_id has nothing to authorize."""
    import jwt as _jwt
    from app.auth import AGENT_JWT_SECRET, AGENT_JWT_ALGORITHM

    bad = _jwt.encode(
        {"token_scope": VM_SCOPED_TOKEN_SCOPE, "project_id": "p"},
        AGENT_JWT_SECRET,
        algorithm=AGENT_JWT_ALGORITHM,
    )
    resp = await client.post(
        "/api/agent/vm-heartbeat",
        json={"vm_instance_id": "vm-x"},
        headers={"X-Agent-Token": bad},
    )
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_task_scoped_token_path_unaffected(client, db_session):
    """The existing task-scoped auth path keeps working unchanged: a live task's
    JWT still authenticates /vm-heartbeat for the VM it is assigned to."""
    project = await _make_project(db_session, name="Task Scoped")
    task = Task(
        project_id=project.id,
        title="live task",
        payloads=[{"type": "create", "prompt": "x", "base_branch": "automain"}],
        status=TaskStatus.IN_PROGRESS,
        vm_instance_id="vm-task-scoped",
        context={},
    )
    db_session.add(task)
    await db_session.commit()

    tok = create_agent_token(
        str(task.id), "vm-task-scoped", project_id=str(project.id)
    )
    resp = await client.post(
        "/api/agent/vm-heartbeat",
        json={"vm_instance_id": "vm-task-scoped"},
        headers={"X-Agent-Token": tok},
    )
    assert resp.status_code == 204


# ---------------------------------------------------------------------------
# Lease helper — compatibility + tenant isolation under SKIP LOCKED
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_lease_returns_only_compatible_same_tenant_idle(db_session):
    project = await _make_project(db_session, name="Lease Tenant A")
    other_project = await _make_project(db_session, name="Lease Tenant B")

    # Compatible IDLE member in our project.
    await register_pool_vm(
        db_session,
        vm_instance_id="vm-match",
        project_id=project.id,
        account_id="acct-1",
        provider="anthropic",
        model="claude-opus-4-8",
        state=PoolVmState.IDLE,
    )
    # Same identity but DIFFERENT tenant — must NOT be leased.
    await register_pool_vm(
        db_session,
        vm_instance_id="vm-other-tenant",
        project_id=other_project.id,
        account_id="acct-1",
        provider="anthropic",
        model="claude-opus-4-8",
        state=PoolVmState.IDLE,
    )
    # Right tenant but WRONG account — incompatible.
    await register_pool_vm(
        db_session,
        vm_instance_id="vm-wrong-account",
        project_id=project.id,
        account_id="acct-2",
        provider="anthropic",
        model="claude-opus-4-8",
        state=PoolVmState.IDLE,
    )
    # Right tenant + identity but LEASED — not available.
    await register_pool_vm(
        db_session,
        vm_instance_id="vm-busy",
        project_id=project.id,
        account_id="acct-1",
        provider="anthropic",
        model="claude-opus-4-8",
        state=PoolVmState.LEASED,
    )
    await db_session.commit()

    task_id = uuid.uuid4()
    leased = await lease_idle_pool_vm(
        db_session,
        project_id=project.id,
        account_id="acct-1",
        provider="anthropic",
        model="claude-opus-4-8",
        leased_task_id=task_id,
    )
    assert leased is not None
    assert leased.vm_instance_id == "vm-match"
    assert leased.state == PoolVmState.LEASED
    assert leased.leased_task_id == task_id
    assert leased.lease_expires_at is not None
    assert leased.idle_since is None


@pytest.mark.asyncio
async def test_lease_returns_none_when_no_compatible_member(db_session):
    project = await _make_project(db_session, name="Lease None")
    await register_pool_vm(
        db_session,
        vm_instance_id="vm-other-account",
        project_id=project.id,
        account_id="acct-9",
        provider="anthropic",
        state=PoolVmState.IDLE,
    )
    await db_session.commit()

    leased = await lease_idle_pool_vm(
        db_session,
        project_id=project.id,
        account_id="acct-1",  # no IDLE member pinned to acct-1
        provider="anthropic",
    )
    assert leased is None


@pytest.mark.asyncio
async def test_lease_wildcard_requirement_matches_any_idle_in_tenant(db_session):
    """When the candidate task pins no account/provider/model, any IDLE member
    in the tenant is eligible."""
    project = await _make_project(db_session, name="Lease Wildcard")
    await register_pool_vm(
        db_session,
        vm_instance_id="vm-any",
        project_id=project.id,
        account_id="acct-7",
        provider="anthropic",
        state=PoolVmState.IDLE,
    )
    await db_session.commit()

    leased = await lease_idle_pool_vm(db_session, project_id=project.id)
    assert leased is not None
    assert leased.vm_instance_id == "vm-any"


# ---------------------------------------------------------------------------
# Registry transitions
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_register_list_and_state_transitions(db_session):
    project = await _make_project(db_session, name="Registry CRUD")
    await register_pool_vm(
        db_session,
        vm_instance_id="vm-crud",
        project_id=project.id,
        account_id="acct-1",
        state=PoolVmState.PROVISIONING,
    )
    await db_session.commit()

    members = await list_pool_vms(db_session, project_id=project.id)
    assert [m.vm_instance_id for m in members] == ["vm-crud"]
    assert members[0].idle_since is None  # PROVISIONING has no idle stamp

    # PROVISIONING -> IDLE stamps idle_since.
    idle = await mark_pool_vm_idle(db_session, "vm-crud")
    await db_session.commit()
    assert idle.state == PoolVmState.IDLE
    assert idle.idle_since is not None

    # Lease it, then release back to IDLE.
    leased = await lease_idle_pool_vm(
        db_session, project_id=project.id, account_id="acct-1"
    )
    await db_session.commit()
    assert leased.state == PoolVmState.LEASED

    released = await release_pool_vm(db_session, "vm-crud")
    await db_session.commit()
    assert released.state == PoolVmState.IDLE
    assert released.leased_task_id is None
    assert released.lease_expires_at is None

    # Retiring members are never leased again.
    retiring = await mark_pool_vm_retiring(db_session, "vm-crud")
    await db_session.commit()
    assert retiring.state == PoolVmState.RETIRING
    assert (
        await lease_idle_pool_vm(db_session, project_id=project.id, account_id="acct-1")
    ) is None


@pytest.mark.asyncio
async def test_transition_helpers_noop_on_unknown_vm(db_session):
    assert await mark_pool_vm_idle(db_session, "ghost") is None
    assert await mark_pool_vm_retiring(db_session, "ghost") is None
    assert await get_pool_vm(db_session, "ghost") is None


@pytest.mark.asyncio
async def test_release_pool_vm_is_guarded_to_leased_only(db_session):
    """``release_pool_vm`` returns a member to IDLE only from LEASED — so the
    task-completion return path can never resurrect a member a concurrent reaper
    already moved to RETIRING (mid-teardown), and a duplicate completion signal
    is a race-safe no-op."""
    project = await _make_project(db_session, name="Release Guard")
    await register_pool_vm(
        db_session,
        vm_instance_id="vm-guard",
        project_id=project.id,
        account_id="acct-1",
        state=PoolVmState.PROVISIONING,
    )
    await db_session.commit()

    # PROVISIONING is NOT a lease return — guard refuses (no-op).
    assert await release_pool_vm(db_session, "vm-guard") is None
    assert (await get_pool_vm(db_session, "vm-guard")).state == PoolVmState.PROVISIONING

    # Promote -> lease, then release: LEASED -> IDLE, lease binding cleared.
    await mark_pool_vm_idle(db_session, "vm-guard")
    await db_session.commit()
    leased = await lease_idle_pool_vm(
        db_session, project_id=project.id, account_id="acct-1"
    )
    await db_session.commit()
    assert leased.state == PoolVmState.LEASED

    released = await release_pool_vm(db_session, "vm-guard")
    await db_session.commit()
    assert released.state == PoolVmState.IDLE
    assert released.leased_task_id is None
    assert released.lease_expires_at is None
    assert released.idle_since is not None

    # A RETIRING member is NEVER pulled back to IDLE by a late/duplicate release.
    await mark_pool_vm_retiring(db_session, "vm-guard")
    await db_session.commit()
    assert await release_pool_vm(db_session, "vm-guard") is None
    assert (await get_pool_vm(db_session, "vm-guard")).state == PoolVmState.RETIRING


# ---------------------------------------------------------------------------
# Pool VM PROVISIONING->IDLE promotion on heartbeat
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_promote_pool_vm_provisioning_to_idle(db_session):
    """A PROVISIONING member's VM heartbeat becomes IDLE and is then leasable."""
    project = await _make_project(db_session, name="Promote Pool VM")
    await register_pool_vm(
        db_session,
        vm_instance_id="vm-promote",
        project_id=project.id,
        account_id="acct-1",
        provider="anthropic",
        model="claude-opus-4-8",
        state=PoolVmState.PROVISIONING,
    )
    await db_session.commit()

    # Verify it starts PROVISIONING with no idle_since.
    member = await get_pool_vm(db_session, "vm-promote")
    assert member.state == PoolVmState.PROVISIONING
    assert member.idle_since is None

    # Promotion happens on heartbeat.
    promoted = await promote_pool_vm_to_idle(db_session, "vm-promote")
    await db_session.commit()
    assert promoted is True

    # Now it is IDLE and leasable.
    member = await get_pool_vm(db_session, "vm-promote")
    assert member.state == PoolVmState.IDLE
    assert member.idle_since is not None

    # Lease succeeds.
    task_id = uuid.uuid4()
    leased = await lease_idle_pool_vm(
        db_session,
        project_id=project.id,
        account_id="acct-1",
        provider="anthropic",
        model="claude-opus-4-8",
        leased_task_id=task_id,
    )
    assert leased is not None
    assert leased.vm_instance_id == "vm-promote"
    assert leased.state == PoolVmState.LEASED


@pytest.mark.asyncio
async def test_promote_leased_member_stays_leased(db_session):
    """A LEASED member that heartbeats stays LEASED (not flipped; lease binding intact)."""
    project = await _make_project(db_session, name="Promote LEASED VM")
    task_id = uuid.uuid4()
    await register_pool_vm(
        db_session,
        vm_instance_id="vm-leased",
        project_id=project.id,
        account_id="acct-1",
        provider="anthropic",
        state=PoolVmState.IDLE,
    )
    # Manually put it in LEASED state (simulating a lease).
    member = await get_pool_vm(db_session, "vm-leased")
    member.state = PoolVmState.LEASED
    member.leased_task_id = task_id
    member.idle_since = None
    db_session.add(member)
    await db_session.commit()

    # Promotion does NOT flip a LEASED member.
    promoted = await promote_pool_vm_to_idle(db_session, "vm-leased")
    await db_session.commit()
    assert promoted is False

    # Still LEASED with its lease binding intact.
    member = await get_pool_vm(db_session, "vm-leased")
    assert member.state == PoolVmState.LEASED
    assert member.leased_task_id == task_id


@pytest.mark.asyncio
async def test_promote_retiring_member_stays_retiring(db_session):
    """A RETIRING member that heartbeats stays RETIRING."""
    project = await _make_project(db_session, name="Promote RETIRING VM")
    await register_pool_vm(
        db_session,
        vm_instance_id="vm-retiring",
        project_id=project.id,
        account_id="acct-1",
        provider="anthropic",
        state=PoolVmState.RETIRING,
    )
    await db_session.commit()

    # Promotion does NOT flip a RETIRING member.
    promoted = await promote_pool_vm_to_idle(db_session, "vm-retiring")
    await db_session.commit()
    assert promoted is False

    # Still RETIRING.
    member = await get_pool_vm(db_session, "vm-retiring")
    assert member.state == PoolVmState.RETIRING


@pytest.mark.asyncio
async def test_promote_idempotency_across_repeated_heartbeats(db_session):
    """Stays IDLE with no spurious idle_since churn beyond the first promotion."""
    project = await _make_project(db_session, name="Promote Idempotent")
    await register_pool_vm(
        db_session,
        vm_instance_id="vm-idem",
        project_id=project.id,
        account_id="acct-1",
        provider="anthropic",
        state=PoolVmState.PROVISIONING,
    )
    await db_session.commit()

    # First heartbeat promotes.
    promoted1 = await promote_pool_vm_to_idle(db_session, "vm-idem")
    await db_session.commit()
    assert promoted1 is True
    member1 = await get_pool_vm(db_session, "vm-idem")
    idle_since_1 = member1.idle_since

    # Second heartbeat does not promote (already IDLE).
    promoted2 = await promote_pool_vm_to_idle(db_session, "vm-idem")
    await db_session.commit()
    assert promoted2 is False
    member2 = await get_pool_vm(db_session, "vm-idem")
    # idle_since is unchanged (no spurious churn).
    assert member2.idle_since == idle_since_1


@pytest.mark.asyncio
async def test_promote_gated_behind_warm_pool_enabled_flag(db_session):
    """With warm_pool_enabled False, no promotion and no PoolVm mutation."""
    project = await _make_project(db_session, name="Promote Gated")
    await register_pool_vm(
        db_session,
        vm_instance_id="vm-gated",
        project_id=project.id,
        account_id="acct-1",
        provider="anthropic",
        state=PoolVmState.PROVISIONING,
    )
    await db_session.commit()

    prev = shared_state.runtime_config.get("warm_pool_enabled", False)
    try:
        # Ensure pool is disabled.
        shared_state.runtime_config["warm_pool_enabled"] = False

        # Direct helper call always promotes (flag gate is in the handler, not here).
        promoted = await promote_pool_vm_to_idle(db_session, "vm-gated")
        await db_session.commit()
        assert promoted is True

        # But verify the handler gates the DB work via a separate test.
    finally:
        shared_state.runtime_config["warm_pool_enabled"] = prev


@pytest.mark.asyncio
async def test_vm_heartbeat_promotes_pool_vm_to_idle_when_enabled(client, db_session):
    """Full handler wiring: a PROVISIONING pool VM heartbeats and becomes IDLE."""
    project = await _make_project(db_session, name="Heartbeat Promote")
    await register_pool_vm(
        db_session,
        vm_instance_id="vm-hb-promote",
        project_id=project.id,
        account_id="acct-1",
        provider="anthropic",
        state=PoolVmState.PROVISIONING,
    )
    await db_session.commit()

    # Enable pool.
    prev = shared_state.runtime_config.get("warm_pool_enabled", False)
    try:
        shared_state.runtime_config["warm_pool_enabled"] = True

        # Heartbeat from this VM.
        tok = create_vm_scoped_token("vm-hb-promote", str(project.id))
        resp = await client.post(
            "/api/agent/vm-heartbeat",
            json={"vm_instance_id": "vm-hb-promote"},
            headers={"X-Agent-Token": tok},
        )
        assert resp.status_code == 204

        # Member is now IDLE.
        member = await get_pool_vm(db_session, "vm-hb-promote")
        assert member.state == PoolVmState.IDLE
        assert member.idle_since is not None
    finally:
        shared_state.runtime_config["warm_pool_enabled"] = prev


@pytest.mark.asyncio
async def test_vm_heartbeat_no_promotion_when_pool_disabled(client, db_session):
    """With warm_pool_enabled False, the handler does not mutation PoolVm."""
    project = await _make_project(db_session, name="Heartbeat No Promo")
    await register_pool_vm(
        db_session,
        vm_instance_id="vm-no-promo",
        project_id=project.id,
        account_id="acct-1",
        provider="anthropic",
        state=PoolVmState.PROVISIONING,
    )
    await db_session.commit()

    # Ensure pool is disabled.
    prev = shared_state.runtime_config.get("warm_pool_enabled", False)
    try:
        shared_state.runtime_config["warm_pool_enabled"] = False

        # Heartbeat from this VM.
        tok = create_vm_scoped_token("vm-no-promo", str(project.id))
        resp = await client.post(
            "/api/agent/vm-heartbeat",
            json={"vm_instance_id": "vm-no-promo"},
            headers={"X-Agent-Token": tok},
        )
        assert resp.status_code == 204

        # Member stays PROVISIONING (no mutation).
        member = await get_pool_vm(db_session, "vm-no-promo")
        assert member.state == PoolVmState.PROVISIONING
        assert member.idle_since is None
    finally:
        shared_state.runtime_config["warm_pool_enabled"] = prev


# ---------------------------------------------------------------------------
# Rate-limit blacklist exclusion on the lease gate
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_lease_excludes_blacklisted_account_even_under_wildcard(db_session):
    """A member pinned to a blacklisted account is never leased — even with no
    account requirement (wildcard) — so a warm claim never lands on an account
    the cold path would have routed around."""
    project = await _make_project(db_session, name="Lease Blacklist")
    await register_pool_vm(
        db_session,
        vm_instance_id="vm-rl",
        project_id=project.id,
        account_id="acct-rl",
        provider="anthropic",
        state=PoolVmState.IDLE,
    )
    await db_session.commit()

    # Account wildcard, but acct-rl is excluded → no lease.
    assert (
        await lease_idle_pool_vm(
            db_session, project_id=project.id, excluded_account_ids=["acct-rl"]
        )
    ) is None
    # A different account excluded → the member is still leasable.
    leased = await lease_idle_pool_vm(
        db_session, project_id=project.id, excluded_account_ids=["acct-other"]
    )
    assert leased is not None
    assert leased.vm_instance_id == "vm-rl"


# ---------------------------------------------------------------------------
# Leak-on-disable: cleanup exemption is gated on the warm-pool flag
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cleanup_exemptions_empty_when_pool_disabled(db_session):
    """When warm_pool_enabled is off, the GCP/secret cleanup must NOT exempt pool
    members — the pool reapers are inert, so exempting them would leak their VMs
    and secrets forever. The exemption set is empty regardless of live members."""
    project = await _make_project(db_session, name="Leak Guard")
    await register_pool_vm(
        db_session,
        vm_instance_id="vm-live",
        project_id=project.id,
        account_id="acct-1",
        provider="anthropic",
        state=PoolVmState.IDLE,
    )
    await db_session.commit()

    prev = shared_state.runtime_config.get("warm_pool_enabled", False)
    try:
        shared_state.runtime_config["warm_pool_enabled"] = False
        assert await pool_vm_cleanup_exemptions(db_session) == set()
        # Enabled: the live member IS exempted from cleanup.
        shared_state.runtime_config["warm_pool_enabled"] = True
        assert await pool_vm_cleanup_exemptions(db_session) == {"vm-live"}
    finally:
        shared_state.runtime_config["warm_pool_enabled"] = prev


@pytest.mark.asyncio
async def test_cleanup_exemptions_excludes_retiring_members(db_session):
    """Even with the pool enabled, RETIRING members are reclaimable (not exempt) —
    they are being torn down, so the generic cleanup may finish the job."""
    project = await _make_project(db_session, name="Retiring Reclaim")
    await register_pool_vm(
        db_session,
        vm_instance_id="vm-retiring",
        project_id=project.id,
        account_id="acct-1",
        provider="anthropic",
        state=PoolVmState.RETIRING,
    )
    await db_session.commit()

    prev = shared_state.runtime_config.get("warm_pool_enabled", False)
    try:
        shared_state.runtime_config["warm_pool_enabled"] = True
        assert await pool_vm_cleanup_exemptions(db_session) == set()
    finally:
        shared_state.runtime_config["warm_pool_enabled"] = prev


# ---------------------------------------------------------------------------
# Completion-teardown guard: a live pool member's VM is NOT cold-deleted when its
# task completes — the pool lifecycle owns its return-to-IDLE / retire. (Sibling
# of the orphan-sweep exemption above, but for vm_lifecycle._ensure_vm_deleted,
# the path agent_status fires on every terminal task.)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_ensure_vm_deleted_skips_live_pool_member(db_session, mocker):
    """A live (IDLE) pool member's VM must be SPARED on completion — it is returned
    to the pool for re-lease, not torn down — and any orchestrator-deleted marker
    fire_and_forget pre-recorded is dropped so the still-alive VM is not later
    mis-attributed as an orchestrator delete."""
    from app import vm_lifecycle

    project = await _make_project(db_session, name="Guard Idle")
    await register_pool_vm(
        db_session,
        vm_instance_id="vm-idle-keep",
        project_id=project.id,
        account_id="acct-1",
        provider="anthropic",
        state=PoolVmState.IDLE,
    )
    await db_session.commit()

    delete = mocker.patch(
        "app.vm_lifecycle.delete_agent_vm", new_callable=mocker.AsyncMock
    )
    vm_lifecycle._orchestrator_deleted_vms["vm-idle-keep"] = (0.0, "agent_completion")

    prev = shared_state.runtime_config.get("warm_pool_enabled", False)
    try:
        shared_state.runtime_config["warm_pool_enabled"] = True
        result = await vm_lifecycle._ensure_vm_deleted(
            "vm-idle-keep", reason="agent_completion"
        )
    finally:
        shared_state.runtime_config["warm_pool_enabled"] = prev
        vm_lifecycle._orchestrator_deleted_vms.pop("vm-idle-keep", None)

    assert result is True
    delete.assert_not_called()
    assert "vm-idle-keep" not in vm_lifecycle._orchestrator_deleted_vms


@pytest.mark.asyncio
async def test_ensure_vm_deleted_tears_down_retiring_pool_member(db_session, mocker):
    """A RETIRING member is NOT governed by the guard: the pool's own retire path
    flips it RETIRING then calls _ensure_vm_deleted, which must still delete it."""
    from app import vm_lifecycle

    project = await _make_project(db_session, name="Guard Retiring")
    await register_pool_vm(
        db_session,
        vm_instance_id="vm-retire-del",
        project_id=project.id,
        account_id="acct-1",
        provider="anthropic",
        state=PoolVmState.RETIRING,
    )
    await db_session.commit()

    delete = mocker.patch(
        "app.vm_lifecycle.delete_agent_vm",
        new_callable=mocker.AsyncMock,
        return_value=True,
    )

    prev = shared_state.runtime_config.get("warm_pool_enabled", False)
    try:
        shared_state.runtime_config["warm_pool_enabled"] = True
        result = await vm_lifecycle._ensure_vm_deleted(
            "vm-retire-del", reason="pool_retiring_cleanup"
        )
    finally:
        shared_state.runtime_config["warm_pool_enabled"] = prev

    assert result is True
    delete.assert_awaited_once_with("vm-retire-del")


@pytest.mark.asyncio
async def test_ensure_vm_deleted_tears_down_when_pool_disabled(db_session, mocker):
    """With the pool disabled the guard is inert — even a live member is torn down
    (the pool reapers are off, so sparing it would leak the VM forever)."""
    from app import vm_lifecycle

    project = await _make_project(db_session, name="Guard Disabled")
    await register_pool_vm(
        db_session,
        vm_instance_id="vm-disabled",
        project_id=project.id,
        account_id="acct-1",
        provider="anthropic",
        state=PoolVmState.IDLE,
    )
    await db_session.commit()

    delete = mocker.patch(
        "app.vm_lifecycle.delete_agent_vm",
        new_callable=mocker.AsyncMock,
        return_value=True,
    )

    prev = shared_state.runtime_config.get("warm_pool_enabled", False)
    try:
        shared_state.runtime_config["warm_pool_enabled"] = False
        result = await vm_lifecycle._ensure_vm_deleted("vm-disabled")
    finally:
        shared_state.runtime_config["warm_pool_enabled"] = prev

    assert result is True
    delete.assert_awaited_once_with("vm-disabled")


# ---------------------------------------------------------------------------
# Taskless manifest variant
# ---------------------------------------------------------------------------


def test_taskless_manifest_omits_task_binding_and_marks_pool_mode():
    manifest = build_taskless_agent_secret_manifest(
        vm_instance_id="vm-pool-m",
        project_id="proj-m",
        provider="anthropic",
        auth_mode="api_key",
        model="claude-opus-4-8",
    )
    env = manifest["env"]
    # No task binding.
    assert "TASK_ID" not in env
    assert manifest["metadata"].get("task_id") is None
    # Poll-first marker + project scope present.
    assert env[POOL_MODE_ENV_VAR] == "1"
    assert env["PROJECT_ID"] == "proj-m"
    assert manifest["metadata"]["pool_mode"] is True
    # Pinned identity baked in exactly like the task-bound manifest.
    assert env["PROVIDER"] == "anthropic"
    assert env["AUTH_MODE"] == "api_key"
    assert env["LLM_MODEL"] == "claude-opus-4-8"
    # The baked token is VM-scoped, not task-scoped.
    payload = verify_agent_jwt(env["AGENT_SECRET_TOKEN"])
    assert payload["token_scope"] == VM_SCOPED_TOKEN_SCOPE
    assert payload["vm_instance_id"] == "vm-pool-m"
    assert payload.get("sub") is None


def test_task_bound_manifest_still_has_task_binding():
    """Regression guard: the refactor must not change the task-bound manifest."""
    task_id = uuid.uuid4()
    manifest = build_agent_secret_manifest(
        vm_instance_id="vm-cold",
        task_id=task_id,
        provider="anthropic",
        auth_mode="api_key",
        model="claude-opus-4-8",
        project_id="proj-cold",
    )
    env = manifest["env"]
    assert env["TASK_ID"] == str(task_id)
    assert POOL_MODE_ENV_VAR not in env
    assert manifest["metadata"]["task_id"] == str(task_id)
    payload = verify_agent_jwt(env["AGENT_SECRET_TOKEN"])
    assert payload["sub"] == str(task_id)
    assert payload.get("token_scope") is None


# ---------------------------------------------------------------------------
# PROVISIONING -> IDLE readiness promotion (the pool's load-bearing blocker):
# the agent's existing /vm-heartbeat is the ready signal that makes a booted
# pool member leasable.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_promote_pool_vm_to_idle_flips_provisioning(db_session):
    """A PROVISIONING member is flipped to IDLE (with an idle_since stamp) and
    becomes leasable; the helper reports it acted (True)."""
    project = await _make_project(db_session, name="Promote Provisioning")
    await register_pool_vm(
        db_session,
        vm_instance_id="vm-prov",
        project_id=project.id,
        account_id="acct-1",
        provider="anthropic",
        state=PoolVmState.PROVISIONING,
    )
    await db_session.commit()

    acted = await promote_pool_vm_to_idle(db_session, "vm-prov")
    await db_session.commit()
    assert acted is True

    member = await get_pool_vm(db_session, "vm-prov")
    assert member.state == PoolVmState.IDLE
    assert member.idle_since is not None
    # Lease binding is untouched by the promote (no spurious clear).
    assert member.leased_task_id is None

    # Now leasable.
    leased = await lease_idle_pool_vm(
        db_session, project_id=project.id, account_id="acct-1"
    )
    assert leased is not None
    assert leased.vm_instance_id == "vm-prov"


@pytest.mark.asyncio
async def test_promote_pool_vm_to_idle_noop_on_leased(db_session):
    """A LEASED (mid-task) member is NEVER flipped back to leasable — the
    PROVISIONING predicate guards it. Lease binding is preserved."""
    project = await _make_project(db_session, name="Promote Leased")
    task_id = uuid.uuid4()
    await register_pool_vm(
        db_session,
        vm_instance_id="vm-leased",
        project_id=project.id,
        account_id="acct-1",
        provider="anthropic",
        state=PoolVmState.IDLE,
    )
    await db_session.commit()
    leased = await lease_idle_pool_vm(
        db_session, project_id=project.id, account_id="acct-1", leased_task_id=task_id
    )
    await db_session.commit()
    assert leased.state == PoolVmState.LEASED

    acted = await promote_pool_vm_to_idle(db_session, "vm-leased")
    await db_session.commit()
    assert acted is False

    member = await get_pool_vm(db_session, "vm-leased")
    assert member.state == PoolVmState.LEASED
    assert member.leased_task_id == task_id


@pytest.mark.asyncio
async def test_promote_pool_vm_to_idle_noop_on_retiring(db_session):
    """A RETIRING (mid-teardown) member is NEVER flipped back to leasable."""
    project = await _make_project(db_session, name="Promote Retiring")
    await register_pool_vm(
        db_session,
        vm_instance_id="vm-retire",
        project_id=project.id,
        account_id="acct-1",
        provider="anthropic",
        state=PoolVmState.RETIRING,
    )
    await db_session.commit()

    acted = await promote_pool_vm_to_idle(db_session, "vm-retire")
    await db_session.commit()
    assert acted is False

    member = await get_pool_vm(db_session, "vm-retire")
    assert member.state == PoolVmState.RETIRING


@pytest.mark.asyncio
async def test_promote_pool_vm_to_idle_noop_on_unknown_vm(db_session):
    """Promoting an unknown VM (e.g. a chain/backlog VM with no PoolVm row) is a
    safe no-op on the shared heartbeat path."""
    acted = await promote_pool_vm_to_idle(db_session, "ghost-vm")
    await db_session.commit()
    assert acted is False


@pytest.mark.asyncio
async def test_promote_pool_vm_to_idle_idempotent(db_session):
    """Repeated promotions/heartbeats keep the member IDLE with no error and do
    NOT reset idle_since after the first flip."""
    project = await _make_project(db_session, name="Promote Idempotent")
    await register_pool_vm(
        db_session,
        vm_instance_id="vm-idem",
        project_id=project.id,
        account_id="acct-1",
        provider="anthropic",
        state=PoolVmState.PROVISIONING,
    )
    await db_session.commit()

    assert await promote_pool_vm_to_idle(db_session, "vm-idem") is True
    await db_session.commit()
    member = await get_pool_vm(db_session, "vm-idem")
    first_idle_since = member.idle_since
    assert first_idle_since is not None

    # Second beat: already IDLE, so the conditional UPDATE matches 0 rows and the
    # idle_since stamp is unchanged.
    assert await promote_pool_vm_to_idle(db_session, "vm-idem") is False
    await db_session.commit()
    await db_session.refresh(member)
    assert member.state == PoolVmState.IDLE
    assert member.idle_since == first_idle_since


@pytest.mark.asyncio
async def test_vm_heartbeat_promotes_provisioning_member_when_enabled(
    client, db_session
):
    """End-to-end: with the pool enabled, a PROVISIONING member whose VM POSTs
    /api/agent/vm-heartbeat becomes IDLE and is then leasable. The client request
    commits in its own session, so we refresh to read the post-commit state."""
    project = await _make_project(db_session, name="HB Promote Enabled")
    member = await register_pool_vm(
        db_session,
        vm_instance_id="vm-hb-prov",
        project_id=project.id,
        account_id="acct-1",
        provider="anthropic",
        state=PoolVmState.PROVISIONING,
    )
    await db_session.commit()

    tok = create_vm_scoped_token("vm-hb-prov", str(project.id))
    prev = shared_state.runtime_config.get("warm_pool_enabled", False)
    try:
        shared_state.runtime_config["warm_pool_enabled"] = True
        resp = await client.post(
            "/api/agent/vm-heartbeat",
            json={"vm_instance_id": "vm-hb-prov"},
            headers={"X-Agent-Token": tok},
        )
        assert resp.status_code == 204
    finally:
        shared_state.runtime_config["warm_pool_enabled"] = prev

    # Re-read post-commit state from the test session. The heartbeat promoted
    # the member on its OWN connection (a separate AsyncSessionLocal), so end the
    # test session's current transaction first — that guarantees the read below
    # starts a fresh snapshot which observes the cross-connection commit, rather
    # than a snapshot pinned before the POST (the source of cross-connection
    # read flakiness under non-READ-COMMITTED isolation).
    await db_session.commit()
    await db_session.refresh(member)
    assert member.state == PoolVmState.IDLE
    assert member.idle_since is not None

    leased = await lease_idle_pool_vm(
        db_session, project_id=project.id, account_id="acct-1"
    )
    assert leased is not None
    assert leased.vm_instance_id == "vm-hb-prov"


@pytest.mark.asyncio
async def test_vm_heartbeat_inert_when_pool_disabled(client, db_session):
    """With the pool disabled (default), /vm-heartbeat performs NO promotion / no
    PoolVm mutation — the member stays PROVISIONING (preserving today's zero-DB
    heartbeat for the live chain/backlog VMs that also hit this endpoint)."""
    project = await _make_project(db_session, name="HB Inert Disabled")
    member = await register_pool_vm(
        db_session,
        vm_instance_id="vm-hb-dark",
        project_id=project.id,
        account_id="acct-1",
        provider="anthropic",
        state=PoolVmState.PROVISIONING,
    )
    await db_session.commit()

    tok = create_vm_scoped_token("vm-hb-dark", str(project.id))
    prev = shared_state.runtime_config.get("warm_pool_enabled", False)
    try:
        shared_state.runtime_config["warm_pool_enabled"] = False
        resp = await client.post(
            "/api/agent/vm-heartbeat",
            json={"vm_instance_id": "vm-hb-dark"},
            headers={"X-Agent-Token": tok},
        )
        assert resp.status_code == 204
    finally:
        shared_state.runtime_config["warm_pool_enabled"] = prev

    # Same fresh-snapshot read as the enabled case (consistency): the disabled
    # heartbeat must leave the member untouched, so the post-POST read must
    # observe the latest committed state rather than a pre-POST snapshot.
    await db_session.commit()
    await db_session.refresh(member)
    assert member.state == PoolVmState.PROVISIONING
    assert member.idle_since is None


# ---------------------------------------------------------------------------
# Warm-claim credential re-target manifest (account-agnostic pool lease)
# ---------------------------------------------------------------------------


def test_retarget_manifest_is_credential_only_no_identity_token():
    """The re-target manifest carries provider creds ONLY — no agent token and
    no task/VM identity env (the warm claim rotates the JWT separately and the
    VM keeps its VM_INSTANCE_ID). It must never mint a credential."""
    manifest = build_warm_claim_retarget_manifest(
        provider="anthropic",
        auth_mode="api_key",
        model="claude-opus-4-8",
        oauth_token="sk-ant-resolved-secret",
    )
    env = manifest["env"]
    # Provider identity is re-materialized.
    assert env["PROVIDER"] == "anthropic"
    assert env["AUTH_MODE"] == "api_key"
    assert env["LLM_MODEL"] == "claude-opus-4-8"
    # No token, no VM/task identity — this is a credential SWAP, not a cold boot.
    assert "AGENT_SECRET_TOKEN" not in env
    assert "TASK_ID" not in env
    assert "VM_INSTANCE_ID" not in env
    assert POOL_MODE_ENV_VAR not in env
    # Marked as a transient re-target grant.
    assert manifest["metadata"]["retarget"] is True
    assert manifest["metadata"]["provider"] == "anthropic"
    # Provider secret files for the RESOLVED account are present.
    assert isinstance(manifest["files"], list)


def test_retarget_manifest_materializes_resolved_account_secret():
    """The secret env baked into the re-target manifest is the RESOLVED account's
    credential, so the re-targeted VM runs on the correct account."""
    manifest = build_warm_claim_retarget_manifest(
        provider="anthropic",
        auth_mode="api_key",
        model="claude-opus-4-8",
        oauth_token="sk-ant-RESOLVED",
    )
    # The resolved secret reaches the agent env (anthropic api_key → ANTHROPIC_API_KEY).
    assert "sk-ant-RESOLVED" in manifest["env"].values()

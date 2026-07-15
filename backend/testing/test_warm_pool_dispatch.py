"""Tests for the warm-pool dispatch hook (efficiency direction d-5fd69fe1).

Covers the four guarantees the task calls out:
  1. A queued task compatible with an IDLE pool member is served by LEASE (pinned,
     NOT cold-provisioned), and the agent's /next-role poll claims it warm with
     origin "warm_pool".
  2. A task incompatible by provider/model is NOT leased to that member.
  3. A task from a DIFFERENT tenant (project) is NEVER leased to a member — the
     hard tenant-isolation gate.
  4. One IDLE member cannot be double-leased by two dispatches.

The hook is inert when ``warm_pool_enabled`` is False (default).
"""

import pytest
from sqlalchemy import select

from app.auth import create_vm_scoped_token
from app.models import PoolVm, PoolVmState, Project, Task, TaskStatus
from app.warm_pool import (
    WARM_POOL_PIN_KEY,
    register_pool_vm,
    try_pool_dispatch,
)
import app.shared_state as shared_state


async def _make_project(db_session, name="Pool Dispatch"):
    project = Project(name=name, user_id="test-user-pool-dispatch")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)
    return project


async def _make_queued_task(db_session, project, *, model="claude-opus-4-8"):
    task = Task(
        project_id=project.id,
        title="pool candidate",
        payloads=[
            {
                "type": "create",
                "prompt": "x",
                "base_branch": "automain",
                "model": model,
            }
        ],
        status=TaskStatus.QUEUED,
        context={},
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)
    return task


async def _make_idle_member(
    db_session,
    project,
    *,
    vm_instance_id,
    account_id="acct-1",
    provider="anthropic",
    auth_mode=None,
    model="claude-opus-4-8",
):
    member = await register_pool_vm(
        db_session,
        vm_instance_id=vm_instance_id,
        project_id=project.id,
        account_id=account_id,
        provider=provider,
        auth_mode=auth_mode,
        model=model,
        state=PoolVmState.IDLE,
    )
    await db_session.commit()
    return member


async def _dispatch(
    db_session,
    task,
    *,
    provider="anthropic",
    model="claude-opus-4-8",
    account="acct-1",
    excluded=None,
):
    """Call try_pool_dispatch with a RESOLVED LLM identity, mirroring how the
    orchestrator invokes it after select_llm_runtime_for_task. The dispatch hook
    no longer reads identity off the task's raw context — the caller passes the
    routed provider/model/account and the effective rate-limit blacklist."""
    return await try_pool_dispatch(
        db_session,
        task,
        required_provider=provider,
        required_model=model,
        required_account=account,
        excluded_account_ids=excluded,
    )


@pytest.fixture
def warm_pool_on():
    """Enable the warm-pool flag for the duration of a test (restored by the
    autouse reset_runtime_config fixture)."""
    shared_state.runtime_config["warm_pool_enabled"] = True
    shared_state.warm_pool_suppressed_until = None
    yield
    shared_state.warm_pool_suppressed_until = None


@pytest.fixture
def warm_pool_retarget_on():
    """Enable warm-pool + the account-agnostic re-target relaxation.

    Mirrors an operator who has flipped on account re-target once the agent-side
    re-materialize step shipped. Same-provider only (cross-provider stays off).
    """
    shared_state.runtime_config["warm_pool_enabled"] = True
    shared_state.runtime_config["warm_pool_account_retarget_enabled"] = True
    shared_state.warm_pool_suppressed_until = None
    yield
    shared_state.warm_pool_suppressed_until = None


def _seed_account(account_id, *, provider="anthropic", auth_mode="api_key"):
    """Seed an LLM account into the in-memory pool so the claim path can build a
    credential re-target manifest for it. Returns the account dict (so a test can
    assert on its active_tasks). Caller must clean up via _clear_accounts."""
    from app.max_plan_pool import _account_by_id

    acct = {
        "id": account_id,
        "name": f"acct-{account_id}",
        "provider": provider,
        "auth_mode": auth_mode,
        "runtime_secret": f"secret-{account_id}",
        "oauth_token": None,
        "claude_config_json": None,
        "provider_metadata": {},
        "active_tasks": 0,
    }
    _account_by_id[account_id] = acct
    return acct


def _clear_accounts():
    from app.max_plan_pool import _account_by_id

    _account_by_id.clear()


# ---------------------------------------------------------------------------
# 1. Compatible task is leased + pinned (not cold-provisioned)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_compatible_task_is_leased_and_pinned(db_session, warm_pool_on):
    project = await _make_project(db_session, name="Lease Pin")
    member = await _make_idle_member(
        db_session, project, vm_instance_id="vm-pool-1", account_id="acct-1"
    )
    task = await _make_queued_task(db_session, project, model="claude-opus-4-8")

    leased = await _dispatch(db_session, task, account="acct-1")
    await db_session.commit()

    assert leased is not None
    assert leased.vm_instance_id == "vm-pool-1"
    # Member flipped to LEASED, bound to this task.
    await db_session.refresh(member)
    assert member.state == PoolVmState.LEASED
    assert member.leased_task_id == task.id
    # Task is PINNED (still QUEUED — the agent claims it warm via /next-role),
    # not cold-provisioned.
    assert task.status == TaskStatus.QUEUED
    assert task.vm_instance_id == "vm-pool-1"
    ctx = task.context or {}
    assert ctx.get("warm_target_vm_id") == "vm-pool-1"
    assert ctx.get(WARM_POOL_PIN_KEY) is True
    assert ctx.get("warm_handoff_deadline")
    # Boot-account identity stamped so per-account accounting attributes the
    # claimed work to the VM's pinned account.
    assert ctx.get("llm_account_id") == "acct-1"


@pytest.mark.asyncio
async def test_inert_when_pool_disabled(db_session):
    """Default (flag off): the hook never leases — task cold-provisions as before."""
    project = await _make_project(db_session, name="Pool Off")
    await _make_idle_member(db_session, project, vm_instance_id="vm-off")
    task = await _make_queued_task(db_session, project)

    assert shared_state.runtime_config.get("warm_pool_enabled", False) is False
    leased = await _dispatch(db_session, task)
    assert leased is None
    assert task.vm_instance_id is None
    assert (task.context or {}).get("warm_target_vm_id") is None


# ---------------------------------------------------------------------------
# 2 + 3. Compatibility + tenant isolation gates
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_incompatible_model_is_not_leased(db_session, warm_pool_on):
    project = await _make_project(db_session, name="Wrong Model")
    await _make_idle_member(
        db_session, project, vm_instance_id="vm-opus", model="claude-opus-4-8"
    )
    # Task requires a DIFFERENT model than the only IDLE member is pinned to.
    task = await _make_queued_task(db_session, project, model="claude-sonnet-4-6")

    leased = await _dispatch(db_session, task, model="claude-sonnet-4-6")
    assert leased is None
    assert task.vm_instance_id is None
    assert (task.context or {}).get(WARM_POOL_PIN_KEY) is None


@pytest.mark.asyncio
async def test_other_tenant_member_is_never_leased(db_session, warm_pool_on):
    """Hard tenant isolation: a member in project B must NEVER serve a task in
    project A, even with an identical pinned identity."""
    project_a = await _make_project(db_session, name="Tenant A")
    project_b = await _make_project(db_session, name="Tenant B")
    # Only IDLE member belongs to the OTHER tenant.
    await _make_idle_member(
        db_session, project_b, vm_instance_id="vm-tenant-b", account_id="acct-1"
    )
    task = await _make_queued_task(db_session, project_a)

    leased = await _dispatch(db_session, task, account="acct-1")
    assert leased is None
    assert task.vm_instance_id is None
    # The other tenant's member is untouched (still IDLE).
    other = (
        await db_session.execute(
            select(PoolVm).where(PoolVm.vm_instance_id == "vm-tenant-b")
        )
    ).scalars().first()
    assert other.state == PoolVmState.IDLE


# ---------------------------------------------------------------------------
# 4. No double-lease of one member
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_single_member_cannot_be_double_leased(db_session, warm_pool_on):
    """Two dispatches contending for one IDLE member: only the first leases it;
    the second finds no free member (the row is now LEASED) and returns None.

    This is the logical double-lease guard that the FOR UPDATE SKIP LOCKED in
    lease_idle_pool_vm enforces under real concurrency on Postgres."""
    project = await _make_project(db_session, name="Single Member")
    member = await _make_idle_member(db_session, project, vm_instance_id="vm-solo")
    task1 = await _make_queued_task(db_session, project)
    task2 = await _make_queued_task(db_session, project)

    leased1 = await _dispatch(db_session, task1)
    await db_session.commit()
    assert leased1 is not None
    assert leased1.vm_instance_id == "vm-solo"

    leased2 = await _dispatch(db_session, task2)
    assert leased2 is None
    assert task2.vm_instance_id is None

    await db_session.refresh(member)
    assert member.state == PoolVmState.LEASED
    assert member.leased_task_id == task1.id


# ---------------------------------------------------------------------------
# Resolved-identity gate: blacklisted account is routed around, not leased
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_member_on_resolved_account_mismatch_is_not_leased(
    db_session, warm_pool_on
):
    """The lease must use the RESOLVED account, not the task's raw context. A
    member pinned to a different account than the routed selection is not leased
    (category/quality + account routing is honored, not bypassed)."""
    project = await _make_project(db_session, name="Account Routing")
    await _make_idle_member(
        db_session, project, vm_instance_id="vm-acctA", account_id="acct-A"
    )
    task = await _make_queued_task(db_session, project)
    # Routing resolved this task to acct-B; the only idle member is on acct-A.
    leased = await _dispatch(db_session, task, account="acct-B")
    assert leased is None
    assert task.vm_instance_id is None


@pytest.mark.asyncio
async def test_member_on_blacklisted_account_is_not_leased(db_session, warm_pool_on):
    """A member pinned to a rate-limit-blacklisted account is NEVER leased — even
    under an account wildcard — so a warm claim never runs straight into the
    rate limit the cold path would have routed around (the MAJOR review finding)."""
    project = await _make_project(db_session, name="Blacklist Gate")
    await _make_idle_member(
        db_session, project, vm_instance_id="vm-bl", account_id="acct-rl"
    )
    task = await _make_queued_task(db_session, project)
    # Resolved selection routed around acct-rl, which is in the effective
    # blacklist; the only idle member is pinned to it.
    leased = await _dispatch(
        db_session, task, account="acct-other", excluded=["acct-rl"]
    )
    assert leased is None
    assert task.vm_instance_id is None
    # Belt-and-suspenders: even with an account WILDCARD (no resolved account),
    # the blacklist exclusion alone still blocks the member.
    leased_wildcard = await _dispatch(
        db_session, task, account=None, excluded=["acct-rl"]
    )
    assert leased_wildcard is None
    assert task.vm_instance_id is None


# ---------------------------------------------------------------------------
# Full flow: pinned task claimed warm via /next-role with origin "warm_pool"
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_next_role_claims_pool_pin_with_warm_pool_origin(
    db_session, client, warm_pool_on
):
    project = await _make_project(db_session, name="Pool Claim Flow")
    await _make_idle_member(
        db_session, project, vm_instance_id="vm-claim", account_id="acct-7"
    )
    task = await _make_queued_task(db_session, project, model="claude-opus-4-8")

    # Orchestrator dispatch hook: lease + pin.
    leased = await _dispatch(db_session, task, account="acct-7")
    await db_session.commit()
    assert leased is not None

    # Agent on the pool VM polls /next-role with its VM-scoped token (no prior
    # task — it is a taskless pool boot).
    tok = create_vm_scoped_token("vm-claim", str(project.id))
    resp = await client.post(
        "/api/agent/next-role",
        json={"vm_instance_id": "vm-claim"},
        headers={"X-Agent-Token": tok},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["action"] == "claim"
    assert body["task_id"] == str(task.id)
    assert body["warm_start"] is True
    assert body.get("agent_token")  # fresh task-scoped token returned to the agent

    # Task transitioned warm with the pool-specific origin.
    await db_session.refresh(task)
    assert task.status == TaskStatus.IN_PROGRESS
    assert task.vm_warm_start is True
    timing = (task.context or {}).get("_timing") or {}
    assert timing.get("warm_start") is True
    assert timing.get("warm_claim_origin") == "warm_pool"
    # Boot-account attribution preserved on the claimed task.
    assert (task.context or {}).get("llm_account_id") == "acct-7"


@pytest.mark.asyncio
async def test_next_role_ignores_pool_pin_when_pool_disabled(db_session, client):
    """A pool pin is NOT claimed via /next-role when warm_pool_enabled is off —
    the pool branch is inert and the (non-chain) pinned task is left QUEUED."""
    project = await _make_project(db_session, name="Pool Claim Disabled")
    # Pin a task manually as if a prior cycle (with the flag on) had pinned it,
    # then claim with the flag OFF.
    task = await _make_queued_task(db_session, project)
    task.vm_instance_id = "vm-disabled"
    task.context = {
        "warm_target_vm_id": "vm-disabled",
        WARM_POOL_PIN_KEY: True,
        "warm_handoff_deadline": "2099-01-01T00:00:00",
    }
    from sqlalchemy.orm.attributes import flag_modified

    flag_modified(task, "context")
    await db_session.commit()

    assert shared_state.runtime_config.get("warm_pool_enabled", False) is False
    tok = create_vm_scoped_token("vm-disabled", str(project.id))
    resp = await client.post(
        "/api/agent/next-role",
        json={"vm_instance_id": "vm-disabled"},
        headers={"X-Agent-Token": tok},
    )
    # No warm-pool claim; no chain pin either (warm_pool_pin is not a chain pin
    # with a deadline the chain branch honors) → 204 keep-polling.
    assert resp.status_code == 204
    await db_session.refresh(task)
    assert task.status == TaskStatus.QUEUED


# ---------------------------------------------------------------------------
# Per-project scope gate: dispatch honors the same allowlist as the maintainer
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_out_of_scope_project_cold_dispatches_despite_idle_member(
    db_session, warm_pool_on
):
    """A non-empty allowlist that EXCLUDES the task's project must block the lease
    at dispatch time even though a compatible IDLE member is sitting in the pool —
    the task falls through to the cold path exactly as if the pool were off for it
    (an operator scope/Disable decision takes effect immediately, not after the
    maintainer's next drain)."""
    project = await _make_project(db_session, name="Out Of Scope")
    member = await _make_idle_member(
        db_session, project, vm_instance_id="vm-oos", account_id="acct-1"
    )
    task = await _make_queued_task(db_session, project, model="claude-opus-4-8")
    # Scope the pool to some OTHER project id — this task's project is excluded.
    shared_state.runtime_config["warm_pool_project_allowlist"] = [
        "00000000-0000-0000-0000-000000000099"
    ]

    leased = await _dispatch(db_session, task, account="acct-1")
    assert leased is None
    # Member stays IDLE (untouched), task not pinned → cold path.
    await db_session.refresh(member)
    assert member.state == PoolVmState.IDLE
    assert task.vm_instance_id is None
    assert (task.context or {}).get(WARM_POOL_PIN_KEY) is None


@pytest.mark.asyncio
async def test_in_scope_project_still_leases(db_session, warm_pool_on):
    """A non-empty allowlist that INCLUDES the task's project leases normally."""
    project = await _make_project(db_session, name="In Scope")
    member = await _make_idle_member(
        db_session, project, vm_instance_id="vm-inscope", account_id="acct-1"
    )
    task = await _make_queued_task(db_session, project, model="claude-opus-4-8")
    shared_state.runtime_config["warm_pool_project_allowlist"] = [str(project.id)]

    leased = await _dispatch(db_session, task, account="acct-1")
    await db_session.commit()
    assert leased is not None
    assert leased.vm_instance_id == "vm-inscope"
    await db_session.refresh(member)
    assert member.state == PoolVmState.LEASED


@pytest.mark.asyncio
async def test_empty_allowlist_preserves_fleet_wide_leasing(db_session, warm_pool_on):
    """An empty/unset allowlist keeps today's fleet-wide behavior: any project
    with the master switch on leases normally (legacy semantics unchanged)."""
    project = await _make_project(db_session, name="Fleet Wide")
    member = await _make_idle_member(
        db_session, project, vm_instance_id="vm-fleet", account_id="acct-1"
    )
    task = await _make_queued_task(db_session, project, model="claude-opus-4-8")
    # Default: warm_pool_project_allowlist unset (empty).
    assert not shared_state.runtime_config.get("warm_pool_project_allowlist")

    leased = await _dispatch(db_session, task, account="acct-1")
    await db_session.commit()
    assert leased is not None
    assert leased.vm_instance_id == "vm-fleet"
    await db_session.refresh(member)
    assert member.state == PoolVmState.LEASED


@pytest.mark.asyncio
async def test_out_of_scope_miss_records_out_of_scope_reason(db_session, warm_pool_on):
    """The out-of-scope skip is recorded in the dispatch funnel with the distinct
    ``out_of_scope`` reason so observability stays honest about WHY a project
    cold-dispatched."""
    from app.warm_backlog_reuse import (
        _pool_lease_miss_events,
        get_pool_funnel_snapshot,
    )

    _pool_lease_miss_events.clear()
    project = await _make_project(db_session, name="OOS Funnel")
    await _make_idle_member(
        db_session, project, vm_instance_id="vm-oos-funnel", account_id="acct-1"
    )
    task = await _make_queued_task(db_session, project, model="claude-opus-4-8")
    shared_state.runtime_config["warm_pool_project_allowlist"] = [
        "00000000-0000-0000-0000-000000000099"
    ]

    leased = await _dispatch(db_session, task, account="acct-1")
    assert leased is None
    snap = get_pool_funnel_snapshot()
    assert snap["lease_miss_by_reason"].get("out_of_scope") == 1


# ---------------------------------------------------------------------------
# Account-agnostic lease + claim-time credential re-target
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_account_mismatch_not_leased_when_retarget_disabled(
    db_session, warm_pool_on
):
    """With re-target OFF (default), the lease stays STRICT-equality: a member on
    a different account than the resolved selection is NOT leased — so the pool is
    never relaxed without a working agent re-materialize step."""
    project = await _make_project(db_session, name="Strict Default")
    await _make_idle_member(
        db_session, project, vm_instance_id="vm-strict", account_id="acct-boot"
    )
    task = await _make_queued_task(db_session, project)
    leased = await _dispatch(db_session, task, account="acct-run")
    assert leased is None
    assert task.vm_instance_id is None


@pytest.mark.asyncio
async def test_account_mismatch_leased_and_pinned_to_resolved_account(
    db_session, warm_pool_retarget_on
):
    """With re-target ON, a same-tenant member on a DIFFERENT account is leased,
    and the task is pinned/accounted to the RESOLVED account (not the member's
    boot account) so per-account caps attribute correctly."""
    project = await _make_project(db_session, name="Retarget Lease")
    member = await _make_idle_member(
        db_session, project, vm_instance_id="vm-rt", account_id="acct-boot"
    )
    task = await _make_queued_task(db_session, project)

    leased = await _dispatch(db_session, task, account="acct-run")
    await db_session.commit()

    assert leased is not None
    assert leased.vm_instance_id == "vm-rt"
    await db_session.refresh(member)
    assert member.state == PoolVmState.LEASED
    # Accounting attributes to the RESOLVED account, NOT the member's boot account.
    ctx = task.context or {}
    assert ctx.get("llm_account_id") == "acct-run"
    assert ctx.get(WARM_POOL_PIN_KEY) is True


@pytest.mark.asyncio
async def test_retarget_lease_still_honors_blacklist(
    db_session, warm_pool_retarget_on
):
    """Even under the relaxed account-agnostic lease, a member pinned to a
    rate-limit-blacklisted account is NEVER leased."""
    project = await _make_project(db_session, name="Retarget Blacklist")
    await _make_idle_member(
        db_session, project, vm_instance_id="vm-rt-bl", account_id="acct-rl"
    )
    task = await _make_queued_task(db_session, project)
    leased = await _dispatch(
        db_session, task, account="acct-run", excluded=["acct-rl"]
    )
    assert leased is None
    assert task.vm_instance_id is None


@pytest.mark.asyncio
async def test_retarget_lease_still_enforces_tenant_isolation(
    db_session, warm_pool_retarget_on
):
    """The account relaxation NEVER relaxes the hard tenant gate: a member in
    project B is never leased for a task in project A."""
    project_a = await _make_project(db_session, name="RT Tenant A")
    project_b = await _make_project(db_session, name="RT Tenant B")
    await _make_idle_member(
        db_session, project_b, vm_instance_id="vm-rt-b", account_id="acct-boot"
    )
    task = await _make_queued_task(db_session, project_a)
    leased = await _dispatch(db_session, task, account="acct-run")
    assert leased is None
    assert task.vm_instance_id is None


@pytest.mark.asyncio
async def test_retarget_lease_restricted_to_same_provider(
    db_session, warm_pool_retarget_on
):
    """Account re-target alone (cross-provider OFF) stays restricted to
    same-provider members: an OpenAI member is not leased for an Anthropic task."""
    project = await _make_project(db_session, name="RT Same Provider")
    await _make_idle_member(
        db_session,
        project,
        vm_instance_id="vm-openai",
        account_id="acct-boot",
        provider="openai",
    )
    task = await _make_queued_task(db_session, project)
    leased = await _dispatch(
        db_session, task, provider="anthropic", account="acct-run"
    )
    assert leased is None
    assert task.vm_instance_id is None


@pytest.mark.asyncio
async def test_next_role_emits_retarget_manifest_on_account_mismatch(
    db_session, client, warm_pool_retarget_on
):
    """Full flow: an account-mismatched lease is claimed warm via /next-role,
    the claim response carries a credential re-target ``secret_manifest`` for the
    RESOLVED account, and the resolved account's concurrency slot is reserved
    (not the member's boot account)."""
    project = await _make_project(db_session, name="RT Claim Flow")
    await _make_idle_member(
        db_session, project, vm_instance_id="vm-rt-claim", account_id="acct-boot"
    )
    boot_acct = _seed_account("acct-boot")
    run_acct = _seed_account("acct-run")
    try:
        task = await _make_queued_task(db_session, project, model="claude-opus-4-8")
        leased = await _dispatch(db_session, task, account="acct-run")
        await db_session.commit()
        assert leased is not None

        tok = create_vm_scoped_token("vm-rt-claim", str(project.id))
        resp = await client.post(
            "/api/agent/next-role",
            json={"vm_instance_id": "vm-rt-claim"},
            headers={"X-Agent-Token": tok},
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["action"] == "claim"
        assert body["task_id"] == str(task.id)
        # Credential re-target manifest emitted for the resolved account.
        manifest = body.get("secret_manifest")
        assert manifest is not None, body
        assert manifest["metadata"]["retarget"] is True
        assert "secret-acct-run" in manifest["env"].values()
        assert "AGENT_SECRET_TOKEN" not in manifest["env"]

        # Accounting: the RESOLVED account's slot is reserved, NOT the boot one.
        assert run_acct["active_tasks"] == 1
        assert boot_acct["active_tasks"] == 0

        await db_session.refresh(task)
        assert task.status == TaskStatus.IN_PROGRESS
        timing = (task.context or {}).get("_timing") or {}
        assert timing.get("warm_claim_origin") == "warm_pool"
        assert (task.context or {}).get("llm_account_id") == "acct-run"
    finally:
        _clear_accounts()


@pytest.mark.asyncio
async def test_next_role_no_manifest_when_account_matches(
    db_session, client, warm_pool_retarget_on
):
    """When the resolved account equals the member's boot account, NO re-target
    manifest is emitted (the frozen boot credentials are already correct)."""
    project = await _make_project(db_session, name="RT Match No Manifest")
    await _make_idle_member(
        db_session, project, vm_instance_id="vm-rt-match", account_id="acct-same"
    )
    _seed_account("acct-same")
    try:
        task = await _make_queued_task(db_session, project)
        leased = await _dispatch(db_session, task, account="acct-same")
        await db_session.commit()
        assert leased is not None

        tok = create_vm_scoped_token("vm-rt-match", str(project.id))
        resp = await client.post(
            "/api/agent/next-role",
            json={"vm_instance_id": "vm-rt-match"},
            headers={"X-Agent-Token": tok},
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["action"] == "claim"
        assert body.get("secret_manifest") is None
    finally:
        _clear_accounts()


@pytest.mark.asyncio
async def test_next_role_refreshes_same_account_openai_oauth_manifest(
    db_session, client, warm_pool_retarget_on
):
    project = await _make_project(db_session, name="Codex Same Account Refresh")
    await _make_idle_member(
        db_session, project, vm_instance_id="vm-codex-same",
        account_id="acct-codex", provider="openai", auth_mode="oauth",
        model="gpt-5.6-terra",
    )
    _seed_account("acct-codex", provider="openai", auth_mode="oauth")
    try:
        task = await _make_queued_task(db_session, project, model="gpt-5.6-terra")
        leased = await _dispatch(db_session, task, provider="openai",
            model="gpt-5.6-terra", account="acct-codex")
        await db_session.commit()
        assert leased is not None
        tok = create_vm_scoped_token("vm-codex-same", str(project.id))
        resp = await client.post(
            "/api/agent/next-role", json={"vm_instance_id": "vm-codex-same"},
            headers={"X-Agent-Token": tok},
        )
        assert resp.status_code == 200, resp.text
        manifest = resp.json().get("secret_manifest")
        assert manifest is not None
        assert manifest["metadata"]["credential_refresh_only"] is True
        auth_file = next(item for item in manifest["files"] if item["path"].endswith("auth.json"))
        assert auth_file["content"] == "secret-acct-codex"
    finally:
        _clear_accounts()


@pytest.mark.asyncio
async def test_next_role_aborts_claim_when_retarget_manifest_unavailable(
    db_session, client, warm_pool_retarget_on
):
    """LANDS-TOGETHER invariant: when an account mismatch is detected but the
    resolved account is UNKNOWN to the in-memory pool (e.g. evicted/reloaded
    between dispatch and claim), no credential re-target manifest can be built.
    The warm claim MUST be aborted rather than served on the member's wrong
    frozen boot credentials — /next-role returns 204 and leaves the task QUEUED
    so the orchestrator re-dispatches it (cold once the deadline expires)."""
    project = await _make_project(db_session, name="RT Manifest Missing")
    await _make_idle_member(
        db_session, project, vm_instance_id="vm-rt-missing", account_id="acct-boot"
    )
    # Seed ONLY the boot account; the RESOLVED run account is intentionally NOT
    # in the in-memory pool, so build_account_retarget_manifest returns None.
    _seed_account("acct-boot")
    try:
        task = await _make_queued_task(db_session, project, model="claude-opus-4-8")
        leased = await _dispatch(db_session, task, account="acct-run")
        await db_session.commit()
        assert leased is not None

        tok = create_vm_scoped_token("vm-rt-missing", str(project.id))
        resp = await client.post(
            "/api/agent/next-role",
            json={"vm_instance_id": "vm-rt-missing"},
            headers={"X-Agent-Token": tok},
        )
        # Aborted: no warm claim served on wrong credentials.
        assert resp.status_code == 204, resp.text
        # Task left QUEUED for re-dispatch (NOT transitioned IN_PROGRESS).
        await db_session.refresh(task)
        assert task.status == TaskStatus.QUEUED
    finally:
        _clear_accounts()


# ---------------------------------------------------------------------------
# Cost-aware auto-drain guard gates the lease side (cluster 2/5)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cost_guard_engaged_blocks_lease(db_session, warm_pool_on):
    """When the cost guard is engaged, a compatible IDLE member is NOT leased —
    leasing pauses the moment idle spend stops being amortized, before the
    maintainer's next drain tick."""
    from app.warm_backlog_reuse import (
        _pool_cold_dispatch_events,
        _pool_lease_hit_events,
        _pool_lease_miss_events,
        get_pool_funnel_snapshot,
        record_pool_lease_miss,
    )
    from app.warm_pool import is_pool_cost_guard_engaged

    _pool_lease_hit_events.clear()
    _pool_cold_dispatch_events.clear()
    _pool_lease_miss_events.clear()
    # Force engagement on the live signals: one ELIGIBLE dispatch (a no_idle_member
    # miss — a task the pool was able to serve but couldn't) at a 0% eligible hit
    # rate, a min-hit-rate floor of 100%, and a $0 idle-cost floor (any accrued
    # idle spend qualifies). The guard gates on the eligible rate, so a lone cold
    # dispatch (never-consulted / out-of-scope) would NOT engage it — only real
    # serviceable demand does. Thresholds are configurable — we tighten them here.
    record_pool_lease_miss("no_idle_member")
    shared_state.runtime_config["warm_pool_cost_guard_min_dispatches"] = 1
    shared_state.runtime_config["warm_pool_cost_guard_min_hit_rate"] = 1.0
    shared_state.runtime_config["warm_pool_cost_guard_min_idle_cost_usd"] = 0.0

    project = await _make_project(db_session, name="Cost Guard On")
    member = await _make_idle_member(
        db_session, project, vm_instance_id="vm-cost-guard", account_id="acct-1"
    )
    task = await _make_queued_task(db_session, project, model="claude-opus-4-8")

    assert await is_pool_cost_guard_engaged(db_session) is True
    leased = await _dispatch(db_session, task, account="acct-1")
    assert leased is None
    # Member stays IDLE (not leased), task not pinned.
    await db_session.refresh(member)
    assert member.state == PoolVmState.IDLE
    assert task.vm_instance_id is None

    snap = get_pool_funnel_snapshot()
    assert snap["lease_miss_by_reason"].get("cost_guard") == 1


@pytest.mark.asyncio
async def test_cost_guard_disengaged_allows_lease(db_session, warm_pool_on):
    """A healthy hit rate (or too few dispatches) leaves the guard disengaged, so
    a compatible member is leased normally."""
    from app.warm_backlog_reuse import (
        _pool_cold_dispatch_events,
        _pool_lease_hit_events,
        _pool_lease_miss_events,
    )
    from app.warm_pool import is_pool_cost_guard_engaged

    _pool_lease_hit_events.clear()
    _pool_cold_dispatch_events.clear()
    _pool_lease_miss_events.clear()
    # No dispatches recorded → below min_dispatches → guard cannot engage.
    project = await _make_project(db_session, name="Cost Guard Off")
    member = await _make_idle_member(
        db_session, project, vm_instance_id="vm-no-guard", account_id="acct-1"
    )
    task = await _make_queued_task(db_session, project, model="claude-opus-4-8")

    assert await is_pool_cost_guard_engaged(db_session) is False
    leased = await _dispatch(db_session, task, account="acct-1")
    assert leased is not None
    assert leased.vm_instance_id == "vm-no-guard"
    # try_pool_dispatch's contract: the caller MUST commit on a non-None
    # return. Commit before the refresh — refresh() discards the pending
    # LEASED state under real ORM expiry.
    await db_session.commit()
    await db_session.refresh(member)
    assert member.state == PoolVmState.LEASED


# NOTE: the pool-hit-vs-cold-dispatch funnel now lives solely in
# warm_backlog_reuse (record_pool_lease_hit / record_pool_cold_dispatch /
# record_pool_lease_miss); its coverage is in test_warm_pool_cost_metrics.py.
# The duplicate trio that used to live in warm_pool was removed.


@pytest.mark.asyncio
async def test_no_idle_member_miss_is_attributed_to_account(db_session, warm_pool_on):
    """A no_idle_member lease miss is attributed to the resolved account in the
    funnel's by_account breakdown, not lumped under 'unattributed'.

    Before this, try_pool_dispatch called record_pool_lease_miss('no_idle_member')
    with no account, so every pool miss (and hit) showed as 'unattributed' in the
    operator panel — the warm pool ran 0% hit for days and the breakdown gave no
    clue why. The account makes per-account misses diagnosable.
    """
    from app.warm_backlog_reuse import (
        _pool_cold_dispatch_events,
        _pool_lease_hit_events,
        _pool_lease_miss_events,
        get_pool_funnel_snapshot,
    )

    _pool_lease_hit_events.clear()
    _pool_cold_dispatch_events.clear()
    _pool_lease_miss_events.clear()

    project = await _make_project(db_session, name="Miss Attribution")
    # No idle member exists for this project, so the lease misses (no_idle_member).
    task = await _make_queued_task(db_session, project, model="claude-opus-4-8")

    leased = await _dispatch(db_session, task, account="acct-xyz")
    assert leased is None

    snap = get_pool_funnel_snapshot()
    assert snap["lease_miss_by_reason"].get("no_idle_member") == 1
    by_account = snap["by_account"]
    assert by_account.get("acct-xyz", {}).get("lease_miss_total") == 1
    assert (
        by_account.get("acct-xyz", {})
        .get("lease_miss_by_reason", {})
        .get("no_idle_member")
        == 1
    )
    # Not lumped into the unattributed bucket anymore.
    assert by_account.get("unattributed", {}).get("lease_miss_total", 0) == 0


@pytest.mark.asyncio
async def test_incompatible_member_records_no_compatible_member(
    db_session, warm_pool_on
):
    """When idle members EXIST for the project but none match the task's resolved
    account/provider/model (and retarget is off), the miss is recorded as
    'no_compatible_member' — the distinct reason that points the operator at the
    retarget toggle rather than at growing the pool.
    """
    from app.warm_backlog_reuse import (
        _pool_cold_dispatch_events,
        _pool_lease_hit_events,
        _pool_lease_miss_events,
        get_pool_funnel_snapshot,
    )

    _pool_lease_hit_events.clear()
    _pool_cold_dispatch_events.clear()
    _pool_lease_miss_events.clear()

    project = await _make_project(db_session, name="Incompatible Idle")
    # An IDLE member exists, but pinned to a DIFFERENT model than the task needs.
    await _make_idle_member(
        db_session, project, vm_instance_id="vm-opus", model="claude-opus-4-8"
    )
    task = await _make_queued_task(db_session, project, model="claude-sonnet-4-6")

    leased = await _dispatch(db_session, task, model="claude-sonnet-4-6", account="acct-1")
    assert leased is None

    snap = get_pool_funnel_snapshot()
    assert snap["lease_miss_by_reason"].get("no_compatible_member") == 1
    # The genuinely-empty reason must NOT have fired.
    assert snap["lease_miss_by_reason"].get("no_idle_member") is None
    # Still attributed to the resolved account.
    by_account = snap["by_account"]
    assert (
        by_account.get("acct-1", {})
        .get("lease_miss_by_reason", {})
        .get("no_compatible_member")
        == 1
    )


@pytest.mark.asyncio
async def test_empty_pool_records_no_idle_member(db_session, warm_pool_on):
    """When the pool is genuinely empty (no idle members for the project), the miss
    is recorded as 'no_idle_member' — the reason that points the operator at growing
    the pool, NOT at the retarget toggle. Complements the incompatible-member case.
    """
    from app.warm_backlog_reuse import (
        _pool_cold_dispatch_events,
        _pool_lease_hit_events,
        _pool_lease_miss_events,
        get_pool_funnel_snapshot,
    )

    _pool_lease_hit_events.clear()
    _pool_cold_dispatch_events.clear()
    _pool_lease_miss_events.clear()

    project = await _make_project(db_session, name="Empty Pool")
    task = await _make_queued_task(db_session, project, model="claude-opus-4-8")

    leased = await _dispatch(db_session, task, account="acct-1")
    assert leased is None

    snap = get_pool_funnel_snapshot()
    assert snap["lease_miss_by_reason"].get("no_idle_member") == 1
    assert snap["lease_miss_by_reason"].get("no_compatible_member") is None

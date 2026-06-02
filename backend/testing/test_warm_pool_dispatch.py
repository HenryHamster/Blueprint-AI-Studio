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
    model="claude-opus-4-8",
):
    member = await register_pool_vm(
        db_session,
        vm_instance_id=vm_instance_id,
        project_id=project.id,
        account_id=account_id,
        provider=provider,
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


# NOTE: the pool-hit-vs-cold-dispatch funnel now lives solely in
# warm_backlog_reuse (record_pool_lease_hit / record_pool_cold_dispatch /
# record_pool_lease_miss); its coverage is in test_warm_pool_cost_metrics.py.
# The duplicate trio that used to live in warm_pool was removed.

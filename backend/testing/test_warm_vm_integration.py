"""End-to-end integration test for the warm-VM continuity flow.

Quality gap (warm-VM continuity rev): no test exercised the full flow
spawn -> warm-pin -> /vm-heartbeat -> /next-role claim. Each piece had
unit coverage but the seams between them were untested. This file fills
that gap with a single test that drives the realistic sequence:

  1. Seed a Project + a source create-task IN_PROGRESS with a vm_instance_id.
  2. Enable runtime_config.warm_continuity_enabled.
  3. Call _spawn_pipeline_task to spawn a review for the source's PR.
     Expect: a QUEUED review with warm_target_vm_id pinning it to the source's VM.
  4. Record a /vm-heartbeat for the source VM (the next-role poll itself
     also doubles as a liveness signal in production; we exercise the
     dedicated heartbeat path here).
  5. POST /api/agent/next-role with the source VM's identity.
     Expect: 200 {action: "claim", task_id, agent_token}.
  6. Use the new agent_token to interrogate the warm-claimed task's state.
     Expect: warm_start=True, warm_claim_origin="next_role_endpoint",
     status IN_PROGRESS, vm_instance_id == the warm VM.

Snapshots and resets app.shared_state.runtime_config["warm_continuity_enabled"]
and shared_state.vm_last_heartbeat so a regression in this flag's default
or a stale heartbeat from a prior test won't pollute neighbours.
"""

import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta

import pytest
from httpx import AsyncClient

from app.models import PoolIdleCostLedger, Project, PoolVmState, Task, TaskStatus
from app.auth import create_agent_token, create_vm_scoped_token, verify_agent_jwt
from app.review_pipeline import _spawn_pipeline_task
from testing.conftest import TestingSessionLocal


@pytest.mark.asyncio
async def test_warm_vm_full_flow_spawn_heartbeat_next_role_claim(
    client: AsyncClient, db_session, mocker
):
    """End-to-end: spawn warm-pinned task -> heartbeat -> next-role claim.

    Mocks broadcast_to_project (no real WebSocket) and snapshots both
    runtime_config["warm_continuity_enabled"] and the heartbeat map so
    test isolation holds.
    """
    import app.shared_state as _shared_state
    from app.shared_state import runtime_config as _runtime_config

    mocker.patch(
        "app.review_pipeline.broadcast_to_project", new_callable=mocker.AsyncMock
    )
    mocker.patch(
        "app.routers.agent.broadcast_to_project", new_callable=mocker.AsyncMock
    )

    # Snapshot mutable shared state and restore in finally so we don't pollute
    # neighbouring tests.
    prior_warm_continuity = _runtime_config.get("warm_continuity_enabled")
    prior_heartbeat = dict(_shared_state.vm_last_heartbeat)
    try:
        _runtime_config["warm_continuity_enabled"] = True

        # ── Step 1: seed Project + IN_PROGRESS source task with a vm_id ─────────
        project = Project(name="Warm-VM Integration", user_id="test-user-123")
        db_session.add(project)
        await db_session.commit()
        await db_session.refresh(project)

        source_vm_id = "agent-vm-warm-int-001"
        source_task = Task(
            project_id=project.id,
            title="Create task that opened PR #6543",
            payloads=[
                {
                    "type": "create",
                    "pr_number": 6543,
                    "branch": "feat/warm-int",
                    "base_branch": "main",
                    "prompt": "implement X",
                }
            ],
            vm_instance_id=source_vm_id,
            status=TaskStatus.IN_PROGRESS,
            context={"scope": "small", "review_loop": {"iteration": 0}},
        )
        db_session.add(source_task)
        await db_session.commit()
        await db_session.refresh(source_task)

        # ── Step 2: spawn review via _spawn_pipeline_task ────────────────────────
        # warm_continuity_enabled=True + source IN_PROGRESS + vm_id set ⇒ pin
        spawned, was_created = await _spawn_pipeline_task(
            source_task,
            "review",
            db_session,
            pr_number=6543,
            branch="feat/warm-int",
            base_branch="main",
        )
        assert was_created is True and spawned is not None
        assert spawned.status == TaskStatus.QUEUED
        # The warm pin: vm_instance_id should be NULL on the spawned task
        # (orchestrator's provisioning loop checks the heartbeat map before
        # cold-provisioning), and warm_target_vm_id carries the source VM.
        assert spawned.vm_instance_id is None
        ctx = spawned.context or {}
        assert ctx.get("warm_target_vm_id") == source_vm_id
        assert str(ctx.get("warm_target_pr")) == "6543"

        # Commit so the next-role endpoint (which opens its own session through
        # the dependency override) sees the QUEUED row.
        await db_session.commit()
        await db_session.refresh(spawned)

        # ── Step 3: record a /vm-heartbeat from the source VM ─────────────────
        # In production the agent on the source VM transitions roles and
        # heartbeats while polling /next-role. We exercise the dedicated
        # endpoint to mirror that contract. Use the source task's JWT —
        # the prior task's token is what the polling agent has at this
        # point in the lifecycle.
        source_token = create_agent_token(str(source_task.id), source_vm_id)
        hb_resp = await client.post(
            "/api/agent/vm-heartbeat",
            json={"vm_instance_id": source_vm_id},
            headers={"x-agent-token": source_token},
        )
        assert hb_resp.status_code == 204
        assert source_vm_id in _shared_state.vm_last_heartbeat

        # ── Step 4: POST /api/agent/next-role to claim the warm-pinned task ──
        nr_resp = await client.post(
            "/api/agent/next-role",
            json={
                "vm_instance_id": source_vm_id,
                "prior_task_id": str(source_task.id),
                "prior_pr_number": 6543,
            },
            headers={"x-agent-token": source_token},
        )
        assert nr_resp.status_code == 200, nr_resp.text
        body = nr_resp.json()
        assert body["action"] == "claim"
        assert body["task_id"] == str(spawned.id)
        assert body["warm_start"] is True
        new_token = body.get("agent_token")
        assert isinstance(new_token, str) and new_token

        # ── Step 5: verify the new JWT is correctly scoped to the warm task ──
        new_claims = verify_agent_jwt(new_token)
        assert new_claims["sub"] == str(spawned.id)
        assert new_claims["vm_instance_id"] == source_vm_id
        assert new_claims["project_id"] == str(project.id)
        assert new_claims["task_type"] == "review"

        # ── Step 6: verify task state after the warm claim ─────────────────────
        # The next-role endpoint committed via its own session. expire() is
        # synchronous on AsyncSession (it just clears the attribute cache);
        # refresh() then re-reads from the DB.
        db_session.expire(spawned)
        await db_session.refresh(spawned)
        assert spawned.status == TaskStatus.IN_PROGRESS
        assert spawned.vm_instance_id == source_vm_id
        timing = (spawned.context or {}).get("_timing", {})
        assert timing.get("warm_start") is True
        # Both context flag and column must be synchronized.
        assert spawned.vm_warm_start is True
        # Issue H3: the diagnostic marker distinguishes server-side seeding
        # from agent-reported values.
        assert timing.get("warm_claim_origin") == "next_role_endpoint"
        assert timing.get("prior_role_vm_id") == source_vm_id

    finally:
        # Restore runtime_config and heartbeat map
        if prior_warm_continuity is None:
            _runtime_config.pop("warm_continuity_enabled", None)
        else:
            _runtime_config["warm_continuity_enabled"] = prior_warm_continuity
        _shared_state.vm_last_heartbeat.clear()
        _shared_state.vm_last_heartbeat.update(prior_heartbeat)


# ---------------------------------------------------------------------------
# Backlog-reuse handoff: assembled end-to-end coverage
# ---------------------------------------------------------------------------
# The grace-window teardown decision (_deferred_backlog_teardown) and the
# /next-role backlog claim each have unit coverage, but their seam is only ever
# tested with _vm_claimed_backlog_work MOCKED. These tests wire the REAL
# /next-role endpoint claim together with the REAL deferred-teardown coroutine
# (mocking only asyncio.sleep + _ensure_vm_deleted, never the DB re-check) so
# the claim is observed through the same DB the teardown loop reads — the whole
# completion -> hold -> warm-claim -> no-teardown path as one flow.


@contextmanager
def _backlog_reuse_enabled():
    """Enable warm_backlog_reuse_enabled and reset the first-refusal registry +
    circuit-breaker so neighbour state cannot suppress the claim or leak out.

    runtime_config keys are restored by the autouse reset_runtime_config fixture;
    the shared_state module-level maps/deques are restored here.
    """
    import app.shared_state as ss
    from app.shared_state import runtime_config as rc

    prior_refusal = dict(ss.warm_backlog_first_refusal)
    rc["warm_backlog_reuse_enabled"] = True
    ss.warm_backlog_reuse_failure_events.clear()
    ss.warm_backlog_reuse_suppressed_until = None
    try:
        yield ss
    finally:
        ss.warm_backlog_first_refusal.clear()
        ss.warm_backlog_first_refusal.update(prior_refusal)
        ss.warm_backlog_reuse_failure_events.clear()
        ss.warm_backlog_reuse_suppressed_until = None


@pytest.mark.asyncio
async def test_warm_vm_backlog_completion_to_reuse_full_flow(
    client: AsyncClient, db_session, mocker
):
    """End-to-end: a VM completes a task while eligible same-project backlog
    exists; the /next-role poll atomically claims a QUEUED backlog task onto that
    VM (warm telemetry stamped, first-refusal registered for the orchestrator),
    and the deferred-teardown coroutine then sees the claim through the REAL DB
    and does NOT tear the VM down (no double-dispatch, no premature teardown)."""
    import app.vm_lifecycle as vml

    mocker.patch(
        "app.routers.agent.broadcast_to_project", new_callable=mocker.AsyncMock
    )

    with _backlog_reuse_enabled() as ss:
        project = Project(name="Warm Backlog E2E", user_id="test-user-123")
        db_session.add(project)
        await db_session.commit()
        await db_session.refresh(project)

        vm_id = "agent-vm-backlog-e2e"
        prior = Task(
            project_id=project.id,
            payloads=[{"type": "create", "prompt": "done"}],
            vm_instance_id=vm_id,
            status=TaskStatus.COMPLETED,
        )
        backlog = Task(
            project_id=project.id,
            title="Queued backlog work",
            payloads=[{"type": "create", "prompt": "x", "base_branch": "automain"}],
            status=TaskStatus.QUEUED,
            context={"scope": "small"},
        )
        db_session.add_all([prior, backlog])
        await db_session.commit()
        await db_session.refresh(prior)
        await db_session.refresh(backlog)

        token = create_agent_token(
            str(prior.id), vm_id, task_type="create", project_id=str(project.id)
        )
        nr = await client.post(
            "/api/agent/next-role",
            json={"vm_instance_id": vm_id, "prior_task_id": str(prior.id)},
            headers={"x-agent-token": token},
        )
        assert nr.status_code == 200, nr.text
        body = nr.json()
        assert body["action"] == "claim"
        assert body["task_id"] == str(backlog.id)
        assert body["warm_start"] is True

        # The endpoint registered the orchestrator's first-refusal window so the
        # cold-dispatch loop holds off provisioning this project's backlog.
        assert ss.is_backlog_first_refusal_active(str(project.id)) is True

        # Warm telemetry stamped and the task is bound IN_PROGRESS to this VM.
        db_session.expire(backlog)
        await db_session.refresh(backlog)
        assert backlog.status == TaskStatus.IN_PROGRESS
        assert backlog.vm_instance_id == vm_id
        assert backlog.vm_warm_start is True
        timing = (backlog.context or {}).get("_timing", {})
        assert timing.get("warm_start") is True
        assert timing.get("warm_claim_origin") == "backlog_reuse"

        # Now the deferred-teardown coroutine must NOT delete the VM: its REAL DB
        # re-check (_vm_claimed_backlog_work, unmocked) finds the IN_PROGRESS task
        # bound to the VM. Only the timing sleep and the actual delete are mocked.
        mocker.patch(
            "app.vm_lifecycle.asyncio.sleep", new_callable=mocker.AsyncMock
        )
        delete_mock = mocker.patch(
            "app.vm_lifecycle._ensure_vm_deleted", new_callable=mocker.AsyncMock
        )
        mocker.patch("app.warm_vm_state.is_vm_alive", return_value=True)

        await vml._deferred_backlog_teardown(vm_id, "agent_completion")
        delete_mock.assert_not_called()


@pytest.mark.asyncio
async def test_warm_vm_backlog_ineligible_skips_claim_and_grace_releases(
    client: AsyncClient, db_session, mocker
):
    """Negative end-to-end: the only QUEUED backlog task is pinned to a DIFFERENT
    VM, so /next-role declines to claim it (204, task untouched). With no claim
    landing, the deferred-teardown coroutine's REAL DB re-check finds no work and
    releases the hold without leaking the VM — both the idle exit (agent stopped
    polling) and the bounded-ceiling exit (still polling, never claimed) tear the
    VM down exactly once."""
    import app.vm_lifecycle as vml
    import app.warm_backlog_reuse as wbr

    mocker.patch(
        "app.routers.agent.broadcast_to_project", new_callable=mocker.AsyncMock
    )

    with _backlog_reuse_enabled():
        project = Project(name="Warm Backlog Ineligible", user_id="test-user-123")
        db_session.add(project)
        await db_session.commit()
        await db_session.refresh(project)

        vm_id = "agent-vm-backlog-ineligible"
        prior = Task(
            project_id=project.id,
            payloads=[{"type": "create", "prompt": "done"}],
            vm_instance_id=vm_id,
            status=TaskStatus.COMPLETED,
        )
        # QUEUED but pinned to a different VM -> ineligible for THIS VM's backlog.
        pinned_elsewhere = Task(
            project_id=project.id,
            title="Pinned to another VM",
            payloads=[{"type": "fix", "prompt": "y", "base_branch": "automain"}],
            status=TaskStatus.QUEUED,
            context={"warm_target_vm_id": "agent-vm-someone-else"},
        )
        db_session.add_all([prior, pinned_elsewhere])
        await db_session.commit()
        await db_session.refresh(prior)
        await db_session.refresh(pinned_elsewhere)

        token = create_agent_token(
            str(prior.id), vm_id, task_type="create", project_id=str(project.id)
        )
        nr = await client.post(
            "/api/agent/next-role",
            json={"vm_instance_id": vm_id, "prior_task_id": str(prior.id)},
            headers={"x-agent-token": token},
        )
        assert nr.status_code == 204

        # The ineligible task is left strictly untouched — not claimed warm.
        db_session.expire(pinned_elsewhere)
        await db_session.refresh(pinned_elsewhere)
        assert pinned_elsewhere.status == TaskStatus.QUEUED
        assert pinned_elsewhere.vm_instance_id is None

        # No claim landed, so the deferred-teardown coroutine must release the
        # hold and delete the VM (no leak). Bound the ceiling so the activity-gate
        # loop terminates quickly under mocked sleep.
        from app.shared_state import runtime_config as rc

        rc["warm_backlog_teardown_defer_secs"] = 20
        rc["warm_backlog_grace_window_secs"] = 40
        rc["warm_backlog_grace_idle_secs"] = 30
        rc["warm_backlog_grace_poll_interval_secs"] = 5
        mocker.patch(
            "app.vm_lifecycle.asyncio.sleep", new_callable=mocker.AsyncMock
        )

        # Idle exit: the agent stopped heartbeating -> early release, deleted once.
        wbr._grace_idle_release_events.clear()
        wbr._grace_ceiling_release_events.clear()
        delete_mock = mocker.patch(
            "app.vm_lifecycle._ensure_vm_deleted", new_callable=mocker.AsyncMock
        )
        mocker.patch("app.warm_vm_state.is_vm_alive", return_value=False)
        await vml._deferred_backlog_teardown(vm_id, "agent_completion")
        delete_mock.assert_awaited_once()
        assert len(wbr._grace_idle_release_events) == 1
        assert len(wbr._grace_ceiling_release_events) == 0

        # Ceiling exit: the agent keeps polling but never claims -> bounded
        # teardown, deleted once.
        wbr._grace_idle_release_events.clear()
        wbr._grace_ceiling_release_events.clear()
        delete_mock.reset_mock()
        mocker.patch("app.warm_vm_state.is_vm_alive", return_value=True)
        await vml._deferred_backlog_teardown(vm_id, "agent_completion")
        delete_mock.assert_awaited_once()
        assert len(wbr._grace_ceiling_release_events) == 1
        assert len(wbr._grace_idle_release_events) == 0


@pytest.mark.asyncio
async def test_warm_vm_next_role_skips_when_heartbeat_missing(
    client: AsyncClient, db_session, mocker
):
    """Auxiliary integration test: if the warm pin is set but the source VM
    has NO recorded heartbeat, /next-role still returns the claim because the
    next-role endpoint itself is a liveness signal — the heartbeat map is
    consulted by the orchestrator's provisioning loop, not by /next-role.

    This pins down the contract: /next-role's claim path is independent of
    the heartbeat map; production simply uses the heartbeat to gate the
    orchestrator's cold-provisioning decision, not the warm-claim itself.
    """
    import app.shared_state as _shared_state
    from app.shared_state import runtime_config as _runtime_config

    mocker.patch(
        "app.review_pipeline.broadcast_to_project", new_callable=mocker.AsyncMock
    )
    mocker.patch(
        "app.routers.agent.broadcast_to_project", new_callable=mocker.AsyncMock
    )

    prior_warm_continuity = _runtime_config.get("warm_continuity_enabled")
    prior_heartbeat = dict(_shared_state.vm_last_heartbeat)
    try:
        _runtime_config["warm_continuity_enabled"] = True

        project = Project(name="Warm-VM No-Heartbeat", user_id="test-user-123")
        db_session.add(project)
        await db_session.commit()
        await db_session.refresh(project)

        source_vm_id = "agent-vm-warm-no-hb"
        source_task = Task(
            project_id=project.id,
            payloads=[{"type": "review", "pr_number": 7777}],
            vm_instance_id=source_vm_id,
            status=TaskStatus.COMPLETED,
        )
        db_session.add(source_task)
        # A QUEUED warm-pinned fix waiting for this VM's pickup.
        deadline = (datetime.utcnow() + timedelta(seconds=240)).isoformat()
        pinned = Task(
            project_id=project.id,
            payloads=[{"type": "fix", "pr_number": 7777, "branch": "feat/no-hb"}],
            vm_instance_id=None,
            status=TaskStatus.QUEUED,
            context={
                "warm_target_vm_id": source_vm_id,
                "warm_target_pr": "7777",
                "warm_handoff_deadline": deadline,
            },
        )
        db_session.add(pinned)
        await db_session.commit()
        await db_session.refresh(source_task)
        await db_session.refresh(pinned)

        # Explicitly clear any heartbeat entry for this VM.
        _shared_state.vm_last_heartbeat.pop(source_vm_id, None)

        token = create_agent_token(str(source_task.id), source_vm_id)
        nr_resp = await client.post(
            "/api/agent/next-role",
            json={
                "vm_instance_id": source_vm_id,
                "prior_task_id": str(source_task.id),
                "prior_pr_number": 7777,
            },
            headers={"x-agent-token": token},
        )
        # The endpoint records its own heartbeat as a side-effect AND claims
        # the task — neither requires a pre-existing heartbeat entry.
        assert nr_resp.status_code == 200
        body = nr_resp.json()
        assert body["action"] == "claim"
        # And the endpoint DID populate the heartbeat map (so the next-role
        # poll itself is a liveness signal as documented).
        assert source_vm_id in _shared_state.vm_last_heartbeat

    finally:
        if prior_warm_continuity is None:
            _runtime_config.pop("warm_continuity_enabled", None)
        else:
            _runtime_config["warm_continuity_enabled"] = prior_warm_continuity
        _shared_state.vm_last_heartbeat.clear()
        _shared_state.vm_last_heartbeat.update(prior_heartbeat)


@pytest.mark.asyncio
async def test_warm_vm_next_role_chain_continuity_blocked_when_paused(
    client: AsyncClient, db_session, mocker
):
    """A paused project's warm chain-continuity claim must be blocked.

    A QUEUED task pinned to a live VM via warm_target_vm_id (a chain pin, no
    warm_pool_pin marker) is the create->review->fix->... handoff. While the
    owning project is paused, /next-role must NOT claim it (204, task stays
    QUEUED); resuming the project restores the claim. Pause blocks new dispatch
    on EVERY path, not just the orchestrator's cold candidate query.
    """
    import app.shared_state as _shared_state
    from app.shared_state import runtime_config as _runtime_config

    mocker.patch(
        "app.review_pipeline.broadcast_to_project", new_callable=mocker.AsyncMock
    )
    mocker.patch(
        "app.routers.agent.broadcast_to_project", new_callable=mocker.AsyncMock
    )

    prior_warm_continuity = _runtime_config.get("warm_continuity_enabled")
    prior_heartbeat = dict(_shared_state.vm_last_heartbeat)
    try:
        _runtime_config["warm_continuity_enabled"] = True

        project = Project(name="Warm-VM Paused Chain", user_id="test-user-123")
        db_session.add(project)
        await db_session.commit()
        await db_session.refresh(project)

        source_vm_id = "agent-vm-warm-paused-chain"
        source_task = Task(
            project_id=project.id,
            payloads=[{"type": "create", "pr_number": 8123}],
            vm_instance_id=source_vm_id,
            status=TaskStatus.IN_PROGRESS,
        )
        db_session.add(source_task)
        # A QUEUED warm-pinned follow-on (chain pin: no warm_pool_pin marker).
        deadline = (datetime.utcnow() + timedelta(seconds=240)).isoformat()
        pinned = Task(
            project_id=project.id,
            payloads=[{"type": "review", "pr_number": 8123, "branch": "feat/paused"}],
            vm_instance_id=None,
            status=TaskStatus.QUEUED,
            context={
                "warm_target_vm_id": source_vm_id,
                "warm_target_pr": "8123",
                "warm_handoff_deadline": deadline,
            },
        )
        db_session.add(pinned)
        await db_session.commit()
        await db_session.refresh(source_task)
        await db_session.refresh(pinned)

        token = create_agent_token(str(source_task.id), source_vm_id)
        nr_json = {
            "vm_instance_id": source_vm_id,
            "prior_task_id": str(source_task.id),
            "prior_pr_number": 8123,
        }

        # ── Paused: the chain-continuity claim must be declined (204) ───────────
        project.paused = True
        db_session.add(project)
        await db_session.commit()

        paused_resp = await client.post(
            "/api/agent/next-role", json=nr_json, headers={"x-agent-token": token}
        )
        assert paused_resp.status_code == 204, paused_resp.text
        # The pinned task is untouched — still QUEUED, no VM assigned.
        db_session.expire(pinned)
        await db_session.refresh(pinned)
        assert pinned.status == TaskStatus.QUEUED
        assert pinned.vm_instance_id is None

        # ── Resume: the same pin is now claimable warm ─────────────────────────
        project.paused = False
        db_session.add(project)
        await db_session.commit()

        resumed_resp = await client.post(
            "/api/agent/next-role", json=nr_json, headers={"x-agent-token": token}
        )
        assert resumed_resp.status_code == 200, resumed_resp.text
        body = resumed_resp.json()
        assert body["action"] == "claim"
        assert body["task_id"] == str(pinned.id)
        assert body["warm_start"] is True

    finally:
        if prior_warm_continuity is None:
            _runtime_config.pop("warm_continuity_enabled", None)
        else:
            _runtime_config["warm_continuity_enabled"] = prior_warm_continuity
        _shared_state.vm_last_heartbeat.clear()
        _shared_state.vm_last_heartbeat.update(prior_heartbeat)


@pytest.mark.asyncio
async def test_spawn_pipeline_task_skips_warm_pin_when_project_paused(
    db_session, mocker
):
    """Defense-in-depth: _spawn_pipeline_task must not set a warm-handoff pin
    for a paused project's source task, even when the source is IN_PROGRESS
    with a live VM. The follow-on is still spawned QUEUED (so resume can
    dispatch it cold), but with no warm_target_vm_id pin and no warm VM."""
    from app.shared_state import runtime_config as _runtime_config

    mocker.patch(
        "app.review_pipeline.broadcast_to_project", new_callable=mocker.AsyncMock
    )

    prior_warm_continuity = _runtime_config.get("warm_continuity_enabled")
    try:
        _runtime_config["warm_continuity_enabled"] = True

        project = Project(
            name="Spawn Pin Paused", user_id="test-user-123", paused=True
        )
        db_session.add(project)
        await db_session.commit()
        await db_session.refresh(project)

        source_vm_id = "agent-vm-spawn-pin-paused"
        source_task = Task(
            project_id=project.id,
            payloads=[
                {
                    "type": "create",
                    "pr_number": 8456,
                    "branch": "feat/spawn-paused",
                    "base_branch": "main",
                    "prompt": "implement X",
                }
            ],
            vm_instance_id=source_vm_id,
            status=TaskStatus.IN_PROGRESS,
            context={"scope": "small", "review_loop": {"iteration": 0}},
        )
        db_session.add(source_task)
        await db_session.commit()
        await db_session.refresh(source_task)

        spawned, was_created = await _spawn_pipeline_task(
            source_task,
            "review",
            db_session,
            pr_number=8456,
            branch="feat/spawn-paused",
            base_branch="main",
        )
        assert was_created is True and spawned is not None
        assert spawned.status == TaskStatus.QUEUED
        # No warm pin: vm_instance_id is the source VM name (cold spawn default),
        # and no warm_target_vm_id was stamped into context.
        ctx = spawned.context or {}
        assert "warm_target_vm_id" not in ctx
        assert "warm_handoff_deadline" not in ctx

    finally:
        if prior_warm_continuity is None:
            _runtime_config.pop("warm_continuity_enabled", None)
        else:
            _runtime_config["warm_continuity_enabled"] = prior_warm_continuity


# ---------------------------------------------------------------------------
# Warm POOL: full flag-on lifecycle assembled end-to-end
# ---------------------------------------------------------------------------
# The pool pieces (lease, maintainer reconcile, idle sweep, idle cost, the
# /next-role pool-claim branch, the dispatch funnel) each have unit coverage,
# but no test wires the WHOLE funnel together with warm_pool_enabled ON:
#   maintainer provisions an idle member  ->  the dispatch hook leases it for a
#   newly-QUEUED task instead of cold-provisioning  ->  the member's agent
#   claims it warm via /api/agent/next-role (origin "warm_pool")  ->  the task
#   completes  ->  banked idle cost is carried into the pool-inclusive cost axis
#   ->  the funnel + reuse telemetry reflect the warm start.
# These tests drive that path through the REAL lease/claim/maintainer/idle-cost
# code (mocking only the websocket broadcast), and also exercise the miss paths
# (no compatible member / blacklisted account / circuit breaker tripped) so the
# lease-miss-by-reason funnel and the cold-start fallback are verified. They are
# ADDITIVE — they do not weaken or duplicate the per-piece unit tests.


class _NoCloseSession:
    """Wrap a test db_session so ``async with factory()`` yields it without
    closing it (the fixture owns the session lifecycle)."""

    def __init__(self, session):
        self._session = session

    async def __aenter__(self):
        return self._session

    async def __aexit__(self, *exc):
        return False


def _factory_for(session):
    return lambda: _NoCloseSession(session)


@contextmanager
def _warm_pool_enabled():
    """Enable warm_pool_enabled, clear the pool circuit breaker, and reset the
    dispatch funnel deques so a neighbour's state can't suppress the lease or
    leak counts into our snapshot. runtime_config keys are restored by the
    autouse reset_runtime_config fixture; the module-level deques/scalars here
    are restored explicitly."""
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


@pytest.mark.asyncio
async def test_warm_pool_full_lifecycle_provision_lease_claim_cost(
    client: AsyncClient, db_session, mocker
):
    """End-to-end with the pool flag ON: maintainer provisions a PROVISIONING
    member -> boot heartbeat promotes PROVISIONING->IDLE -> dispatch hook leases
    it for a QUEUED task (no cold provision) -> the member's agent claims it
    warm via /next-role (origin "warm_pool") -> the task completes -> banked
    idle cost lands in the pool-inclusive cost axis, and the reuse + pool-hit
    funnel telemetry reflect the warm start."""
    from app.warm_pool import (
        WARM_POOL_PIN_KEY,
        get_pool_vm,
        register_pool_vm,
        try_pool_dispatch,
    )
    from app.warm_pool_maintainer import run_pool_maintainer_cycle
    from app.warm_backlog_reuse import get_pool_funnel_snapshot, record_pool_lease_hit
    from app.efficiency_snapshot import compute_pool_idle_cost
    from app.shared_state import runtime_config as rc

    mocker.patch(
        "app.routers.agent_warm_continuity.broadcast_to_project",
        new_callable=mocker.AsyncMock,
    )

    with _warm_pool_enabled():
        project = Project(name="Warm Pool E2E", user_id="test-user-123")
        db_session.add(project)
        await db_session.commit()
        await db_session.refresh(project)

        # ── Pool sizing: a positive target + ample dispatch/pool budget. ────────
        rc["warm_pool_target_size"] = 1
        rc["warm_pool_max_vms"] = 20
        rc["max_concurrent_vms"] = 10

        # A newly-QUEUED task makes the project demand-active so the maintainer
        # bootstraps its (zero-member) pool up to target via the deficit path.
        acct, provider, model = "acct-warm-1", "anthropic", "claude-opus-4-8"
        task = Task(
            project_id=project.id,
            title="Queued work to be served warm by the pool",
            payloads=[{"type": "create", "prompt": "x", "base_branch": "automain"}],
            status=TaskStatus.QUEUED,
            context={"scope": "small"},
        )
        db_session.add(task)
        await db_session.commit()
        await db_session.refresh(task)

        # ── Step 1: maintainer provisions ONE PROVISIONING pool member. ────────
        pool_vm_id = f"agent-vm-pool-{uuid.uuid4().hex}"

        async def _registering_provision(session, project_id):
            await register_pool_vm(
                session,
                vm_instance_id=pool_vm_id,
                project_id=project_id,
                account_id=acct,
                provider=provider,
                model=model,
                # Register in PROVISIONING state (the default); the boot heartbeat
                # will promote it to IDLE.
                state=PoolVmState.PROVISIONING,
            )
            await session.commit()
            return True

        totals = await run_pool_maintainer_cycle(
            runtime_config=rc,
            provision_fn=_registering_provision,
            session_factory=_factory_for(db_session),
        )
        assert totals["provisioned"] == 1
        member = await get_pool_vm(db_session, pool_vm_id)
        assert member is not None and member.state == PoolVmState.PROVISIONING
        assert member.idle_since is None

        # ── Step 2: boot heartbeat promotes PROVISIONING -> IDLE. ──────────────
        # A taskless pool VM boots with a VM-SCOPED token (no TASK_ID), mirroring
        # build_taskless_agent_secret_manifest / compute.provision_pool_vm.

        # Verify the pool is enabled at this point (inside the context manager).
        from app.warm_pool import is_warm_pool_enabled
        assert is_warm_pool_enabled() is True

        pool_token = create_vm_scoped_token(pool_vm_id, str(project.id))
        hb_resp = await client.post(
            "/api/agent/vm-heartbeat",
            json={"vm_instance_id": pool_vm_id},
            headers={"x-agent-token": pool_token},
        )
        assert hb_resp.status_code == 204

        # The endpoint committed via its own session. Read through a fresh session
        # (the test's long-lived db_session holds a snapshot that may not see
        # another connection's commit). This mirrors production: the dispatch hook
        # gets a fresh session just like the heartbeat endpoint does.
        async with TestingSessionLocal() as fresh_session:
            member = await get_pool_vm(fresh_session, pool_vm_id)
        assert member is not None and member.state == PoolVmState.IDLE
        assert member.idle_since is not None

        # Backdate idle_since so the IDLE->LEASED settle banks a measurable span.
        member.idle_since = datetime.utcnow() - timedelta(seconds=600)
        db_session.add(member)
        await db_session.commit()

        # ── Step 2: dispatch hook leases the member for the QUEUED task. ────────
        leased = await try_pool_dispatch(
            db_session,
            task,
            required_provider=provider,
            required_model=model,
            required_account=acct,
        )
        assert leased is not None and leased.vm_instance_id == pool_vm_id
        assert leased.state == PoolVmState.LEASED
        # The task is pinned (still QUEUED) to the leased member; cold provision
        # is skipped. Mirror orchestrator.py:2400-2422: commit, then record hit.
        await db_session.commit()
        record_pool_lease_hit()
        await db_session.refresh(task)
        assert task.status == TaskStatus.QUEUED
        assert task.vm_instance_id == pool_vm_id
        assert (task.context or {}).get(WARM_POOL_PIN_KEY) is True
        assert (task.context or {}).get("warm_target_vm_id") == pool_vm_id

        # Idle cost accrued while the member sat IDLE was banked at lease time.
        await db_session.refresh(leased)
        assert (leased.idle_cost_banked or 0.0) > 0.0

        # ── Step 3: the member's agent claims the task warm via /next-role. ─────
        # A taskless pool VM boots with a VM-SCOPED token (no TASK_ID), mirroring
        # build_taskless_agent_secret_manifest / compute.provision_pool_vm.
        token = create_vm_scoped_token(pool_vm_id, str(project.id))
        nr = await client.post(
            "/api/agent/next-role",
            json={"vm_instance_id": pool_vm_id},
            headers={"x-agent-token": token},
        )
        assert nr.status_code == 200, nr.text
        body = nr.json()
        assert body["action"] == "claim"
        assert body["task_id"] == str(task.id)
        assert body["warm_start"] is True

        # ── Step 4: warm telemetry stamped; task bound IN_PROGRESS to the VM. ───
        db_session.expire(task)
        await db_session.refresh(task)
        assert task.status == TaskStatus.IN_PROGRESS
        assert task.vm_instance_id == pool_vm_id
        assert task.vm_warm_start is True
        timing = (task.context or {}).get("_timing", {})
        assert timing.get("warm_start") is True
        assert timing.get("warm_claim_origin") == "warm_pool"

        # ── Step 5: the task completes. ─────────────────────────────────────────
        task.status = TaskStatus.COMPLETED
        db_session.add(task)
        await db_session.commit()

        # ── Step 6: idle cost carried into the pool-inclusive cost axis. ────────
        cost = await compute_pool_idle_cost(db_session, project_id=project.id)
        assert cost["settled_idle_cost_usd"] > 0.0
        assert cost["total_idle_cost_usd"] > 0.0

        # ── Step 7: funnel reflects the warm start (one hit, no cold). ──────────
        funnel = get_pool_funnel_snapshot()
        assert funnel["pool_lease_hits"] == 1
        assert funnel["cold_dispatches"] == 0
        assert funnel["pool_hit_rate"] == 1.0


@pytest.mark.asyncio
async def test_warm_pool_miss_paths_record_lease_miss_by_reason(
    client: AsyncClient, db_session, mocker
):
    """Miss paths with the pool flag ON: the dispatch hook declines to lease and
    records a typed lease-miss for each of (a) no compatible idle member, (b) a
    blacklisted boot account, and (c) the circuit breaker tripped — so the
    cold-start fallback fires and lease_miss_by_reason reflects each reason."""
    from app.warm_pool import register_pool_vm, try_pool_dispatch
    from app.warm_backlog_reuse import get_pool_funnel_snapshot

    with _warm_pool_enabled() as ss:
        project = Project(name="Warm Pool Miss", user_id="test-user-123")
        db_session.add(project)
        await db_session.commit()
        await db_session.refresh(project)

        # One IDLE member pinned to a specific account/provider/model.
        acct, provider, model = "acct-miss-1", "anthropic", "claude-opus-4-8"
        await register_pool_vm(
            db_session,
            vm_instance_id=f"agent-vm-miss-{uuid.uuid4().hex}",
            project_id=project.id,
            account_id=acct,
            provider=provider,
            model=model,
            state=PoolVmState.IDLE,
        )
        await db_session.commit()

        def _new_task():
            t = Task(
                project_id=project.id,
                payloads=[
                    {"type": "create", "prompt": "x", "base_branch": "automain"}
                ],
                status=TaskStatus.QUEUED,
                context={"scope": "small"},
            )
            db_session.add(t)
            return t

        # (a) Idle inventory exists, but no COMPATIBLE member: required model
        # differs from the member's, so this is a no_compatible_member miss.
        t_a = _new_task()
        await db_session.commit()
        await db_session.refresh(t_a)
        assert (
            await try_pool_dispatch(
                db_session,
                t_a,
                required_provider=provider,
                required_model="claude-sonnet-4-6",
                required_account=acct,
            )
            is None
        )

        # (b) Blacklisted boot account: idle inventory still exists, but the
        # member is ineligible, so this is also a no_compatible_member miss.
        t_b = _new_task()
        await db_session.commit()
        await db_session.refresh(t_b)
        assert (
            await try_pool_dispatch(
                db_session,
                t_b,
                required_provider=provider,
                required_model=model,
                required_account=acct,
                excluded_account_ids=[acct],
            )
            is None
        )

        # (c) Circuit breaker tripped: the pool is suppressed, no lease attempted.
        ss.warm_pool_suppressed_until = datetime.utcnow() + timedelta(seconds=300)
        t_c = _new_task()
        await db_session.commit()
        await db_session.refresh(t_c)
        assert (
            await try_pool_dispatch(
                db_session,
                t_c,
                required_provider=provider,
                required_model=model,
                required_account=acct,
            )
            is None
        )

        # None of the misses pinned a task — all fall back to cold provision.
        for t in (t_a, t_b, t_c):
            await db_session.refresh(t)
            assert t.status == TaskStatus.QUEUED
            assert t.vm_instance_id is None

        # The funnel recorded a typed miss for each reason and zero hits.
        funnel = get_pool_funnel_snapshot()
        assert funnel["pool_lease_hits"] == 0
        by_reason = funnel["lease_miss_by_reason"]
        assert by_reason.get("no_compatible_member", 0) >= 2  # (a) + (b)
        assert by_reason.get("no_idle_member", 0) == 0
        assert by_reason.get("suppressed", 0) >= 1  # (c)
        assert funnel["lease_miss_total"] >= 3


# ---------------------------------------------------------------------------
# Warm POOL: downstream cost/metric accounting (cluster 2/2)
# ---------------------------------------------------------------------------
# Cluster 1 wired the happy-path funnel (provision -> lease -> warm claim ->
# complete -> idle cost -> funnel) and the lease-miss-by-reason paths. These
# additive tests cover the lifecycle stages DOWNSTREAM of the claim that cluster
# 1 left unasserted, all through public APIs (no production edits):
#   * the warm claim actually moves the efficiency reuse-rate axis to 1.0 AND the
#     pool-inclusive cost axis exceeds the pool-exclusive one (idle spend is
#     honestly folded in);
#   * teardown of a stranded lease HARD-DELETES the member yet carries its banked
#     idle cost into the durable PoolIdleCostLedger (spend survives the delete);
#   * the cost-aware auto-drain guard BOTH blocks the dispatch-side lease (typed
#     cost_guard miss) AND drains the idle member on the maintainer side.


@pytest.mark.asyncio
async def test_warm_pool_warm_claim_lifts_reuse_rate_axis(
    client: AsyncClient, db_session, mocker
):
    """A pool-origin warm start moves the efficiency axes: vm_reuse_rate rises to
    1.0 (the COMPLETED task's ``_timing.warm_start`` participates as a warm VM),
    and ``cost_per_merged_pr_incl_pool_idle`` strictly exceeds the pool-exclusive
    ``cost_per_merged_pr`` because in-window pool idle spend (a ledger row whose
    ``retired_at`` falls inside the 7-day axis window) is folded into the
    net-cost numerator. This is the enablement-confidence assertion cluster 1
    did not make: the measured lift is genuinely pool-origin AND the idle cost is
    accounted, not hidden."""
    from app.warm_pool import register_pool_vm
    from app.efficiency_snapshot import compute_current_axes, AXIS_VM_REUSE_RATE

    with _warm_pool_enabled():
        project = Project(name="Warm Pool Reuse Axis", user_id="test-user-123")
        db_session.add(project)
        await db_session.commit()
        await db_session.refresh(project)

        # A pool member carrying banked idle spend (the IDLE->LEASED settle banked
        # it in production; we set it directly and idle_since=None so no live
        # accrual perturbs the exact comparison). The windowed axis intentionally
        # excludes timestampless ``idle_cost_banked``, so this member only feeds
        # the all-time ``pool_idle_cost_usd`` metric — the axis delta below comes
        # from the deterministic in-window ledger row instead.
        member = await register_pool_vm(
            db_session,
            vm_instance_id=f"agent-vm-axis-{uuid.uuid4().hex}",
            project_id=project.id,
            account_id="acct-axis-1",
            provider="anthropic",
            model="claude-opus-4-8",
            state=PoolVmState.IDLE,
        )
        member.idle_since = None
        member.idle_cost_banked = 0.02
        db_session.add(member)

        # In-window idle spend: a retired member's ledger row whose retired_at
        # falls inside the 7-day axis window. _compute_windowed_idle_cost sums
        # exactly these rows into the pool-inclusive numerator, keeping the
        # cost delta deterministic (no clock-dependent live accrual).
        db_session.add(
            PoolIdleCostLedger(
                project_id=project.id,
                vm_instance_id="agent-vm-axis-retired",
                idle_cost_usd=0.02,
                retired_at=datetime.utcnow() - timedelta(days=1),
            )
        )

        # A COMPLETED, merged task that was served WARM by the pool: it has a
        # provisioned->stopped VM interval (so cost_per_merged_pr is computable),
        # pr_merged (the merged denominator), and _timing.warm_start=True (the
        # reuse-rate numerator).
        now = datetime.utcnow()
        task = Task(
            project_id=project.id,
            title="Warm-served merged task",
            payloads=[{"type": "create", "prompt": "x", "base_branch": "automain"}],
            status=TaskStatus.COMPLETED,
            vm_provisioned_at=now - timedelta(minutes=5),
            vm_stopped_at=now,
            context={
                "scope": "small",
                "pr_merged": True,
                "_timing": {
                    "warm_start": True,
                    "warm_claim_origin": "warm_pool",
                },
            },
        )
        db_session.add(task)
        await db_session.commit()

        axes = await compute_current_axes(db_session, project.id)

        # Reuse-rate axis lifted to 1.0: the only participating VM was warm.
        assert axes[AXIS_VM_REUSE_RATE] == 1.0

        # Pool-inclusive cost strictly exceeds pool-exclusive cost: the $0.02 of
        # in-window ledger idle spend is folded into the net numerator.
        cost_excl = axes["cost_per_merged_pr"]
        cost_incl = axes["cost_per_merged_pr_incl_pool_idle"]
        assert cost_excl is not None and cost_incl is not None
        assert cost_incl > cost_excl
        assert axes["pool_idle_cost_usd"] > 0.0


@pytest.mark.asyncio
async def test_warm_pool_teardown_carries_idle_cost_into_ledger(
    client: AsyncClient, db_session, mocker
):
    """A leased member whose handoff window expired after its task completed is
    reclaimed by the idle reaper via ``pool_lease_orphaned``: the registry row is
    HARD-DELETED, yet ``compute_pool_idle_cost`` still reports the member's banked
    idle spend through the durable ``PoolIdleCostLedger`` carry-forward. Without
    the carry-forward the delete would silently erase measured idle cost and the
    pool-inclusive cost axis would undercount."""
    from app.warm_pool import register_pool_vm, get_pool_vm, try_pool_dispatch
    from app.warm_pool_maintainer import sweep_idle_pool_vms
    from app.efficiency_snapshot import compute_pool_idle_cost
    from app.shared_state import runtime_config as rc

    with _warm_pool_enabled():
        project = Project(name="Warm Pool Ledger", user_id="test-user-123")
        db_session.add(project)
        await db_session.commit()
        await db_session.refresh(project)

        acct, provider, model = "acct-ledger-1", "anthropic", "claude-opus-4-8"
        pool_vm_id = f"agent-vm-ledger-{uuid.uuid4().hex}"
        member = await register_pool_vm(
            db_session,
            vm_instance_id=pool_vm_id,
            project_id=project.id,
            account_id=acct,
            provider=provider,
            model=model,
            state=PoolVmState.IDLE,
        )
        # Backdate idle_since so the IDLE->LEASED settle banks a measurable span.
        member.idle_since = datetime.utcnow() - timedelta(seconds=600)
        db_session.add(member)

        task = Task(
            project_id=project.id,
            payloads=[{"type": "create", "prompt": "x", "base_branch": "automain"}],
            status=TaskStatus.QUEUED,
            context={"scope": "small"},
        )
        db_session.add(task)
        await db_session.commit()
        await db_session.refresh(task)

        # Lease the member (banks the idle cost), then drive the lease to the
        # stranded-orphan state: the task completed but the handoff window expired.
        leased = await try_pool_dispatch(
            db_session,
            task,
            required_provider=provider,
            required_model=model,
            required_account=acct,
        )
        assert leased is not None
        banked_at_lease = leased.idle_cost_banked or 0.0
        assert banked_at_lease > 0.0

        task.status = TaskStatus.COMPLETED
        leased.lease_expires_at = datetime.utcnow() - timedelta(seconds=30)
        db_session.add_all([task, leased])
        await db_session.commit()

        # The reaper reclaims the stranded lease. is_vm_alive False so the orphan
        # predicate fires; _ensure_vm_deleted is the only mocked seam (no real GCP).
        mocker.patch("app.warm_vm_state.is_vm_alive", return_value=False)
        delete_mock = mocker.patch(
            "app.vm_lifecycle._ensure_vm_deleted", new_callable=mocker.AsyncMock
        )
        retired = await sweep_idle_pool_vms(
            db_session, datetime.utcnow(), runtime_config=rc
        )
        assert retired == 1
        delete_mock.assert_awaited_once()

        # The registry row is gone (hard delete) ...
        assert await get_pool_vm(db_session, pool_vm_id) is None

        # ... but the banked idle spend survived via the durable ledger carry-forward.
        cost = await compute_pool_idle_cost(db_session, project_id=project.id)
        assert cost["retired_idle_cost_usd"] > 0.0
        assert cost["settled_idle_cost_usd"] > 0.0
        # Pool size now zero (the only member was reaped) yet spend is still counted.
        assert cost["pool_size"] == 0
        # ``total_idle_cost_usd`` is rounded to 6 dp by ``compute_pool_idle_cost``,
        # while ``banked_at_lease`` is the raw, unrounded ``idle_cost_banked``. When
        # the banked value's 7th decimal rounds *down* (e.g. 0.0111674 -> 0.011167)
        # the rounded metric lands a hair below the raw value, so a bare ``>=``
        # against the unrounded number is a timing-sensitive flake (the banked cost
        # tracks elapsed idle seconds, which shifts with shard load). Compare against
        # the banked value rounded to the metric's own precision: this still asserts
        # the carry-forward preserved the banked spend (any genuinely lost spend
        # would drop the rounded total below the rounded banked value) without
        # tripping on sub-microdollar rounding.
        assert cost["total_idle_cost_usd"] >= round(banked_at_lease, 6)


@pytest.mark.asyncio
async def test_evaluating_parent_dead_vm_lease_is_reclaimed(
    client: AsyncClient, db_session, mocker
):
    """A leased member whose task moved to EVALUATING (parent waiting on
    children) with a dead VM and an expired lease is reclaimed via
    ``pool_lease_orphaned``. EVALUATING parents run no further VM-side work
    (children get their own VMs), yet before this they were invisible to every
    cleanup layer: completion release fires only on terminal statuses,
    _ensure_vm_deleted skips governed members, the generic orphan sweep exempts
    LIVE_POOL_STATES, and drain only takes IDLE. The powered-off VM sat
    TERMINATED holding its SSD disk until the parent completed — five such
    zombies exhausted the region SSD quota and halted provisioning 2026-06-07."""
    from app.warm_pool import register_pool_vm, get_pool_vm, try_pool_dispatch
    from app.warm_pool_maintainer import sweep_idle_pool_vms
    from app.shared_state import runtime_config as rc

    with _warm_pool_enabled():
        project = Project(name="Warm Pool EVALUATING Zombie", user_id="test-user-123")
        db_session.add(project)
        await db_session.commit()
        await db_session.refresh(project)

        acct, provider, model = "acct-evalz-1", "anthropic", "claude-opus-4-8"
        pool_vm_id = f"agent-vm-evalz-{uuid.uuid4().hex}"
        await register_pool_vm(
            db_session,
            vm_instance_id=pool_vm_id,
            project_id=project.id,
            account_id=acct,
            provider=provider,
            model=model,
            state=PoolVmState.IDLE,
        )

        task = Task(
            project_id=project.id,
            payloads=[{"type": "research", "prompt": "x", "base_branch": "main"}],
            status=TaskStatus.QUEUED,
            context={"scope": "small"},
        )
        db_session.add(task)
        await db_session.commit()
        await db_session.refresh(task)

        leased = await try_pool_dispatch(
            db_session,
            task,
            required_provider=provider,
            required_model=model,
            required_account=acct,
        )
        assert leased is not None

        # Parent finished its VM-side work and is now waiting on children;
        # the VM powered off and the dispatch->claim handoff window passed.
        task.status = TaskStatus.EVALUATING
        leased.lease_expires_at = datetime.utcnow() - timedelta(seconds=30)
        db_session.add_all([task, leased])
        await db_session.commit()

        mocker.patch("app.warm_vm_state.is_vm_alive", return_value=False)
        delete_mock = mocker.patch(
            "app.vm_lifecycle._ensure_vm_deleted", new_callable=mocker.AsyncMock
        )
        retired = await sweep_idle_pool_vms(
            db_session, datetime.utcnow(), runtime_config=rc
        )
        assert retired == 1
        delete_mock.assert_awaited_once()
        assert await get_pool_vm(db_session, pool_vm_id) is None


@pytest.mark.asyncio
async def test_evaluating_parent_live_vm_lease_is_spared(
    client: AsyncClient, db_session, mocker
):
    """Counterpart guard: an EVALUATING-leased member whose VM is still
    heartbeating is NOT reclaimed — liveness guard (b) protects a VM that is
    alive (e.g. warm-continuity reuse), regardless of the leased task's
    status."""
    from app.warm_pool import register_pool_vm, get_pool_vm, try_pool_dispatch
    from app.warm_pool_maintainer import sweep_idle_pool_vms
    from app.shared_state import runtime_config as rc

    with _warm_pool_enabled():
        project = Project(name="Warm Pool EVALUATING Live", user_id="test-user-123")
        db_session.add(project)
        await db_session.commit()
        await db_session.refresh(project)

        acct, provider, model = "acct-evalz-2", "anthropic", "claude-opus-4-8"
        pool_vm_id = f"agent-vm-evalz-{uuid.uuid4().hex}"
        await register_pool_vm(
            db_session,
            vm_instance_id=pool_vm_id,
            project_id=project.id,
            account_id=acct,
            provider=provider,
            model=model,
            state=PoolVmState.IDLE,
        )

        task = Task(
            project_id=project.id,
            payloads=[{"type": "research", "prompt": "x", "base_branch": "main"}],
            status=TaskStatus.QUEUED,
            context={"scope": "small"},
        )
        db_session.add(task)
        await db_session.commit()
        await db_session.refresh(task)

        leased = await try_pool_dispatch(
            db_session,
            task,
            required_provider=provider,
            required_model=model,
            required_account=acct,
        )
        assert leased is not None

        task.status = TaskStatus.EVALUATING
        leased.lease_expires_at = datetime.utcnow() - timedelta(seconds=30)
        db_session.add_all([task, leased])
        await db_session.commit()

        mocker.patch("app.warm_vm_state.is_vm_alive", return_value=True)
        delete_mock = mocker.patch(
            "app.vm_lifecycle._ensure_vm_deleted", new_callable=mocker.AsyncMock
        )
        retired = await sweep_idle_pool_vms(
            db_session, datetime.utcnow(), runtime_config=rc
        )
        assert retired == 0
        delete_mock.assert_not_awaited()
        assert await get_pool_vm(db_session, pool_vm_id) is not None


@pytest.mark.asyncio
async def test_warm_pool_cost_guard_drains_and_blocks_lease(
    client: AsyncClient, db_session, mocker
):
    """With idle spend that recent hits are NOT amortizing, the cost-aware
    auto-drain guard engages on BOTH sides off the SAME live signals: the
    dispatch-side ``try_pool_dispatch`` declines to lease (typed ``cost_guard``
    miss, task falls back to cold) AND a maintainer tick forces the project's IDLE
    target to 0 and drains the idle member. This is the unattended-cost protection
    that lets an operator enable the dark pool without risking a cost-per-PR
    regression."""
    from app.warm_pool import register_pool_vm, get_pool_vm, try_pool_dispatch
    from app.warm_pool_maintainer import run_pool_maintainer_cycle
    from app.warm_backlog_reuse import (
        get_pool_funnel_snapshot,
        record_pool_lease_miss,
    )
    from app.efficiency_snapshot import compute_pool_idle_cost
    from app.shared_state import runtime_config as rc

    with _warm_pool_enabled():
        project = Project(name="Warm Pool Cost Guard", user_id="test-user-123")
        db_session.add(project)
        await db_session.commit()
        await db_session.refresh(project)

        # Cost-guard thresholds set EXPLICITLY so the test owns the engagement
        # condition and doesn't perturb sibling assertions on the module-global
        # runtime_config: trip on as few as 3 dispatches and a trivial idle floor.
        rc["warm_pool_cost_guard_enabled"] = True
        rc["warm_pool_cost_guard_min_hit_rate"] = 0.10
        rc["warm_pool_cost_guard_min_dispatches"] = 3
        rc["warm_pool_cost_guard_min_idle_cost_usd"] = 1e-9
        rc["warm_pool_cost_guard_window_hours"] = 24
        # Pool sizing so the maintainer would otherwise keep one idle member.
        rc["warm_pool_target_size"] = 1
        rc["warm_pool_max_vms"] = 20
        rc["max_concurrent_vms"] = 10

        acct, provider, model = "acct-guard-1", "anthropic", "claude-opus-4-8"
        pool_vm_id = f"agent-vm-guard-{uuid.uuid4().hex}"
        member = await register_pool_vm(
            db_session,
            vm_instance_id=pool_vm_id,
            project_id=project.id,
            account_id=acct,
            provider=provider,
            model=model,
            state=PoolVmState.IDLE,
        )
        # Backdate idle_since so live idle-cost accrual clears the (trivial) floor.
        member.idle_since = datetime.utcnow() - timedelta(seconds=600)
        db_session.add(member)
        await db_session.commit()

        # Seed a low ELIGIBLE-hit-rate funnel: several serviceable misses
        # (no_idle_member — the pool was able to serve but had no idle member),
        # zero hits -> the guard's eligible-hit-rate floor is breached. The guard
        # gates on the eligible rate, so cold dispatches (never-consulted /
        # out-of-scope) would NOT trip it — only real serviceable demand does.
        for _ in range(3):
            record_pool_lease_miss("no_idle_member")
        idle_cost = await compute_pool_idle_cost(db_session)
        assert idle_cost["total_idle_cost_usd"] > 0.0

        # ── Dispatch side: the guard blocks the lease even though a COMPATIBLE
        #    idle member exists; the miss is typed cost_guard and the task is
        #    untouched (cold fallback).
        task = Task(
            project_id=project.id,
            payloads=[{"type": "create", "prompt": "x", "base_branch": "automain"}],
            status=TaskStatus.QUEUED,
            context={"scope": "small"},
        )
        db_session.add(task)
        await db_session.commit()
        await db_session.refresh(task)

        leased = await try_pool_dispatch(
            db_session,
            task,
            required_provider=provider,
            required_model=model,
            required_account=acct,
        )
        assert leased is None
        await db_session.refresh(task)
        assert task.status == TaskStatus.QUEUED
        assert task.vm_instance_id is None
        funnel = get_pool_funnel_snapshot()
        assert funnel["lease_miss_by_reason"].get("cost_guard", 0) >= 1
        assert funnel["pool_lease_hits"] == 0

        # ── Maintainer side: a tick with the guard engaged forces target 0 and
        #    drains the idle member (no provisioning). _ensure_vm_deleted is the
        #    only mocked seam.
        mocker.patch(
            "app.warm_pool_maintainer._stale_pool_image_ids",
            new_callable=mocker.AsyncMock,
            return_value=set(),
        )
        delete_mock = mocker.patch(
            "app.vm_lifecycle._ensure_vm_deleted", new_callable=mocker.AsyncMock
        )
        # Suppress the cost-guard alert broadcast (deferred import in
        # _maybe_emit_cost_guard_alert; best-effort + try/excepted, but keep it
        # inert so no real WebSocket fan-out is attempted).
        mocker.patch(
            "app.broadcast.broadcast_to_project",
            new_callable=mocker.AsyncMock,
            create=True,
        )
        totals = await run_pool_maintainer_cycle(
            runtime_config=rc,
            session_factory=_factory_for(db_session),
        )
        assert totals["drained"] == 1
        assert totals["provisioned"] == 0
        delete_mock.assert_awaited()

        # The idle member is gone, but its banked idle cost carried into the ledger.
        assert await get_pool_vm(db_session, pool_vm_id) is None
        cost = await compute_pool_idle_cost(db_session, project_id=project.id)
        assert cost["retired_idle_cost_usd"] > 0.0


@pytest.mark.asyncio
async def test_cost_guard_ignores_pre_window_idle_spend(db_session, mocker):
    """Pre-window idle spend must NOT trip the cost guard's materiality floor.

    Seeds only a PoolIdleCostLedger row retired BEFORE the guard's window, so
    the windowed idle cost is $0. Even with dispatches below the min_hit_rate
    and idle_cost_usd set to a tiny floor, the guard must NOT engage because
    the windowed spend never crosses the floor. This regression-tests the fix
    that switches evaluate_pool_cost_guard from compute_pool_idle_cost (all-time)
    to _compute_windowed_idle_cost.
    """
    from app.warm_pool_maintainer import evaluate_pool_cost_guard
    from app.warm_backlog_reuse import record_pool_lease_miss

    project = Project(name="Cost Guard Window Test", user_id="test-user-123")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    now = datetime.utcnow()

    # A ledger row retired BEFORE the 24h guard window (50 hours ago).
    # All-time compute_pool_idle_cost would see this; windowed must not.
    db_session.add(
        PoolIdleCostLedger(
            project_id=project.id,
            vm_instance_id="vm-pre-window",
            idle_cost_usd=100.0,
            retired_at=now - timedelta(hours=50),
        )
    )
    await db_session.commit()

    # Seed a low eligible hit rate (3 misses, 0 hits) so the hit-rate condition
    # is breached — only the idle-spend floor blocks engagement.
    for _ in range(3):
        record_pool_lease_miss("no_idle_member")

    # Set a very low floor ($0.10) so even a tiny in-window spend would trip it.
    # The pre-window ledger row is $100 — if it leaked through, the guard engages.
    cfg_override = {
        "warm_pool_cost_guard_enabled": True,
        "warm_pool_cost_guard_min_hit_rate": 0.50,
        "warm_pool_cost_guard_min_dispatches": 1,
        "warm_pool_cost_guard_min_idle_cost_usd": 0.10,
        "warm_pool_cost_guard_window_hours": 24,
    }

    guard = await evaluate_pool_cost_guard(db_session, runtime_config=cfg_override)

    assert not guard["engaged"], (
        "Cost guard must NOT engage when ALL idle spend is pre-window. "
        f"idle_cost_usd={guard['idle_cost_usd']}, min_idle_cost_usd=0.10"
    )
    assert guard["idle_cost_usd"] < 0.10, (
        f"Windowed idle_cost_usd={guard['idle_cost_usd']} should be < $0.10; "
        "the pre-window $100 ledger row must not be counted"
    )

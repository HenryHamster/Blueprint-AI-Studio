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

from datetime import datetime, timedelta

import pytest
from httpx import AsyncClient

from app.models import Project, Task, TaskStatus
from app.auth import create_agent_token, verify_agent_jwt
from app.review_pipeline import _spawn_pipeline_task


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

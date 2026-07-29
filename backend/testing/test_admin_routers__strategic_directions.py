# AUTO-SPLIT from test_admin_routers.py — see refactor: decompose test_admin_routers.py
# monolith into focused domain slices. Behavior-preserving: conftest.py fixtures
# (client, db_session, session) are inherited automatically; shared helpers are
# duplicated here to keep this file self-contained.
"""Domain slice: admin_project_strategic_session.py — direction create/retire/
churn/revival, strategic message acceptance, and resident reply recovery.

Split from test_admin_routers.py; no behavior change."""

import logging
import uuid
from unittest.mock import (
    AsyncMock,
    patch,
)

import pytest
from httpx import AsyncClient
from sqlmodel import select

from app.models import (
    Project,
    Task,
    TaskStatus,
    ProjectMessage,
    MessageHandlingState,
    MessageSurface,
    MessageType,
    _utcnow,
)


# ── Constants ────────────────────────────────────────────────

TEST_USER_ID = "test-user-123"


@pytest.fixture(autouse=True)
def _default_no_migration_pr():
    """Default the pre-merge Alembic dual-head guard to a no-op for merge tests.

    The operator-approval merge paths (``_handle_rollup_pr_approval``,
    ``_merge_critical_pr``) and the operator-triggered rollup auto-merge now run
    ``enforce_premerge_migration_guard`` before merging, which calls
    ``github.get_pr_files``. Most merge tests only mock ``merge_pull_request``;
    returning a benign non-migration file list short-circuits the guard to
    ``ok`` so those merges proceed unchanged. A non-empty list is used so other
    ``get_pr_files`` consumers (e.g. empty-PR detection) keep their pre-change
    behavior. Tests exercising the guard itself re-patch ``app.github.get_pr_files``.
    """
    with patch(
        "app.github.get_pr_files",
        new_callable=AsyncMock,
        return_value=[{"filename": "README.md"}],
    ):
        yield


async def _make_strategic_retire_task(
    db_session, *, name, vm, direction_id, context=None, gap_id=None
):
    """Build a project + strategic_review task with one active direction."""
    from app import strategic_cycle

    direction = {
        "id": direction_id,
        "text": "model gc garbage collection optimize performance",
        "status": "active",
        "priority": "medium",
        "notes": [],
        "research_history": [],
    }
    if gap_id:
        direction["gap_id"] = gap_id
    project = Project(
        name=name,
        user_id=TEST_USER_ID,
        vision={"purpose": "test", "revision": 1},
        directions=[direction],
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)
    cycle = await strategic_cycle.create(db_session, project_id=project.id)
    task = Task(
        project_id=project.id,
        status=TaskStatus.IN_PROGRESS,
        vm_instance_id=vm,
        payloads=[
            {"type": "strategic_review", "prompt": "review", "vision": {"revision": 1}}
        ],
        context={"strategic_cycle_id": cycle.id, **(context or {})},
        strategic_cycle_id=cycle.id,
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)
    return project, task


async def _make_strategic_churn_recommended_task(
    db_session, *, name, vm, direction_id, gap_id="g-churn"
):
    """Build a project + strategic_review task with one churn-retirement-recommended
    direction held PAUSED, staffing a problem-model gap."""
    from app import strategic_cycle

    direction = {
        "id": direction_id,
        "text": "zombie direction pending keep/retire decision",
        "status": "paused",
        "priority": "medium",
        "notes": [],
        "research_history": [],
        "gap_id": gap_id,
        "auto_paused": True,
        "paused_reason": "Churn guardrail: repeated no-merge",
        "paused_at": "2026-06-01T00:00:00",
        "churn_family_pause_count": 2,
        "churn_pause_evidence": {"windowed_non_merge": 9},
        "needs_manual_intervention": True,
        "needs_manual_intervention_at": "2026-06-01T00:00:00",
        "churn_retirement_recommended": True,
        "churn_retirement_recommended_reason": "Retirement recommended: zombie loop",
        "churn_retirement_recommended_at": "2026-06-01T00:00:00",
    }
    project = Project(
        name=name,
        user_id=TEST_USER_ID,
        vision={"purpose": "test", "revision": 1},
        directions=[direction],
        problem_model={
            "revision": 1,
            "gaps": [
                {
                    "id": gap_id,
                    "name": "churn gap",
                    "status": "active",
                    "active_moves": [direction_id],
                    "past_moves": [],
                }
            ],
        },
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)
    cycle = await strategic_cycle.create(db_session, project_id=project.id)
    task = Task(
        project_id=project.id,
        status=TaskStatus.IN_PROGRESS,
        vm_instance_id=vm,
        payloads=[
            {"type": "strategic_review", "prompt": "review", "vision": {"revision": 1}}
        ],
        context={"strategic_cycle_id": cycle.id},
        strategic_cycle_id=cycle.id,
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)
    return project, task


async def _make_resident_message_worker(
    db_session,
    *,
    session_id: str = "resident-receipt-session",
    generation: int = 1,
):
    project = Project(name="Resident Receipt", user_id=TEST_USER_ID)
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)
    task = Task(
        project_id=project.id,
        status=TaskStatus.IN_PROGRESS,
        vm_instance_id="vm-resident-receipt",
        payloads=[{"type": "strategic_review", "prompt": "reply"}],
        context={
            "_resident_session": True,
            "strategic_session_id": session_id,
            "_resident_worker_generation": generation,
        },
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)
    project.vision = {
        "_resident_strategic_session": {
            "session_id": session_id,
            "task_id": str(task.id),
            "worker_generation": generation,
            "status": "active",
        }
    }
    db_session.add(project)
    await db_session.commit()
    return project, task


@pytest.mark.asyncio
async def test_strategic_api_output_update_events_include_live_linkage(
    client: AsyncClient, db_session, mocker
):
    from app import strategic_cycle
    from app.auth import create_agent_token

    emit = AsyncMock()
    mocker.patch("app.routers.admin_agent_ops.emit_strategic_updated", new=emit)
    project = Project(
        name="Strategic Events",
        user_id=TEST_USER_ID,
        vision={"purpose": "test", "revision": 1},
        directions=[],
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)
    cycle = await strategic_cycle.create(db_session, project_id=project.id)
    task = Task(
        project_id=project.id,
        status=TaskStatus.IN_PROGRESS,
        vm_instance_id="vm-strategic-events",
        payloads=[
            {"type": "strategic_review", "prompt": "review", "vision": {"revision": 1}}
        ],
        context={"strategic_cycle_id": cycle.id},
        strategic_cycle_id=cycle.id,
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)
    token = create_agent_token(
        str(task.id), "vm-strategic-events", task_type="strategic_review"
    )
    headers = {"x-agent-token": token}

    create_resp = await client.post(
        "/api/agent/strategic",
        json={
            "task_id": str(task.id),
            "op": "create_direction",
            "data": {
                "text": "Stabilize billing webhooks",
                "priority": "high",
                "rationale": "operator steering requested billing reliability",
                "success_evidence": "Webhook retries are verified",
                "retirement_condition": "No open webhook incidents",
                "category": "mixed",
            },
        },
        headers=headers,
    )
    assert create_resp.status_code == 200, create_resp.text
    created_id = create_resp.json()["direction_id"]
    await db_session.refresh(project)
    created_direction = next(
        direction for direction in project.directions if direction["id"] == created_id
    )
    assert created_direction["category"] == "mixed"

    pm_resp = await client.post(
        "/api/agent/strategic",
        json={
            "task_id": str(task.id),
            "op": "set_problem_model",
            "data": {
                "intent": {"purpose": "Ship the thing", "current_focus": "harden"},
                "capabilities": [],
                "gaps": [],
            },
        },
        headers=headers,
    )
    assert pm_resp.status_code == 200, pm_resp.text

    ru_resp = await client.post(
        "/api/agent/strategic",
        json={
            "task_id": str(task.id),
            "op": "set_running_update",
            "data": {"markdown": "## Running update\nBilling reliability is active."},
        },
        headers=headers,
    )
    assert ru_resp.status_code == 200, ru_resp.text

    assert emit.await_count == 3
    first_call = emit.await_args_list[0]
    assert first_call.args == (project.id, "directions")
    assert first_call.kwargs == {
        "task_id": task.id,
        "cycle_id": cycle.id,
        "output_kind": "directions",
        "op": "create_direction",
        "changed_direction_ids": [created_id],
        "changed_count": 1,
        "problem_model_changed": None,
        "running_update_changed": None,
    }
    assert emit.await_args_list[1].args == (project.id, "problem_model")
    assert emit.await_args_list[1].kwargs["cycle_id"] == cycle.id
    assert emit.await_args_list[1].kwargs["op"] == "set_problem_model"
    assert emit.await_args_list[1].kwargs["problem_model_changed"] is True
    assert emit.await_args_list[2].args == (project.id, "running_update")
    assert emit.await_args_list[2].kwargs["cycle_id"] == cycle.id
    assert emit.await_args_list[2].kwargs["op"] == "set_running_update"
    assert emit.await_args_list[2].kwargs["running_update_changed"] is True


@pytest.mark.asyncio
async def test_retire_direction_records_retired_memory(
    client: AsyncClient, db_session, mocker
):
    """API retire_direction records a _recently_retired_directions memory entry.

    The strategist-driven retire path must write cross-cycle dedup memory (with
    a descriptive reason and the retire rationale as the outcome) so the same
    gap cannot be immediately re-staffed next cycle.
    """
    from app.auth import create_agent_token

    mocker.patch(
        "app.routers.admin_agent_ops.emit_strategic_updated", new=AsyncMock()
    )
    project, task = await _make_strategic_retire_task(
        db_session,
        name="RetireMemory",
        vm="vm-retire-mem",
        direction_id="d-retire-mem",
        gap_id="g-model-gc",
    )
    token = create_agent_token(
        str(task.id), "vm-retire-mem", task_type="strategic_review"
    )
    resp = await client.post(
        "/api/agent/strategic",
        json={
            "task_id": str(task.id),
            "op": "retire_direction",
            "data": {
                "direction_id": "d-retire-mem",
                "rationale": "lane is complete, evidence shipped",
                "terminal_reason": "explicit_abandonment",
            },
        },
        headers={"x-agent-token": token},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "ok"

    await db_session.refresh(project)
    mem = (project.vision or {}).get("_recently_retired_directions") or []
    entry = next((m for m in mem if m.get("id") == "d-retire-mem"), None)
    assert entry is not None, f"retire must record retired memory; got {mem}"
    assert entry["reason"] == "strategic_retire"
    assert entry.get("gap_id") == "g-model-gc"
    assert entry.get("outcome") == "lane is complete, evidence shipped"


@pytest.mark.asyncio
async def test_strategic_decide_churn_retirement_keep(
    client: AsyncClient, db_session, mocker
):
    """The strategist decide_churn_retirement KEEP op reactivates the held direction
    and resets the churn ladder, clearing the recommendation."""
    from app.auth import create_agent_token

    mocker.patch(
        "app.routers.admin_agent_ops.emit_strategic_updated", new=AsyncMock()
    )
    project, task = await _make_strategic_churn_recommended_task(
        db_session, name="ChurnKeep", vm="vm-churn-keep", direction_id="d-churn-keep"
    )
    token = create_agent_token(
        str(task.id), "vm-churn-keep", task_type="strategic_review"
    )
    resp = await client.post(
        "/api/agent/strategic",
        json={
            "task_id": str(task.id),
            "op": "decide_churn_retirement",
            "data": {"direction_id": "d-churn-keep", "decision": "KEEP"},
        },
        headers={"x-agent-token": token},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "kept"

    await db_session.refresh(project)
    d = next(x for x in project.directions if x["id"] == "d-churn-keep")
    assert d["status"] == "active"
    assert not d.get("churn_retirement_recommended")
    assert d["churn_family_pause_count"] == 0
    assert not d.get("needs_manual_intervention")


@pytest.mark.asyncio
async def test_strategic_decide_churn_retirement_retire(
    client: AsyncClient, db_session, mocker
):
    """The strategist decide_churn_retirement RETIRE op completes the direction,
    clears the recommendation, reconciles the gap, and records retired memory."""
    from app.auth import create_agent_token

    mocker.patch(
        "app.routers.admin_agent_ops.emit_strategic_updated", new=AsyncMock()
    )
    project, task = await _make_strategic_churn_recommended_task(
        db_session,
        name="ChurnRetire",
        vm="vm-churn-retire",
        direction_id="d-churn-retire",
        gap_id="g-churn-r",
    )
    token = create_agent_token(
        str(task.id), "vm-churn-retire", task_type="strategic_review"
    )
    resp = await client.post(
        "/api/agent/strategic",
        json={
            "task_id": str(task.id),
            "op": "decide_churn_retirement",
            "data": {
                "direction_id": "d-churn-retire",
                "decision": "RETIRE",
                "rationale": "no path to value",
            },
        },
        headers={"x-agent-token": token},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "retired"

    await db_session.refresh(project)
    d = next(x for x in project.directions if x["id"] == "d-churn-retire")
    assert d["status"] == "completed"
    assert d["auto_completed"] is True
    assert not d.get("churn_retirement_recommended")
    gap = next(g for g in project.problem_model["gaps"] if g["id"] == "g-churn-r")
    assert "d-churn-retire" not in gap["active_moves"]
    assert "d-churn-retire" in gap["past_moves"]
    mem = (project.vision or {}).get("_recently_retired_directions") or []
    assert any(m.get("id") == "d-churn-retire" for m in mem)


@pytest.mark.asyncio
async def test_retire_direction_challenge_gate_removed_proceeds(
    client: AsyncClient, db_session, mocker
):
    """The retirement-challenge deferral gate has been removed.

    Formerly, enforce mode with a rejected retirement challenge deferred the
    retire (direction stayed active, no memory recorded). That gate is gone, so
    the retire now proceeds regardless of any (now-ignored) challenge record or
    the inert ``strategic_retirement_challenge_gate_mode`` config: the direction
    is retired and a _recently_retired_directions memory entry is written.
    """
    from app.auth import create_agent_token

    mocker.patch(
        "app.routers.admin_agent_ops.emit_strategic_updated", new=AsyncMock()
    )
    project, task = await _make_strategic_retire_task(
        db_session,
        name="RetireChallengeGateRemoved",
        vm="vm-defer-mem",
        direction_id="d-defer-mem",
        gap_id="g-model-gc",
    )
    token = create_agent_token(
        str(task.id), "vm-defer-mem", task_type="strategic_review"
    )
    resp = await client.post(
        "/api/agent/strategic",
        json={
            "task_id": str(task.id),
            "op": "retire_direction",
            "data": {
                "direction_id": "d-defer-mem",
                "rationale": "kill it",
                "terminal_reason": "explicit_abandonment",
            },
        },
        headers={"x-agent-token": token},
    )

    assert resp.status_code == 200, resp.text
    # No longer deferred — the retire completes.
    assert resp.json()["status"] == "ok"
    await db_session.refresh(project)
    target = next(
        d for d in (project.directions or []) if d.get("id") == "d-defer-mem"
    )
    assert target["status"] != "active"
    mem = (project.vision or {}).get("_recently_retired_directions") or []
    entry = next((m for m in mem if m.get("id") == "d-defer-mem"), None)
    assert entry is not None, (
        f"retire must record retired memory now the gate is gone; got {mem}"
    )
    assert entry["reason"] == "strategic_retire"


@pytest.mark.asyncio
async def test_update_direction_expired_records_retired_memory(
    client: AsyncClient, db_session, mocker
):
    """API update_direction flipping status->expired records retired memory."""
    from app.auth import create_agent_token

    mocker.patch(
        "app.routers.admin_agent_ops.emit_strategic_updated", new=AsyncMock()
    )
    project, task = await _make_strategic_retire_task(
        db_session,
        name="UpdateExpiredMemory",
        vm="vm-upd-exp",
        direction_id="d-upd-exp",
        gap_id="g-model-gc",
    )
    token = create_agent_token(str(task.id), "vm-upd-exp", task_type="strategic_review")
    resp = await client.post(
        "/api/agent/strategic",
        json={
            "task_id": str(task.id),
            "op": "update_direction",
            "data": {
                "direction_id": "d-upd-exp",
                "rationale": "superseded by refactor lane",
                "terminal_reason": "superseded",
                "changes": {"status": "expired"},
            },
        },
        headers={"x-agent-token": token},
    )
    assert resp.status_code == 200, resp.text
    await db_session.refresh(project)
    mem = (project.vision or {}).get("_recently_retired_directions") or []
    entry = next((m for m in mem if m.get("id") == "d-upd-exp"), None)
    assert entry is not None, f"update->expired must record memory; got {mem}"
    assert entry["reason"] == "strategic_expired"
    assert entry.get("gap_id") == "g-model-gc"
    assert entry.get("outcome") == "superseded by refactor lane"


@pytest.mark.asyncio
async def test_update_direction_superseded_records_retired_memory(
    client: AsyncClient, db_session, mocker
):
    """API update_direction flipping status->superseded records memory with successor."""
    from app.auth import create_agent_token

    mocker.patch(
        "app.routers.admin_agent_ops.emit_strategic_updated", new=AsyncMock()
    )
    project, task = await _make_strategic_retire_task(
        db_session,
        name="UpdateSupersededMemory",
        vm="vm-upd-sup",
        direction_id="d-upd-sup",
    )
    token = create_agent_token(str(task.id), "vm-upd-sup", task_type="strategic_review")
    resp = await client.post(
        "/api/agent/strategic",
        json={
            "task_id": str(task.id),
            "op": "update_direction",
            "data": {
                "direction_id": "d-upd-sup",
                "rationale": "replaced",
                "terminal_reason": "superseded",
                "changes": {
                    "status": "superseded",
                    "superseded_by": {
                        "direction_id": "d-successor",
                        "set_at": "2026-01-01T00:00:00",
                    },
                },
            },
        },
        headers={"x-agent-token": token},
    )
    assert resp.status_code == 200, resp.text
    await db_session.refresh(project)
    mem = (project.vision or {}).get("_recently_retired_directions") or []
    entry = next((m for m in mem if m.get("id") == "d-upd-sup"), None)
    assert entry is not None, f"update->superseded must record memory; got {mem}"
    assert entry["reason"] == "strategic_superseded"
    assert entry.get("outcome") == "superseded by d-successor"


@pytest.mark.asyncio
async def test_update_direction_zero_attempt_terminal_requires_typed_reason(
    client: AsyncClient, db_session, mocker
):
    """The generic update path cannot bypass the zero-attempt terminal guard."""
    from app.auth import create_agent_token

    mocker.patch(
        "app.routers.admin_agent_ops.emit_strategic_updated", new=AsyncMock()
    )
    project, task = await _make_strategic_retire_task(
        db_session,
        name="UpdateTerminalGuard",
        vm="vm-upd-guard",
        direction_id="d-upd-guard",
    )
    token = create_agent_token(
        str(task.id), "vm-upd-guard", task_type="strategic_review"
    )
    resp = await client.post(
        "/api/agent/strategic",
        json={
            "task_id": str(task.id),
            "op": "update_direction",
            "data": {
                "direction_id": "d-upd-guard",
                "rationale": "stop without a typed reason",
                "changes": {"status": "expired"},
            },
        },
        headers={"x-agent-token": token},
    )

    assert resp.status_code == 409, resp.text
    assert (
        resp.json()["error"]["message"]["error"]
        == "zero_attempt_direction_requires_typed_terminal_reason"
    )
    await db_session.refresh(project)
    target = next(d for d in project.directions if d["id"] == "d-upd-guard")
    assert target["status"] == "active"


@pytest.mark.asyncio
async def test_update_direction_non_terminal_records_no_retired_memory(
    client: AsyncClient, db_session, mocker
):
    """API update_direction that does NOT flip to terminal records no memory."""
    from app.auth import create_agent_token

    mocker.patch(
        "app.routers.admin_agent_ops.emit_strategic_updated", new=AsyncMock()
    )
    project, task = await _make_strategic_retire_task(
        db_session,
        name="UpdateNonTerminalMemory",
        vm="vm-upd-live",
        direction_id="d-upd-live",
    )
    token = create_agent_token(
        str(task.id), "vm-upd-live", task_type="strategic_review"
    )
    resp = await client.post(
        "/api/agent/strategic",
        json={
            "task_id": str(task.id),
            "op": "update_direction",
            "data": {
                "direction_id": "d-upd-live",
                "rationale": "raise priority",
                "changes": {"priority": "high"},
            },
        },
        headers={"x-agent-token": token},
    )
    assert resp.status_code == 200, resp.text
    await db_session.refresh(project)
    mem = (project.vision or {}).get("_recently_retired_directions") or []
    assert not any(m.get("id") == "d-upd-live" for m in mem), (
        f"non-terminal update must NOT record memory; got {mem}"
    )


@pytest.mark.asyncio
async def test_create_direction_grounding_verdict_in_op_result(client: AsyncClient, db_session):
    from app.auth import create_agent_token

    project = Project(
        name="Strategic Grounding",
        user_id=TEST_USER_ID,
        vision={
            "purpose": "test",
            "revision": 1,
            "_recently_retired_directions": [
                {
                    "id": "d-retired-1",
                    "text_tokens": ["billing", "webhooks", "stabilize", "retries"],
                    "retired_at": "2026-07-05T00:00:00+00:00",
                    "reason": "strategic_retire",
                }
            ],
        },
        directions=[],
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = Task(
        project_id=project.id,
        status=TaskStatus.IN_PROGRESS,
        vm_instance_id="vm-strategic-grounding",
        payloads=[{"type": "strategic_review", "prompt": "review"}],
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    token = create_agent_token(
        str(task.id), "vm-strategic-grounding", task_type="strategic_review"
    )
    resp = await client.post(
        "/api/agent/strategic",
        headers={"x-agent-token": token},
        json={
            "task_id": str(task.id),
            "op": "create_direction",
            "data": {
                "text": "Stabilize billing webhooks",
                "priority": "high",
                "success_evidence": "Webhook retries are verified",
                "retirement_condition": "No open webhook incidents",
                "dedup_reasoning": {
                    "closest_directions": ["d-retired-1"],
                    "distinction": "This one hardens retries rather than changing scope",
                },
            },
        },
    )

    assert resp.status_code == 200, resp.text
    body = resp.json()
    verdict = body["grounding_verdict"]
    assert verdict["status"] == "validated"
    assert verdict["retired_direction_ids"] == ["d-retired-1"]


@pytest.mark.asyncio
async def test_update_direction_revival_attaches_prior_outcome(client: AsyncClient, db_session):
    from app.auth import create_agent_token
    from app.models import TaskStatus
    from app import strategic_cycle

    project = Project(
        name="Strategic Revival",
        user_id=TEST_USER_ID,
        vision={
            "purpose": "test",
            "revision": 1,
            "_recently_retired_directions": [
                {
                    "id": "d-retired-1",
                    "text_tokens": ["billing", "webhooks", "stabilize", "retries"],
                    "retired_at": "2026-07-05T00:00:00+00:00",
                    "reason": "strategic_retire",
                }
            ],
        },
        problem_model={
            "gaps": [
                {"id": "g-old", "name": "Old gap", "status": "active"},
                {"id": "g-open", "name": "Open gap", "status": "active"},
            ]
        },
        directions=[
            {
                "id": "d-revive",
                "text": "Retired billing webhooks work",
                "status": "expired",
                "priority": "medium",
                "gap_id": "g-old",
                "notes": [],
                "research_history": [],
            }
        ],
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    cycle = await strategic_cycle.create(db_session, project_id=project.id)
    await db_session.commit()

    task = Task(
        project_id=project.id,
        status=TaskStatus.IN_PROGRESS,
        vm_instance_id="vm-update-revival",
        payloads=[{"type": "strategic_review", "prompt": "review", "vision": {"revision": 1}}],
        context={"strategic_cycle_id": cycle.id},
        strategic_cycle_id=cycle.id,
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    token = create_agent_token(str(task.id), "vm-update-revival", task_type="strategic_review")
    resp = await client.post(
        "/api/agent/strategic",
        headers={"x-agent-token": token},
        json={
            "task_id": str(task.id),
            "op": "update_direction",
            "data": {
                "direction_id": "d-revive",
                "dedup_reasoning": {
                    "closest_directions": ["d-retired-1"],
                    "distinction": "This revives the prior work on a new gap",
                },
                "changes": {
                    "status": "active",
                    "gap_id": "g-open",
                    "text": "Revived billing webhook stabilizer",
                },
            },
        },
    )

    assert resp.status_code == 200, resp.text
    body = resp.json()
    verdict = body["grounding_verdict"]
    assert verdict["status"] == "validated"
    assert verdict["retired_direction_ids"] == ["d-retired-1"]

    await db_session.refresh(project)
    revived = next(d for d in project.directions if d.get("id") == "d-revive")
    assert revived["status"] == "active"
    assert revived["gap_id"] == "g-open"
    assert (revived.get("context") or {}).get("grounding_verdict") == verdict


@pytest.mark.asyncio
async def test_create_direction_endpoint_surfaces_prior_retired_outcome(
    client: AsyncClient, db_session, caplog
):
    from app import strategic_cycle
    from app.auth import create_agent_token
    from app.models import KnowledgeDocument, SourceKind

    retired_memory = [
        {
            "id": "d-landed",
            "text_tokens": ["gpu", "runner", "lane", "verification"],
            "retired_at": _utcnow().isoformat(),
            "reason": "success_evidence_met",
            "outcome": {
                "classification": "landed",
                "metrics": {"prs_merged": 2, "cycles": 3},
            },
        }
    ]
    project = Project(
        name="Retired Grounding",
        user_id=TEST_USER_ID,
        vision={
            "purpose": "test",
            "revision": 1,
            "_recently_retired_directions": retired_memory,
        },
        directions=[],
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    cycle = await strategic_cycle.create(db_session, project_id=project.id)

    source_task = Task(
        project_id=project.id,
        payloads=[{"type": "memo", "prompt": "retirement memo"}],
        context={},
    )
    db_session.add(source_task)
    await db_session.commit()
    await db_session.refresh(source_task)

    memo = KnowledgeDocument(
        project_id=project.id,
        source_task_id=source_task.id,
        source_kind=SourceKind.DIRECTION_RETIREMENT.value,
        title="Direction retired: GPU runner lane",
        summary="GPU runner lane shipped and verified on automain.",
        body="Retirement memo body",
        topic="direction-retirement",
        direction_id="d-landed",
        tags=["direction-retirement"],
    )
    db_session.add(memo)
    await db_session.commit()
    await db_session.refresh(memo)

    task = Task(
        project_id=project.id,
        status=TaskStatus.IN_PROGRESS,
        vm_instance_id="vm-retired-grounding",
        payloads=[
            {"type": "strategic_review", "prompt": "review", "vision": {"revision": 1}}
        ],
        context={"strategic_cycle_id": cycle.id},
        strategic_cycle_id=cycle.id,
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    token = create_agent_token(
        str(task.id), "vm-retired-grounding", task_type="strategic_review"
    )
    with caplog.at_level(logging.INFO, logger="app.routers.admin_agent_ops"):
        resp = await client.post(
            "/api/agent/strategic",
            json={
                "task_id": str(task.id),
                "op": "create_direction",
                "data": {
                    "text": "GPU runner lane verification with stable release gates",
                    "priority": "medium",
                    "rationale": "recreate the retired lane with updated constraints",
                    "success_evidence": "Verification is present",
                    "retirement_condition": "No open GPU runner incidents",
                },
            },
            headers={"x-agent-token": token},
        )

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "ok"
    assert body["op"] == "create_direction"
    assert body["direction_id"]
    assert body["retired_grounding"] == {
        "direction_id": "d-landed",
        "classification": "landed",
        "metrics": {"prs_merged": 2, "cycles": 3},
        "reason": "success_evidence_met",
        "retirement_memo": {
            "knowledge_doc_id": str(memo.id),
            "summary": "GPU runner lane shipped and verified on automain.",
        },
    }
    assert any(
        "matches recently-retired" in record.message
        and "prior_outcome=landed" in record.message
        and str(memo.id) in record.message
        for record in caplog.records
    )


@pytest.mark.asyncio
async def test_create_direction_endpoint_without_retired_match_has_unchanged_shape(
    client: AsyncClient, db_session
):
    from app.auth import create_agent_token
    from app import strategic_cycle

    project = Project(
        name="No Retired Grounding",
        user_id=TEST_USER_ID,
        vision={"purpose": "test", "revision": 1},
        directions=[],
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    cycle = await strategic_cycle.create(db_session, project_id=project.id)
    task = Task(
        project_id=project.id,
        status=TaskStatus.IN_PROGRESS,
        vm_instance_id="vm-no-retired-grounding",
        payloads=[
            {"type": "strategic_review", "prompt": "review", "vision": {"revision": 1}}
        ],
        context={"strategic_cycle_id": cycle.id},
        strategic_cycle_id=cycle.id,
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    token = create_agent_token(
        str(task.id), "vm-no-retired-grounding", task_type="strategic_review"
    )
    resp = await client.post(
        "/api/agent/strategic",
        json={
            "task_id": str(task.id),
            "op": "create_direction",
            "data": {
                "text": "Launch a new telemetry lane",
                "priority": "high",
                "rationale": "expand instrumentation coverage",
                "success_evidence": "Telemetry is visible",
                "retirement_condition": "Telemetry remains stable",
            },
        },
        headers={"x-agent-token": token},
    )

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert "retired_grounding" not in body
    assert set(body) == {"status", "op", "direction_id", "grounding_verdict"}


@pytest.mark.asyncio
async def test_strategic_create_message_rejects_oversized_context(
    client: AsyncClient, db_session
):
    from app.auth import create_agent_token

    project = Project(name="Strategic Message Context", user_id=TEST_USER_ID)
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = Task(
        project_id=project.id,
        status=TaskStatus.IN_PROGRESS,
        vm_instance_id="vm-strategic-message-context",
        payloads=[{"type": "strategic_review", "prompt": "review"}],
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    token = create_agent_token(
        str(task.id),
        "vm-strategic-message-context",
        task_type="strategic_review",
    )
    resp = await client.post(
        "/api/agent/strategic",
        headers={"x-agent-token": token},
        json={
            "task_id": str(task.id),
            "op": "create_message",
            "data": {
                "type": "NOTE",
                "content": "Context should be bounded.",
                "context": {"blob": "z" * 60_000},
            },
        },
    )

    assert resp.status_code == 400, resp.text
    assert "context too large" in resp.json()["error"]["message"].lower()
    rows = (
        await db_session.execute(
            select(ProjectMessage).where(ProjectMessage.project_id == project.id)
        )
    ).scalars().all()
    assert rows == []


@pytest.mark.asyncio
async def test_agent_create_message_idempotency_dedupes(
    client: AsyncClient, db_session
):
    """Two agent create_message calls sharing an idempotency_key collapse to one row.

    This exercises the live-incident fix: './messages send' persisted server-side
    but exited nonzero; a retry must not produce a second ProjectMessage row.
    """
    from app.auth import create_agent_token

    project = Project(name="Agent Message Idem", user_id=TEST_USER_ID)
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = Task(
        project_id=project.id,
        status=TaskStatus.IN_PROGRESS,
        vm_instance_id="vm-agent-msg-idem",
        payloads=[{"type": "strategic_review", "prompt": "review"}],
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    token = create_agent_token(
        str(task.id),
        "vm-agent-msg-idem",
        task_type="strategic_review",
    )
    body = {
        "task_id": str(task.id),
        "op": "create_message",
        "data": {
            "type": "NOTE",
            "content": "Found an issue with the build.",
            "idempotency_key": "agent-idem-test-key-abc123",
        },
    }

    resp1 = await client.post(
        "/api/agent/strategic",
        headers={"x-agent-token": token},
        json=body,
    )
    assert resp1.status_code == 200, resp1.text
    first_id = resp1.json()["message_id"]

    # Simulate retry: same idempotency_key, same content
    resp2 = await client.post(
        "/api/agent/strategic",
        headers={"x-agent-token": token},
        json=body,
    )
    assert resp2.status_code == 200, resp2.text
    assert resp2.json()["message_id"] == first_id, "Retry must return the same message_id"

    rows = (
        await db_session.execute(
            select(ProjectMessage).where(ProjectMessage.project_id == project.id)
        )
    ).scalars().all()
    assert len(rows) == 1, f"Expected 1 message row, got {len(rows)}"
    assert rows[0].idempotency_key == "agent-idem-test-key-abc123"

    changed = {
        **body,
        "data": {**body["data"], "content": "Different content with the same key."},
    }
    collision = await client.post(
        "/api/agent/strategic",
        headers={"x-agent-token": token},
        json=changed,
    )
    assert collision.status_code == 409


@pytest.mark.asyncio
async def test_resident_reply_receipt_is_atomic_and_idempotent(
    client: AsyncClient, db_session
):
    """A retry after the receipt committed returns the same reply, read-only."""
    from app.auth import create_agent_token

    project, task = await _make_resident_message_worker(db_session)
    inbound = ProjectMessage(
        project_id=project.id,
        sequence=1,
        surface=MessageSurface.CONVERSATION,
        handling_state=MessageHandlingState.PENDING,
        message_type=MessageType.NOTE,
        from_type="user",
        status="open",
        content="Please account for this.",
        resident_session_id="resident-receipt-session",
    )
    db_session.add(inbound)
    await db_session.commit()
    await db_session.refresh(inbound)
    token = create_agent_token(
        str(task.id), "vm-resident-receipt", task_type="strategic_review"
    )
    body = {
        "task_id": str(task.id),
        "op": "create_message",
        "data": {
            "type": "NOTE",
            "content": "I accounted for it.",
            "idempotency_key": "resident-receipt-idempotent",
            "covered_message_ids": [str(inbound.id)],
            "resident_turn_state": {
                "exam_ledger": '{"v":3}',
                "decisions_reg": "[]",
                "message_cursor": "2026-07-22T12:00:00Z",
                "operator_decision_summary": {"total": 1, "captured": 1},
            },
        },
    }
    headers = {"x-agent-token": token}

    first = await client.post("/api/agent/strategic", headers=headers, json=body)
    assert first.status_code == 200, first.text
    await db_session.refresh(inbound)
    await db_session.refresh(task)
    assert inbound.status == "handled"
    assert task.context["_exam_ledger"] == '{"v":3}'
    reply = await db_session.get(
        ProjectMessage, uuid.UUID(first.json()["message_id"])
    )
    assert reply is not None
    assert reply.resident_session_id == "resident-receipt-session"
    assert reply.context["_resident_reply_covered_message_ids"] == [str(inbound.id)]

    replay = await client.post("/api/agent/strategic", headers=headers, json=body)
    assert replay.status_code == 200, replay.text
    assert replay.json()["message_id"] == first.json()["message_id"]
    rows = (
        await db_session.execute(
            select(ProjectMessage).where(ProjectMessage.project_id == project.id)
        )
    ).scalars().all()
    assert len(rows) == 2


@pytest.mark.asyncio
async def test_resident_direct_message_resolution_is_rejected(
    client: AsyncClient, db_session
):
    """Residents cannot create a receipt that races their atomic reply."""
    from app.auth import create_agent_token

    project, task = await _make_resident_message_worker(db_session)
    inbound = ProjectMessage(
        project_id=project.id,
        sequence=1,
        surface=MessageSurface.CONVERSATION,
        handling_state=MessageHandlingState.PENDING,
        message_type=MessageType.NOTE,
        from_type="user",
        status="open",
        content="Please reply without closing this first.",
        resident_session_id="resident-receipt-session",
    )
    db_session.add(inbound)
    await db_session.commit()
    token = create_agent_token(
        str(task.id), "vm-resident-receipt", task_type="strategic_review"
    )

    response = await client.post(
        "/api/agent/strategic",
        headers={"x-agent-token": token},
        json={
            "task_id": str(task.id),
            "op": "resolve_message",
            "data": {"message_id": str(inbound.id), "resolution": "Addressed"},
        },
    )

    assert response.status_code == 409, response.text
    assert "create_message" in response.text
    await db_session.refresh(inbound)
    assert inbound.status == "open"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "vision_update",
    [
        {"session_id": "different-session"},
        {"worker_generation": 2},
    ],
)
async def test_resident_message_write_rejects_wrong_session_or_generation(
    client: AsyncClient, db_session, vision_update
):
    from app.auth import create_agent_token

    project, task = await _make_resident_message_worker(db_session)
    resident = dict(project.vision["_resident_strategic_session"])
    resident.update(vision_update)
    project.vision = {"_resident_strategic_session": resident}
    db_session.add(project)
    await db_session.commit()
    token = create_agent_token(
        str(task.id), "vm-resident-receipt", task_type="strategic_review"
    )

    response = await client.post(
        "/api/agent/strategic",
        headers={"x-agent-token": token},
        json={
            "task_id": str(task.id),
            "op": "create_message",
            "data": {
                "type": "NOTE",
                "content": "This stale worker must not write.",
                "idempotency_key": "resident-stale-write",
            },
        },
    )
    assert response.status_code == 409
    assert "superseded" in response.text
    rows = (
        await db_session.execute(
            select(ProjectMessage).where(ProjectMessage.project_id == project.id)
        )
    ).scalars().all()
    assert rows == []


@pytest.mark.asyncio
async def test_agent_create_message_separates_routine_activity_and_promotes_reply(
    client: AsyncClient, db_session
):
    from app.auth import create_agent_token

    project = Project(name="Agent Message Projection", user_id=TEST_USER_ID)
    db_session.add(project)
    await db_session.commit()
    task = Task(
        project_id=project.id,
        status=TaskStatus.IN_PROGRESS,
        vm_instance_id="vm-agent-msg-projection",
        payloads=[{"type": "strategic_review", "prompt": "review"}],
    )
    question = ProjectMessage(
        project_id=project.id,
        sequence=1,
        surface=MessageSurface.CONVERSATION,
        message_type=MessageType.QUESTION,
        from_type="user",
        content="What did you decide?",
    )
    db_session.add_all([task, question])
    await db_session.commit()
    token = create_agent_token(
        str(task.id),
        "vm-agent-msg-projection",
        task_type="strategic_review",
    )

    routine = await client.post(
        "/api/agent/strategic",
        headers={"x-agent-token": token},
        json={
            "task_id": str(task.id),
            "op": "create_message",
            "data": {
                "type": "NOTE",
                "content": "PR merged.",
                "context": {"event_type": "pr_merged"},
                "idempotency_key": "agent-routine-1",
            },
        },
    )
    assert routine.status_code == 200, routine.text
    routine_row = await db_session.get(
        ProjectMessage, uuid.UUID(routine.json()["message_id"])
    )
    assert routine_row.surface == MessageSurface.ACTIVITY
    assert isinstance(routine_row.sequence, int)
    assert routine_row.sequence > 0

    reply = await client.post(
        "/api/agent/strategic",
        headers={"x-agent-token": token},
        json={
            "task_id": str(task.id),
            "op": "create_message",
            "data": {
                "type": "NOTE",
                "content": "I chose the safer rollout.",
                "context": {"reply_to": str(question.id)},
                "idempotency_key": "agent-reply-1",
            },
        },
    )
    assert reply.status_code == 200, reply.text
    reply_row = await db_session.get(ProjectMessage, uuid.UUID(reply.json()["message_id"]))
    assert reply_row.surface == MessageSurface.CONVERSATION
    assert reply_row.reply_to_message_id == question.id
    await db_session.refresh(question)
    assert question.handling_state == MessageHandlingState.RESPONDED
    assert question.handled_by_task_id == task.id

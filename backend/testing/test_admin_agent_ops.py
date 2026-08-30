"""
Tests for admin_agent_ops.py — the strategist-facing /api/agent/strategic ops.

Focused on strategist spawn contracts: dependency base-branch gating and the
canonical direction scope carried by direct research tasks.
"""

import pytest
from httpx import AsyncClient
from unittest.mock import AsyncMock

from app.auth import create_agent_token
from app.models import Project, ProjectMember, Task, TaskStatus

TEST_USER_ID = "u-admin-agent-ops"


async def _make_project(session, owner_id=TEST_USER_ID, name="Test Project"):
    """Create a project with the given user as an owner member."""
    project = Project(name=name, user_id=owner_id)
    session.add(project)
    await session.commit()
    await session.refresh(project)
    member = ProjectMember(project_id=project.id, user_id=owner_id, role="owner")
    session.add(member)
    await session.commit()
    return project


@pytest.mark.asyncio
async def test_create_direction_accepts_cost_budget_usd(
    client: AsyncClient, db_session, monkeypatch
):
    """The authenticated strategist path persists, updates, and clears budgets."""
    project = await _make_project(db_session, name="Strategic direction budget")
    task = Task(
        project_id=project.id,
        status=TaskStatus.IN_PROGRESS,
        vm_instance_id="agent-vm-direction-budget",
        payloads=[{"type": "strategic_review", "prompt": "review"}],
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)
    token = create_agent_token(
        str(task.id), "agent-vm-direction-budget", task_type="strategic_review"
    )
    monkeypatch.setattr(
        "app.routers.admin_agent_ops.emit_strategic_updated", AsyncMock()
    )
    headers = {"x-agent-token": token}

    created = await client.post(
        "/api/agent/strategic",
        headers=headers,
        json={
            "task_id": str(task.id),
            "op": "create_direction",
            "data": {
                "text": "Ship a bounded campaign",
                "success_evidence": "The campaign completes within its cost envelope",
                "retirement_condition": "The campaign lands or the hypothesis is invalidated",
                "cost_budget_usd": 0,
            },
        },
    )
    assert created.status_code == 200, created.text
    direction_id = created.json()["direction_id"]
    await db_session.refresh(project)
    direction = next(d for d in project.directions if d["id"] == direction_id)
    assert direction["cost_budget_usd"] == 0.0

    updated = await client.post(
        "/api/agent/strategic",
        headers=headers,
        json={
            "task_id": str(task.id),
            "op": "update_direction",
            "data": {"direction_id": direction_id, "cost_budget_usd": 12.5},
        },
    )
    assert updated.status_code == 200, updated.text
    await db_session.refresh(project)
    direction = next(d for d in project.directions if d["id"] == direction_id)
    assert direction["cost_budget_usd"] == 12.5

    cleared = await client.post(
        "/api/agent/strategic",
        headers=headers,
        json={
            "task_id": str(task.id),
            "op": "update_direction",
            "data": {
                "direction_id": direction_id,
                "changes": {"cost_budget_usd": None},
            },
        },
    )
    assert cleared.status_code == 200, cleared.text
    await db_session.refresh(project)
    direction = next(d for d in project.directions if d["id"] == direction_id)
    assert "cost_budget_usd" not in direction


@pytest.mark.asyncio
async def test_direction_owned_research_spawn_carries_canonical_prompt_scope(
    client: AsyncClient, db_session, monkeypatch
):
    """A direct Strategic research spawn must not reach the agent as broad,
    direction-free or decision-free research merely because ownership was
    stored only in task context. The stored payload and claim response carry
    the exact bounded-cycle envelope rendered by ``handleResearch``."""
    import uuid

    from app import shared_state
    from app.discovery_admission import direction_authority_snapshot
    from app.max_plan_pool import resolve_task_routing
    from app.models import DiscoveryCycle
    from app.research_completion import effective_research_finding_headroom

    # Prove that old per-type pins cannot downgrade/bypass the canonical
    # research route, while creation-time prompt/capability context is carried.
    monkeypatch.setitem(
        shared_state.runtime_config.setdefault("task_type_efforts", {}),
        "research",
        "low",
    )
    monkeypatch.setitem(
        shared_state.runtime_config.setdefault("task_type_models", {}),
        "research",
        "legacy-research-model",
    )
    monkeypatch.setitem(
        shared_state.runtime_config.setdefault("task_type_routing", {}),
        "research",
        {"providers": ["openai"], "effort": "max"},
    )
    configured_suffix = "Use the configured direct-research evidence discipline."
    monkeypatch.setitem(
        shared_state.runtime_config.setdefault("agent_prompt_suffixes", {}),
        "research",
        configured_suffix,
    )

    project = await _make_project(db_session, name="Direct research scope")
    owned_direction = {
        "id": "d-direct-research",
        "text": "Keep direct Strategic research scoped to scheduler reliability",
        "status": "active",
        "priority": "high",
        "category": "reliability",
        "system_id": "scheduler",
        "completion_criteria": "The scheduler diagnosis is decision-ready.",
        "research_context": "Inspect scheduler admission and dispatch only.",
        "paths": [
            "backend/app/orchestrator.py",
            "backend/app/max_plan_pool.py",
        ],
        "context": {
            "success_evidence": "A bounded scheduler diagnosis is recorded.",
            "retirement_condition": "The scoped scheduler risk is resolved.",
            "implementation_constraints": "Do not broaden into unrelated queues.",
            "untrusted_extra": "must not enter the authority snapshot",
        },
        "notes": [{"text": "mutable history must not inflate the task payload"}],
        "research_history": [{"task_id": "old-task"}],
    }
    unrelated_direction = {
        "id": "d-unrelated",
        "text": "Unrelated product exploration",
        "status": "active",
        "priority": "low",
    }
    project.directions = [owned_direction, unrelated_direction]
    project.problem_model = {
        "capabilities": [
            {"name": "Scheduler Diagnosis", "status": "working"},
            {"name": "Queue Replay", "status": "planned"},
        ]
    }
    project.capability_proof_records = {
        "scheduler-diagnosis": [
            {"proof_kind": "named_test", "commit_sha": "abc123"}
        ]
    }
    db_session.add(project)

    strategic = Task(
        project_id=project.id,
        title="Strategic review for scoped research",
        status=TaskStatus.IN_PROGRESS,
        payloads=[{"type": "strategic_review", "prompt": "review"}],
        vm_instance_id="agent-vm-direct-research-scope",
    )
    db_session.add(strategic)
    await db_session.commit()
    await db_session.refresh(strategic)
    token = create_agent_token(
        str(strategic.id),
        "agent-vm-direct-research-scope",
        task_type="strategic_review",
    )
    commissioned_question = (
        "Which scheduler admission race causes childless research parents?"
    )

    response = await client.post(
        "/api/agent/strategic",
        headers={"x-agent-token": token},
        json={
            "task_id": str(strategic.id),
            "op": "spawn_task",
            "data": {
                "type": "research",
                "title": "Investigate scheduler reliability",
                "prompt": commissioned_question,
                "direction_id": owned_direction["id"],
            },
        },
    )

    assert response.status_code == 200, response.text
    spawned = await db_session.get(Task, uuid.UUID(response.json()["task_id"]))
    assert spawned is not None
    research_payload = spawned.first_payload or {}
    assert research_payload["current_direction_id"] == owned_direction["id"]
    assert research_payload["directions"] == [
        direction_authority_snapshot(owned_direction)
    ]
    assert research_payload["directions"][0]["id"] != unrelated_direction["id"]
    assert research_payload["directions"][0]["paths"] == owned_direction["paths"]
    assert "notes" not in research_payload["directions"][0]
    assert "research_history" not in research_payload["directions"][0]

    assert spawned.discovery_cycle_id is not None
    cycle = await db_session.get(DiscoveryCycle, spawned.discovery_cycle_id)
    assert cycle is not None
    assert cycle.bet_snapshot["claim"] == commissioned_question
    assert cycle.bet_snapshot["decision_question"] == commissioned_question
    assert research_payload["discovery_generation"] == cycle.generation
    assert research_payload["discovery_phase"] == cycle.phase
    assert research_payload["discovery_intent_snapshot"] == cycle.intent_snapshot
    assert research_payload["discovery_bet_snapshot"] == cycle.bet_snapshot
    assert research_payload["work_headroom"] == effective_research_finding_headroom(0)
    assert research_payload["prompt_suffix"] == configured_suffix
    assert research_payload["available_capabilities"] == [
        {
            "name": "scheduler-diagnosis",
            "status": "working",
            "proof_status": "proven",
        },
        {
            "name": "queue-replay",
            "status": "planned",
            "proof_status": "unproven",
        },
    ]

    # These legacy config maps are not authoritative routing inputs. An
    # explicit payload pin would outrank task_type_routing and silently reduce
    # this research parent to low effort.
    assert "model" not in research_payload
    assert "effort" not in research_payload
    assert resolve_task_routing(spawned, shared_state.runtime_config) == {
        "providers": ["openai"],
        "effort": "max",
    }

    # Claim is the final boundary consumed by the TypeScript renderer. It adds
    # bounded-cycle mode but must preserve the same frozen generation, phase,
    # intent, bet, headroom, direction scope, and creation-time suffix.
    claim_token = create_agent_token(
        str(spawned.id),
        spawned.vm_instance_id,
        task_type="research",
    )
    claim = await client.post(
        "/api/agent/claim",
        headers={
            "x-agent-token": claim_token,
            "x-research-output-contract-version": "2",
        },
    )
    assert claim.status_code == 200, claim.text
    claimed_payload = claim.json()["payloads"][0]
    assert claimed_payload["discovery_mode"] == "bounded_cycle"
    assert claimed_payload["discovery_cycle_id"] == str(cycle.id)
    for field in (
        "directions",
        "current_direction_id",
        "discovery_generation",
        "discovery_phase",
        "discovery_intent_snapshot",
        "discovery_bet_snapshot",
        "work_headroom",
        "available_capabilities",
        "prompt_suffix",
    ):
        assert claimed_payload[field] == research_payload[field]

    # The cycle is now occupied by ``spawned``. An exact lost-response replay
    # must return that source rather than either 409ing or creating a second
    # parent on the same cycle.
    replay = await client.post(
        "/api/agent/strategic",
        headers={"x-agent-token": token},
        json={
            "task_id": str(strategic.id),
            "op": "spawn_task",
            "data": {
                "type": "research",
                "title": "Investigate scheduler reliability",
                "prompt": commissioned_question,
                "direction_id": owned_direction["id"],
            },
        },
    )
    assert replay.status_code == 200, replay.text
    assert replay.json()["task_id"] == str(spawned.id)


@pytest.mark.asyncio
async def test_direction_free_research_spawn_keeps_root_capability_and_suffix_parity(
    client: AsyncClient, db_session, monkeypatch
):
    """Capability and configured-prompt context belong to the research root,
    not to direction ownership; a direction-free direct root must retain them
    without fabricating a bounded-cycle envelope."""
    import uuid

    from app import shared_state
    from app.research_completion import effective_research_finding_headroom

    configured_suffix = "Apply the configured direction-free research discipline."
    monkeypatch.setitem(
        shared_state.runtime_config.setdefault("agent_prompt_suffixes", {}),
        "research",
        configured_suffix,
    )

    project = await _make_project(db_session, name="Direction-free research parity")
    project.problem_model = {
        "capabilities": [{"name": "Repository Mapping", "status": "working"}]
    }
    project.capability_proof_records = {
        "repository-mapping": [
            {"proof_kind": "named_test", "commit_sha": "def456"}
        ]
    }
    db_session.add(project)

    strategic = Task(
        project_id=project.id,
        title="Strategic review for direction-free research",
        status=TaskStatus.IN_PROGRESS,
        payloads=[{"type": "strategic_review", "prompt": "review"}],
        vm_instance_id="agent-vm-direction-free-research",
    )
    db_session.add(strategic)
    await db_session.commit()
    await db_session.refresh(strategic)
    token = create_agent_token(
        str(strategic.id),
        "agent-vm-direction-free-research",
        task_type="strategic_review",
    )

    response = await client.post(
        "/api/agent/strategic",
        headers={"x-agent-token": token},
        json={
            "task_id": str(strategic.id),
            "op": "spawn_task",
            "data": {
                "type": "research",
                "title": "Map repository reliability risks",
                "prompt": "Map decision-relevant reliability risks.",
            },
        },
    )

    assert response.status_code == 200, response.text
    spawned = await db_session.get(Task, uuid.UUID(response.json()["task_id"]))
    assert spawned is not None
    research_payload = spawned.first_payload or {}
    assert research_payload["available_capabilities"] == [
        {
            "name": "repository-mapping",
            "status": "working",
            "proof_status": "proven",
        }
    ]
    assert research_payload["prompt_suffix"] == configured_suffix
    assert research_payload["work_headroom"] == effective_research_finding_headroom(0)
    for cycle_owned_field in (
        "directions",
        "current_direction_id",
        "discovery_generation",
        "discovery_phase",
        "discovery_intent_snapshot",
        "discovery_bet_snapshot",
    ):
        assert cycle_owned_field not in research_payload


def test_research_envelope_preserves_explicit_valid_root_fields():
    from app.direct_research_spawn import enrich_direct_research_payload

    explicit_capabilities = [{"name": "commission-specific", "status": "working"}]
    payload = {
        "type": "research",
        "work_headroom": 0,
        "available_capabilities": explicit_capabilities,
        "prompt_suffix": "Keep this commission-specific suffix.",
    }
    enrich_direct_research_payload(
        payload,
        project=Project(
            name="Explicit research envelope",
            user_id=TEST_USER_ID,
            problem_model={
                "capabilities": [{"name": "Registry Capability", "status": "working"}]
            },
        ),
        direction_entry=None,
        direction_id=None,
        direct_cycle=None,
        runtime_config={
            "agent_prompt_suffixes": {"research": "Configured default suffix."}
        },
    )

    assert payload["work_headroom"] == 0
    assert payload["available_capabilities"] is explicit_capabilities
    assert payload["prompt_suffix"] == "Keep this commission-specific suffix."


@pytest.mark.asyncio
async def test_direction_owned_research_rejects_a_distinct_parent_on_occupied_cycle(
    client: AsyncClient, db_session, monkeypatch
):
    """An open cycle's existing source owns its carry and finding budget.
    Direct Strategic work must not join it as a second full-headroom parent."""
    from app.discovery_admission import ensure_discovery_cycle_for_research
    from sqlmodel import select

    # Make the occupied source look like the new caller's requested work at the
    # task-key/hash layer. The missing parent-bound request receipt must still
    # prevent this distinct Strategic parent from being treated as a replay.
    monkeypatch.setattr(
        "app.direct_research_spawn.stable_work_specification_hash",
        lambda _specification: "matching-same-project-work-hash",
    )
    monkeypatch.setattr(
        "app.direct_research_spawn.strategic_work_idempotency_key",
        lambda **_kwargs: "matching-same-project-active-key",
    )

    project = await _make_project(db_session, name="Occupied research cycle")
    owned_direction = {
        "id": "d-occupied-cycle",
        "text": "Keep the occupied cycle single-parented",
        "status": "active",
        "priority": "high",
        "completion_criteria": "Only one research source owns the cycle.",
    }
    project.directions = [owned_direction]
    db_session.add(project)
    cycle = await ensure_discovery_cycle_for_research(
        db_session,
        project=project,
        direction_key=owned_direction["id"],
        direction=owned_direction,
    )
    assert cycle is not None
    existing_source = Task(
        project_id=project.id,
        title="Existing autonomous research source",
        status=TaskStatus.IN_PROGRESS,
        payloads=[
            {
                "type": "research",
                "title": "Existing autonomous research source",
                "prompt": "Own the currently open discovery cycle.",
                "direction_id": owned_direction["id"],
            }
        ],
        context={
            "direction_id": owned_direction["id"],
            "_strategic_work_specification_hash": (
                "matching-same-project-work-hash"
            ),
        },
        discovery_cycle_id=cycle.id,
        idempotency_key="matching-same-project-active-key",
        vm_instance_id="agent-vm-existing-cycle-source",
        created_by="ORCHESTRATOR",
    )
    db_session.add(existing_source)
    await db_session.flush()
    cycle.source_research_task_id = existing_source.id
    db_session.add(cycle)

    strategic = Task(
        project_id=project.id,
        title="Strategic review attempting a second source",
        status=TaskStatus.IN_PROGRESS,
        payloads=[{"type": "strategic_review", "prompt": "review"}],
        vm_instance_id="agent-vm-occupied-cycle-strategic",
    )
    db_session.add(strategic)
    await db_session.commit()
    token = create_agent_token(
        str(strategic.id),
        "agent-vm-occupied-cycle-strategic",
        task_type="strategic_review",
    )

    response = await client.post(
        "/api/agent/strategic",
        headers={"x-agent-token": token},
        json={
            "task_id": str(strategic.id),
            "op": "spawn_task",
            "data": {
                "type": "research",
                "title": "Distinct direct research parent",
                "prompt": "Attempt to join the occupied cycle with fresh work.",
                "direction_id": owned_direction["id"],
                "idempotency_key": "distinct-direct-parent",
            },
        },
    )

    assert response.status_code == 409, response.text
    assert (
        "refusing to attach a second parent"
        in response.json()["error"]["message"]
    )
    research_tasks = (
        await db_session.execute(
            select(Task).where(Task.project_id == project.id)
        )
    ).scalars().all()
    research_tasks = [
        candidate
        for candidate in research_tasks
        if candidate.first_payload.get("type") == "research"
    ]
    assert [candidate.id for candidate in research_tasks] == [existing_source.id]


@pytest.mark.asyncio
async def test_direction_owned_research_rejects_cross_project_cycle_source(
    client: AsyncClient, db_session, monkeypatch
):
    """A corrupt foreign source edge cannot authorize a second local parent.

    The foreign task deliberately carries the exact occupancy key/hash that
    would otherwise make it look like an active lost-response replay. Project
    ownership remains the first trust boundary.
    """
    from app.discovery_admission import ensure_discovery_cycle_for_research
    from sqlmodel import select

    monkeypatch.setattr(
        "app.direct_research_spawn.stable_work_specification_hash",
        lambda _specification: "matching-cross-project-work-hash",
    )
    monkeypatch.setattr(
        "app.direct_research_spawn.strategic_work_idempotency_key",
        lambda **_kwargs: "matching-cross-project-active-key",
    )

    project = await _make_project(db_session, name="Local research cycle")
    foreign_project = await _make_project(
        db_session, name="Foreign research source"
    )
    owned_direction = {
        "id": "d-cross-project-cycle-source",
        "text": "Keep discovery-cycle sources project-local",
        "status": "active",
        "priority": "high",
        "completion_criteria": "Foreign source edges fail closed.",
    }
    project.directions = [owned_direction]
    db_session.add(project)
    cycle = await ensure_discovery_cycle_for_research(
        db_session,
        project=project,
        direction_key=owned_direction["id"],
        direction=owned_direction,
    )
    assert cycle is not None

    foreign_source = Task(
        project_id=foreign_project.id,
        title="Foreign task impersonating an exact replay",
        status=TaskStatus.IN_PROGRESS,
        payloads=[
            {
                "type": "research",
                "title": "Foreign task impersonating an exact replay",
                "prompt": "Must not occupy another project's cycle.",
            }
        ],
        context={
            "_strategic_work_specification_hash": (
                "matching-cross-project-work-hash"
            )
        },
        idempotency_key="matching-cross-project-active-key",
        vm_instance_id="agent-vm-foreign-cycle-source",
        created_by="ORCHESTRATOR",
    )
    db_session.add(foreign_source)
    await db_session.flush()
    cycle.source_research_task_id = foreign_source.id
    db_session.add(cycle)

    strategic = Task(
        project_id=project.id,
        title="Strategic review facing a corrupt foreign source edge",
        status=TaskStatus.IN_PROGRESS,
        payloads=[{"type": "strategic_review", "prompt": "review"}],
        vm_instance_id="agent-vm-cross-project-cycle-strategic",
    )
    db_session.add(strategic)
    await db_session.commit()
    token = create_agent_token(
        str(strategic.id),
        "agent-vm-cross-project-cycle-strategic",
        task_type="strategic_review",
    )

    response = await client.post(
        "/api/agent/strategic",
        headers={"x-agent-token": token},
        json={
            "task_id": str(strategic.id),
            "op": "spawn_task",
            "data": {
                "type": "research",
                "title": "Local direct research parent",
                "prompt": "Do not trust the foreign source replay envelope.",
                "direction_id": owned_direction["id"],
                "idempotency_key": "matching-request",
            },
        },
    )

    assert response.status_code == 409, response.text
    assert (
        "refusing to attach a second parent"
        in response.json()["error"]["message"]
    )
    local_research_tasks = (
        await db_session.execute(
            select(Task).where(Task.project_id == project.id)
        )
    ).scalars().all()
    assert [
        candidate.id
        for candidate in local_research_tasks
        if candidate.first_payload.get("type") == "research"
    ] == []


class TestStrategistSpawnExactRoute:
    async def _make_strategic_task(self, db_session, project):
        task = Task(
            project_id=project.id,
            title="Strategic exact-route author",
            status=TaskStatus.IN_PROGRESS,
            payloads=[{"type": "strategic_review", "prompt": "review"}],
            vm_instance_id="agent-vm-exact-route",
        )
        db_session.add(task)
        await db_session.commit()
        await db_session.refresh(task)
        return task, create_agent_token(
            str(task.id),
            "agent-vm-exact-route",
            task_type="strategic_review",
        )

    @pytest.mark.asyncio
    async def test_exact_model_effort_pair_persists_complete_route_authority(
        self, client: AsyncClient, db_session
    ):
        import uuid

        from app.max_plan_pool import EXACT_MODEL_ROUTE_CONTEXT_KEY

        project = await _make_project(db_session, name="Exact route spawn")
        strategic, token = await self._make_strategic_task(db_session, project)
        response = await client.post(
            "/api/agent/strategic",
            headers={"x-agent-token": token},
            json={
                "task_id": str(strategic.id),
                "op": "spawn_task",
                "data": {
                    "type": "create",
                    "title": "Pinned frontier implementation",
                    "prompt": "Execute only with the requested runtime.",
                    "model": "gpt-5.6-sol",
                    "effort": "high",
                },
            },
        )
        assert response.status_code == 200, response.text
        body = response.json()
        child = await db_session.get(Task, uuid.UUID(body["task_id"]))
        assert child is not None
        assert child.first_payload["model"] == "gpt-5.6-sol"
        assert child.first_payload["effort"] == "high"
        assert child.first_payload["provider_preference"] == ["openai"]
        assert child.context[EXACT_MODEL_ROUTE_CONTEXT_KEY] == {
            "provider": "openai",
            "model": "gpt-5.6-sol",
            "effort": "high",
        }
        assert body["requested_model"] == "gpt-5.6-sol"
        assert body["requested_effort"] == "high"
        assert body["requested_provider"] == "openai"

    @pytest.mark.parametrize("effort", [[], {}])
    @pytest.mark.asyncio
    async def test_malformed_effort_returns_400_not_type_error(
        self, client: AsyncClient, db_session, effort
    ):
        project = await _make_project(db_session, name="Malformed effort spawn")
        strategic, token = await self._make_strategic_task(db_session, project)
        response = await client.post(
            "/api/agent/strategic",
            headers={"x-agent-token": token},
            json={
                "task_id": str(strategic.id),
                "op": "spawn_task",
                "data": {
                    "type": "create",
                    "title": "Reject malformed effort",
                    "prompt": "Fail closed.",
                    "model": "gpt-5.6-sol",
                    "effort": effort,
                },
            },
        )
        assert response.status_code == 400, response.text
        assert "effort must be one of" in response.text


class TestStrategistSpawnDependsOnBaseBranch:
    """spawn_task depends_on edges must reject cross-base-branch dependencies."""

    async def _make_strategic_task(self, db_session, project, vm="agent-vm-dep-base"):
        task = Task(
            project_id=project.id,
            title="Strategic review for dependency base gating",
            status=TaskStatus.IN_PROGRESS,
            payloads=[{"type": "strategic_review", "prompt": "review"}],
            vm_instance_id=vm,
        )
        db_session.add(task)
        await db_session.commit()
        await db_session.refresh(task)
        token = create_agent_token(
            str(task.id), vm, task_type="strategic_review"
        )
        return task, token

    @pytest.mark.asyncio
    async def test_spawn_task_rejects_cross_base_depends_on(
        self, client: AsyncClient, db_session
    ):
        """A strategist depends_on edge across base branches is rejected with a
        clear error naming the dependency and both base branches — the same
        guard the operator-facing depends_on path enforces (a cross-base edge is
        silently skipped by the dispatch loop's _same_base_branch wait)."""
        from app.shared_state import get_project_integration_branch

        project = await _make_project(db_session)
        strategic, token = await self._make_strategic_task(db_session, project)

        dep = Task(
            project_id=project.id,
            title="Cross-base prerequisite",
            status=TaskStatus.QUEUED,
            payloads=[
                {"type": "create", "prompt": "x", "base_branch": "plan/other"}
            ],
        )
        db_session.add(dep)
        await db_session.commit()
        await db_session.refresh(dep)

        resp = await client.post(
            "/api/agent/strategic",
            headers={"x-agent-token": token},
            json={
                "task_id": str(strategic.id),
                "op": "spawn_task",
                "data": {
                    "type": "create",
                    "title": "Must not spawn",
                    "prompt": "Cross-base depends_on must be rejected.",
                    "depends_on": [str(dep.id)],
                },
            },
        )
        assert resp.status_code == 400, (
            f"cross-base depends_on must be rejected; got "
            f"{resp.status_code}: {resp.text}"
        )
        body = resp.json()
        msg = body.get("detail") or body.get("error", {}).get("message", "")
        assert "Cross-base dependency" in msg, f"error must name the guard; got {msg!r}"
        assert str(dep.id) in msg, f"error must name the dependency; got {msg!r}"
        assert "plan/other" in msg, f"error must name the dep base; got {msg!r}"
        integration = get_project_integration_branch(project)
        assert integration in msg, (
            f"error must name the spawn base '{integration}'; got {msg!r}"
        )

    @pytest.mark.asyncio
    async def test_spawn_task_accepts_same_base_depends_on(
        self, client: AsyncClient, db_session
    ):
        """A strategist depends_on edge onto a sibling on the same integration
        base is still accepted — the guard must reject cross-base edges only,
        not every dependency."""
        from app.shared_state import get_project_integration_branch

        project = await _make_project(db_session)
        strategic, token = await self._make_strategic_task(db_session, project)

        integration = get_project_integration_branch(project)
        dep = Task(
            project_id=project.id,
            title="Same-base prerequisite",
            status=TaskStatus.QUEUED,
            payloads=[
                {"type": "create", "prompt": "x", "base_branch": integration}
            ],
        )
        db_session.add(dep)
        await db_session.commit()
        await db_session.refresh(dep)

        resp = await client.post(
            "/api/agent/strategic",
            headers={"x-agent-token": token},
            json={
                "task_id": str(strategic.id),
                "op": "spawn_task",
                "data": {
                    "type": "create",
                    "title": "Spawn on the integration base",
                    "prompt": "Same-base depends_on must be accepted.",
                    "depends_on": [str(dep.id)],
                },
            },
        )
        assert resp.status_code == 200, (
            f"same-base depends_on must be accepted; got "
            f"{resp.status_code}: {resp.text}"
        )
        body = resp.json()
        assert body.get("depends_on") == [str(dep.id)], (
            f"spawn must echo the accepted edge; got {body!r}"
        )


class TestStrategistSpawnSequencedDeclaration:
    """spawn_task sequenced-spawn declaration (require_deps_landed_on /
    release_base_branch): a declared landing gate admits cross-base depends_on
    edges (the deterministic gate governs them instead of the _same_base_branch
    heuristic) and both keys are mirrored verbatim into payloads[0]."""

    async def _make_strategic_task(self, db_session, project, vm="agent-vm-seq-spawn"):
        task = Task(
            project_id=project.id,
            title="Strategic review for sequenced spawn",
            status=TaskStatus.IN_PROGRESS,
            payloads=[{"type": "strategic_review", "prompt": "review"}],
            vm_instance_id=vm,
        )
        db_session.add(task)
        await db_session.commit()
        await db_session.refresh(task)
        token = create_agent_token(
            str(task.id), vm, task_type="strategic_review"
        )
        return task, token

    @pytest.mark.asyncio
    async def test_spawn_task_landing_gate_admits_cross_base_depends_on(
        self, client: AsyncClient, db_session
    ):
        """A cross-base depends_on edge is ACCEPTED when the spawn declares
        require_deps_landed_on — the landing gate holds dispatch until the dep
        has landed on the named branch and release_base_branch re-cuts the base,
        so the edge is neither silently skipped nor dispatched against a stale
        base. Both declared keys must land verbatim in payloads[0]."""
        from app.shared_state import get_project_integration_branch

        project = await _make_project(db_session)
        strategic, token = await self._make_strategic_task(db_session, project)

        dep = Task(
            project_id=project.id,
            title="Cross-base prerequisite (rung A)",
            status=TaskStatus.QUEUED,
            payloads=[
                {"type": "create", "prompt": "x", "base_branch": "plan/rung-a"}
            ],
        )
        db_session.add(dep)
        await db_session.commit()
        await db_session.refresh(dep)

        integration = get_project_integration_branch(project)
        resp = await client.post(
            "/api/agent/strategic",
            headers={"x-agent-token": token},
            json={
                "task_id": str(strategic.id),
                "op": "spawn_task",
                "data": {
                    "type": "create",
                    "title": "Rung B builds on rung A",
                    "prompt": "Sequenced cross-base spawn must be accepted.",
                    "depends_on": [str(dep.id)],
                    "require_deps_landed_on": integration,
                    "release_base_branch": integration,
                },
            },
        )
        assert resp.status_code == 200, (
            f"declared landing gate must admit the cross-base edge; got "
            f"{resp.status_code}: {resp.text}"
        )
        body = resp.json()
        assert body.get("depends_on") == [str(dep.id)], (
            f"spawn must echo the accepted edge; got {body!r}"
        )
        import uuid as _uuid

        spawned = await db_session.get(Task, _uuid.UUID(body["task_id"]))
        assert spawned is not None
        payload = spawned.first_payload or {}
        assert payload.get("require_deps_landed_on") == integration, (
            f"landing gate must be mirrored into payloads[0]; got {payload!r}"
        )
        assert payload.get("release_base_branch") == integration, (
            f"release base must be mirrored into payloads[0]; got {payload!r}"
        )

    @pytest.mark.asyncio
    async def test_spawn_task_rejects_cross_base_depends_on_without_gate(
        self, client: AsyncClient, db_session
    ):
        """Declaring only release_base_branch (no landing gate) does NOT admit a
        cross-base edge: without require_deps_landed_on the dispatch loop still
        applies the heuristic path, so the ungated edge stays rejected."""
        from app.shared_state import get_project_integration_branch

        project = await _make_project(db_session)
        strategic, token = await self._make_strategic_task(db_session, project)

        dep = Task(
            project_id=project.id,
            title="Cross-base prerequisite",
            status=TaskStatus.QUEUED,
            payloads=[
                {"type": "create", "prompt": "x", "base_branch": "plan/other"}
            ],
        )
        db_session.add(dep)
        await db_session.commit()
        await db_session.refresh(dep)

        resp = await client.post(
            "/api/agent/strategic",
            headers={"x-agent-token": token},
            json={
                "task_id": str(strategic.id),
                "op": "spawn_task",
                "data": {
                    "type": "create",
                    "title": "Must not spawn",
                    "prompt": "Ungated cross-base edge stays rejected.",
                    "depends_on": [str(dep.id)],
                    # Only the integration branch is an admissible sequencing
                    # ref for a direct strategist spawn; the resolver must
                    # still reject the ungated cross-base edge on its own.
                    "release_base_branch": get_project_integration_branch(project),
                },
            },
        )
        assert resp.status_code == 400, (
            f"ungated cross-base depends_on must stay rejected; got "
            f"{resp.status_code}: {resp.text}"
        )
        body = resp.json()
        msg = body.get("detail") or body.get("error", {}).get("message", "")
        assert "Cross-base dependency" in msg

    @pytest.mark.asyncio
    async def test_spawn_task_sequenced_keys_must_be_branch_names(
        self, client: AsyncClient, db_session
    ):
        """Blank or non-string sequenced keys are rejected up front, and a
        reserved branch shape is rejected by the payload schema."""
        project = await _make_project(db_session)
        strategic, token = await self._make_strategic_task(db_session, project)

        for bad in ("", "   ", 123):
            resp = await client.post(
                "/api/agent/strategic",
                headers={"x-agent-token": token},
                json={
                    "task_id": str(strategic.id),
                    "op": "spawn_task",
                    "data": {
                        "type": "create",
                        "title": "Must not spawn",
                        "prompt": "Invalid sequenced key.",
                        "require_deps_landed_on": bad,
                    },
                },
            )
            assert resp.status_code == 400, (
                f"require_deps_landed_on={bad!r} must be rejected; got "
                f"{resp.status_code}: {resp.text}"
            )

        resp = await client.post(
            "/api/agent/strategic",
            headers={"x-agent-token": token},
            json={
                "task_id": str(strategic.id),
                "op": "spawn_task",
                "data": {
                    "type": "create",
                    "title": "Must not spawn",
                    "prompt": "Reserved branch component.",
                    "require_deps_landed_on": "feat/..sneaky",
                },
            },
        )
        assert resp.status_code == 400, (
            f"reserved branch shape must be rejected by the payload schema; "
            f"got {resp.status_code}: {resp.text}"
        )

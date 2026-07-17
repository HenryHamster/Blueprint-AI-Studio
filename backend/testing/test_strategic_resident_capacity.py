"""Truthful scheduler-readiness projection for accepted Resident messages."""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import event

from app import max_plan_pool, shared_state
from app import strategic_resident_provider_capacity as provider_capacity
from app import orchestrator_runner_dispatch as runner_dispatch
from app.models import LocalRunner, Project, StrategicResident, Task, TaskStatus
from app.strategic_resident_capacity import waiting_for_required_runner
from app.task_capabilities import STRATEGIC_LOCAL_AGENT_TASK_TYPES
from app.time_utils import _utcnow


@pytest.fixture(autouse=True)
def _capacity_signals(monkeypatch):
    monkeypatch.setattr(shared_state, "monthly_budget_gate_engaged", False)
    monkeypatch.setattr(shared_state, "project_ceiling_deferred_ids", set())
    monkeypatch.setattr(shared_state, "monthly_budget_deferred_project_ids", set())
    monkeypatch.setitem(shared_state.runtime_config, "max_concurrent_vms", 8)
    monkeypatch.setattr(max_plan_pool, "_max_plan_pools", {})


async def _seed(db_session, *, runner_policy=None, provider_policy=None):
    project = Project(
        name=f"Resident capacity {uuid.uuid4()}",
        user_id=f"capacity-user-{uuid.uuid4()}",
    )
    resident = StrategicResident(
        project_id=project.id,
        engine_owner="strategic_resident",
        authority_generation=3,
        runner_policy=runner_policy or {"mode": "cloud"},
        provider_policy=provider_policy or {},
    )
    db_session.add(project)
    db_session.add(resident)
    await db_session.flush()
    return project, resident


@pytest.mark.asyncio
@pytest.mark.parametrize("capacity", [0, 1])
async def test_waits_when_cloud_is_disabled_or_saturated(db_session, capacity):
    project, resident = await _seed(db_session)
    shared_state.runtime_config["max_concurrent_vms"] = capacity
    if capacity:
        db_session.add(
            Task(
                project_id=project.id,
                status=TaskStatus.IN_PROGRESS,
                vm_instance_id="agent-vm-saturated",
                vm_machine_type="e2-standard-4",
                payloads=[{"type": "create"}],
            )
        )
        await db_session.flush()
    assert await waiting_for_required_runner(db_session, resident)


@pytest.mark.asyncio
async def test_waits_for_generation_fenced_authoritative_hold(db_session):
    _project, resident = await _seed(db_session)
    resident.recovery_state = {
        "status": "spend_hold",
        "authority_generation": resident.authority_generation,
    }
    assert await waiting_for_required_runner(db_session, resident)


@pytest.mark.asyncio
async def test_stale_shared_or_superseded_holds_fail_open(db_session):
    project, resident = await _seed(db_session)
    shared_state.monthly_budget_gate_engaged = True
    shared_state.project_ceiling_deferred_ids = {str(project.id)}
    shared_state.monthly_budget_deferred_project_ids = {str(project.id)}
    resident.recovery_state = {
        "status": "spend_hold",
        "authority_generation": resident.authority_generation - 1,
    }
    assert not await waiting_for_required_runner(db_session, resident)


@pytest.mark.asyncio
async def test_waits_for_db_confirmed_rate_limited_configured_provider_pool(
    db_session, monkeypatch
):
    model = "claude-opus-4-8"
    project, resident = await _seed(
        db_session,
        provider_policy={"provider": "anthropic", "model": model},
    )
    max_plan_pool._max_plan_pools[project.user_id] = [
        {
            "id": "resident-rate-limited",
            "user_id": project.user_id,
            "provider": "anthropic",
            "auth_mode": "oauth",
            "oauth_token": "secret-ref",
            "enabled": True,
            "disabled_models": {
                model: {"at": _utcnow(), "kind": "rate_limit", "hits": 1}
            },
        }
    ]

    async def _confirmed(**_kwargs):
        return True

    monkeypatch.setattr(
        provider_capacity, "db_pool_wide_rate_limit_cooldown", _confirmed
    )
    assert await waiting_for_required_runner(db_session, resident)


@pytest.mark.asyncio
async def test_stale_in_memory_rate_limit_without_db_confirmation_fails_open(
    db_session, monkeypatch
):
    _project, resident = await _seed(
        db_session,
        provider_policy={"provider": "anthropic", "model": "resident-model"},
    )
    monkeypatch.setattr(
        provider_capacity, "pool_wide_rate_limit_cooldown", lambda **_kwargs: True
    )

    async def _not_confirmed(**_kwargs):
        return False

    monkeypatch.setattr(
        provider_capacity, "db_pool_wide_rate_limit_cooldown", _not_confirmed
    )
    assert not await waiting_for_required_runner(db_session, resident)


@pytest.mark.asyncio
async def test_forbidden_explicit_provider_route_is_confirmed_waiting(
    db_session, monkeypatch
):
    monkeypatch.setitem(
        shared_state.runtime_config,
        "task_type_routing",
        {"strategic_resident_turn": {"providers": ["openai"]}},
    )
    _project, resident = await _seed(
        db_session,
        provider_policy={"provider": "anthropic", "model": "resident-model"},
    )
    assert await waiting_for_required_runner(db_session, resident)


@pytest.mark.asyncio
async def test_process_local_project_account_cap_is_unknown_and_fails_open(
    db_session, monkeypatch
):
    project, resident = await _seed(
        db_session,
        provider_policy={"provider": "anthropic", "model": "resident-model"},
    )
    max_plan_pool._max_plan_pools[project.user_id] = [
        {
            "id": "project-capped",
            "user_id": project.user_id,
            "provider": "anthropic",
            "enabled": True,
            "oauth_token": "secret-ref",
            "project_max_concurrent_tasks": {str(project.id): 1},
            "project_state": {str(project.id): {"active_tasks": 1}},
        }
    ]

    async def _must_not_confirm(**_kwargs):
        raise AssertionError("empty eligible pool is unknown, not rate-limit proof")

    monkeypatch.setattr(
        provider_capacity, "db_pool_wide_rate_limit_cooldown", _must_not_confirm
    )
    assert not await waiting_for_required_runner(db_session, resident)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("vm_instance_id", "vm_machine_type"),
    [(None, None), ("runner:local", None), ("cloud-run-job", "cloud-run-1vcpu-2gib")],
)
async def test_ambiguous_local_and_cloud_run_rows_do_not_consume_gce_capacity(
    db_session, vm_instance_id, vm_machine_type
):
    project, resident = await _seed(db_session)
    shared_state.runtime_config["max_concurrent_vms"] = 1
    db_session.add(
        Task(
            project_id=project.id,
            status=TaskStatus.IN_PROGRESS,
            vm_instance_id=vm_instance_id,
            vm_machine_type=vm_machine_type,
            payloads=[{"type": "create"}],
        )
    )
    await db_session.flush()
    assert not await waiting_for_required_runner(db_session, resident)


@pytest.mark.asyncio
async def test_compatible_local_fallback_survives_cloud_saturation(db_session):
    project, resident = await _seed(
        db_session,
        runner_policy={"allow_local_fallback": True},
    )
    shared_state.runtime_config["max_concurrent_vms"] = 0
    db_session.add(
        LocalRunner(
            project_id=project.id,
            token_hash="a" * 64,
            status="active",
            last_seen_at=_utcnow(),
            capabilities={
                "task_types": sorted(STRATEGIC_LOCAL_AGENT_TASK_TYPES),
                "supported_agent_task_types": sorted(
                    STRATEGIC_LOCAL_AGENT_TASK_TYPES
                ),
                "agent_available": True,
                "max_concurrent": 1,
            },
        )
    )
    await db_session.flush()
    assert not await waiting_for_required_runner(db_session, resident)


@pytest.mark.asyncio
async def test_local_capacity_counts_persisted_compute_reservations(
    db_session, monkeypatch
):
    project, resident = await _seed(
        db_session, runner_policy={"require_local_runner": True}
    )
    runner = LocalRunner(
        project_id=project.id,
        token_hash="b" * 64,
        status="active",
        last_seen_at=_utcnow(),
        capabilities={
            "task_types": sorted(STRATEGIC_LOCAL_AGENT_TASK_TYPES),
            "supported_agent_task_types": sorted(STRATEGIC_LOCAL_AGENT_TASK_TYPES),
            "agent_available": True,
            "max_concurrent": 1,
        },
    )
    db_session.add(runner)
    await db_session.flush()

    async def _reserved(_session, _runner_ids):
        return {runner.id: ["compute-job"]}

    monkeypatch.setattr(
        runner_dispatch, "_load_runner_compute_job_assignments", _reserved
    )
    assert await waiting_for_required_runner(db_session, resident)


@pytest.mark.asyncio
async def test_healthy_cloud_is_ready_with_one_capacity_query(db_session):
    _project, resident = await _seed(db_session)
    statements = 0
    bind = db_session.sync_session.bind

    def _count_statement(*_args):
        nonlocal statements
        statements += 1

    event.listen(bind, "before_cursor_execute", _count_statement)
    try:
        assert not await waiting_for_required_runner(db_session, resident)
    finally:
        event.remove(bind, "before_cursor_execute", _count_statement)
    assert statements == 1

"""Focused fences for local-only bounded Resident execution."""

import inspect
from datetime import timedelta
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException

from app.models import LocalRunner, Project, StrategicResident, Task, TaskStatus
from app.orchestrator_provision import provision_and_handle
from app.orchestrator_runner_dispatch import (
    _fail_stale_local_runner_holds,
    _task_requires_strategic_local_runner,
)
from app.routers.agent_warm_continuity import _finalize_warm_claim
from app.strategic_resident_capacity import waiting_for_required_runner
from app.strategic_resident_runner_policy import (
    is_resident_turn,
    resident_local_claim_route_matches,
    resident_turn_requires_local_runner,
)
from app.strategic_resident_saturated_dispatch import (
    dispatch_local_required_resident_turns_without_cloud_slots,
)
from app.task_capabilities import STRATEGIC_LOCAL_AGENT_TASK_TYPES
from app.time_utils import _utcnow
from app.warm_backlog_reuse import _eligible_backlog_ids
from app.warm_pool import try_pool_dispatch
from app.workbench_state import STRATEGIC_LOCAL_RUNNER_HOLD_CONTEXT_KEY


def _resident_task(*, local: bool = True, context: dict | None = None) -> Task:
    return Task(
        project_id=Project(name="placeholder", user_id="runner-policy").id,
        title="Resident turn",
        payloads=[{
            "type": "strategic_resident_turn",
            "resident_runner_policy": (
                {"require_local_runner": True} if local else {"mode": "cloud"}
            ),
        }],
        context=context or {},
        status=TaskStatus.QUEUED,
        archived=True,
    )


def test_local_requirement_comes_from_first_payload_not_mutable_context():
    local = _resident_task(
        local=True, context={"strategic_local_runner_required": False}
    )
    cloud = _resident_task(
        local=False, context={"strategic_local_runner_required": True}
    )
    assert is_resident_turn(local)
    assert resident_turn_requires_local_runner(local)
    assert not resident_turn_requires_local_runner(cloud)
    assert _task_requires_strategic_local_runner(local)


def test_local_claim_requires_selected_runner_route():
    task = _resident_task(
        context={"runner_target_id": "11111111-1111-1111-1111-111111111111"}
    )
    assert resident_local_claim_route_matches(
        task, "runner:11111111-1111-1111-1111-111111111111"
    )
    assert not resident_local_claim_route_matches(task, "agent-cloud-1")
    assert not resident_local_claim_route_matches(
        task, "runner:22222222-2222-2222-2222-222222222222"
    )


def test_zero_cloud_slot_callsite_dispatches_before_monitoring_and_continue():
    from app.orchestrator import orchestrator_worker

    source = inspect.getsource(orchestrator_worker)
    zero_slots = source.index("if slots_available == 0:")
    resident_dispatch = source.index(
        "dispatch_local_required_resident_turns_without_cloud_slots", zero_slots
    )
    monitoring = source.index("await _check_monitoring_window()", resident_dispatch)
    deadlocks = source.index("await _recover_stale_dependency_deadlocks", monitoring)
    early_continue = source.index("\n                    continue", deadlocks)
    assert zero_slots < resident_dispatch < monitoring < deadlocks < early_continue


@pytest.mark.asyncio
async def test_generic_warm_paths_refuse_resident_turns(db_session):
    project = Project(name="Resident warm exclusion", user_id="runner-policy")
    db_session.add(project)
    await db_session.flush()
    task = _resident_task(local=False)
    task.project_id = project.id
    db_session.add(task)
    await db_session.commit()
    assert await _eligible_backlog_ids(
        db_session, project.id, "vm-warm"
    ) == []
    assert await try_pool_dispatch(None, task) is None
    with pytest.raises(HTTPException) as exc_info:
        await _finalize_warm_claim(
            None, task, "vm-warm", origin="backlog_reuse", refresh_capable=True
        )
    assert "dedicated lineage-validated" in str(exc_info.value.detail)


@pytest.mark.asyncio
async def test_expired_archived_resident_hold_never_cloud_falls_back(db_session):
    project = Project(name="Resident no fallback", user_id="runner-policy")
    db_session.add(project)
    await db_session.flush()
    task = _resident_task(context={
        STRATEGIC_LOCAL_RUNNER_HOLD_CONTEXT_KEY: (
            _utcnow() - timedelta(days=7)
        ).isoformat(),
        "strategic_local_runner_required": True,
    })
    task.project_id = project.id
    db_session.add(task)
    await db_session.commit()
    assert await _fail_stale_local_runner_holds(db_session) == 0
    await db_session.refresh(task)
    assert task.status == TaskStatus.QUEUED
    assert resident_turn_requires_local_runner(task)


@pytest.mark.asyncio
async def test_waiting_requires_compatible_ceiling_floor_and_capacity(db_session):
    project = Project(name="Resident runner policy", user_id="runner-policy")
    db_session.add(project)
    await db_session.flush()
    resident = StrategicResident(
        project_id=project.id,
        runner_policy={"require_local_runner": True},
    )
    db_session.add(resident)
    incompatible = LocalRunner(
        project_id=project.id,
        token_hash="a" * 64,
        last_seen_at=_utcnow(),
        capabilities={
            "task_types": ["strategic_review"],
            "supported_agent_task_types": sorted(STRATEGIC_LOCAL_AGENT_TASK_TYPES),
            "agent_available": True,
            "max_concurrent": 1,
        },
    )
    db_session.add(incompatible)
    await db_session.commit()
    assert await waiting_for_required_runner(db_session, resident)

    compatible = LocalRunner(
        project_id=project.id,
        token_hash="b" * 64,
        last_seen_at=_utcnow(),
        capabilities={
            "task_types": sorted(STRATEGIC_LOCAL_AGENT_TASK_TYPES),
            "supported_agent_task_types": sorted(STRATEGIC_LOCAL_AGENT_TASK_TYPES),
            "agent_available": True,
            "max_concurrent": 1,
        },
    )
    db_session.add(compatible)
    await db_session.commit()
    assert not await waiting_for_required_runner(db_session, resident)

    active = _resident_task(local=False)
    active.project_id = project.id
    active.vm_instance_id = f"runner:{compatible.id}"
    active.status = TaskStatus.IN_PROGRESS
    db_session.add(active)
    await db_session.commit()
    assert await waiting_for_required_runner(db_session, resident)


@pytest.mark.asyncio
async def test_cloud_saturation_path_pins_local_resident_without_cloud_slot(db_session):
    from app.orchestrator import _lock_dispatchable_task_for_mutation

    project = Project(name="Resident saturated cloud", user_id="runner-policy")
    db_session.add(project)
    await db_session.flush()
    runner = LocalRunner(
        project_id=project.id,
        token_hash="c" * 64,
        status="active",
        last_seen_at=_utcnow(),
        capabilities={
            "task_types": sorted(STRATEGIC_LOCAL_AGENT_TASK_TYPES),
            "supported_agent_task_types": sorted(STRATEGIC_LOCAL_AGENT_TASK_TYPES),
            "agent_available": True,
            "max_concurrent": 1,
        },
    )
    task = _resident_task(
        context={
            "_resident_mutation_authorized": True,
            "_resident_input_frozen": True,
        }
    )
    task.project_id = project.id
    task.payloads[0].update(
        {"engine_owner": "strategic_resident", "shadow_only": False}
    )
    db_session.add(task)
    await db_session.commit()

    pinned, held = (
        await dispatch_local_required_resident_turns_without_cloud_slots(
            db_session,
            lock_dispatchable_task=_lock_dispatchable_task_for_mutation,
        )
    )
    await db_session.refresh(task)
    assert (pinned, held) == (0, 1)
    assert task.status == TaskStatus.QUEUED
    assert task.vm_instance_id is None
    assert STRATEGIC_LOCAL_RUNNER_HOLD_CONTEXT_KEY in (task.context or {})

    db_session.add(runner)
    await db_session.commit()
    pinned, held = (
        await dispatch_local_required_resident_turns_without_cloud_slots(
            db_session,
            lock_dispatchable_task=_lock_dispatchable_task_for_mutation,
        )
    )

    await db_session.refresh(task)
    assert (pinned, held) == (1, 0)
    assert task.status == TaskStatus.PROVISIONING
    assert task.vm_instance_id == f"runner:{runner.id}"
    assert (task.context or {})["runner_target_id"] == str(runner.id)


class _BorrowedSession:
    def __init__(self, session):
        self.session = session

    async def __aenter__(self):
        return self.session

    async def __aexit__(self, *_args):
        return False


@pytest.mark.asyncio
async def test_provision_preflight_parks_before_cloud_io(db_session, monkeypatch):
    import app.max_plan_pool as max_plan_pool
    import app.orchestrator as orchestrator

    project = Project(name="Resident provision fence", user_id="runner-policy")
    db_session.add(project)
    await db_session.flush()
    task = _resident_task(local=True)
    task.project_id = project.id
    task.status = TaskStatus.PROVISIONING
    task.vm_instance_id = "agent-cloud-resident"
    db_session.add(task)
    await db_session.commit()

    upload = AsyncMock()
    prepare = AsyncMock()
    cloud_run = AsyncMock()
    gce = AsyncMock()
    release = AsyncMock()
    monkeypatch.setattr(
        orchestrator, "AsyncSessionLocal", lambda: _BorrowedSession(db_session)
    )
    monkeypatch.setattr(orchestrator, "upload_json_payload", upload)
    monkeypatch.setattr(orchestrator, "prepare_and_store_agent_secret", prepare)
    monkeypatch.setattr(orchestrator, "launch_agent_cloud_run_job", cloud_run)
    monkeypatch.setattr(orchestrator, "provision_agent_vm", gce)
    monkeypatch.setattr(max_plan_pool, "release_max_plan_account", release)

    await provision_and_handle(
        "agent-cloud-resident",
        task.id,
        project.id,
        task.payloads,
        None,
        None,
        "unused-account",
        None,
        None,
        None,
        None,
        None,
        None,
    )
    await db_session.refresh(task)
    assert task.status == TaskStatus.QUEUED
    assert task.vm_instance_id is None
    upload.assert_not_awaited()
    prepare.assert_not_awaited()
    cloud_run.assert_not_awaited()
    gce.assert_not_awaited()
    release.assert_awaited_once()

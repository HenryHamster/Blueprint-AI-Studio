"""Security, tenancy, and boundedness tests for Resident runner evidence."""

import json
import uuid
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import HTTPException

from app.models import (
    LocalRunner,
    Project,
    RunnerHeartbeatSnapshot,
    RunnerProjectBinding,
)
from app.routers import agent_resident_capabilities as capability_router
from app.routers.agent_resident_capabilities import ResidentCapabilityRequest
from app.strategic_resident_inspection_receipt import build_inspection_receipt
from app.strategic_resident_capabilities import (
    INVOKABLE_CAPABILITY_IDS,
    compact_capability_catalog,
)
from app.strategic_resident_runner_inspection import (
    MAX_RUNNERS_PER_INSPECTION,
    ResidentRunnerEvidence,
    ResidentRunnerNotFound,
    inspect_project_runner,
    inspect_project_runners,
)
from app.strategic_resident_task_inspection import MAX_EVIDENCE_RESPONSE_BYTES


def _runner(project_id, *, hostname="runner", status="active", capabilities=None):
    return LocalRunner(
        project_id=project_id,
        token_hash="f" * 64,
        hostname=hostname,
        status=status,
        capabilities=capabilities
        or {
            "task_types": "all",
            "max_concurrent": 4,
            "active_task_ids": [],
            "has_gpu": False,
        },
    )


@pytest.mark.asyncio
async def test_runner_inspection_enforces_project_allow_list(db_session):
    own_project = Project(name="own", user_id="u")
    other_project = Project(name="other", user_id="u")
    db_session.add_all([own_project, other_project])
    await db_session.flush()
    own = _runner(own_project.id, hostname="own")
    shared = _runner(other_project.id, hostname="shared")
    private = _runner(other_project.id, hostname="private")
    deleted = _runner(own_project.id, hostname="deleted", status="deleted")
    db_session.add_all([own, shared, private, deleted])
    await db_session.flush()
    db_session.add(
        RunnerProjectBinding(runner_id=shared.id, project_id=own_project.id)
    )
    await db_session.commit()

    listed = await inspect_project_runners(
        db_session,
        project_id=own_project.id,
        observed_at=datetime(2026, 7, 18, tzinfo=timezone.utc),
    )
    listed_ids = {item["runner_id"] for item in listed.data["runners"]}
    assert listed_ids == {str(own.id), str(shared.id)}
    assert {
        item["capacity"]["source"] for item in listed.data["runners"]
    } == {"unknown"}
    assert all(
        item["capacity"]["active_work_count"] is None
        for item in listed.data["runners"]
    )

    shared_evidence = await inspect_project_runner(
        db_session,
        project_id=own_project.id,
        runner_id=str(shared.id),
        observed_at=datetime(2026, 7, 18, tzinfo=timezone.utc),
    )
    assert shared_evidence.source_id == str(shared.id)
    with pytest.raises(ResidentRunnerNotFound):
        await inspect_project_runner(
            db_session,
            project_id=own_project.id,
            runner_id=str(private.id),
        )
    with pytest.raises(ResidentRunnerNotFound):
        await inspect_project_runner(
            db_session,
            project_id=own_project.id,
            runner_id=str(deleted.id),
        )


@pytest.mark.asyncio
async def test_runner_projection_excludes_credentials_and_cross_project_ids(db_session):
    project = Project(name="redaction", user_id="u")
    db_session.add(project)
    await db_session.flush()
    runner = _runner(
        project.id,
        hostname="runner-secret",
        capabilities={
            "task_types": ["create", "review"],
            "max_concurrent": 6,
            "active_task_ids": ["other-project-task-secret"],
            "active_compute_job_ids": ["other-project-job-secret"],
            "has_gpu": True,
            "gpu_count": 2,
            "api_token": "capability-secret",
            "environment": {"PASSWORD": "environment-secret"},
            "config": {"credential": "config-secret"},
        },
    )
    db_session.add(runner)
    await db_session.flush()
    db_session.add(
        RunnerHeartbeatSnapshot(
            runner_id=runner.id,
            captured_at=datetime(2026, 7, 17, tzinfo=timezone.utc),
            active_task_count=40,
            active_compute_job_count=2,
            agent_available=False,
        )
    )
    db_session.add(
        RunnerHeartbeatSnapshot(
            runner_id=runner.id,
            captured_at=datetime(2026, 7, 18, tzinfo=timezone.utc),
            active_task_count=1,
            active_compute_job_count=1,
            agent_available=True,
        )
    )
    await db_session.commit()

    evidence = await inspect_project_runner(
        db_session,
        project_id=project.id,
        runner_id=str(runner.id),
        observed_at=datetime(2026, 7, 18, tzinfo=timezone.utc),
    )
    encoded = evidence.model_dump_json()
    for secret in (
        "f" * 64,
        "other-project-task-secret",
        "other-project-job-secret",
        "capability-secret",
        "environment-secret",
        "config-secret",
    ):
        assert secret not in encoded
    assert evidence.data["capacity"]["active_work_count"] == 2
    assert evidence.data["capacity"]["source"] == "heartbeat_snapshot_advisory"
    assert evidence.data["capacity"]["agent_available"] is True
    assert evidence.data["hardware"] == {"has_gpu": True, "gpu_count": 2}


@pytest.mark.asyncio
async def test_runner_list_is_deterministic_count_and_byte_bounded(db_session):
    project = Project(name="bounds", user_id="u")
    db_session.add(project)
    await db_session.flush()
    base = datetime(2026, 7, 18, 12, tzinfo=timezone.utc)
    for index in range(MAX_RUNNERS_PER_INSPECTION + 5):
        runner = _runner(
            project.id,
            # hostname is varchar(255) in Postgres; keep the seed near that ceiling
            # (still a long label) so the byte-bound is exercised without violating
            # the column constraint. The oversized capabilities blob below (JSONB,
            # unbounded) remains the real stress that proves the projection excludes
            # it from the response.
            hostname=(f"runner-{index}-" + "h" * 230),
            capabilities={
                "task_types": [f"type-{item}-" + "t" * 500 for item in range(100)],
                "max_concurrent": 8,
                "active_task_ids": [str(uuid.uuid4()) for _ in range(index)],
                "untrusted": {f"key-{item}": "x" * 2_000 for item in range(100)},
            },
        )
        runner.created_at = base + timedelta(seconds=index)
        runner.last_seen_at = base
        db_session.add(runner)
    await db_session.commit()
    observed_at = base + timedelta(seconds=10)

    first = await inspect_project_runners(
        db_session, project_id=project.id, observed_at=observed_at
    )
    second = await inspect_project_runners(
        db_session, project_id=project.id, observed_at=observed_at
    )
    assert first.data["truncated"] is True
    assert 0 < first.data["returned_count"] <= MAX_RUNNERS_PER_INSPECTION
    assert len(first.data["runners"]) == first.data["returned_count"]
    assert first.revision == second.revision
    assert first.checksum == second.checksum
    assert len(first.model_dump_json().encode()) <= MAX_EVIDENCE_RESPONSE_BYTES
    assert len(first.model_dump_json().encode()) <= 5_500
    assert len(json.dumps(first.data).encode()) <= 4_800


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("capability_id", "parameters", "kind"),
    [
        ("runner.inspect", {"runner_id": str(uuid.uuid4())}, "runner"),
        ("runner.list", {}, "runner_capacity"),
    ],
)
async def test_runner_capability_route_dispatches_and_writes_citable_kind(
    db_session, monkeypatch, capability_id, parameters, kind
):
    project_id = uuid.uuid4()
    turn_id = uuid.uuid4()
    turn = SimpleNamespace(
        id=turn_id,
        project_id=project_id,
        first_payload={"authority_generation": 2},
        context={"_resident_turn_context_hash": "ctx-runner-inspection"},
    )
    monkeypatch.setattr(
        capability_router,
        "load_live_resident_agent_task_locked",
        AsyncMock(return_value=turn),
    )
    evidence = ResidentRunnerEvidence(
        kind=kind,
        project_id=project_id,
        source_id="runner-source",
        revision="runner:v1",
        checksum="a" * 64,
        observed_at=datetime(2026, 7, 18, tzinfo=timezone.utc),
        data={},
    )
    inspect_one = AsyncMock(return_value=evidence)
    inspect_many = AsyncMock(return_value=evidence)
    monkeypatch.setattr(capability_router, "inspect_project_runner", inspect_one)
    monkeypatch.setattr(capability_router, "inspect_project_runners", inspect_many)
    add = MagicMock()
    monkeypatch.setattr(db_session, "add", add)
    token = {
        "sub": str(turn_id),
        "project_id": str(project_id),
        "vm_instance_id": "vm-1",
        "task_type": "strategic_resident_turn",
        "claim_nonce": "nonce-1",
    }

    response = await capability_router.invoke_resident_capability(
        None,
        ResidentCapabilityRequest(
            capability_id=capability_id,
            parameters=parameters,
        ),
        db_session,
        token,
    )
    assert response["evidence"]["kind"] == kind
    assert response["evidence"]["inspected_at"] == "2026-07-18T00:00:00Z"
    receipt = add.call_args.args[0]
    assert receipt.capability == capability_id
    assert receipt.receipt["structured_result"]["evidence"][0]["kind"] == kind
    if capability_id == "runner.inspect":
        inspect_one.assert_awaited_once_with(
            db_session,
            project_id=project_id,
            runner_id=parameters["runner_id"],
        )
        inspect_many.assert_not_awaited()
    else:
        inspect_many.assert_awaited_once_with(db_session, project_id=project_id)
        inspect_one.assert_not_awaited()


@pytest.mark.asyncio
async def test_runner_inspection_rejects_non_uuid_before_authority_lock(
    db_session, monkeypatch
):
    load = AsyncMock()
    monkeypatch.setattr(
        capability_router, "load_live_resident_agent_task_locked", load
    )
    with pytest.raises(HTTPException) as exc_info:
        await capability_router.invoke_resident_capability(
            None,
            ResidentCapabilityRequest(
                capability_id="runner.inspect",
                parameters={"runner_id": "runner-by-hostname"},
            ),
            db_session,
            {
                "sub": str(uuid.uuid4()),
                "project_id": str(uuid.uuid4()),
                "vm_instance_id": "vm-1",
                "task_type": "strategic_resident_turn",
                "claim_nonce": "nonce-1",
            },
        )
    assert exc_info.value.status_code == 400
    load.assert_not_awaited()


def test_runner_list_receipt_uses_capacity_operation_and_trust_kind():
    project_id = uuid.uuid4()
    turn_id = uuid.uuid4()
    evidence_json = ResidentRunnerEvidence(
        kind="runner_capacity",
        project_id=project_id,
        source_id=f"project:{project_id}:runners",
        revision="runner-capacity:v1",
        checksum="b" * 64,
        observed_at=datetime(2026, 7, 18, tzinfo=timezone.utc),
        data={"runners": []},
    ).model_dump(mode="json")
    turn = SimpleNamespace(
        id=turn_id,
        project_id=project_id,
        first_payload={"authority_generation": 7},
        context={},
    )
    receipt = build_inspection_receipt(
        capability_id="runner.list",
        turn_task=turn,
        context_hash="c" * 64,
        request_hash="request",
        evidence_json=evidence_json,
    )
    assert receipt is not None
    assert receipt.operation_kind == "inspect_runner_capacity"
    cited = receipt.receipt["structured_result"]["evidence"][0]
    assert cited["kind"] == "runner_capacity"
    assert "inspected_at" in cited


def test_runner_queries_project_json_scalars_not_full_capabilities_blob():
    from sqlmodel import select

    from app.strategic_resident_runner_inspection import _runner_columns

    statement = str(select(*_runner_columns())).casefold()
    assert "localrunner.capabilities as capabilities" not in statement
    assert "localrunner.token_hash" not in statement
    assert "max_concurrent" in statement
    assert "runner_heartbeat_snapshot" in statement


def test_runner_routing_mutations_remain_proposal_only():
    availability = {
        item["id"]: item["availability"] for item in compact_capability_catalog()
    }
    for capability_id in ("task.dispatch", "task.redirect"):
        assert capability_id not in INVOKABLE_CAPABILITY_IDS
        assert availability[capability_id] == "propose_only"

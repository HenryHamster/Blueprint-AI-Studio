"""Safety-contract regressions for Resident-managed Task creation."""

import pytest
from types import SimpleNamespace
from unittest.mock import AsyncMock

from app.strategic_resident_capabilities import (
    INVOKABLE_CAPABILITY_IDS,
    CapabilityMode,
    RESIDENT_CAPABILITIES,
    capability_for,
    compact_capability_catalog,
)
from app.strategic_resident_managed_task import (
    MANAGED_TASK_CREATE_SERVICE,
    MAX_MANAGED_TASK_ARTIFACT_REFS,
    MAX_MANAGED_TASK_SOURCE_EVIDENCE,
    ManagedTaskCreateWriter,
)
from app.artifact_refs import RESIDENT_ARTIFACT_REF_GRANTS_MAX
from app.strategic_resident_capability_invocation import ResidentCapabilityInvocation
from app.routers import agent_resident_capabilities as capability_router


def test_only_bounded_create_and_debug_managed_tasks_are_invokable():
    assert {
        "task.code_change.create",
        "task.debug.create",
    } <= INVOKABLE_CAPABILITY_IDS
    catalog = {item["id"]: item for item in compact_capability_catalog()}
    assert catalog["task.large_review.create"]["availability"] == "propose_only"
    assert catalog["task.ci_repair.create"]["availability"] == "propose_only"
    for capability_id in ("task.code_change.create", "task.debug.create"):
        spec = capability_for(capability_id)
        assert spec.service == MANAGED_TASK_CREATE_SERVICE
        assert spec.required_parameters == (
            ("task_id", "title", "prompt")
            if capability_id == "task.debug.create"
            else ("title", "prompt")
        )
        assert spec.evidence_preconditions == (
            () if capability_id == "task.code_change.create" else ("task",)
        )


def test_fresh_code_change_uses_frozen_turn_lineage_without_fake_task_evidence():
    import uuid

    invocation = ResidentCapabilityInvocation(
        capability_id="task.code_change.create",
        project_id=uuid.uuid4(),
        turn_task_id=uuid.uuid4(),
        authority_generation=1,
        parameters={"title": "Bounded change", "prompt": "Implement the request."},
        operation_id="op-fresh-code-change",
        expected_revisions={"context_hash": "a" * 64},
        evidence=(),
    )

    assert invocation.evidence == ()


def test_managed_task_writer_is_single_use_and_deterministic():
    import uuid

    receipt_id = uuid.uuid4()
    writer = ManagedTaskCreateWriter(
        receipt_id,
        authorized_capability="task.code_change.create",
    )
    task_id = writer.create(
        capability_id="task.code_change.create",
        task_type="create",
        title="  Bounded change  ",
        prompt="  Implement it.  ",
        source_evidence=[
            {
                "kind": "task",
                "project_id": str(uuid.uuid4()),
                "source_id": "task:1",
                "revision": "revision-1",
                "checksum": "a" * 64,
                "inspected_at": "2026-07-18T00:00:00Z",
            }
        ],
    )
    assert task_id == uuid.uuid5(receipt_id, "strategic-resident-managed-task")
    plan = writer.validated_plan()
    assert plan is not None
    assert plan.title == "Bounded change"
    assert plan.prompt == "Implement it."
    assert plan.source_evidence[0]["source_id"] == "task:1"
    with pytest.raises(Exception, match="already contains"):
        writer.create(
            capability_id="task.code_change.create",
            task_type="create",
            title="Second",
            prompt="Forbidden duplicate",
        )


def test_managed_source_handoff_limits_are_strict():
    import uuid

    assert MAX_MANAGED_TASK_ARTIFACT_REFS < RESIDENT_ARTIFACT_REF_GRANTS_MAX
    writer = ManagedTaskCreateWriter(
        uuid.uuid4(),
        authorized_capability="task.code_change.create",
    )
    evidence = [
        {
            "kind": "task",
            "project_id": str(uuid.uuid4()),
            "source_id": str(uuid.uuid4()),
            "revision": f"revision-{index}",
            "checksum": "a" * 64,
            "inspected_at": "2026-07-18T00:00:00Z",
        }
        for index in range(MAX_MANAGED_TASK_SOURCE_EVIDENCE + 1)
    ]
    with pytest.raises(Exception, match="bounded handoff limit"):
        writer.create(
            capability_id="task.code_change.create",
            task_type="create",
            title="Too much context",
            prompt="Keep this bounded.",
            source_evidence=evidence,
        )


def test_human_authored_tool_writes_remain_unavailable_to_resident():
    invokable_mutations = {
        spec.capability_id
        for spec in RESIDENT_CAPABILITIES
        if spec.mode is CapabilityMode.mutate
        and spec.capability_id in INVOKABLE_CAPABILITY_IDS
    }
    assert not {
        capability_id
        for capability_id in invokable_mutations
        if "tool" in capability_id or "artifact.write" in capability_id
    }
    with pytest.raises(ValueError, match="unknown Strategic Resident capability"):
        capability_for("project_tool.write")


def test_model_cannot_override_server_owned_source_evidence():
    import uuid
    from pydantic import ValidationError

    with pytest.raises(ValidationError, match="unsupported capability parameters"):
        ResidentCapabilityInvocation(
            capability_id="task.code_change.create",
            project_id=uuid.uuid4(),
            turn_task_id=uuid.uuid4(),
            authority_generation=3,
            operation_id="create-managed-override",
            parameters={
                "title": "Bounded change",
                "prompt": "Implement it.",
                "_source_evidence": [],
            },
            expected_revisions={"authority_generation": 3},
            evidence=(),
        )


@pytest.mark.asyncio
async def test_managed_task_route_binds_validated_evidence_into_fenced_request(
    monkeypatch,
):
    import uuid

    project_id = uuid.uuid4()
    turn_id = uuid.uuid4()
    executor = AsyncMock(
        return_value=SimpleNamespace(
            id=uuid.uuid4(),
            operation_id="create-managed-1",
            status="applied",
            unknown_effects=[],
            receipt={},
        )
    )
    monkeypatch.setattr(capability_router, "execute_fenced_db_operation", executor)
    monkeypatch.setattr(
        capability_router, "_check_evidence_refs_against_receipts", AsyncMock()
    )
    invocation = ResidentCapabilityInvocation(
        capability_id="task.code_change.create",
        project_id=project_id,
        turn_task_id=turn_id,
        authority_generation=3,
        operation_id="create-managed-1",
        parameters={"title": "Bounded change", "prompt": "Implement it."},
        expected_revisions={"authority_generation": 3},
        evidence=(
            {
                "kind": "task",
                "project_id": project_id,
                "source_id": "task:source",
                "revision": "revision-1",
                "checksum": "d" * 64,
                "inspected_at": "2026-07-18T00:00:00Z",
            },
        ),
    )
    await capability_router.invoke_resident_mutation(
        None,
        invocation,
        session=SimpleNamespace(),
        token={
            "sub": str(turn_id),
            "project_id": str(project_id),
            "vm_instance_id": "vm-1",
            "task_type": "strategic_resident_turn",
            "claim_nonce": "nonce-1",
        },
    )
    fenced_request = executor.await_args.args[1]
    source = fenced_request.request_payload["_source_evidence"][0]
    assert source["source_id"] == "task:source"
    assert source["checksum"] == "d" * 64

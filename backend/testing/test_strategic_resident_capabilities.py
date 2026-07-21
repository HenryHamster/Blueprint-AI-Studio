import uuid
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import HTTPException
from pydantic import ValidationError

from app.models import Project, Task, TaskStatus
from app.strategic_operation_authority_validation import OperationPayloadTooLarge
from app.strategic_resident_capabilities import (
    CapabilityMode,
    RESIDENT_CAPABILITIES,
    capability_for,
    compact_capability_catalog,
)
from app.routers import agent_resident_capabilities as capability_router
from app.routers.agent_resident_capabilities import ResidentCapabilityRequest
from app.strategic_resident_capability_invocation import (
    CorrectableCapabilityParameterError,
    ResidentCapabilityInvocation,
    validated_capability_parameters,
)
from app.strategic_resident_task_inspection import (
    ResidentTaskEvidence,
    ResidentTaskNotFound,
    inspect_project_task,
    inspect_project_task_lineage,
)


def test_inspection_capability_kinds_match_real_capabilities():
    """Every INSPECTION_CAPABILITY_KINDS key must be a real inspect capability_id.

    A stale/typo'd key silently drops that inspection's revisioned evidence from
    the trusted-evidence index (fail-closed) the moment inspection receipts are
    written, so guard the map against drift and duplicate operation_kinds.
    """
    from app.strategic_resident_evidence_policy import INSPECTION_CAPABILITY_KINDS

    inspect_ids = {
        item.capability_id
        for item in RESIDENT_CAPABILITIES
        if item.mode is CapabilityMode.inspect
    }
    unknown = set(INSPECTION_CAPABILITY_KINDS) - inspect_ids
    assert not unknown, f"map references non-existent inspect capabilities: {sorted(unknown)}"
    # The five task inspections that actually produce evidence today must be mapped.
    invokable = {
        "task.inspect",
        "task.logs.inspect",
        "task.failure.inspect",
        "task.provider.inspect",
        "task.lineage.inspect",
    }
    assert invokable <= set(INSPECTION_CAPABILITY_KINDS)
    # operation_kind values are the receipt contract — they must be distinct.
    kinds = list(INSPECTION_CAPABILITY_KINDS.values())
    assert len(kinds) == len(set(kinds)), "duplicate operation_kind in the map"


def test_wired_task_lifecycle_preconditions_are_produced_by_task_inspection():
    """Every invoked lifecycle adapter can cite canonical inspection evidence."""

    for capability_id in (
        "task.cancel",
        "task.requeue",
        "task.retry",
        "task.unblock",
    ):
        assert capability_for(capability_id).evidence_preconditions == ("task",)


def test_capability_registry_declares_complete_safety_contracts():
    assert len({item.capability_id for item in RESIDENT_CAPABILITIES}) == len(
        RESIDENT_CAPABILITIES
    )
    required = {
        "task.inspect",
        "task.lineage.inspect",
        "task.logs.inspect",
        "task.failure.inspect",
        "task.provider.inspect",
        "runner.list",
        "pipeline.inspect",
        "qualification.inspect",
        "pr.diff.inspect",
        "pr.checks.inspect",
        "ci.logs.inspect",
        "deployment.inspect",
        "task.dispatch",
        "task.redirect",
        "task.retry",
        "task.requeue",
        "task.cancel",
        "task.salvage",
        "task.review_fix_loop",
        "pr.create",
        "pr.merge",
        "qualification.run",
        "audit.run",
        "deployment.deploy",
        "deployment.rollback",
    }
    assert required <= {item.capability_id for item in RESIDENT_CAPABILITIES}
    for item in RESIDENT_CAPABILITIES:
        assert item.scope == "project"
        assert item.service
        assert item.receipt_fields
        assert tuple(spec.name for spec in item.parameter_specs) == item.required_parameters
        if item.mode is CapabilityMode.mutate:
            if item.capability_id == "task.code_change.create":
                assert item.evidence_preconditions == ()
            else:
                assert item.evidence_preconditions
            assert item.idempotency == "operation_id"
            assert item.reconciliation != "not_required"


def test_compact_catalog_omits_verbose_receipt_and_evidence_metadata():
    catalog = compact_capability_catalog()
    assert len(catalog) == len(RESIDENT_CAPABILITIES)
    assert set(catalog[0]) == {
        "id", "mode", "risk", "parameter_specs", "approval", "availability"
    }
    task = next(item for item in catalog if item["id"] == "task.inspect")
    pull_request = next(item for item in catalog if item["id"] == "pr.inspect")
    rollback = next(item for item in catalog if item["id"] == "deployment.rollback")
    assert task["parameter_specs"] == {"task_id": "uuid"}
    assert pull_request["parameter_specs"] == {
        "pr_number": {"type": "integer", "min": 1, "max": 2**31 - 1}
    }
    assert rollback["parameter_specs"] == {
        "revision": {"type": "ref", "min_len": 1, "max_len": 512}
    }
    assert capability_for("task.inspect").service == "resident_task_inspection"
    assert next(item for item in catalog if item["id"] == "task.inspect")["availability"] == "invoke"
    assert next(item for item in catalog if item["id"] == "runner.inspect")["availability"] == "invoke"
    assert next(item for item in catalog if item["id"] == "runner.list")["availability"] == "invoke"
    assert next(item for item in catalog if item["id"] == "pr.inspect")["availability"] == "invoke"
    assert next(item for item in catalog if item["id"] == "pr.diff.inspect")["availability"] == "invoke"
    assert next(item for item in catalog if item["id"] == "pr.checks.inspect")["availability"] == "invoke"
    assert next(item for item in catalog if item["id"] == "deployment.inspect")["availability"] == "propose_only"
    assert capability_for("pr.checks.inspect").required_parameters == (
        "pr_number",
        "head_sha",
    )
    assert next(item for item in catalog if item["id"] == "pr.merge")["availability"] == "propose_only"


def test_mutation_invocation_requires_authority_and_evidence():
    base = {
        "capability_id": "pr.merge",
        "project_id": uuid.uuid4(),
        "turn_task_id": uuid.uuid4(),
        "authority_generation": 4,
        "parameters": {"pr_number": 12, "expected_head_sha": "abc1234"},
    }
    project_id = base["project_id"]
    with pytest.raises(ValidationError, match="operation_id"):
        ResidentCapabilityInvocation.model_validate(base)
    valid = ResidentCapabilityInvocation.model_validate(
        {
            **base,
            "operation_id": "merge-12-abc1234",
            "expected_revisions": {"task_version": 7},
            "evidence": tuple(
                {
                    "kind": kind,
                    "project_id": project_id,
                    "source_id": "pr:12",
                    "revision": "abc1234",
                    "checksum": "a" * 64,
                    "inspected_at": "2026-07-17T12:00:00Z",
                }
                for kind in ("pr", "pr_diff", "pr_check")
            ),
        }
    )
    assert valid.capability_id == "pr.merge"
    bool_revision = valid.model_dump()
    bool_revision["expected_revisions"] = {"task_version": True}
    with pytest.raises(ValidationError):
        ResidentCapabilityInvocation.model_validate(bool_revision)
    excessive_evidence = valid.model_dump()
    excessive_evidence["evidence"] = excessive_evidence["evidence"] * 17
    with pytest.raises(ValidationError):
        ResidentCapabilityInvocation.model_validate(excessive_evidence)
    with pytest.raises(ValidationError, match="credential scope"):
        ResidentCapabilityInvocation.model_validate(
            {**valid.model_dump(), "parameters": {**valid.parameters, "token": "x"}}
        )
    foreign = valid.model_dump()
    foreign["evidence"][0]["project_id"] = uuid.uuid4()
    with pytest.raises(ValidationError, match="another project"):
        ResidentCapabilityInvocation.model_validate(foreign)
    nested_secret = valid.model_dump()
    nested_secret["parameters"] = {
        **valid.parameters,
        "unexpected": {"token": "secret"},
    }
    with pytest.raises(ValidationError, match="credential scope"):
        ResidentCapabilityInvocation.model_validate(nested_secret)


def test_unknown_capability_fails_closed():
    with pytest.raises(ValueError, match="unknown Strategic Resident capability"):
        capability_for("shell.admin")


def test_capability_parameter_contracts_reject_type_and_casefold_bypasses():
    project_id = uuid.uuid4()
    turn_task_id = uuid.uuid4()
    base = {
        "capability_id": "task.cancel",
        "project_id": project_id,
        "turn_task_id": turn_task_id,
        "authority_generation": 1,
        "operation_id": "cancel-one",
        "expected_revisions": {"task": 1},
        "evidence": ({
            "kind": "task",
            "project_id": project_id,
            "source_id": "task:one",
            "revision": "v1",
            "checksum": "a" * 64,
            "inspected_at": "2026-07-17T12:00:00Z",
        },),
    }
    with pytest.raises(ValidationError, match="task_id must be a UUID string"):
        ResidentCapabilityInvocation.model_validate(
            {**base, "parameters": {"task_id": [], "reason": "operator request"}}
        )
    with pytest.raises(ValidationError, match="reason must be a string"):
        ResidentCapabilityInvocation.model_validate(
            {
                **base,
                "parameters": {
                    "task_id": str(uuid.uuid4()),
                    "reason": {"safe": "nested"},
                },
            }
        )
    with pytest.raises(ValidationError, match="credential scope"):
        ResidentCapabilityInvocation.model_validate(
            {
                **base,
                "parameters": {
                    "task_id": str(uuid.uuid4()),
                    "reason": {"Api-Key": "secret"},
                },
            }
        )
    with pytest.raises(OperationPayloadTooLarge, match="16384 bytes"):
        validated_capability_parameters(
            capability_for("task.cancel"),
            {"task_id": str(uuid.uuid4()), "reason": "x" * 20_000},
        )
    with pytest.raises(ValueError, match="control characters"):
        validated_capability_parameters(
            capability_for("task.cancel"),
            {"task_id": str(uuid.uuid4()), "reason": "bad\x00reason"},
        )
    with pytest.raises(ValueError, match="reference contains whitespace"):
        validated_capability_parameters(
            capability_for("branch.inspect"),
            {"ref": "main branch"},
        )


@pytest.mark.parametrize(
    ("capability_id", "parameters", "parameter", "issue", "expected", "message"),
    [
        ("task.inspect", {"task_id": []}, "task_id", "type", {"kind": "uuid"}, "task_id must be a UUID string"),
        ("task.inspect", {"task_id": "bad"}, "task_id", "format", {"kind": "uuid"}, "task_id must be a UUID string"),
        ("pr.inspect", {"pr_number": "7"}, "pr_number", "type", {"kind": "integer", "minimum": 1, "maximum": 2**31 - 1}, "pr_number must be an integer"),
        ("pr.inspect", {"pr_number": 0}, "pr_number", "minimum", {"kind": "integer", "minimum": 1, "maximum": 2**31 - 1}, "pr_number is below its minimum"),
        ("pr.inspect", {"pr_number": 2**31}, "pr_number", "maximum", {"kind": "integer", "minimum": 1, "maximum": 2**31 - 1}, "pr_number exceeds its maximum"),
    ],
)
def test_scalar_parameter_errors_expose_narrow_correction_detail(
    capability_id, parameters, parameter, issue, expected, message
):
    with pytest.raises(CorrectableCapabilityParameterError) as invalid:
        validated_capability_parameters(capability_for(capability_id), parameters)
    assert invalid.value.detail() == {
        "code": "resident_capability_parameter_validation",
        "parameter": parameter,
        "issue": issue,
        "expected": expected,
        "message": message,
    }


@pytest.mark.parametrize(
    ("capability_id", "parameters", "message"),
    [
        ("task.inspect", {}, "missing required"),
        (
            "task.inspect",
            {"task_id": "00000000-0000-0000-0000-000000000001", "extra": 1},
            "unsupported capability parameters",
        ),
        ("task.inspect", {"task_id": {"token": "secret"}}, "credential scope"),
        ("branch.inspect", {"ref": 123}, "must be a string"),
    ],
)
def test_non_scalar_parameter_errors_are_not_marked_correctable(
    capability_id, parameters, message
):
    with pytest.raises(ValueError, match=message) as invalid:
        validated_capability_parameters(capability_for(capability_id), parameters)
    assert not isinstance(invalid.value, CorrectableCapabilityParameterError)


def test_capability_rate_limit_identity_is_task_token_scoped_and_hashed():
    request_one = SimpleNamespace(
        headers={"x-agent-token": "resident-token-one"},
        client=SimpleNamespace(host="10.0.0.1"),
    )
    request_same = SimpleNamespace(
        headers={"x-agent-token": "resident-token-one"},
        client=SimpleNamespace(host="10.0.0.2"),
    )
    request_two = SimpleNamespace(
        headers={"x-agent-token": "resident-token-two"},
        client=SimpleNamespace(host="10.0.0.1"),
    )
    key_one = capability_router._capability_rate_key(request_one)
    assert key_one == capability_router._capability_rate_key(request_same)
    assert key_one != capability_router._capability_rate_key(request_two)
    assert "resident-token" not in key_one


@pytest.mark.asyncio
async def test_exact_task_inspection_is_project_scoped_and_bounded(db_session):
    project = Project(name="one", user_id="u")
    other = Project(name="two", user_id="u")
    db_session.add_all([project, other])
    await db_session.flush()
    task = Task(
        project_id=project.id,
        title="Exact task",
        status=TaskStatus.FAILED,
        payloads=[{"type": "fix", "pr_number": 41, "branch": "fix/41"}],
        provider="anthropic",
        model="claude-test",
        status_log=[{"status": "QUEUED"}, {"status": "FAILED"}],
        latest_status_message="failed cleanly",
        context={"failure_category": "test_failed", "error": "focused failure"},
    )
    db_session.add(task)
    await db_session.commit()

    observed = datetime(2026, 7, 17, 12, 0, 0, tzinfo=timezone.utc)
    evidence = await inspect_project_task(
        db_session,
        project_id=project.id,
        task_id=task.id,
        kind="failure",
        observed_at=observed,
    )
    assert evidence.data["task_id"] == str(task.id)
    assert evidence.data["failure_category"] == "test_failed"
    assert evidence.data["error"] == "focused failure"
    assert evidence.observed_at == observed
    assert len(evidence.checksum) == 64

    logs = await inspect_project_task(
        db_session,
        project_id=project.id,
        task_id=task.id,
        kind="logs",
        log_limit=1,
    )
    assert logs.data["status_log"] == [{"status": "FAILED"}]
    with pytest.raises(ResidentTaskNotFound):
        await inspect_project_task(
            db_session,
            project_id=other.id,
            task_id=task.id,
        )


@pytest.mark.asyncio
async def test_log_inspection_is_byte_bounded(db_session):
    project = Project(name="bounded logs", user_id="u")
    db_session.add(project)
    await db_session.flush()
    task = Task(
        project_id=project.id,
        title="large logs",
        status_log=[{"message": "x" * 100_000, "index": index} for index in range(20)],
    )
    db_session.add(task)
    await db_session.commit()

    evidence = await inspect_project_task(
        db_session,
        project_id=project.id,
        task_id=task.id,
        kind="logs",
        log_limit=20,
    )
    encoded = evidence.model_dump_json().encode()
    assert len(encoded) < 64_000
    assert evidence.data["status_log_truncated"] is True
    assert all(item.get("truncated") is True for item in evidence.data["status_log"])


@pytest.mark.asyncio
async def test_task_metadata_and_log_window_are_bounded(db_session):
    project = Project(name="bounded metadata", user_id="u")
    db_session.add(project)
    await db_session.flush()
    task = Task(
        project_id=project.id,
        title="t" * 20_000,
        depends_on=[str(uuid.uuid4()) for _ in range(80)],
        status_log=[{"index": index} for index in range(100)],
        context={"branch": "b" * 20_000},
    )
    db_session.add(task)
    await db_session.commit()

    metadata = await inspect_project_task(
        db_session,
        project_id=project.id,
        task_id=task.id,
        kind="task",
    )
    assert len(metadata.data["title"]) == 512
    assert len(metadata.data["branch"]) == 512
    assert len(metadata.data["depends_on"]) == 50
    assert metadata.data["depends_on_truncated"] is True

    logs = await inspect_project_task(
        db_session,
        project_id=project.id,
        task_id=task.id,
        kind="logs",
        log_limit=10,
    )
    assert [item["index"] for item in logs.data["status_log"]] == list(range(90, 100))
    assert logs.data["status_log_truncated"] is True


@pytest.mark.asyncio
async def test_task_evidence_redacts_nested_and_inline_credentials(db_session):
    project = Project(name="sanitized evidence", user_id="u")
    db_session.add(project)
    await db_session.flush()
    github_token = "ghp_" + "x" * 32
    jwt_token = "eyJ" + "a" * 24 + ".eyJ" + "b" * 24 + "." + "c" * 24
    task = Task(
        project_id=project.id,
        latest_status_message=f"clone {github_token}",
        status_log=[
            {
                "Token": "short-secret",
                "nested": {"api-key": "another-secret"},
                "dsn": "postgresql://user:password@db.example/test",
                "jwt": jwt_token,
            }
        ],
        context={
            "error": f"Authorization: Bearer {jwt_token}",
            "failure_category": {"clientSecret": "hidden-value"},
        },
    )
    db_session.add(task)
    await db_session.commit()

    logs = await inspect_project_task(
        db_session,
        project_id=project.id,
        task_id=task.id,
        kind="logs",
    )
    failure = await inspect_project_task(
        db_session,
        project_id=project.id,
        task_id=task.id,
        kind="failure",
    )
    encoded = logs.model_dump_json() + failure.model_dump_json()
    for secret in (
        github_token,
        jwt_token,
        "short-secret",
        "another-secret",
        "password@db.example",
        "hidden-value",
    ):
        assert secret not in encoded
    assert "[redacted]" in encoded or "***" in encoded


@pytest.mark.asyncio
async def test_task_bound_capability_route_invokes_only_implemented_inspection(
    db_session, monkeypatch
):
    project_id = uuid.uuid4()
    turn_id = uuid.uuid4()
    target_id = uuid.uuid4()
    monkeypatch.setattr(
        capability_router,
        "load_live_resident_agent_task_locked",
        AsyncMock(
            return_value=SimpleNamespace(
                id=turn_id,
                project_id=project_id,
                first_payload={"authority_generation": 3},
                context={"_resident_turn_context_hash": "ctx-route"},
            )
        ),
    )
    monkeypatch.setattr(
        capability_router,
        "build_inspection_receipt",
        MagicMock(return_value=None),
    )
    evidence = ResidentTaskEvidence(
        kind="task",
        project_id=project_id,
        source_id=str(target_id),
        revision="task:v1",
        checksum="a" * 64,
        observed_at=datetime.now(timezone.utc),
        data={"task_id": str(target_id)},
    )
    inspect = AsyncMock(return_value=evidence)
    monkeypatch.setattr(capability_router, "inspect_project_task", inspect)
    token = {
        "sub": str(turn_id),
        "project_id": str(project_id),
        "vm_instance_id": "vm-1",
        "task_type": "strategic_resident_turn",
        "claim_nonce": "nonce-1",
    }
    result = await capability_router.invoke_resident_capability(
        None,
        ResidentCapabilityRequest(
            capability_id="task.inspect",
            parameters={"task_id": str(target_id)},
        ),
        db_session,
        token,
    )
    assert result["evidence"]["source_id"] == str(target_id)
    inspect.assert_awaited_once_with(
        db_session,
        project_id=project_id,
        task_id=target_id,
        kind="task",
    )

    external = AsyncMock(return_value={"capability_id": "pr.diff.inspect"})
    monkeypatch.setattr(capability_router, "invoke_pr_diff_inspection", external)
    assert await capability_router.invoke_resident_capability(
        None,
        ResidentCapabilityRequest(
            capability_id="pr.diff.inspect", parameters={"pr_number": 12}
        ),
        db_session,
        token,
    ) == {"capability_id": "pr.diff.inspect"}
    external.assert_awaited_once()

    checks = AsyncMock(return_value={"capability_id": "pr.checks.inspect"})
    monkeypatch.setattr(capability_router, "invoke_pr_checks_inspection", checks)
    head_sha = "b" * 40
    assert await capability_router.invoke_resident_capability(
        None,
        ResidentCapabilityRequest(
            capability_id="pr.checks.inspect",
            parameters={"pr_number": 12, "head_sha": head_sha},
        ),
        db_session,
        token,
    ) == {"capability_id": "pr.checks.inspect"}
    assert checks.await_args.kwargs["parameters"] == {
        "pr_number": 12,
        "head_sha": head_sha,
    }

    with pytest.raises(HTTPException) as mutation:
        await capability_router.invoke_resident_capability(
            None,
            ResidentCapabilityRequest(
                capability_id="task.cancel",
                parameters={"task_id": str(target_id), "reason": "operator request"},
            ),
            db_session,
            token,
        )
    assert mutation.value.status_code == 501


@pytest.mark.asyncio
async def test_capability_route_enforces_durable_unique_budget_and_retry(
    db_session, monkeypatch
):
    project_id = uuid.uuid4()
    turn_id = uuid.uuid4()
    turn = SimpleNamespace(
        id=turn_id,
        project_id=project_id,
        first_payload={"authority_generation": 3},
        context={"_resident_turn_context_hash": "ctx-budget"},
    )
    load = AsyncMock(return_value=turn)
    monkeypatch.setattr(
        capability_router,
        "load_live_resident_agent_task_locked",
        load,
    )
    evidence = ResidentTaskEvidence(
        kind="task",
        project_id=project_id,
        source_id="task",
        revision="task:v1",
        checksum="a" * 64,
        observed_at=datetime.now(timezone.utc),
        data={},
    )
    inspect = AsyncMock(return_value=evidence)
    monkeypatch.setattr(capability_router, "inspect_project_task", inspect)
    monkeypatch.setattr(
        capability_router,
        "build_inspection_receipt",
        MagicMock(return_value=None),
    )
    canonical_replay = AsyncMock(
        return_value={
            "kind": "task",
            "project_id": str(project_id),
            "source_id": "task",
            "revision": "task:v1",
            "checksum": "a" * 64,
            "inspected_at": datetime.now(timezone.utc).isoformat(),
            "data": {},
        }
    )
    monkeypatch.setattr(
        capability_router,
        "canonical_inspection_replay",
        canonical_replay,
    )
    token = {
        "sub": str(turn_id),
        "project_id": str(project_id),
        "vm_instance_id": "vm-1",
        "task_type": "strategic_resident_turn",
        "claim_nonce": "nonce-1",
    }
    targets = [uuid.uuid4() for _ in range(9)]

    async def invoke(target: uuid.UUID):
        return await capability_router.invoke_resident_capability(
            None,
            ResidentCapabilityRequest(
                capability_id="task.inspect",
                parameters={"task_id": str(target)},
            ),
            db_session,
            token,
        )

    for target in targets[:8]:
        await invoke(target)
    assert len(turn.context["_strategic_resident_capability_requests"]) == 8

    await invoke(targets[0])
    assert len(turn.context["_strategic_resident_capability_requests"]) == 8
    with pytest.raises(HTTPException) as exhausted:
        await invoke(targets[8])
    assert exhausted.value.status_code == 429
    assert inspect.await_count == 8
    canonical_replay.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("capability_id", "parameters", "expected_detail"),
    [
        (
            "task.inspect",
            {"task_id": "not-a-uuid"},
            {
                "code": "resident_capability_parameter_validation",
                "parameter": "task_id",
                "issue": "format",
                "expected": {"kind": "uuid"},
                "message": "task_id must be a UUID string",
            },
        ),
        (
            "pr.inspect",
            {"pr_number": "7"},
            {
                "code": "resident_capability_parameter_validation",
                "parameter": "pr_number",
                "issue": "type",
                "expected": {"kind": "integer", "minimum": 1, "maximum": 2**31 - 1},
                "message": "pr_number must be an integer",
            },
        ),
    ],
)
async def test_capability_route_rejects_invalid_parameters_before_authority_locks(
    db_session, monkeypatch, capability_id, parameters, expected_detail
):
    load = AsyncMock()
    monkeypatch.setattr(
        capability_router,
        "load_live_resident_agent_task_locked",
        load,
    )
    with pytest.raises(HTTPException) as invalid:
        await capability_router.invoke_resident_capability(
            None,
            ResidentCapabilityRequest(
                capability_id=capability_id,
                parameters=parameters,
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
    assert invalid.value.status_code == 400
    assert invalid.value.detail == expected_detail
    load.assert_not_awaited()


@pytest.mark.asyncio
async def test_inspection_requires_frozen_context_before_budget_or_adapter(
    db_session, monkeypatch
):
    project_id = uuid.uuid4()
    turn_id = uuid.uuid4()
    turn = SimpleNamespace(id=turn_id, project_id=project_id, context={})
    monkeypatch.setattr(
        capability_router,
        "load_live_resident_agent_task_locked",
        AsyncMock(return_value=turn),
    )
    inspect = AsyncMock()
    monkeypatch.setattr(capability_router, "inspect_project_task", inspect)

    with pytest.raises(HTTPException) as missing_context:
        await capability_router.invoke_resident_capability(
            None,
            ResidentCapabilityRequest(
                capability_id="task.inspect",
                parameters={"task_id": str(uuid.uuid4())},
            ),
            db_session,
            {
                "sub": str(turn_id),
                "project_id": str(project_id),
                "vm_instance_id": "vm-1",
                "task_type": "strategic_resident_turn",
                "claim_nonce": "nonce-1",
            },
        )
    assert missing_context.value.status_code == 409
    assert "_strategic_resident_capability_requests" not in turn.context
    inspect.assert_not_awaited()


def test_inspection_receipt_maps_kind_and_is_trusted():
    """An inspection receipt makes its evidence citable via the trusted index."""
    from app.strategic_resident_evidence_validation import (
        _canonical_hash,
        build_trusted_evidence_index,
    )
    from app.strategic_resident_inspection_receipt import (
        build_inspection_receipt,
        citable_inspection_evidence,
    )

    project_id = uuid.uuid4()
    turn_id = uuid.uuid4()
    source_id = str(uuid.uuid4())
    evidence_json = {
        "kind": "logs",
        "project_id": str(project_id),
        "source_id": source_id,
        "revision": "task:v1",
        "checksum": "e" * 64,
        "observed_at": "2026-07-17T00:00:00+00:00",
        "data": {"task_id": source_id},
    }
    # A logs inspection maps into the task_log trust kind.
    assert citable_inspection_evidence(evidence_json)["kind"] == "task_log"

    turn_task = SimpleNamespace(
        id=turn_id,
        project_id=project_id,
        first_payload={"authority_generation": 3},
        context={},
    )
    receipt = build_inspection_receipt(
        capability_id="task.logs.inspect",
        turn_task=turn_task,
        context_hash="ctx-123",
        request_hash="f" * 64,
        evidence_json=evidence_json,
    )
    assert receipt is not None
    assert receipt.operation_kind == "inspect_task_log"
    assert receipt.status == "applied"
    assert receipt.risk_level == "low"
    assert receipt.expected_revisions == {"context_hash": "ctx-123"}

    provider_input = {
        "lineage": {"context_hash": "ctx-123", "evidence_hash": _canonical_hash([])},
        "evidence": [],
    }
    index = build_trusted_evidence_index(
        provider_input, [receipt], expected_context_hash="ctx-123"
    )
    assert ("task_log", source_id, "task:v1", "e" * 64) in index.entries

    # The context_hash binding is meaningful: the SAME receipt is NOT trusted in a
    # turn with a different context_hash (its evidence is dropped from the index).
    other = build_trusted_evidence_index(
        {
            "lineage": {"context_hash": "ctx-other", "evidence_hash": _canonical_hash([])},
            "evidence": [],
        },
        [receipt],
        expected_context_hash="ctx-other",
    )
    assert ("task_log", source_id, "task:v1", "e" * 64) not in other.entries


def test_inspection_receipt_requires_context_hash_and_mapped_capability():
    from app.strategic_resident_inspection_receipt import build_inspection_receipt

    turn_task = SimpleNamespace(
        id=uuid.uuid4(), project_id=uuid.uuid4(), first_payload={}, context={}
    )
    # The low-level builder cannot bind a receipt without a persisted context
    # hash. The route rejects this state before consuming budget or inspecting.
    assert (
        build_inspection_receipt(
            capability_id="task.inspect",
            turn_task=turn_task,
            context_hash="",
            request_hash="a" * 64,
            evidence_json={"kind": "task"},
        )
        is None
    )
    # A capability with no inspection-kind mapping is not receipted.
    assert (
        build_inspection_receipt(
            capability_id="pr.merge",
            turn_task=turn_task,
            context_hash="ctx",
            request_hash="a" * 64,
            evidence_json={"kind": "task"},
        )
        is None
    )


def test_all_resident_capability_routes_are_token_allowlisted():
    """Every capability route must be in the resident-token allowlist.

    ``verify_agent_token`` 403s any resident-turn token hitting a path outside
    ``_RESIDENT_AGENT_ALLOWED_PATHS`` before the endpoint runs, so a new endpoint
    that isn't allowlisted is dead-on-arrival for the only tokens that call it.
    """
    from app.routers.agent import _RESIDENT_AGENT_ALLOWED_PATHS

    paths = {route.path for route in capability_router.router.routes}
    # Sanity-check the extraction against a known-allowlisted route, then assert
    # every route (including /mutate) is covered.
    assert "/api/agent/resident-turn/capability" in paths
    missing = paths - _RESIDENT_AGENT_ALLOWED_PATHS
    assert not missing, f"resident capability routes not token-allowlisted: {sorted(missing)}"


def _mutation_invocation(project_id, turn_task_id, **overrides):
    from app.strategic_resident_capability_invocation import CapabilityEvidenceRef

    payload = {
        "capability_id": "strategy.running_update.set",
        "project_id": project_id,
        "turn_task_id": turn_task_id,
        "authority_generation": 3,
        "parameters": {"running_update": "grounded working update"},
        "operation_id": "op-strategy-1",
        "expected_revisions": {
            "authority_generation": 3,
            "active_turn_task_version": 1,
            "current_checkpoint_id": str(uuid.uuid4()),
            "resident_checkpoint_version": 1,
            "resident_checkpoint_hash": "a" * 64,
            "running_update_revision": "b" * 64,
        },
        "evidence": (
            CapabilityEvidenceRef(
                kind="task",
                project_id=project_id,
                source_id=str(uuid.uuid4()),
                revision="task:v1",
                checksum="c" * 64,
                inspected_at=datetime.now(timezone.utc),
            ),
        ),
    }
    payload.update(overrides)
    return ResidentCapabilityInvocation.model_validate(payload)


def _mutation_token(project_id, turn_task_id):
    return {
        "sub": str(turn_task_id),
        "project_id": str(project_id),
        "vm_instance_id": "vm-1",
        "task_type": "strategic_resident_turn",
        "claim_nonce": "nonce-1",
    }


def _evidence_of_kinds(project_id, *kinds):
    from app.strategic_resident_capability_invocation import CapabilityEvidenceRef

    return tuple(
        CapabilityEvidenceRef(
            kind=kind,
            project_id=project_id,
            source_id=f"{kind}-{index}",
            revision=f"{kind}:v1",
            checksum=str(index) * 64,
            inspected_at=datetime.now(timezone.utc),
        )
        for index, kind in enumerate(kinds)
    )


@pytest.mark.asyncio
async def test_mutation_route_dispatches_project_field_capability(monkeypatch):
    project_id = uuid.uuid4()
    turn_id = uuid.uuid4()
    receipt = SimpleNamespace(
        operation_id="op-strategy-1", status="applied", id=uuid.uuid4()
    )
    executor = AsyncMock(return_value=receipt)
    monkeypatch.setattr(capability_router, "execute_fenced_db_operation", executor)
    monkeypatch.setattr(
        capability_router, "_check_evidence_refs_against_receipts", AsyncMock()
    )

    result = await capability_router.invoke_resident_mutation(
        None,
        _mutation_invocation(project_id, turn_id),
        session=SimpleNamespace(),
        token=_mutation_token(project_id, turn_id),
    )
    assert result["status"] == "applied"
    assert result["capability_id"] == "strategy.running_update.set"
    assert result["operation_id"] == "op-strategy-1"
    # The fenced request is built from the token authority + declared operation_kind.
    fenced_request = executor.await_args.args[1]
    assert fenced_request.project_id == project_id
    assert fenced_request.turn_task_id == turn_id
    assert fenced_request.operation_kind == "set_running_update"
    assert fenced_request.capability == "strategy.running_update.set"
    assert fenced_request.risk == "low"


@pytest.mark.parametrize(
    "receipt_status",
    [
        "pending",
        "applying",
        "applied",
        "reconciling",
        "reconciled",
        "failed",
        "unknown_effect",
    ],
)
def test_completion_projection_is_admissible_for_every_durable_status(
    receipt_status,
):
    from app.strategic_resident_turn_results import StrategicResidentTurnResult

    receipt_id = uuid.uuid4()
    unsettled = receipt_status in {
        "pending",
        "applying",
        "reconciling",
        "unknown_effect",
    }
    projection = capability_router._completion_projection(
        SimpleNamespace(
            id=receipt_id,
            operation_id="retry:worker-1",
            status=receipt_status,
            unknown_effects=(
                [
                    {
                        "effect_id": "requeue_task:retry:worker-1",
                        "kind": "requeue_task",
                    }
                ]
                if unsettled
                else []
            ),
        ),
        capability_id="task.retry",
    )
    assert projection["operation"]["status"] == (
        "applying" if unsettled else receipt_status
    )
    assert projection["receipt"]["status"] == receipt_status
    if unsettled:
        assert projection["unknown_effects"] == [
            {
                "effect_id": "requeue_task:retry:worker-1",
                "operation_id": "retry:worker-1",
                "receipt_id": str(receipt_id),
                "summary": "The external effect outcome is unknown and requires reconciliation.",
                "reconciliation_status": "requires_attention",
            }
        ]
        assert projection["required_attention"][0]["receipt_ids"] == [
            str(receipt_id)
        ]
    else:
        assert projection["unknown_effects"] == []
        assert projection["required_attention"] == []
    StrategicResidentTurnResult.model_validate(
        {
            "operations": [projection["operation"]],
            "receipts": [projection["receipt"]],
            "unknown_effects": projection["unknown_effects"],
            "required_attention": projection["required_attention"],
        }
    )


@pytest.mark.asyncio
async def test_mutation_route_rejects_body_authority_not_matching_token(monkeypatch):
    project_id = uuid.uuid4()
    turn_id = uuid.uuid4()
    executor = AsyncMock()
    monkeypatch.setattr(capability_router, "execute_fenced_db_operation", executor)
    # Body targets a different turn task than the signed token authorizes.
    with pytest.raises(HTTPException) as exc:
        await capability_router.invoke_resident_mutation(
            None,
            _mutation_invocation(project_id, uuid.uuid4()),
            session=SimpleNamespace(),
            token=_mutation_token(project_id, turn_id),
        )
    assert exc.value.status_code == 403
    executor.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("capability", "expected_kind"),
    [
        ("task.cancel", "cancel_task"),
        ("task.requeue", "requeue_task"),
        ("task.retry", "retry_task"),
        ("task.unblock", "unblock_task"),
    ],
)
async def test_task_lifecycle_routes_through_the_external_executor(
    monkeypatch, capability, expected_kind
):
    """cancel/requeue/retry/unblock run the operator modify path via the executor."""
    project_id = uuid.uuid4()
    turn_id = uuid.uuid4()
    receipt = SimpleNamespace(
        operation_id="op-life", status="reconciled", id=uuid.uuid4()
    )
    external = AsyncMock(return_value=receipt)
    monkeypatch.setattr(
        capability_router, "execute_external_effect_operation", external
    )
    monkeypatch.setattr(
        capability_router, "_check_evidence_refs_against_receipts", AsyncMock()
    )
    evidence_kind = {
        "task.cancel": "task",
        "task.requeue": "task",
        "task.retry": "task",
        "task.unblock": "task",
    }[capability]
    params = {"task_id": str(uuid.uuid4())}
    if capability == "task.cancel":
        params["reason"] = "held by strategy"
    invocation = _mutation_invocation(
        project_id,
        turn_id,
        capability_id=capability,
        parameters=params,
        evidence=_evidence_of_kinds(project_id, evidence_kind),
    )
    invocation.expected_revisions["target_task_updated_at"] = (
        "2026-07-18T00:00:00"
    )
    result = await capability_router.invoke_resident_mutation(
        None,
        invocation,
        session=SimpleNamespace(),
        token=_mutation_token(project_id, turn_id),
    )
    assert result["status"] == "reconciled"
    external.assert_awaited_once()
    fenced_request = external.await_args.args[1]
    assert fenced_request.capability == capability
    assert fenced_request.operation_kind == expected_kind


@pytest.mark.asyncio
async def test_external_effect_pr_create_stays_proposal_only(monkeypatch):
    """A partial adapter never makes an evidence-unreachable capability invokable."""
    project_id = uuid.uuid4()
    turn_id = uuid.uuid4()
    receipt = SimpleNamespace(
        operation_id="op-ext", status="reconciled", id=uuid.uuid4()
    )
    external = AsyncMock(return_value=receipt)
    monkeypatch.setattr(
        capability_router, "execute_external_effect_operation", external
    )
    invocation = _mutation_invocation(
        project_id,
        turn_id,
        capability_id="pr.create",
        parameters={"head": "feature", "base": "main", "title": "PR"},
        evidence=_evidence_of_kinds(project_id, "branch"),
    )
    with pytest.raises(HTTPException) as exc_info:
        await capability_router.invoke_resident_mutation(
            None,
            invocation,
            session=SimpleNamespace(),
            token=_mutation_token(project_id, turn_id),
        )
    assert exc_info.value.status_code == 501
    external.assert_not_awaited()


@pytest.mark.asyncio
async def test_external_effect_unwired_capability_stays_501(monkeypatch):
    """An unwired declared mutation stays proposal-only (501)."""
    project_id = uuid.uuid4()
    turn_id = uuid.uuid4()
    external = AsyncMock()
    monkeypatch.setattr(
        capability_router, "execute_external_effect_operation", external
    )
    # pr.update is a github_pulls capability with no wired adapter yet.
    invocation = _mutation_invocation(
        project_id,
        turn_id,
        capability_id="pr.update",
        parameters={"pr_number": 5},
        evidence=_evidence_of_kinds(project_id, "pr"),
    )
    with pytest.raises(HTTPException) as exc:
        await capability_router.invoke_resident_mutation(
            None,
            invocation,
            session=SimpleNamespace(),
            token=_mutation_token(project_id, turn_id),
        )
    assert exc.value.status_code == 501
    external.assert_not_awaited()


@pytest.mark.asyncio
async def test_mutation_route_501_for_capability_without_fenced_adapter(monkeypatch):
    project_id = uuid.uuid4()
    turn_id = uuid.uuid4()
    executor = AsyncMock()
    monkeypatch.setattr(capability_router, "execute_fenced_db_operation", executor)
    # task.park is a real mutate capability but has no fenced adapter yet.
    invocation = _mutation_invocation(
        project_id,
        turn_id,
        capability_id="task.park",
        parameters={"task_id": str(uuid.uuid4()), "reason": "held for review"},
    )
    with pytest.raises(HTTPException) as exc:
        await capability_router.invoke_resident_mutation(
            None,
            invocation,
            session=SimpleNamespace(),
            token=_mutation_token(project_id, turn_id),
        )
    assert exc.value.status_code == 501
    executor.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("error_name", "expected_status"),
    [
        ("OperationIdentityCollision", 409),
        ("InactiveTurnAuthority", 409),
        ("ExpectedRevisionMismatch", 409),
        ("AuthorityGenerationMismatch", 409),
        ("FencedOperationUsageError", 400),
    ],
)
async def test_mutation_route_maps_authority_errors(
    monkeypatch, error_name, expected_status
):
    from app import strategic_operation_authority_validation as authority

    project_id = uuid.uuid4()
    turn_id = uuid.uuid4()
    error_cls = getattr(authority, error_name)
    executor = AsyncMock(side_effect=error_cls("fenced rejection"))
    monkeypatch.setattr(capability_router, "execute_fenced_db_operation", executor)
    monkeypatch.setattr(
        capability_router, "_check_evidence_refs_against_receipts", AsyncMock()
    )
    with pytest.raises(HTTPException) as exc:
        await capability_router.invoke_resident_mutation(
            None,
            _mutation_invocation(project_id, turn_id),
            session=SimpleNamespace(),
            token=_mutation_token(project_id, turn_id),
        )
    assert exc.value.status_code == expected_status


@pytest.mark.asyncio
async def test_mutation_route_rejects_evidence_ref_not_backed_by_inspection(monkeypatch):
    """Evidence refs not matching any inspection receipt for this turn are rejected 403."""
    project_id = uuid.uuid4()
    turn_id = uuid.uuid4()
    executor = AsyncMock()
    monkeypatch.setattr(capability_router, "execute_fenced_db_operation", executor)

    # Session returns no receipts — evidence ref has no backing real inspection.
    execute_result = MagicMock()
    execute_result.scalars.return_value.all.return_value = []
    session = AsyncMock()
    session.scalar.return_value = {"_resident_turn_context_hash": "ctx-hash"}
    session.execute.return_value = execute_result

    with pytest.raises(HTTPException) as exc:
        await capability_router.invoke_resident_mutation(
            None,
            _mutation_invocation(project_id, turn_id),
            session=session,
            token=_mutation_token(project_id, turn_id),
        )
    assert exc.value.status_code == 403
    assert "evidence ref" in exc.value.detail
    executor.assert_not_awaited()
    session.rollback.assert_awaited()


@pytest.mark.asyncio
async def test_mutation_route_accepts_evidence_refs_matching_inspection_receipts(monkeypatch):
    """Evidence refs whose (kind, source_id, revision, checksum) appear in a real
    inspection receipt for this turn are accepted and the mutation proceeds."""
    project_id = uuid.uuid4()
    turn_id = uuid.uuid4()

    invocation = _mutation_invocation(project_id, turn_id)
    ref = invocation.evidence[0]

    # Build a SimpleNamespace receipt that backs the evidence ref above.
    receipt_row = SimpleNamespace(
        capability="task.inspect",
        operation_kind="inspect_task",
        status="applied",
        expected_revisions={"context_hash": "ctx-hash"},
        receipt={
            "structured_result": {
                "evidence": [
                    {
                        "kind": ref.kind,
                        "source_id": ref.source_id,
                        "revision": ref.revision,
                        "checksum": ref.checksum,
                    }
                ]
            }
        },
    )
    execute_result = MagicMock()
    execute_result.scalars.return_value.all.return_value = [receipt_row]
    session = AsyncMock()
    session.scalar.return_value = {"_resident_turn_context_hash": "ctx-hash"}
    session.execute.return_value = execute_result

    db_receipt = SimpleNamespace(operation_id="op-strategy-1", status="applied", id=uuid.uuid4())
    executor = AsyncMock(return_value=db_receipt)
    monkeypatch.setattr(capability_router, "execute_fenced_db_operation", executor)

    result = await capability_router.invoke_resident_mutation(
        None,
        invocation,
        session=session,
        token=_mutation_token(project_id, turn_id),
    )
    assert result["status"] == "applied"
    executor.assert_awaited_once()
    session.rollback.assert_awaited()


@pytest.mark.asyncio
async def test_build_receipt_evidence_set_extracts_only_successful_inspection_receipts():
    """_build_receipt_evidence_set only trusts applied/reconciled inspection receipts."""
    from app.routers.agent_resident_capabilities import _build_receipt_evidence_set

    valid_key = ("task", "task-id-1", "v1", "a" * 64)
    valid_receipt = SimpleNamespace(
        capability="task.inspect",
        operation_kind="inspect_task",
        status="applied",
        expected_revisions={"context_hash": "ctx-hash"},
        receipt={
            "structured_result": {
                "evidence": [
                    {"kind": "task", "source_id": "task-id-1", "revision": "v1", "checksum": "a" * 64}
                ]
            }
        },
    )
    # Pending receipt: should be excluded.
    pending_receipt = SimpleNamespace(
        capability="task.inspect",
        operation_kind="inspect_task",
        status="pending",
        expected_revisions={"context_hash": "ctx-hash"},
        receipt={"structured_result": {"evidence": [
            {"kind": "task", "source_id": "task-id-2", "revision": "v2", "checksum": "b" * 64}
        ]}},
    )
    # Wrong operation_kind (mutation, not inspection): should be excluded.
    mutate_receipt = SimpleNamespace(
        capability="strategy.running_update.set",
        operation_kind="set_running_update",
        status="applied",
        expected_revisions={"context_hash": "ctx-hash"},
        receipt={"structured_result": {"evidence": [
            {"kind": "task", "source_id": "task-id-3", "revision": "v3", "checksum": "c" * 64}
        ]}},
    )
    result = _build_receipt_evidence_set(
        [valid_receipt, pending_receipt, mutate_receipt],
        expected_context_hash="ctx-hash",
    )
    assert valid_key in result
    assert ("task", "task-id-2", "v2", "b" * 64) not in result
    assert ("task", "task-id-3", "v3", "c" * 64) not in result


@pytest.mark.asyncio
async def test_lineage_inspection_stays_in_project_and_is_stable(db_session):
    project = Project(name="lineage", user_id="u")
    other = Project(name="other", user_id="u")
    db_session.add_all([project, other])
    await db_session.flush()
    parent = Task(project_id=project.id, title="parent", payloads=[{"type": "plan"}])
    db_session.add(parent)
    await db_session.flush()
    child = Task(
        project_id=project.id,
        parent_task_id=parent.id,
        title="child",
        payloads=[{"type": "create"}],
    )
    foreign = Task(
        project_id=other.id,
        parent_task_id=parent.id,
        title="foreign",
        payloads=[{"type": "fix"}],
    )
    db_session.add_all([child, foreign])
    await db_session.commit()

    evidence = await inspect_project_task_lineage(
        db_session,
        project_id=project.id,
        task_id=parent.id,
    )
    assert [item["title"] for item in evidence.data["tasks"]] == ["parent", "child"]
    assert "foreign" not in str(evidence.data)


def test_explicit_policy_invocation_uses_durable_approval_not_message_ref():
    """The exact durable approval is verified by the mutation route."""
    project_id = uuid.uuid4()
    base = {
        "capability_id": "pr.merge",
        "project_id": project_id,
        "turn_task_id": uuid.uuid4(),
        "authority_generation": 1,
        "operation_id": "merge-op",
        "parameters": {"pr_number": 42, "expected_head_sha": "abc1234"},
        "expected_revisions": {"pr_version": 1},
        "evidence": tuple(
            {
                "kind": kind,
                "project_id": project_id,
                "source_id": "pr:42",
                "revision": "abc1234",
                "checksum": "a" * 64,
                "inspected_at": "2026-07-17T12:00:00Z",
            }
                for kind in ("pr", "pr_diff", "pr_check")
        ),
    }
    invocation = ResidentCapabilityInvocation.model_validate(base)
    assert invocation.capability_id == "pr.merge"
    assert not hasattr(invocation, "approval_ref")

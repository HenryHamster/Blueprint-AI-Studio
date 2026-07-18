"""Two-phase external-effect executor + gate (M4 increment 4).

Covers the durable-intent -> perform -> settle lifecycle, crash-recovery probe,
idempotency, authority fencing, and the default-off arming gate. The external
effect is stubbed (no real GitHub I/O) so the executor's receipt lifecycle is
exercised in isolation.
"""

import hashlib

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool
from sqlmodel import SQLModel, select

from app.models import (
    Project,
    StrategicOperationReceipt,
    StrategicResident,
    StrategicResidentCheckpoint,
    Task,
    TaskStatus,
)
from app.strategic_operation_authority_validation import (
    FencedOperationRequest,
    InactiveTurnAuthority,
    OperationIdentityCollision,
)
from app.strategic_operation_external_effect import (
    ExternalEffectNotPerformed,
    execute_external_effect_operation,
)


@pytest_asyncio.fixture()
async def db():
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    async with engine.begin() as conn:
        await conn.run_sync(SQLModel.metadata.create_all)
    factory = sessionmaker(
        engine, class_=AsyncSession, expire_on_commit=False, autoflush=False
    )
    async with factory() as session:
        yield session
    await engine.dispose()


async def _rows(session, *, generation=3, owner="strategic_resident", authorized=True):
    project = Project(name="X", user_id="op", running_update="steady")
    session.add(project)
    await session.commit()
    checkpoint = StrategicResidentCheckpoint(
        project_id=project.id,
        version=1,
        content_hash=hashlib.sha256(b'{"schema_version":1}').hexdigest(),
        checkpoint={"schema_version": 1},
        authority_generation=generation,
    )
    session.add(checkpoint)
    await session.commit()
    task = Task(
        project_id=project.id,
        status=TaskStatus.IN_PROGRESS,
        payloads=[
            {
                "type": "strategic_resident_turn",
                "resident_id": str(project.id),
                "authority_generation": generation,
                "engine_owner": owner,
                "checkpoint_id": str(checkpoint.id),
                "checkpoint_version": checkpoint.version,
                "checkpoint_hash": checkpoint.content_hash,
            }
        ],
        context={
            "_resident_mutation_authorized": authorized,
            "_resident_input_frozen": True,
            "_resident_project_id": str(project.id),
            "_resident_authority_generation": generation,
            "_resident_engine_owner_observed": owner,
            "_resident_shadow_only": False,
        },
    )
    session.add(task)
    await session.commit()
    await session.refresh(task)
    resident = StrategicResident(
        project_id=project.id,
        engine_owner=owner,
        engine_version="v2",
        authority_generation=generation,
        active_turn_task_id=task.id,
        active_turn_lane="interactive",
        current_checkpoint_id=checkpoint.id,
    )
    session.add(resident)
    await session.commit()
    return project, task, checkpoint


def _request(project, task, checkpoint, *, operation_id="ext-1"):
    return FencedOperationRequest(
        project_id=project.id,
        turn_task_id=task.id,
        authority_generation=3,
        operation_id=operation_id,
        operation_kind="create_pull_request",
        capability="pr.create",
        risk="medium",
        expected_revisions={
            "authority_generation": 3,
            "active_turn_task_version": int(task.version),
            "current_checkpoint_id": str(checkpoint.id),
            "resident_checkpoint_version": checkpoint.version,
            "resident_checkpoint_hash": checkpoint.content_hash,
        },
        request_payload={"head": "feature", "base": "main", "title": "PR"},
    )


@pytest.mark.asyncio
async def test_success_settles_to_reconciled(db):
    project, task, checkpoint = await _rows(db)
    calls = []

    async def perform(_session):
        calls.append(1)
        return {"pr_number": 42}

    receipt = await execute_external_effect_operation(
        db, _request(project, task, checkpoint), perform=perform, effect_kind="create_pull_request"
    )
    assert receipt.status == "reconciled"
    assert receipt.receipt["structured_result"]["external_effect"]["pr_number"] == 42
    assert receipt.unknown_effects == []
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_perform_error_requires_unknown_effect_reconciliation(db):
    project, task, checkpoint = await _rows(db)

    async def perform(_session):
        raise RuntimeError("github 503")

    receipt = await execute_external_effect_operation(
        db, _request(project, task, checkpoint), perform=perform, effect_kind="create_pull_request"
    )
    assert receipt.status == "unknown_effect"
    assert "github 503" in receipt.receipt["structured_result"]["error"]
    assert receipt.completed_at is None
    assert receipt.unknown_effects and receipt.unknown_effects[0]["effect_id"]

    calls = []

    async def must_not_repeat(_session):
        calls.append(1)
        return {}

    replay = await execute_external_effect_operation(
        db,
        _request(project, task, checkpoint),
        perform=must_not_repeat,
        effect_kind="create_pull_request",
    )
    assert replay.status == "unknown_effect"
    assert calls == []


@pytest.mark.asyncio
async def test_proven_pre_effect_failure_is_terminal_and_clears_unknown(db):
    project, task, checkpoint = await _rows(db)

    async def perform(_session):
        raise ExternalEffectNotPerformed("target revision changed")

    receipt = await execute_external_effect_operation(
        db,
        _request(project, task, checkpoint),
        perform=perform,
        effect_kind="create_pull_request",
    )
    assert receipt.status == "failed"
    assert receipt.completed_at is not None
    assert receipt.unknown_effects == []


@pytest.mark.asyncio
async def test_idempotent_replay_adopts_terminal_receipt(db):
    project, task, checkpoint = await _rows(db)
    calls = []

    async def perform(_session):
        calls.append(1)
        return {"pr_number": 7}

    req = _request(project, task, checkpoint)
    first = await execute_external_effect_operation(
        db, req, perform=perform, effect_kind="create_pull_request"
    )
    second = await execute_external_effect_operation(
        db, req, perform=perform, effect_kind="create_pull_request"
    )
    assert first.id == second.id
    assert second.status == "reconciled"
    assert len(calls) == 1  # perform not re-run for a terminal receipt
    all_receipts = (await db.execute(select(StrategicOperationReceipt))).scalars().all()
    assert len(all_receipts) == 1


@pytest.mark.asyncio
async def test_stranded_applying_receipt_is_probed_and_settled(db):
    project, task, checkpoint = await _rows(db)
    req = _request(project, task, checkpoint)
    from app.strategic_operation_authority_validation import (
        _request_fingerprint,
        _validated_request_payload,
    )

    fingerprint = _request_fingerprint(
        req, request_payload=_validated_request_payload(req.request_payload)
    )
    # Simulate a crash between perform and settle: a durably-`applying` receipt.
    db.add(
        StrategicOperationReceipt(
            project_id=project.id,
            turn_task_id=task.id,
            authority_generation=3,
            operation_id="ext-1",
            operation_kind="create_pull_request",
            capability="pr.create",
            risk_level="medium",
            request_hash=fingerprint,
            status="applying",
            expected_revisions=dict(req.expected_revisions),
            unknown_effects=[{"effect_id": "create_pull_request:ext-1"}],
        )
    )
    await db.commit()

    async def perform(_session):
        return {"pr_number": 99}  # idempotent re-run returns the existing PR

    receipt = await execute_external_effect_operation(
        db, req, perform=perform, effect_kind="create_pull_request"
    )
    assert receipt.status == "reconciled"
    assert receipt.receipt["structured_result"]["external_effect"]["pr_number"] == 99
    all_receipts = (await db.execute(select(StrategicOperationReceipt))).scalars().all()
    assert len(all_receipts) == 1  # settled the stranded row, no duplicate


@pytest.mark.asyncio
async def test_in_flight_reconciling_receipt_is_not_re_performed(db):
    """A receipt already claimed (reconciling) is returned without performing.

    This is the single-owner guarantee: a worker that finds the perform already
    claimed by another does not create a second external effect.
    """
    project, task, checkpoint = await _rows(db)
    req = _request(project, task, checkpoint)
    from app.strategic_operation_authority_validation import (
        _request_fingerprint,
        _validated_request_payload,
    )

    fingerprint = _request_fingerprint(
        req, request_payload=_validated_request_payload(req.request_payload)
    )
    db.add(
        StrategicOperationReceipt(
            project_id=project.id,
            turn_task_id=task.id,
            authority_generation=3,
            operation_id="ext-1",
            operation_kind="create_pull_request",
            capability="pr.create",
            risk_level="medium",
            request_hash=fingerprint,
            status="reconciling",  # another worker owns the in-flight perform
            expected_revisions=dict(req.expected_revisions),
            unknown_effects=[{"effect_id": "create_pull_request:ext-1"}],
        )
    )
    await db.commit()
    ran = []

    async def perform(_session):
        ran.append(1)
        return {"pr_number": 1}

    receipt = await execute_external_effect_operation(
        db, req, perform=perform, effect_kind="create_pull_request"
    )
    assert receipt.status == "reconciling"
    assert ran == []  # did NOT perform a second external effect


@pytest.mark.asyncio
async def test_failed_error_body_scrubs_secrets(db):
    project, task, checkpoint = await _rows(db)

    async def perform(_session):
        raise RuntimeError(
            "401 for https://api.github.com with Bearer ghp_" + "A" * 36
        )

    receipt = await execute_external_effect_operation(
        db, _request(project, task, checkpoint), perform=perform, effect_kind="create_pull_request"
    )
    assert receipt.status == "unknown_effect"
    error = receipt.receipt["structured_result"]["error"]
    assert "ghp_" not in error
    assert "Bearer ghp" not in error
    assert "[redacted]" in error


@pytest.mark.asyncio
async def test_critical_risk_external_operation_reaches_the_executor(db):
    project, task, checkpoint = await _rows(db)
    request = _request(project, task, checkpoint)
    request = FencedOperationRequest(**{**request.__dict__, "risk": "critical"})

    async def perform(_session):
        return {"merged": True}

    receipt = await execute_external_effect_operation(
        db, request, perform=perform, effect_kind="merge_pull_request"
    )
    assert receipt.status == "reconciled"
    assert receipt.risk_level == "critical"


@pytest.mark.asyncio
async def test_operation_id_reuse_different_request_collides(db):
    project, task, checkpoint = await _rows(db)

    async def perform(_session):
        return {"pr_number": 1}

    await execute_external_effect_operation(
        db, _request(project, task, checkpoint, operation_id="dup"), perform=perform, effect_kind="create_pull_request"
    )
    other = _request(project, task, checkpoint, operation_id="dup")
    other = FencedOperationRequest(
        **{**other.__dict__, "request_payload": {"head": "x", "base": "main", "title": "diff"}}
    )
    with pytest.raises(OperationIdentityCollision):
        await execute_external_effect_operation(
            db, other, perform=perform, effect_kind="create_pull_request"
        )


@pytest.mark.asyncio
async def test_stale_generation_fails_closed_without_performing(db):
    project, task, checkpoint = await _rows(db, generation=5)
    ran = []

    async def perform(_session):
        ran.append(1)
        return {}

    req = _request(project, task, checkpoint)  # request carries generation=3
    with pytest.raises(InactiveTurnAuthority.__mro__[1]):  # StrategicAuthorityError
        await execute_external_effect_operation(
            db, req, perform=perform, effect_kind="create_pull_request"
        )
    assert ran == []  # never performed the external effect
    assert (await db.execute(select(StrategicOperationReceipt))).scalar_one_or_none() is None


@pytest.mark.asyncio
async def test_pr_merge_adapter_forwards_the_approved_head_sha(monkeypatch):
    """The pr.merge perform must pin the approved head sha at merge time.

    Guards the approve-then-push TOCTOU: without this, a head that advanced after
    the operator's approval would be merged (unreviewed code lands on trunk).
    """
    import uuid as _uuid

    from app import github_pulls, github_token_resolver, rollup_migration_validation
    from app.strategic_resident_external_effect_adapters import build_external_effect

    captured = {}

    async def _fake_token(task_id, session):
        return ("ghs_token", "owner", "repo")

    async def _fake_merge(token, owner, repo, pr_number, **kwargs):
        captured["pr_number"] = pr_number
        captured["expected_head_sha"] = kwargs.get("expected_head_sha")
        return {"merged": True, "sha": kwargs.get("expected_head_sha")}

    async def _fake_get_pr(token, owner, repo, pr_number, **kwargs):
        return {"base": {"ref": "automain"}}

    async def _fake_guard(token, owner, repo, pr_number, target_branch, **kwargs):
        captured["guard_target_branch"] = target_branch
        return (True, None)  # ok verdict -> merge proceeds

    monkeypatch.setattr(github_token_resolver, "get_github_token_for_task", _fake_token)
    monkeypatch.setattr(github_pulls, "get_pull_request", _fake_get_pr)
    monkeypatch.setattr(github_pulls, "merge_pull_request", _fake_merge)
    monkeypatch.setattr(
        rollup_migration_validation, "enforce_premerge_migration_guard", _fake_guard
    )

    perform = build_external_effect(
        "pr.merge",
        project_id=_uuid.uuid4(),
        turn_task_id=_uuid.uuid4(),
        parameters={"pr_number": 42, "expected_head_sha": "abc1234"},
    )
    result = await perform(None)
    assert captured["pr_number"] == 42
    assert captured["expected_head_sha"] == "abc1234"  # the approved sha is pinned
    assert captured["guard_target_branch"] == "automain"  # PR base fed to the guard
    assert result["merged"] is True


async def test_pr_merge_adapter_fails_closed_when_migration_guard_blocks(monkeypatch):
    """A block/defer verdict from the pre-merge dual-head guard must abort the merge.

    Without the guard an autonomous resident merge could land a migration-colliding
    PR onto an integration/promotion branch and wedge ``alembic upgrade head``.
    The merge primitive must never be reached on a non-``ok`` verdict.
    """
    import uuid as _uuid

    from app import github_pulls, github_token_resolver, rollup_migration_validation
    from app.strategic_resident_external_effect_adapters import build_external_effect

    merge_attempted = {"value": False}

    async def _fake_token(task_id, session):
        return ("ghs_token", "owner", "repo")

    async def _fake_get_pr(token, owner, repo, pr_number, **kwargs):
        return {"base": {"ref": "automain"}}

    async def _fake_merge(token, owner, repo, pr_number, **kwargs):
        merge_attempted["value"] = True
        return {"merged": True}

    async def _fake_guard(token, owner, repo, pr_number, target_branch, **kwargs):
        return (False, "duplicate revision id 0631 — combined chain has two heads")

    monkeypatch.setattr(github_token_resolver, "get_github_token_for_task", _fake_token)
    monkeypatch.setattr(github_pulls, "get_pull_request", _fake_get_pr)
    monkeypatch.setattr(github_pulls, "merge_pull_request", _fake_merge)
    monkeypatch.setattr(
        rollup_migration_validation, "enforce_premerge_migration_guard", _fake_guard
    )

    perform = build_external_effect(
        "pr.merge",
        project_id=_uuid.uuid4(),
        turn_task_id=_uuid.uuid4(),
        parameters={"pr_number": 42, "expected_head_sha": "abc1234"},
    )
    with pytest.raises(RuntimeError, match="migration guard blocked"):
        await perform(None)
    assert merge_attempted["value"] is False  # merge primitive never reached

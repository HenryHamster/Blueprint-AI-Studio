"""Operator-approval verification for explicit-approval Resident operations."""

from datetime import timedelta

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool
from sqlmodel import SQLModel, select

from app.models import Project, StrategicOperationApproval
from app.strategic_resident_operation_approval import (
    OperatorApprovalRequired,
    capability_requires_operator_approval,
    derive_approval_key,
    grant_operation_approval,
    verify_and_consume_operation_approval,
)
from app.time_utils import _utcnow


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


async def _project(db):
    # StrategicOperationApproval.project_id FKs strategicresident.project_id, but
    # SQLite does not enforce FKs by default here, so a bare project id suffices.
    project = Project(name="Approvals", user_id="op")
    db.add(project)
    await db.commit()
    return project


def test_derive_approval_key_binds_parameters():
    key_a = derive_approval_key("pr.merge", {"pr_number": 12, "expected_head_sha": "abc"})
    key_b = derive_approval_key("pr.merge", {"pr_number": 13, "expected_head_sha": "abc"})
    key_c = derive_approval_key("pr.merge", {"pr_number": 12, "expected_head_sha": "xyz"})
    assert key_a != key_b  # different PR
    assert key_a != key_c  # different head sha
    # Deterministic.
    assert key_a == derive_approval_key(
        "pr.merge", {"pr_number": 12, "expected_head_sha": "abc"}
    )


def test_only_declared_capabilities_are_approvable():
    assert capability_requires_operator_approval("pr.merge")
    assert capability_requires_operator_approval("deployment.deploy")
    assert not capability_requires_operator_approval("pr.create")
    assert not capability_requires_operator_approval("task.reprioritize")


@pytest.mark.asyncio
async def test_grant_then_consume_succeeds(db):
    project = await _project(db)
    key = derive_approval_key("pr.merge", {"pr_number": 12, "expected_head_sha": "abc"})
    await grant_operation_approval(
        db,
        project_id=project.id,
        capability="pr.merge",
        approval_key=key,
        granted_by="operator-1",
        ttl_seconds=600,
    )
    await db.commit()

    consumed = await verify_and_consume_operation_approval(
        db,
        project_id=project.id,
        capability="pr.merge",
        approval_key=key,
        operation_id="op-1",
    )
    assert consumed.consumed_operation_id == "op-1"
    assert consumed.consumed_at is not None


@pytest.mark.asyncio
async def test_consume_is_idempotent_for_same_operation_but_not_another(db):
    project = await _project(db)
    key = derive_approval_key("pr.merge", {"pr_number": 12, "expected_head_sha": "abc"})
    await grant_operation_approval(
        db, project_id=project.id, capability="pr.merge", approval_key=key,
        granted_by="op", ttl_seconds=600,
    )
    await db.commit()

    first = await verify_and_consume_operation_approval(
        db, project_id=project.id, capability="pr.merge", approval_key=key, operation_id="op-1"
    )
    # Same operation retries -> the same approval, no second consumption.
    again = await verify_and_consume_operation_approval(
        db, project_id=project.id, capability="pr.merge", approval_key=key, operation_id="op-1"
    )
    assert again.id == first.id
    # A DIFFERENT operation cannot reuse the consumed approval.
    with pytest.raises(OperatorApprovalRequired):
        await verify_and_consume_operation_approval(
            db, project_id=project.id, capability="pr.merge", approval_key=key, operation_id="op-2"
        )


@pytest.mark.asyncio
async def test_missing_approval_is_rejected(db):
    project = await _project(db)
    key = derive_approval_key("pr.merge", {"pr_number": 99, "expected_head_sha": "z"})
    with pytest.raises(OperatorApprovalRequired):
        await verify_and_consume_operation_approval(
            db, project_id=project.id, capability="pr.merge", approval_key=key, operation_id="op-x"
        )


@pytest.mark.asyncio
async def test_wrong_key_is_rejected(db):
    project = await _project(db)
    granted_key = derive_approval_key(
        "pr.merge", {"pr_number": 12, "expected_head_sha": "abc"}
    )
    await grant_operation_approval(
        db, project_id=project.id, capability="pr.merge", approval_key=granted_key,
        granted_by="op", ttl_seconds=600,
    )
    await db.commit()
    # Operator approved PR 12, but the Resident tries to merge PR 13.
    attacker_key = derive_approval_key(
        "pr.merge", {"pr_number": 13, "expected_head_sha": "abc"}
    )
    with pytest.raises(OperatorApprovalRequired):
        await verify_and_consume_operation_approval(
            db, project_id=project.id, capability="pr.merge", approval_key=attacker_key, operation_id="op-1"
        )


@pytest.mark.asyncio
async def test_expired_approval_is_rejected(db):
    project = await _project(db)
    key = derive_approval_key("pr.merge", {"pr_number": 12, "expected_head_sha": "abc"})
    past = _utcnow() - timedelta(hours=2)
    db.add(
        StrategicOperationApproval(
            project_id=project.id,
            capability="pr.merge",
            approval_key=key,
            granted_by="op",
            granted_at=past,
            expires_at=past + timedelta(minutes=1),  # already expired
        )
    )
    await db.commit()
    with pytest.raises(OperatorApprovalRequired):
        await verify_and_consume_operation_approval(
            db, project_id=project.id, capability="pr.merge", approval_key=key, operation_id="op-1"
        )
    # It was not consumed.
    row = (await db.execute(select(StrategicOperationApproval))).scalar_one()
    assert row.consumed_at is None

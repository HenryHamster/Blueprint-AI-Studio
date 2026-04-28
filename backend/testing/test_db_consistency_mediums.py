import uuid

import pytest
from sqlalchemy import CheckConstraint, UniqueConstraint
from sqlalchemy.exc import IntegrityError

from app.models import (
    Project, ResidualCaseStatus, ResidualRemediationCase,
    Task, TaskStatus, UserIntegration,
)
from app.orchestrator import _lock_provisioning_task_for_vm
from app.review_pipeline import _get_active_pipeline_task_by_key, _pipeline_task_idempotency_key


def test_userintegration_user_id_has_named_unique_constraint():
    constraints = UserIntegration.__table__.constraints
    assert any(
        isinstance(constraint, UniqueConstraint)
        and constraint.name == "uq_userintegration_user_id"
        and [column.name for column in constraint.columns] == ["user_id"]
        for constraint in constraints
    )


def test_task_active_idempotency_key_index_declared():
    index = next(
        idx for idx in Task.__table__.indexes
        if idx.name == "ux_task_active_idempotency_key"
    )
    assert index.unique is True
    assert [expr.name for expr in index.expressions] == ["idempotency_key"]


def test_pipeline_task_idempotency_key_is_stable_for_same_spawn():
    project_id = uuid.uuid4()
    first = _pipeline_task_idempotency_key(
        project_id=project_id,
        task_type="review",
        pr_number=123,
        original_task_id="orig",
        iteration=1,
        is_rollup=False,
    )
    second = _pipeline_task_idempotency_key(
        project_id=project_id,
        task_type="review",
        pr_number=123,
        original_task_id="orig",
        iteration=1,
        is_rollup=False,
    )
    assert first == second
    assert first != _pipeline_task_idempotency_key(
        project_id=project_id,
        task_type="fix",
        pr_number=123,
        original_task_id="orig",
        iteration=1,
        is_rollup=False,
    )


@pytest.mark.asyncio
async def test_active_pipeline_task_lookup_ignores_terminal_same_key(db_session):
    project = Project(name="Consistency", user_id="user-1")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    key = "review_pipeline:test-key"
    completed = Task(
        project_id=project.id,
        title="old completed review",
        status=TaskStatus.COMPLETED,
        idempotency_key=key,
        payloads=[{"type": "review", "pr_number": 1, "original_task_id": "orig"}],
    )
    active = Task(
        project_id=project.id,
        title="active review",
        status=TaskStatus.QUEUED,
        idempotency_key=key,
        payloads=[{"type": "review", "pr_number": 1, "original_task_id": "orig"}],
    )
    db_session.add_all([completed, active])
    await db_session.commit()

    found = await _get_active_pipeline_task_by_key(db_session, key)

    assert found is not None
    assert found.id == active.id


@pytest.mark.asyncio
async def test_provisioning_vm_lock_rejects_stale_vm_result(db_session):
    project = Project(name="Consistency", user_id="user-1")
    task = Task(
        project_id=project.id,
        title="requeued task",
        status=TaskStatus.QUEUED,
        vm_instance_id="agent-vm-new",
        payloads=[{"type": "create", "prompt": "x"}],
    )
    db_session.add(project)
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    locked = await _lock_provisioning_task_for_vm(db_session, task.id, "agent-vm-old")

    assert locked is None


# ── ResidualRemediationCase lifecycle (migration 0054) ──────────────────────

def test_residual_case_status_enum_includes_lifecycle_values():
    """Migration 0054 added REMEDIATING/MERGED/FAILED/SUSPENDED for the
    autonomous-remediation lifecycle, mirroring CI auto-fix states.
    Existing passive states (ACTIVE/IGNORED/CLOSED_ORGANICALLY/RESOLVED)
    must remain — both the reaper auto-close path and the IGNORED admin
    path keep using them."""
    values = {s.value for s in ResidualCaseStatus}
    # Pre-existing
    assert {"ACTIVE", "IGNORED", "CLOSED_ORGANICALLY", "RESOLVED"} <= values
    # Lifecycle additions
    assert {"REMEDIATING", "MERGED", "FAILED", "SUSPENDED"} <= values


def test_residual_case_status_check_constraint_declared():
    """The CHECK constraint gates the column to known states so a typo
    in a notification handler can't write a junk status that the admin
    UI's filter would silently miss."""
    constraints = ResidualRemediationCase.__table__.constraints
    assert any(
        isinstance(c, CheckConstraint)
        and c.name == "ck_residualremediationcase_status"
        for c in constraints
    )


def test_residual_case_unique_constraint_on_project_fingerprint():
    """The UNIQUE on (project_id, fingerprint) is what makes the spawn-
    dedup check single-row instead of a SELECT-then-aggregate. Without
    it, concurrent record_residual_fingerprint writes could create two
    rows for the same fingerprint and the cooldown check would miss one."""
    constraints = ResidualRemediationCase.__table__.constraints
    assert any(
        isinstance(c, UniqueConstraint)
        and c.name == "ux_residualremediationcase_project_fingerprint"
        and [col.name for col in c.columns] == ["project_id", "fingerprint"]
        for c in constraints
    )


def test_residual_case_lifecycle_columns_declared():
    """The lifecycle column set must be wired on the model so SQLModel
    serializes them on insert/update — a missing column here would mean
    we read the right shape from the DB but never write it back."""
    cols = ResidualRemediationCase.__table__.columns
    expected = {
        "last_spawned_task_id", "last_spawned_pr_number", "last_spawned_at",
        "consecutive_failure_count", "suspended_at",
        "sample_messages", "sample_statuses", "task_ids",
    }
    assert expected <= set(cols.keys())


def test_residual_case_last_spawned_task_fk_set_null_on_task_delete():
    """If a remediation task gets deleted (rare — happens via Task FK
    cascade from project deletion), the case row must NOT cascade-
    delete with it. The case is still useful state about the
    fingerprint independent of the historical spawn task."""
    fks = list(
        ResidualRemediationCase.__table__.c.last_spawned_task_id.foreign_keys
    )
    assert len(fks) == 1
    assert fks[0].target_fullname == "task.id"
    assert fks[0].ondelete == "SET NULL"


@pytest.mark.asyncio
async def test_residual_case_unique_constraint_blocks_duplicate(db_session):
    """End-to-end: two inserts with the same (project_id, fingerprint)
    fail at the DB layer. This is the contract the dedup check relies on."""
    project = Project(name="Residual Dedup", user_id="user-1")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    case_a = ResidualRemediationCase(
        project_id=project.id, fingerprint="fp-abc",
    )
    db_session.add(case_a)
    await db_session.commit()

    case_b = ResidualRemediationCase(
        project_id=project.id, fingerprint="fp-abc",
    )
    db_session.add(case_b)
    with pytest.raises(IntegrityError):
        await db_session.commit()
    await db_session.rollback()


@pytest.mark.asyncio
async def test_residual_case_status_check_blocks_unknown_value(db_session):
    """A misspelled status (e.g. 'remediated' for 'REMEDIATING') gets
    rejected at the DB layer instead of polluting the table."""
    project = Project(name="Residual Status Check", user_id="user-1")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    case = ResidualRemediationCase(
        project_id=project.id, fingerprint="fp-xyz", status="remediated",
    )
    db_session.add(case)
    with pytest.raises(IntegrityError):
        await db_session.commit()
    await db_session.rollback()

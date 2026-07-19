"""Project deletion cascade regression tests.

Audit 2026-04-29 (Phase A2 / finding C1+M-S3): project deletion would
FK-violate before this fix because several Project-FK tables had no
ondelete clause and the ORM cascade only covered Project.tasks. Migration
0056 normalizes ON DELETE CASCADE across every Project-FK table and adds
the missing ``alert.task_id`` FK with ON DELETE SET NULL.

These tests are gated to PostgreSQL because:
 - SQLite (default test path) does not enforce FK ondelete unless
   ``PRAGMA foreign_keys = ON`` is set, which the project doesn't.
 - The conftest cleanup loop already deletes child tables explicitly
   before deleting their parent; that path bypasses cascade entirely.
The Postgres path is the production reality, and the only path where
the cascade vs. caller-side-cleanup distinction actually matters.
"""

import os
import uuid

import pytest
from sqlalchemy import delete as sql_delete, select, text

from testing._resident_seed import seed_without_authority_enforcement

from app.models import (
    Alert,
    AnomalyFingerprintState,
    Artifact,
    ArtifactType,
    ComputeJob,
    MessagesLastSeen,
    MessagesNoisePrefs,
    MessagesSavedViews,
    MigrationReservation,
    PoolIdleCostLedger,
    PoolVm,
    Project,
    ProjectMember,
    ProjectMessage,
    ResidualCaseStatus,
    ResidualRemediationCase,
    StrategicCycle,
    StrategicOperationReceipt,
    StrategicResident,
    StrategicResidentCheckpoint,
    SystemMaturity,
    Task,
    TaskStatus,
)


USE_POSTGRES = os.getenv("USE_TEST_POSTGRES", "").lower() == "true" and os.getenv(
    "DATABASE_URL", ""
).startswith("postgresql")


pytestmark = pytest.mark.skipif(
    not USE_POSTGRES,
    reason="FK cascade behavior is Postgres-specific (SQLite default has FKs off)",
)


@pytest.mark.asyncio
async def test_project_delete_cascades_all_dependents(db_session):
    """Deleting a Project must cascade to every Project-FK child row, and
    must NULL out alert.task_id when the parent task is gone (vs. dropping
    the alert)."""
    project = Project(name="cascade-test", user_id="user-cascade-test")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    # ProjectMember
    member = ProjectMember(
        project_id=project.id, user_id="user-cascade-test", role="owner"
    )
    db_session.add(member)

    # Task (parent of Alert.task_id)
    task = Task(
        project_id=project.id,
        title="cascade child task",
        status=TaskStatus.COMPLETED,
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    # Alert (project_id FK + task_id FK)
    alert = Alert(
        project_id=project.id,
        task_id=task.id,
        severity="ERROR",
        title="cascade child alert",
        message="cascade child alert",
    )
    db_session.add(alert)

    # ProjectMessage
    message = ProjectMessage(
        project_id=project.id,
        from_type="user",
        content="cascade child message",
    )
    db_session.add(message)

    # StrategicCycle
    cycle = StrategicCycle(
        id=uuid.uuid4().hex,
        project_id=project.id,
        outcome="in_progress",
    )
    db_session.add(cycle)

    # ResidualRemediationCase
    rcase = ResidualRemediationCase(
        project_id=project.id,
        fingerprint="cascade-fingerprint-" + uuid.uuid4().hex[:8],
        status=ResidualCaseStatus.ACTIVE,
    )
    db_session.add(rcase)

    # SystemMaturity
    maturity = SystemMaturity(
        project_id=project.id,
        system_id="cascade-system",
        score=50,
    )
    db_session.add(maturity)

    await db_session.commit()

    # Capture child IDs for the post-delete assertions.
    member_id = member.id
    task_id = task.id
    alert_id = alert.id
    message_id = message.id
    cycle_id = cycle.id
    rcase_id = rcase.id
    maturity_id = maturity.id

    # Delete the project. This should NOT raise an FK violation.
    await db_session.delete(project)
    await db_session.commit()

    # Project gone.
    assert (
        await db_session.execute(select(Project).where(Project.id == project.id))
    ).scalar_one_or_none() is None

    # Every Project-FK child row is gone (CASCADE).
    for model, row_id in [
        (ProjectMember, member_id),
        (Task, task_id),
        (Alert, alert_id),
        (ProjectMessage, message_id),
        (StrategicCycle, cycle_id),
        (ResidualRemediationCase, rcase_id),
        (SystemMaturity, maturity_id),
    ]:
        result = await db_session.execute(select(model).where(model.id == row_id))
        assert result.scalar_one_or_none() is None, (
            f"{model.__name__}({row_id}) survived project delete — FK cascade missing"
        )


@pytest.mark.asyncio
async def test_task_delete_nulls_alert_task_id(db_session):
    """When a Task is deleted (e.g. via project cascade), child Alert rows
    must have ``task_id`` set to NULL — not be cascaded away. Alerts survive
    so the admin alert UI keeps the historical record."""
    project = Project(name="alert-task-null-test", user_id="user-alert-null")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = Task(
        project_id=project.id,
        title="alert parent task",
        status=TaskStatus.COMPLETED,
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    alert = Alert(
        project_id=project.id,
        task_id=task.id,
        severity="ERROR",
        title="orphanable alert",
        message="orphanable alert",
    )
    db_session.add(alert)
    await db_session.commit()
    alert_id = alert.id

    # Sanity-check: the FK on alert.task_id must exist with ondelete=SET NULL.
    # If the migration didn't apply, fail here with a clear message rather
    # than the downstream assertion's confusing "task_id still set" error.
    # pg_constraint.confdeltype codes:
    #   'a' = NO ACTION, 'r' = RESTRICT, 'c' = CASCADE,
    #   'n' = SET NULL,  'd' = SET DEFAULT.
    fk_check = (
        await db_session.execute(
            text(
                "SELECT confdeltype FROM pg_constraint WHERE conname = 'alert_task_id_fkey'"
            )
        )
    ).scalar_one_or_none()
    # asyncpg returns the single-char as bytes; normalize.
    if isinstance(fk_check, (bytes, bytearray)):
        fk_check = fk_check.decode("ascii")
    assert fk_check == "n", (
        f"alert_task_id_fkey ondelete is {fk_check!r}, expected 'n' (SET NULL). "
        f"Migration 0056 may not have applied."
    )

    # Delete the task; Alert row should remain with task_id=NULL.
    await db_session.delete(task)
    await db_session.commit()

    # Read the column directly via raw SQL — SQLAlchemy's identity-map can
    # serve a cached Alert entity here, so a typed select(Alert) would mask
    # whether the DB-level SET NULL actually fired.
    raw_task_id = (
        await db_session.execute(
            text("SELECT task_id FROM alert WHERE id = :id"), {"id": alert_id}
        )
    ).scalar_one_or_none()
    assert raw_task_id is None, (
        f"alert.task_id={raw_task_id!r} after parent task delete, expected NULL "
        f"(FK ondelete=SET NULL did not fire)"
    )

    # Sanity: the alert row still exists (SET NULL, not CASCADE).
    raw_id = (
        await db_session.execute(
            text("SELECT id FROM alert WHERE id = :id"), {"id": alert_id}
        )
    ).scalar_one_or_none()
    assert raw_id == alert_id, "alert row was cascaded away — should have been SET NULL"


@pytest.mark.asyncio
async def test_project_delete_cascades_resident_checkpoint_and_receipt(db_session):
    """The Resident control plane is project-owned durable state."""

    project = Project(name="resident-cascade", user_id="resident-cascade-user")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = Task(
        project_id=project.id,
        title="resident turn",
        status=TaskStatus.IN_PROGRESS,
        payloads=[{"type": "strategic_resident_turn"}],
        context={"_resident_mutation_authorized": True},
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    # Seed the resident + current checkpoint + applied receipt under enforcement
    # bypass: the 0611 fences (checkpoint-insert / receipt mutation-authority)
    # reject direct seeding of this authoritative starting state on Postgres. The
    # project DELETE below then runs under LIVE enforcement — that cascade is what
    # this test verifies.
    async with seed_without_authority_enforcement(db_session):
        resident = StrategicResident(
            project_id=project.id,
            engine_owner="strategic_resident",
            authority_generation=1,
            active_turn_task_id=task.id,
            active_turn_lane="interactive",
        )
        db_session.add(resident)
        await db_session.flush()
        checkpoint = StrategicResidentCheckpoint(
            project_id=project.id,
            version=1,
            schema_version=1,
            content_hash="a" * 64,
            checkpoint={"schema_version": 1},
            authority_generation=1,
            source_turn_task_id=None,
        )
        db_session.add(checkpoint)
        await db_session.flush()
        resident.current_checkpoint_id = checkpoint.id
        receipt = StrategicOperationReceipt(
            project_id=project.id,
            turn_task_id=task.id,
            authority_generation=1,
            operation_id="cascade-op",
            operation_kind="project_mutation",
            capability="update_direction",
            request_hash="b" * 64,
        )
        db_session.add(receipt)
        await db_session.commit()
    checkpoint_id = checkpoint.id
    receipt_id = receipt.id

    await db_session.delete(project)
    await db_session.commit()
    # Assert the actual cascaded DB state, not stale identity-map instances: on
    # the Postgres shard get() can return a still-cached row for the resident/
    # checkpoint even though the ondelete=CASCADE FKs (migration 0611) removed
    # them in the DB. The sibling task-delete test refreshes for the same reason.
    db_session.expunge_all()

    assert await db_session.get(StrategicResident, project.id) is None
    assert await db_session.get(StrategicResidentCheckpoint, checkpoint_id) is None
    assert await db_session.get(StrategicOperationReceipt, receipt_id) is None


@pytest.mark.asyncio
async def test_task_delete_clears_resident_active_pair_and_preserves_receipt(db_session):
    """Task cleanup cannot violate the active task/lane pair constraint."""

    project = Project(name="resident-task-delete", user_id="resident-task-user")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)
    task = Task(
        project_id=project.id,
        title="deletable resident turn",
        status=TaskStatus.IN_PROGRESS,
        payloads=[{"type": "strategic_resident_turn"}],
        context={"_resident_mutation_authorized": True},
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    # Seed the resident + applied receipt under enforcement bypass (the live
    # 0611 fences reject direct seeding of this authoritative state on Postgres);
    # the task DELETE below runs under LIVE enforcement — that is the behavior
    # under test (active-pair cleared, receipt preserved).
    async with seed_without_authority_enforcement(db_session):
        resident = StrategicResident(
            project_id=project.id,
            engine_owner="strategic_resident",
            authority_generation=3,
            active_turn_task_id=task.id,
            active_turn_lane="background",
        )
        receipt = StrategicOperationReceipt(
            project_id=project.id,
            turn_task_id=task.id,
            authority_generation=3,
            operation_id="preserved-op",
            operation_kind="project_mutation",
            capability="set_memory",
            request_hash="c" * 64,
        )
        db_session.add(resident)
        db_session.add(receipt)
        await db_session.commit()
    task_id = task.id
    receipt_id = receipt.id

    await db_session.delete(task)
    await db_session.commit()
    await db_session.refresh(resident)
    await db_session.refresh(receipt)

    assert resident.active_turn_task_id is None
    assert resident.active_turn_lane is None
    assert receipt.turn_task_id == task_id
    assert receipt.id == receipt_id


@pytest.mark.asyncio
async def test_task_delete_preserves_compute_job_with_task_id_nulled(db_session):
    """ComputeJob is durable, first-class persistence decoupled from Task: when
    its originating Task is deleted (or a project reset bulk-deletes tasks), the
    compute-job row must SURVIVE with ``task_id`` set to NULL — not be cascaded
    away. This guards the detach-on-delete (SET NULL) FK on ``computejob.task_id``
    so bulk ``delete(Task)`` cleanup paths never FK-violate."""
    project = Project(name="cj-task-null-test", user_id="user-cj-null")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = Task(
        project_id=project.id,
        title="compute-job parent task",
        status=TaskStatus.COMPLETED,
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    job = ComputeJob(
        project_id=project.id,
        task_id=task.id,
        backend="gce",
    )
    db_session.add(job)
    await db_session.commit()
    job_id = job.id

    # Sanity-check: computejob.task_id FK must exist with ondelete=SET NULL.
    fk_check = (
        await db_session.execute(
            text(
                "SELECT confdeltype FROM pg_constraint "
                "WHERE conname = 'computejob_task_id_fkey'"
            )
        )
    ).scalar_one_or_none()
    if isinstance(fk_check, (bytes, bytearray)):
        fk_check = fk_check.decode("ascii")
    assert fk_check == "n", (
        f"computejob_task_id_fkey ondelete is {fk_check!r}, expected 'n' (SET NULL)."
    )

    await db_session.delete(task)
    await db_session.commit()

    # Read the column directly via raw SQL to bypass the identity-map cache.
    raw_task_id = (
        await db_session.execute(
            text("SELECT task_id FROM computejob WHERE id = :id"), {"id": job_id}
        )
    ).scalar_one_or_none()
    assert raw_task_id is None, (
        f"computejob.task_id={raw_task_id!r} after parent task delete, expected NULL "
        f"(FK ondelete=SET NULL did not fire)"
    )

    raw_id = (
        await db_session.execute(
            text("SELECT id FROM computejob WHERE id = :id"), {"id": job_id}
        )
    ).scalar_one_or_none()
    assert raw_id == job_id, (
        "compute-job row was cascaded away — should have been SET NULL (history lost)"
    )


@pytest.mark.asyncio
async def test_task_delete_preserves_compute_job_with_artifact_id_nulled(db_session):
    """When a ComputeJob references a task-owned Artifact, deleting the task
    CASCADE-deletes the artifact (artifact.task_id is ON DELETE CASCADE). The
    compute-job row must SURVIVE with ``artifact_id`` set to NULL rather than
    blocking the delete or being cascaded away with the artifact."""
    project = Project(name="cj-artifact-null-test", user_id="user-cj-art")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = Task(
        project_id=project.id,
        title="artifact parent task",
        status=TaskStatus.COMPLETED,
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    artifact = Artifact(task_id=task.id, artifact_type=ArtifactType.GIT_COMMIT)
    db_session.add(artifact)
    await db_session.commit()
    await db_session.refresh(artifact)

    job = ComputeJob(
        project_id=project.id,
        task_id=task.id,
        backend="gce",
        artifact_id=artifact.id,
    )
    db_session.add(job)
    await db_session.commit()
    job_id = job.id

    # Sanity-check: computejob.artifact_id FK must exist with ondelete=SET NULL.
    fk_check = (
        await db_session.execute(
            text(
                "SELECT confdeltype FROM pg_constraint "
                "WHERE conname = 'computejob_artifact_id_fkey'"
            )
        )
    ).scalar_one_or_none()
    if isinstance(fk_check, (bytes, bytearray)):
        fk_check = fk_check.decode("ascii")
    assert fk_check == "n", (
        f"computejob_artifact_id_fkey ondelete is {fk_check!r}, "
        f"expected 'n' (SET NULL)."
    )

    # Deleting the task cascades the artifact away; the compute job must survive.
    await db_session.delete(task)
    await db_session.commit()

    raw_artifact_id = (
        await db_session.execute(
            text("SELECT artifact_id FROM computejob WHERE id = :id"), {"id": job_id}
        )
    ).scalar_one_or_none()
    assert raw_artifact_id is None, (
        f"computejob.artifact_id={raw_artifact_id!r} after artifact cascade-delete, "
        f"expected NULL (FK ondelete=SET NULL did not fire)"
    )

    raw_id = (
        await db_session.execute(
            text("SELECT id FROM computejob WHERE id = :id"), {"id": job_id}
        )
    ).scalar_one_or_none()
    assert raw_id == job_id, (
        "compute-job row was cascaded away with its artifact — should have survived"
    )


@pytest.mark.asyncio
async def test_delete_nonexistent_project_is_clean(db_session):
    """Deleting a Project that doesn't exist must not raise. SQLAlchemy's
    session.delete on a transient/non-persisted instance is a no-op; the
    test guards against any future change that would FK-error or 500 on
    the no-op path."""
    fake_id = uuid.uuid4()
    # Raw DELETE on a non-existent row: must not raise. SQLAlchemy's
    # session.delete() requires a persistent instance; the production
    # delete-by-id path issues a SQL DELETE directly, so that's what
    # we exercise here.
    await db_session.execute(sql_delete(Project).where(Project.id == fake_id))
    await db_session.commit()

    assert (
        await db_session.execute(select(Project).where(Project.id == fake_id))
    ).scalar_one_or_none() is None


@pytest.mark.asyncio
async def test_project_delete_cascades_phase2_tables(db_session):
    """Migration 0274 regression: force-deleting a project that has rows in the
    previously-uncovered child tables (messageslaatseen, messagesnoiseprefs,
    messagessavedviews, anomalyfingerprintstate, poolvm, poolidlecostledger,
    migrationreservation) must not raise an FK violation.

    This guards the fix that adds ON DELETE CASCADE to every project-child FK
    that lacked it after migration 0058 (which only covered the tables existing
    at that point in the chain).
    """
    project = Project(name="cascade-phase2-test", user_id="user-cascade-p2")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    # MessagesLastSeen
    mls = MessagesLastSeen(user_id="user-cascade-p2", project_id=project.id)
    db_session.add(mls)

    # MessagesNoisePrefs
    mnp = MessagesNoisePrefs(user_id="user-cascade-p2", project_id=project.id)
    db_session.add(mnp)

    # MessagesSavedViews
    msv = MessagesSavedViews(user_id="user-cascade-p2", project_id=project.id)
    db_session.add(msv)

    # AnomalyFingerprintState
    afs = AnomalyFingerprintState(
        project_id=project.id,
        fingerprint_key="cascade-p2-fingerprint-" + uuid.uuid4().hex[:8],
    )
    db_session.add(afs)

    # PoolVm
    pvm = PoolVm(
        vm_instance_id="cascade-p2-vm-" + uuid.uuid4().hex[:8],
        project_id=project.id,
    )
    db_session.add(pvm)

    # PoolIdleCostLedger
    picl = PoolIdleCostLedger(
        project_id=project.id,
        vm_instance_id="cascade-p2-vm-ledger",
        idle_cost_usd=0.01,
    )
    db_session.add(picl)

    # MigrationReservation
    mr = MigrationReservation(
        project_id=project.id,
        provisional_revision="9999",
        provisional_down_revision="9998",
    )
    db_session.add(mr)

    await db_session.commit()

    mls_id = mls.id
    mnp_id = mnp.id
    msv_id = msv.id
    afs_id = afs.id
    pvm_id = pvm.id
    picl_id = picl.id
    mr_id = mr.id

    # Delete the project — must not raise an FK violation.
    await db_session.delete(project)
    await db_session.commit()

    # Project is gone.
    assert (
        await db_session.execute(select(Project).where(Project.id == project.id))
    ).scalar_one_or_none() is None

    # All phase-2 child rows must be gone (CASCADE).
    for model, row_id in [
        (MessagesLastSeen, mls_id),
        (MessagesNoisePrefs, mnp_id),
        (MessagesSavedViews, msv_id),
        (AnomalyFingerprintState, afs_id),
        (PoolVm, pvm_id),
        (PoolIdleCostLedger, picl_id),
        (MigrationReservation, mr_id),
    ]:
        result = await db_session.execute(select(model).where(model.id == row_id))
        assert result.scalar_one_or_none() is None, (
            f"{model.__name__}({row_id}) survived project delete — "
            f"ON DELETE CASCADE missing (migration 0274 may not have applied)"
        )

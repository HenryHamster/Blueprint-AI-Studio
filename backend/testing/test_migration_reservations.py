"""Tests for the migration revision reservation allocator.

Covers the three guarantees of ``allocate_migration_reservation``: monotonic
numbering above the merged chain head, distinct ids for concurrent allocators,
and idempotent reread/reclaim for the same task — plus the expiry transition
that frees a provisional id for re-allocation.

The on-disk migration head is pinned via ``_current_merged_head`` so these tests
are independent of the live alembic chain (which grows as migrations land).
"""

import asyncio

import pytest
from sqlmodel import select

from app import migration_reservations
from app.migration_reservations import (
    allocate_migration_reservation,
    finalize_migration_reservation,
    expire_migration_reservation,
    get_migration_reservation_for_task,
)
from app.models import (
    MigrationReservation,
    MigrationReservationStatus,
    Project,
    Task,
    TaskStatus,
)
from app.reaper_sweeps import sweep_stale_migration_reservations
from app.task_completion import _finalize_migration_reservation_after_merge


@pytest.fixture(autouse=True)
def _pin_head(monkeypatch):
    """Pin the merged chain head to 0151 regardless of the live alembic dir."""
    monkeypatch.setattr(
        migration_reservations,
        "_current_merged_head",
        lambda versions_dir=None: "0151",
    )


async def _make_project(session, name="Migration Reservation"):
    project = Project(name=name, user_id="test-user")
    session.add(project)
    await session.commit()
    await session.refresh(project)
    return project


async def _make_task(session, project_id):
    task = Task(project_id=project_id, status=TaskStatus.IN_PROGRESS)
    session.add(task)
    await session.commit()
    await session.refresh(task)
    return task


@pytest.mark.asyncio
async def test_allocates_above_head_and_anchors_to_head(db_session):
    project = await _make_project(db_session)
    task = await _make_task(db_session, project.id)

    reservation = await allocate_migration_reservation(db_session, task)

    assert reservation.provisional_revision == "0152"
    assert reservation.provisional_down_revision == "0151"
    assert reservation.status == MigrationReservationStatus.ACTIVE
    assert reservation.task_id == task.id
    assert reservation.project_id == project.id


@pytest.mark.asyncio
async def test_considers_active_reservations_for_monotonic_numbering(db_session):
    project = await _make_project(db_session)
    task_a = await _make_task(db_session, project.id)
    task_b = await _make_task(db_session, project.id)

    first = await allocate_migration_reservation(db_session, task_a)
    second = await allocate_migration_reservation(db_session, task_b)

    # The second allocation sees the first's still-ACTIVE reservation and steps
    # above it, even though both anchor onto the same merged head.
    assert first.provisional_revision == "0152"
    assert second.provisional_revision == "0153"
    assert first.provisional_down_revision == "0151"
    assert second.provisional_down_revision == "0151"


@pytest.mark.asyncio
async def test_concurrent_allocation_yields_distinct_monotonic_revisions(db_session):
    project = await _make_project(db_session)
    tasks = [await _make_task(db_session, project.id) for _ in range(4)]

    reservations = await asyncio.gather(
        *(allocate_migration_reservation(db_session, t) for t in tasks)
    )

    revisions = sorted(r.provisional_revision for r in reservations)
    assert revisions == ["0152", "0153", "0154", "0155"], revisions
    # No duplicates, and every reservation anchors onto the merged head.
    assert len({r.provisional_revision for r in reservations}) == 4
    assert all(r.provisional_down_revision == "0151" for r in reservations)


@pytest.mark.asyncio
async def test_idempotent_reread_returns_same_reservation(db_session):
    project = await _make_project(db_session)
    task = await _make_task(db_session, project.id)

    first = await allocate_migration_reservation(db_session, task)
    second = await allocate_migration_reservation(db_session, task)

    # Reclaim returns the SAME row and id — a retry never burns a fresh revision.
    assert second.id == first.id
    assert second.provisional_revision == first.provisional_revision

    rows = (
        (
            await db_session.execute(
                select(MigrationReservation).where(
                    MigrationReservation.task_id == task.id
                )
            )
        )
        .scalars()
        .all()
    )
    assert len(rows) == 1


@pytest.mark.asyncio
async def test_expire_frees_provisional_revision_for_reallocation(db_session):
    project = await _make_project(db_session)
    task_a = await _make_task(db_session, project.id)
    task_b = await _make_task(db_session, project.id)

    first = await allocate_migration_reservation(db_session, task_a)
    assert first.provisional_revision == "0152"

    expired = await expire_migration_reservation(db_session, task_id=task_a.id)
    assert expired is not None
    assert expired.status == MigrationReservationStatus.EXPIRED

    # With A expired (no longer ACTIVE), B reuses the freed id off the head.
    second = await allocate_migration_reservation(db_session, task_b)
    assert second.provisional_revision == "0152"
    assert second.id != first.id


@pytest.mark.asyncio
async def test_get_reservation_for_task_filters_to_active(db_session):
    project = await _make_project(db_session)
    task = await _make_task(db_session, project.id)

    assert await get_migration_reservation_for_task(db_session, task_id=task.id) is None

    reservation = await allocate_migration_reservation(db_session, task)
    found = await get_migration_reservation_for_task(db_session, task_id=task.id)
    assert found is not None and found.id == reservation.id

    await expire_migration_reservation(db_session, task_id=task.id)
    assert await get_migration_reservation_for_task(db_session, task_id=task.id) is None


@pytest.mark.asyncio
async def test_finalize_marks_active_reservation_with_shipped_revision(db_session):
    project = await _make_project(db_session)
    task = await _make_task(db_session, project.id)

    reservation = await allocate_migration_reservation(db_session, task)
    finalized = await finalize_migration_reservation(
        db_session, task_id=task.id, finalized_revision="0154"
    )

    assert finalized is not None
    assert finalized.id == reservation.id
    assert finalized.status == MigrationReservationStatus.FINALIZED
    assert finalized.finalized_revision == "0154"
    assert await get_migration_reservation_for_task(db_session, task_id=task.id) is None


@pytest.mark.asyncio
async def test_finalize_is_idempotent_for_same_final_revision(db_session):
    project = await _make_project(db_session)
    task = await _make_task(db_session, project.id)

    reservation = await allocate_migration_reservation(db_session, task)
    first = await finalize_migration_reservation(
        db_session, task_id=task.id, finalized_revision="0152"
    )
    second = await finalize_migration_reservation(
        db_session, task_id=task.id, finalized_revision="0152"
    )

    assert first is not None and second is not None
    assert first.id == reservation.id == second.id
    assert second.status == MigrationReservationStatus.FINALIZED
    assert second.finalized_revision == "0152"


@pytest.mark.asyncio
async def test_merge_finalization_uses_provisional_revision_when_no_rewrite_context(
    db_session,
):
    project = await _make_project(db_session)
    task = await _make_task(db_session, project.id)

    reservation = await allocate_migration_reservation(db_session, task)

    await _finalize_migration_reservation_after_merge(task, db_session)

    row = await db_session.get(MigrationReservation, reservation.id)
    assert row is not None
    assert row.status == MigrationReservationStatus.FINALIZED
    assert row.finalized_revision == reservation.provisional_revision


@pytest.mark.asyncio
async def test_non_terminal_merge_keeps_reservation_active_until_downstream_landing(
    db_session,
):
    project = await _make_project(db_session)
    upstream_task = await _make_task(db_session, project.id)
    downstream_task = await _make_task(db_session, project.id)

    reservation = await allocate_migration_reservation(db_session, upstream_task)

    await _finalize_migration_reservation_after_merge(
        upstream_task,
        db_session,
        terminal_merge=False,
    )

    row = await db_session.get(MigrationReservation, reservation.id)
    assert row is not None
    assert row.status == MigrationReservationStatus.ACTIVE
    assert row.finalized_revision is None

    # A later allocation still sees the upstream rollup child's ACTIVE slot and
    # must step above it rather than reusing the same provisional revision.
    later = await allocate_migration_reservation(db_session, downstream_task)
    assert later.provisional_revision == "0153"

    await _finalize_migration_reservation_after_merge(upstream_task, db_session)

    row = await db_session.get(MigrationReservation, reservation.id)
    assert row is not None
    assert row.status == MigrationReservationStatus.FINALIZED
    assert row.finalized_revision == reservation.provisional_revision


@pytest.mark.asyncio
async def test_non_terminal_merge_preserves_source_task_rewrite_for_downstream(
    db_session,
):
    """A renumbered migration auto-finalized on the CI task must survive a
    non-terminal merge so the downstream terminal landing records the rewritten
    revision, not the stale provisional one."""
    project = await _make_project(db_session)
    orig_task = await _make_task(db_session, project.id)
    # The CI/review task that performed the merge carries the rewrite metadata on
    # its own context — NOT on the reservation-owning orig task.
    ci_task = await _make_task(db_session, project.id)
    ci_task.context = {
        "_migration_finalization": {
            "old_revision": "0152",
            "finalized_revision": "0157",
        }
    }
    db_session.add(ci_task)
    await db_session.commit()

    reservation = await allocate_migration_reservation(db_session, orig_task)
    assert reservation.provisional_revision == "0152"

    # Non-terminal (staged rollup) merge: reservation stays ACTIVE, but the
    # rewrite metadata must be copied onto the orig task.
    await _finalize_migration_reservation_after_merge(
        orig_task,
        db_session,
        source_task=ci_task,
        terminal_merge=False,
    )

    row = await db_session.get(MigrationReservation, reservation.id)
    assert row is not None
    assert row.status == MigrationReservationStatus.ACTIVE
    assert (orig_task.context or {}).get("_migration_finalization", {}).get(
        "finalized_revision"
    ) == "0157"

    # Downstream terminal landing — its source_task (a different CI task) never
    # saw the rewrite, so the finalized revision must come from the orig context.
    downstream_ci = await _make_task(db_session, project.id)
    await _finalize_migration_reservation_after_merge(
        orig_task,
        db_session,
        source_task=downstream_ci,
        terminal_merge=True,
    )

    row = await db_session.get(MigrationReservation, reservation.id)
    assert row is not None
    assert row.status == MigrationReservationStatus.FINALIZED
    assert row.finalized_revision == "0157"


@pytest.mark.asyncio
async def test_stale_reservation_sweep_expires_terminal_failed_cancelled_blocked(db_session):
    project = await _make_project(db_session)
    failed = await _make_task(db_session, project.id)
    cancelled = await _make_task(db_session, project.id)
    blocked = await _make_task(db_session, project.id)
    healthy = await _make_task(db_session, project.id)

    for task in (failed, cancelled, blocked, healthy):
        await allocate_migration_reservation(db_session, task)

    failed.status = TaskStatus.FAILED
    cancelled.status = TaskStatus.CANCELLED
    blocked.status = TaskStatus.BLOCKED
    healthy.status = TaskStatus.IN_PROGRESS
    db_session.add(failed)
    db_session.add(cancelled)
    db_session.add(blocked)
    db_session.add(healthy)
    await db_session.commit()

    expired = await sweep_stale_migration_reservations(db_session)

    assert expired == 3
    rows = (
        (
            await db_session.execute(
                select(MigrationReservation).order_by(MigrationReservation.task_id)
            )
        )
        .scalars()
        .all()
    )
    by_task = {row.task_id: row for row in rows}
    assert by_task[failed.id].status == MigrationReservationStatus.EXPIRED
    assert by_task[cancelled.id].status == MigrationReservationStatus.EXPIRED
    assert by_task[blocked.id].status == MigrationReservationStatus.EXPIRED
    assert by_task[healthy.id].status == MigrationReservationStatus.ACTIVE


@pytest.mark.asyncio
async def test_stale_reservation_sweep_leaves_finalized_rows_untouched(db_session):
    project = await _make_project(db_session)
    task = await _make_task(db_session, project.id)

    reservation = await allocate_migration_reservation(db_session, task)
    await finalize_migration_reservation(
        db_session, task_id=task.id, finalized_revision=reservation.provisional_revision
    )
    task.status = TaskStatus.FAILED
    db_session.add(task)
    await db_session.commit()

    expired = await sweep_stale_migration_reservations(db_session)

    assert expired == 0
    row = await db_session.get(MigrationReservation, reservation.id)
    assert row is not None
    assert row.status == MigrationReservationStatus.FINALIZED
    assert row.finalized_revision == reservation.provisional_revision


@pytest.mark.asyncio
async def test_rejects_terminal_task(db_session):
    project = await _make_project(db_session)
    task = await _make_task(db_session, project.id)
    task.status = TaskStatus.COMPLETED
    db_session.add(task)
    await db_session.commit()

    with pytest.raises(ValueError):
        await allocate_migration_reservation(db_session, task)

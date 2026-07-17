"""Concurrency-safe allocation of provisional Alembic revision ids.

Migration-producing work serializes the whole pipeline: the orchestrator holds
a single "one migration-producing task at a time" slot for a task's ENTIRE
agent run (authoring + the internal test-fix loop + full Docker test gates),
which can run for hours. This module is the backend foundation for breaking
that convoy — a persisted :class:`app.models.MigrationReservation` registry plus
an allocator that hands a migration-producing task the NEXT distinct provisional
revision id the moment it needs one, so concurrent tasks can each author a
migration in parallel and be linearized at merge time.

The allocator mirrors the ``compute_jobs.create_compute_job`` template (lock the
task, short-circuit an existing active row for idempotency, then insert) and
guarantees distinct monotonic numbering across concurrent callers two ways:

* an in-process per-project :class:`asyncio.Lock` serializes the
  read-compute-insert-commit critical section within one backend process (this
  is what makes the single-event-loop test deterministic); and
* a Postgres transaction-scoped advisory lock (``pg_advisory_xact_lock``,
  dialect-guarded so SQLite test runs skip it) serializes across processes.

Allocation considers BOTH the merged chain head and every still-``ACTIVE``
reservation, so two concurrent tasks never receive the same id. The agent-facing
contract is intentionally additive — the ORM row is returned as-is — so a later
claim-response rollout can expose the reservation without breaking callers.
"""

from __future__ import annotations

import asyncio
import contextlib
import glob
import logging
import os
import uuid

from sqlalchemy import text
from sqlmodel import select

from app.database import resolve_dialect_name
from app.migration_chain import build_migration_chain, find_heads, next_revision
from app.models import (
    MigrationReservation,
    MigrationReservationStatus,
    Project,
    Task,
    TaskStatus,
)
from app.time_utils import _utcnow

logger = logging.getLogger(__name__)

_TASK_TERMINAL_STATUSES = {
    TaskStatus.COMPLETED,
    TaskStatus.FAILED,
    TaskStatus.CANCELLED,
    TaskStatus.BLOCKED,
}

# Per-project in-process serialization of the allocation critical section. Keyed
# by project id, but the lock is recreated whenever the running event loop
# changes (``asyncio.Lock`` binds to the loop it is first awaited on and raises
# if reused from another loop — every pytest-asyncio test gets a fresh loop). In
# production the loop is stable, so the same lock is reused across allocations.
_PROJECT_LOCKS: dict[str, tuple[object, asyncio.Lock]] = {}


class MigrationHeadUnavailableError(RuntimeError):
    """The configured remote migration head could not be determined safely."""


def _project_lock(project_id: uuid.UUID) -> asyncio.Lock:
    loop = asyncio.get_running_loop()
    key = str(project_id)
    entry = _PROJECT_LOCKS.get(key)
    if entry is None or entry[0] is not loop:
        lock = asyncio.Lock()
        _PROJECT_LOCKS[key] = (loop, lock)
        return lock
    return entry[1]


def _advisory_lock_key(project_id: uuid.UUID) -> int:
    """A stable signed-64-bit key for ``pg_advisory_xact_lock`` from a project id."""
    raw = uuid.UUID(str(project_id)).int & ((1 << 63) - 1)
    return raw


def _versions_dir() -> str:
    return os.path.join(
        os.path.dirname(os.path.abspath(__file__)),
        "..",
        "alembic",
        "versions",
    )


def _current_merged_head(versions_dir: str | None = None) -> str:
    """The current single head revision id of the on-disk migration chain.

    Globs the alembic ``versions`` directory, builds the revision chain with the
    shared :mod:`app.migration_chain` helpers, and returns the single head. Falls
    back to the numeric-max revision if the chain momentarily has more than one
    head, and to ``"0000"`` when there are no migrations at all (so the first
    allocation yields ``"0001"``). Patched in tests to pin a deterministic head
    independent of the live chain.
    """
    if versions_dir is None:
        versions_dir = _versions_dir()
    sources: dict[str, str] = {}
    for path in glob.glob(os.path.join(versions_dir, "[0-9]*.py")):
        # suppress, not try/except-in-loop (PERF203): an unreadable migration
        # file is skipped rather than crashing allocation.
        with contextlib.suppress(OSError), open(path) as fh:
            sources[os.path.basename(path)] = fh.read()
    chain, _duplicates, _unparseable = build_migration_chain(sources)
    if not chain:
        return "0000"
    heads = find_heads(chain)
    if len(heads) == 1:
        return heads[0]
    # Degenerate (multi-head) chain: fall back to the numeric maximum so the
    # next allocation is still strictly above every existing id.
    return max(chain, key=int)


async def get_migration_reservation_for_task(
    session, *, task_id: uuid.UUID
) -> MigrationReservation | None:
    """The task's current ACTIVE reservation, if any (idempotent-reclaim source)."""
    result = await session.execute(
        select(MigrationReservation)
        .where(
            MigrationReservation.task_id == task_id,
            MigrationReservation.status == MigrationReservationStatus.ACTIVE,
        )
        .order_by(MigrationReservation.created_at.desc())
    )
    return result.scalars().first()


async def get_migration_reservation_for_task_chain(
    session, *, task_ids: list[uuid.UUID]
) -> MigrationReservation | None:
    """Return the first ACTIVE reservation matching any id in the ancestor chain.

    Fix and review tasks inherit the reservation allocated for the original
    task: the list should be ordered from the claiming task's id to the root
    ancestor (i.e. [task.id, parent.id, grandparent.id, ...]).  The most
    recently created reservation among those ids is returned.
    """
    if not task_ids:
        return None
    result = await session.execute(
        select(MigrationReservation)
        .where(
            MigrationReservation.task_id.in_(task_ids),
            MigrationReservation.status == MigrationReservationStatus.ACTIVE,
        )
        .order_by(MigrationReservation.created_at.desc())
    )
    return result.scalars().first()


async def _active_reservations_for_project(
    session, *, project_id: uuid.UUID
) -> list[MigrationReservation]:
    result = await session.execute(
        select(MigrationReservation).where(
            MigrationReservation.project_id == project_id,
            MigrationReservation.status == MigrationReservationStatus.ACTIVE,
        )
    )
    return list(result.scalars().all())


def _effective_task_base_branch(task: Task, project: Project) -> str:
    """Resolve the branch whose merged migrations an agent will author against."""
    from app.branch_resolution import get_project_integration_branch

    context = task.context or {}
    review_loop = context.get("review_loop") or {}
    return (
        review_loop.get("base_branch")
        or (task.first_payload or {}).get("base_branch")
        or get_project_integration_branch(project)
    )


def _head_from_migration_filenames(filenames: list[str]) -> str:
    """Return the numeric migration-id floor advertised by a repository listing."""
    revisions = [
        name.split("_", 1)[0]
        for name in filenames
        if name.split("_", 1)[0].isdigit()
    ]
    return max(revisions, key=int) if revisions else "0000"


async def _task_base_migration_head(session, task: Task) -> str | None:
    """Remote migration head for the task's effective base branch.

    Returns ``None`` when no GitHub repository is configured (caller falls back
    to the local merged head).  When a repository IS configured, any failure
    (missing token, network error, API error) propagates — a stale-local-head
    fallback in that case would recreate the pre-#15085 stale-chain reservation.
    """
    project = await session.get(Project, task.project_id)
    if project is None or not project.github_owner or not project.github_repo:
        return None
    from app.github import list_versions_at_ref
    from app.github_auth import get_repo_installation_read_token

    try:
        token = await get_repo_installation_read_token(
            project.github_owner, project.github_repo
        )
        if not token:
            raise MigrationHeadUnavailableError(
                f"no GitHub installation token available for "
                f"{project.github_owner}/{project.github_repo}; "
                "cannot safely determine task-base migration head"
            )
        filenames = await list_versions_at_ref(
            token,
            project.github_owner,
            project.github_repo,
            _effective_task_base_branch(task, project),
            project_id=str(project.id),
            versions_dir=project.alembic_versions_path,
        )
    except MigrationHeadUnavailableError:
        raise
    except Exception as exc:
        raise MigrationHeadUnavailableError(
            f"cannot safely determine task-base migration head for "
            f"{project.github_owner}/{project.github_repo}: {exc}"
        ) from exc
    return _head_from_migration_filenames(filenames)


async def allocate_migration_reservation(
    session,
    task: Task,
    *,
    versions_dir: str | None = None,
) -> MigrationReservation:
    """Reserve the next provisional revision id for *task*'s migration.

    Idempotent: if the task already holds an ACTIVE reservation it is returned
    unchanged (retry / reclaim), so re-invoking after a reaped attempt does not
    burn a fresh revision id. Otherwise the next id strictly above both the
    best-effort task-base chain head (or local fallback) and every still-ACTIVE
    reservation in the project is allocated. ``provisional_revision`` is
    verbatim; ``provisional_down_revision`` is an advisory authoring anchor.

    Concurrency is serialized per project by an in-process lock plus a Postgres
    advisory lock, so two concurrent allocators receive distinct monotonic ids.
    """
    if task.id is None:
        raise ValueError("task must be persisted before reserving a revision")
    if task.status in _TASK_TERMINAL_STATUSES:
        raise ValueError("cannot reserve a revision for a terminal task")
    project_id = task.project_id

    # Reclaim is the idempotent fast path and must not depend on GitHub being
    # available after the reservation has already been persisted. The locked
    # critical section below repeats this check to close concurrent allocation.
    existing = await get_migration_reservation_for_task(session, task_id=task.id)
    if existing is not None:
        return existing

    # Network I/O is intentionally completed before taking either allocator
    # lock: a slow GitHub response must never hold a DB/advisory critical section.
    remote_head = await _task_base_migration_head(session, task)
    head = remote_head if remote_head is not None else _current_merged_head(versions_dir)

    async with _project_lock(project_id):
        # Cross-process serialization on Postgres; a no-op (skipped) on SQLite
        # test runs where the in-process lock already serializes everything.
        if resolve_dialect_name(session) == "postgresql":
            await session.execute(
                text("SELECT pg_advisory_xact_lock(:k)"),
                {"k": _advisory_lock_key(project_id)},
            )

        locked_task = await _lock_task(session, task.id)
        if locked_task is None:
            raise ValueError("task not found")
        if locked_task.status in _TASK_TERMINAL_STATUSES:
            raise ValueError("cannot reserve a revision for a terminal task")

        existing = await get_migration_reservation_for_task(
            session, task_id=locked_task.id
        )
        if existing is not None:
            return existing

        active = await _active_reservations_for_project(
            session, project_id=project_id
        )
        provisional = next_revision(
            [head, *(r.provisional_revision for r in active)]
        )

        reservation = MigrationReservation(
            project_id=project_id,
            task_id=locked_task.id,
            provisional_revision=provisional,
            provisional_down_revision=head,
            status=MigrationReservationStatus.ACTIVE,
        )
        session.add(reservation)
        await session.commit()
        await session.refresh(reservation)
        logger.info(
            "allocated migration reservation %s -> revision %s (down %s) "
            "for task %s",
            reservation.id,
            provisional,
            head,
            locked_task.id,
        )
        return reservation


async def expire_migration_reservation(
    session, *, task_id: uuid.UUID
) -> MigrationReservation | None:
    """Mark the task's ACTIVE reservation EXPIRED (reaper/abandon cleanup).

    Returns the updated row, or ``None`` when the task held no active
    reservation. Frees the provisional id for re-allocation; the row is retained
    as durable history rather than deleted.
    """
    reservation = await get_migration_reservation_for_task(session, task_id=task_id)
    if reservation is None:
        return None
    reservation.status = MigrationReservationStatus.EXPIRED
    reservation.updated_at = _utcnow()
    session.add(reservation)
    await session.commit()
    await session.refresh(reservation)
    return reservation


async def finalize_migration_reservation(
    session, *, task_id: uuid.UUID, finalized_revision: str
) -> MigrationReservation | None:
    """Mark the task's ACTIVE reservation FINALIZED with its shipped revision.

    Returns the updated row, or ``None`` when the task held no active
    reservation. Idempotent for the common retry/re-entry shape: when the
    reservation is already FINALIZED to the same revision the row is simply
    returned unchanged.
    """
    result = await session.execute(
        select(MigrationReservation)
        .where(MigrationReservation.task_id == task_id)
        .order_by(MigrationReservation.created_at.desc())
    )
    reservation = result.scalars().first()
    if reservation is None:
        return None
    if (
        reservation.status == MigrationReservationStatus.FINALIZED
        and reservation.finalized_revision == finalized_revision
    ):
        return reservation
    if reservation.status != MigrationReservationStatus.ACTIVE:
        return None
    reservation.status = MigrationReservationStatus.FINALIZED
    reservation.finalized_revision = finalized_revision
    reservation.updated_at = _utcnow()
    session.add(reservation)
    await session.commit()
    await session.refresh(reservation)
    return reservation


async def _lock_task(session, task_id: uuid.UUID) -> Task | None:
    result = await session.execute(
        select(Task)
        .where(Task.id == task_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    return result.scalars().first()


__all__ = [
    "allocate_migration_reservation",
    "finalize_migration_reservation",
    "expire_migration_reservation",
    "get_migration_reservation_for_task",
    "MigrationHeadUnavailableError",
]

"""Runner-level project allow-list resolver + binding helpers.

A fleet ``LocalRunner`` enrolls against exactly one project (``LocalRunner.
project_id``, its ENROLLMENT / default project). The ``runner_project_binding``
join table (see ``app.models.RunnerProjectBinding``) adds an editable, additive
many-to-many allow-list so ONE daemon per physical box can serve N projects
under a single unified GPU-index/concurrency ledger.

This module is the read/write seam over that table. It is deliberately small
and dependency-light so later subtasks (dispatch pinning, job-claim authz,
admin allow-list editing) can share one canonical notion of "which projects is
this runner allowed to serve" without each re-deriving the enrollment-union
rule.

Invariant enforced here: a runner is ALWAYS implicitly allowed on its own
enrollment ``project_id``, even when no ``RunnerProjectBinding`` row exists yet
(e.g. a freshly enrolled runner, or a legacy runner predating this table). The
resolver UNIONs the bound project ids with the enrollment project id so callers
never have to special-case the anchor member.
"""

from __future__ import annotations

import uuid

from sqlalchemy import or_
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql.elements import ColumnElement
from sqlmodel import select

from app.models import LocalRunner, RunnerProjectBinding


def runner_allowed_for_project(project_id: uuid.UUID) -> ColumnElement[bool]:
    """SQL predicate selecting runners allowed to serve ``project_id``.

    This is the query-side equivalent of :func:`allowed_project_ids`: the
    enrollment project remains an implicit member, while explicit secondary
    grants come from ``RunnerProjectBinding``.  Keeping this union in one helper
    prevents discovery/read paths from drifting back to enrollment-only scope.
    """
    bound_runner_ids = select(RunnerProjectBinding.runner_id).where(
        RunnerProjectBinding.project_id == project_id
    )
    return or_(
        LocalRunner.project_id == project_id,
        LocalRunner.id.in_(bound_runner_ids),
    )


async def bound_project_ids(
    session: AsyncSession, runner_id: uuid.UUID
) -> set[uuid.UUID]:
    """Return the set of project ids EXPLICITLY bound to ``runner_id``.

    This is the raw allow-list rows only — it does NOT include the runner's
    enrollment project unless a binding row for it exists. Most callers want
    :func:`allowed_project_ids`, which folds in the enrollment anchor.
    """
    result = await session.execute(
        select(RunnerProjectBinding.project_id).where(
            RunnerProjectBinding.runner_id == runner_id
        )
    )
    return set(result.scalars().all())


async def allowed_project_ids(
    session: AsyncSession, runner: LocalRunner
) -> set[uuid.UUID]:
    """Return every project id ``runner`` is allowed to serve.

    The explicit ``RunnerProjectBinding`` rows UNIONed with the runner's
    enrollment ``project_id`` — so the enrollment project is always a member
    even if no binding row has been created for it yet.
    """
    allowed = await bound_project_ids(session, runner.id)
    allowed.add(runner.project_id)
    return allowed


async def add_binding(
    session: AsyncSession, runner_id: uuid.UUID, project_id: uuid.UUID
) -> RunnerProjectBinding:
    """Add (idempotently) a (runner, project) allow-list binding.

    Returns the existing row if one already links this pair — the
    ``uq_runner_project_binding_runner_project`` unique constraint means a
    second insert would otherwise raise. The caller is responsible for
    committing the session.
    """
    result = await session.execute(
        select(RunnerProjectBinding).where(
            RunnerProjectBinding.runner_id == runner_id,
            RunnerProjectBinding.project_id == project_id,
        )
    )
    existing = result.scalars().first()
    if existing is not None:
        return existing
    binding = RunnerProjectBinding(runner_id=runner_id, project_id=project_id)
    session.add(binding)
    await session.flush()
    return binding


async def backfill_enrollment_bindings(session: AsyncSession) -> int:
    """Ensure every runner has an allow-list row for its enrollment project.

    Mirrors the ``0547`` migration's data backfill at the ORM layer (so it is
    testable without driving Alembic): inserts one ``RunnerProjectBinding`` per
    ``LocalRunner`` mapping ``runner.id -> runner.project_id``, skipping any pair
    that already has a row. Idempotent — a second call adds nothing. Returns the
    number of rows created. The caller is responsible for committing.
    """
    runners = (await session.execute(select(LocalRunner))).scalars().all()
    existing = {
        (b.runner_id, b.project_id)
        for b in (
            await session.execute(select(RunnerProjectBinding))
        ).scalars().all()
    }
    created = 0
    for runner in runners:
        key = (runner.id, runner.project_id)
        if key in existing:
            continue
        session.add(
            RunnerProjectBinding(
                runner_id=runner.id, project_id=runner.project_id
            )
        )
        existing.add(key)
        created += 1
    if created:
        await session.flush()
    return created


async def remove_binding(
    session: AsyncSession, runner_id: uuid.UUID, project_id: uuid.UUID
) -> bool:
    """Remove a (runner, project) allow-list binding if present.

    Returns ``True`` if a row was deleted, ``False`` if none matched. Removing
    the binding for the runner's enrollment project does NOT revoke access:
    :func:`allowed_project_ids` still folds the enrollment anchor back in. The
    caller is responsible for committing the session.
    """
    result = await session.execute(
        select(RunnerProjectBinding).where(
            RunnerProjectBinding.runner_id == runner_id,
            RunnerProjectBinding.project_id == project_id,
        )
    )
    row = result.scalars().first()
    if row is None:
        return False
    await session.delete(row)
    return True

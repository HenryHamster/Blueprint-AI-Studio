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

import logging
import uuid
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import func, or_
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql.elements import ColumnElement
from sqlmodel import select

from app.models import LocalRunner, Project, RunnerProjectBinding

logger = logging.getLogger(__name__)

#: Grant endpoint an operator (or this module's onboard auto-grant) must use to
#: attach a project to a runner's allow-list. Named in every blocked-reason
#: message so the reader learns what clears the gate, not merely that it bound.
RUNNER_BINDING_GRANT_ENDPOINT = (
    "POST /api/admin/projects/{enrollment_project_id}/runners/{runner_id}/projects"
)

#: Historic wording for "the fleet exists for this project but has no free slot
#: right now". Correct ONLY when at least one runner is actually bound.
LOCAL_CAPACITY_WAIT_MESSAGE = (
    "Waiting for local capacity: managed cloud execution is disabled"
)

#: Wording for the structurally different case: managed cloud execution is off
#: AND zero runners are bound to this project, so no amount of waiting can ever
#: dispatch the task. This is not a capacity shortage, it is a missing grant.
NO_RUNNER_BOUND_WAIT_MESSAGE = (
    "Blocked: no local runner is bound to this project (0 runners on its "
    "allow-list) and managed cloud execution is disabled, so this task can "
    "never be dispatched by waiting. Grant a runner to this project with "
    f"{RUNNER_BINDING_GRANT_ENDPOINT}."
)


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


async def allowed_project_ids_by_runner(
    session: AsyncSession, runners: list[LocalRunner]
) -> dict[uuid.UUID, set[uuid.UUID]]:
    """Resolve canonical allow-lists for many loaded runners in one query.

    The enrollment project is always included, including for legacy runners
    without a materialized binding row.  Callers which already loaded runners
    should use this instead of issuing one binding query per runner.
    """
    allowed = {runner.id: {runner.project_id} for runner in runners}
    if not allowed:
        return allowed
    rows = await session.execute(
        select(RunnerProjectBinding.runner_id, RunnerProjectBinding.project_id).where(
            RunnerProjectBinding.runner_id.in_(allowed)
        )
    )
    for runner_id, project_id in rows.all():
        allowed[runner_id].add(project_id)
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


async def count_runners_allowed_for_project(
    session: AsyncSession, project_id: uuid.UUID | None
) -> int | None:
    """Count the non-deleted runners allowed to serve ``project_id``.

    Uses the same enrollment-UNION-bindings rule as dispatch
    (:func:`runner_allowed_for_project`), so the number answers exactly the
    question a blocked reader asks: "is ANY runner attached to this project?"
    A ``0`` means the project is structurally undispatchable on local hardware
    — not that the fleet is momentarily busy.

    Returns ``None`` when the count cannot be determined (no project id, or the
    query failed). Callers must treat ``None`` as "unknown" and fall back to the
    pre-existing capacity wording rather than accusing the operator of a missing
    grant on the strength of a failed query.
    """
    if project_id is None:
        return None
    try:
        result = await session.execute(
            select(func.count())
            .select_from(LocalRunner)
            .where(
                LocalRunner.status != "deleted",
                runner_allowed_for_project(project_id),
            )
        )
        return int(result.scalar_one())
    except Exception:
        logger.exception(
            "[RUNNERS] Failed to count runners allowed for project %s", project_id
        )
        return None


def local_capacity_wait_message(bound_runner_count: int | None) -> str:
    """Pick the hold message that names the condition which actually bound.

    ``0`` bound runners is a missing-grant defect with a concrete remedy; any
    other value (including ``None`` for "could not determine") keeps the legacy
    capacity wording, which is accurate when a fleet exists but is busy, paused,
    or unhealthy.
    """
    if bound_runner_count == 0:
        return NO_RUNNER_BOUND_WAIT_MESSAGE
    return LOCAL_CAPACITY_WAIT_MESSAGE


def runner_binding_wait_snapshot(bound_runner_count: int | None) -> dict[str, Any]:
    """Durable audit metadata explaining which local-execution gate bound."""

    if bound_runner_count is None:
        reason = "runner_binding_unknown"
        remedy = None
    elif bound_runner_count == 0:
        reason = "no_runner_bound_to_project"
        remedy = RUNNER_BINDING_GRANT_ENDPOINT
    else:
        reason = "local_capacity_unavailable"
        remedy = None
    return {
        "reason": reason,
        "bound_runner_count": bound_runner_count,
        "remedy": remedy,
        "recorded_at": datetime.now(timezone.utc).isoformat(),
    }


async def resolve_local_capacity_hold(
    session: AsyncSession, project_id: uuid.UUID | None
) -> tuple[str, dict[str, Any]]:
    """Return ``(status_message, audit_snapshot)`` for a local-execution hold.

    One seam so agent dispatch, agent provisioning, and compute-job launch all
    emit the SAME distinction between "no runner is bound to this project" and
    "the bound fleet has no free slot".
    """
    count = await count_runners_allowed_for_project(session, project_id)
    return local_capacity_wait_message(count), runner_binding_wait_snapshot(count)


async def grant_project_to_owner_runners(
    session: AsyncSession, project: Project
) -> dict[str, Any]:
    """Bind every runner owned by ``project``'s account to ``project``.

    A newly created project starts on ZERO runner allow-lists, so every task it
    spawns holds QUEUED until a human POSTs a binding. This grants the owner's
    own already-enrolled fleet at creation time via the canonical
    :func:`add_binding` write path (the same one the admin endpoint uses), so no
    second write path with different validation exists.

    Security boundary (same-owner MVP consent model, mirroring
    ``add_runner_project``): a runner is eligible ONLY when its ENROLLMENT
    project's ``Project.user_id`` equals ``project.user_id``. A runner belonging
    to any other account is never granted, under any circumstance.

    Idempotent (``add_binding`` returns the existing row) and non-fatal: a
    per-runner failure is collected as a warning instead of raising, so the
    caller's onboarding still succeeds. The caller commits.
    """
    granted_runner_ids: list[str] = []
    warnings: list[str] = []
    owner_user_id = getattr(project, "user_id", None)
    if not owner_user_id:
        warnings.append(
            "This project has no owner account on record, so no local runner "
            "was granted to it automatically. Its tasks will hold QUEUED until "
            f"a runner is granted with {RUNNER_BINDING_GRANT_ENDPOINT}."
        )
        return {"granted_runner_ids": granted_runner_ids, "warnings": warnings}

    try:
        runners = (
            (
                await session.execute(
                    select(LocalRunner)
                    .join(Project, Project.id == LocalRunner.project_id)
                    .where(
                        Project.user_id == owner_user_id,
                        LocalRunner.status != "deleted",
                    )
                )
            )
            .scalars()
            .all()
        )
    except Exception as exc:
        logger.exception(
            "[RUNNERS] Failed to list owner runners for project %s", project.id
        )
        warnings.append(
            f"Could not list this account's local runners ({exc}); no runner was "
            "granted to this project automatically. Grant one with "
            f"{RUNNER_BINDING_GRANT_ENDPOINT}."
        )
        return {"granted_runner_ids": granted_runner_ids, "warnings": warnings}

    for runner in runners:
        try:
            await add_binding(session, runner.id, project.id)
        except Exception as exc:
            logger.exception(
                "[RUNNERS] Failed to grant runner %s access to project %s",
                runner.id,
                project.id,
            )
            warnings.append(
                f"Failed to grant runner {runner.id} access to this project "
                f"({exc}). Grant it manually with {RUNNER_BINDING_GRANT_ENDPOINT}."
            )
            continue
        granted_runner_ids.append(str(runner.id))

    if not granted_runner_ids and not warnings:
        warnings.append(
            "No local runner is enrolled under this account, so this project "
            "has zero runners bound to it. Until a runner is enrolled and "
            f"granted with {RUNNER_BINDING_GRANT_ENDPOINT}, its tasks will hold "
            "QUEUED whenever managed cloud execution is disabled."
        )

    if granted_runner_ids:
        logger.info(
            "[RUNNERS] Auto-granted %d owner runner(s) to new project %s",
            len(granted_runner_ids),
            project.id,
        )
    return {"granted_runner_ids": granted_runner_ids, "warnings": warnings}


# ── Unsatisfiable local placement ────────────────────────────────────────────
# ``count_runners_allowed_for_project`` answers "is ANY runner attached to this
# project?" and deliberately counts a runner that is merely paused or unhealthy
# (see :data:`LOCAL_CAPACITY_WAIT_MESSAGE`).  That is the right question for the
# managed-compute hold, but it is NOT sufficient for the ``local_only``
# execution-placement wait, which promises local execution and has no timeout.
#
# A project whose only bound runner is PAUSED has a non-zero bound count and yet
# zero dispatchable capacity: no cycle can ever pin its work, and no amount of
# waiting changes that.  The distinction that matters for a no-timeout wait is
# therefore "can any LIVE runner serve this project?", which is exactly what the
# orchestrator's active-runner cache already encodes (status='active' AND a
# fresh heartbeat, expanded over the binding allow-list).  These helpers name
# that condition and its remedy.

#: Structured reason code stamped on a ``local_only`` wait that cannot be
#: satisfied by waiting.  Distinct from ``no_eligible_local_runner`` (the
#: genuinely transient "bound fleet is busy" backpressure) so read models,
#: alerting, and the stalled-cycle reaper can tell a permanent block from a
#: temporary one without parsing prose.
NO_ACTIVE_RUNNER_BOUND_REASON = "no_active_runner_bound_to_project"


def unsatisfiable_local_placement_clause(bound_runner_count: int | None) -> str:
    """Operator-facing clause for "zero LIVE runners can serve this project".

    ``bound_runner_count`` refines the REMEDY, not the diagnosis: the caller has
    already established that no live runner is available.  Zero bound runners is
    a missing grant; a non-zero count means runners are attached but none are
    active and heartbeating, so the fix is to resume/restore one rather than to
    grant another.
    """
    if bound_runner_count == 0:
        return (
            "no local runner is bound to this project (0 runners on its "
            "allow-list), so this wait can never be satisfied — grant one with "
            f"{RUNNER_BINDING_GRANT_ENDPOINT}"
        )
    if bound_runner_count is None:
        return (
            "no active local runner is bound to this project, so this wait can "
            "never be satisfied — bind or resume a runner for it with "
            f"{RUNNER_BINDING_GRANT_ENDPOINT}"
        )
    return (
        f"{bound_runner_count} local runner(s) are bound to this project but "
        "NONE are active and heartbeating, so this wait can never be satisfied "
        "— resume or restore a bound runner, or grant an active one with "
        f"{RUNNER_BINDING_GRANT_ENDPOINT}"
    )


def unsatisfiable_local_placement_snapshot(
    bound_runner_count: int | None,
) -> dict[str, Any]:
    """Durable, machine-readable record of an unsatisfiable ``local_only`` wait."""

    return {
        "reason": NO_ACTIVE_RUNNER_BOUND_REASON,
        "message": unsatisfiable_local_placement_clause(bound_runner_count),
        "bound_runner_count": bound_runner_count,
        "active_runner_count": 0,
        "remedy": RUNNER_BINDING_GRANT_ENDPOINT,
        "recorded_at": datetime.now(timezone.utc).isoformat(),
    }


async def resolve_unsatisfiable_local_placement(
    session: AsyncSession, project_id: uuid.UUID | None
) -> dict[str, Any]:
    """Build the unsatisfiable-placement snapshot for ``project_id``.

    Call ONLY once the caller has established from the live dispatch cache that
    zero active runners can serve the project.  That emptiness is the
    authoritative signal and it comes from the caller, not from this query --
    the bound-runner count read here only refines the remedy wording (grant a
    runner vs. resume the one already bound).
    """
    return unsatisfiable_local_placement_snapshot(
        await count_runners_allowed_for_project(session, project_id)
    )

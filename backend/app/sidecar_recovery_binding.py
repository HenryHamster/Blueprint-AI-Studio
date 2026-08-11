"""Authenticated repair for legacy boolean sidecar-awaiting markers."""

from __future__ import annotations

import logging
import uuid
from collections.abc import Sequence

from sqlalchemy import func, or_
from sqlalchemy.orm import load_only
from sqlalchemy.orm.attributes import flag_modified
from sqlmodel import select

from app.database import resolve_dialect_name
from app.models import Task, TaskStatus
from app.sidecar_authority import (
    FINDING_CHALLENGE_SOURCE_CONTEXT_KEY,
    QUALITY_GRADER_SOURCE_CONTEXT_KEY,
    is_authenticated_backend_sidecar_shape,
)

logger = logging.getLogger(__name__)

AWAITING_SIDECAR_KEYS = (
    "awaiting_quality_grader",
    "awaiting_adversarial_grader",
    "awaiting_finding_challenge",
)


def awaiting_sidecar_context_predicate(session):
    """SQL predicate selecting rows that actually carry an awaiting lane.

    Apply this before the recovery sweep's per-cycle LIMIT.  Limiting all
    EVALUATING tasks first lets an unrelated stable backlog permanently starve
    a later stranded sidecar source.
    """

    if resolve_dialect_name(session) == "postgresql":
        return or_(
            *(
                Task.context[key].as_string().isnot(None)
                for key in AWAITING_SIDECAR_KEYS
            )
        )
    return or_(
        *(
            func.json_extract(Task.context, f"$.{key}").isnot(None)
            for key in AWAITING_SIDECAR_KEYS
        )
    )


def exact_terminal_sidecar_lanes(
    source: Task,
    sidecars_by_id: dict[uuid.UUID, Task],
    terminal_statuses: set[TaskStatus],
) -> list[tuple[str, Task]]:
    """Return only exact, same-project, authenticated terminal awaited lanes."""

    lanes: list[tuple[str, Task]] = []
    source_ctx = source.context or {}
    for key in AWAITING_SIDECAR_KEYS:
        try:
            sidecar_id = uuid.UUID(str(source_ctx.get(key)))
        except (TypeError, ValueError, AttributeError):
            continue
        sidecar = sidecars_by_id.get(sidecar_id)
        if sidecar is None or sidecar.status not in terminal_statuses:
            continue
        if not _is_exact_authorized_lane(source, key, sidecar):
            logger.warning(
                "[SIDECAR-RECOVERY] Refusing unauthorized sidecar %s for "
                "source %s lane %s",
                sidecar.id,
                source.id,
                key,
            )
            continue
        lanes.append((key, sidecar))
    return lanes


def _is_exact_authorized_lane(source: Task, key: str, sidecar: Task) -> bool:
    """Return whether ``sidecar`` is the backend owner of ``source`` lane ``key``."""

    sidecar_ctx = sidecar.context or {}
    if key == "awaiting_finding_challenge":
        source_ref = sidecar_ctx.get(FINDING_CHALLENGE_SOURCE_CONTEXT_KEY)
    else:
        source_ref = sidecar_ctx.get(QUALITY_GRADER_SOURCE_CONTEXT_KEY)
        if bool(sidecar_ctx.get("adversarial_grader")) != (
            key == "awaiting_adversarial_grader"
        ):
            return False
    return bool(
        sidecar.project_id == source.project_id
        and str(source_ref or "") == str(source.id)
        and is_authenticated_backend_sidecar_shape(sidecar)
    )


async def rebind_legacy_boolean_sidecars(
    session,
    evaluating: Sequence[Task],
    *,
    limit: int,
) -> list[Task]:
    """Bind legacy ``awaiting_*=True`` slots to authenticated exact UUIDs.

    The former two-commit spawn protocol could commit the boolean placeholder
    and the sidecar row, then crash before publishing the sidecar UUID. Match
    only the immutable backend-authored shape and exact source/project lane;
    an arbitrary agent-created task with a copied source key is never eligible.
    Returns a fresh EVALUATING snapshot after any successful binding so callers
    can resolve a terminal sidecar in the same sweep.
    """
    legacy_slots = [
        (source, key)
        for source in evaluating
        for key in AWAITING_SIDECAR_KEYS
        if (source.context or {}).get(key) is True
    ]
    if not legacy_slots:
        return list(evaluating)

    # Lazy to keep this leaf out of the task-evaluator import graph at startup.
    from app.task_evaluators import (
        FINDING_CHALLENGE_SOURCE_KEY,
        QUALITY_GRADER_SOURCE_KEY,
    )

    source_ids = {str(source.id) for source, _ in legacy_slots}
    project_ids = {source.project_id for source, _ in legacy_slots}
    candidates = (
        (
            await session.execute(
                select(Task)
                .where(
                    Task.project_id.in_(project_ids),
                    Task.created_by == "ORCHESTRATOR",
                    Task.parent_task_id.is_(None),
                    Task.payloads[0]["type"].as_string() == "investigate",
                    or_(
                        Task.context[QUALITY_GRADER_SOURCE_KEY]
                        .as_string()
                        .in_(source_ids),
                        Task.context[FINDING_CHALLENGE_SOURCE_KEY]
                        .as_string()
                        .in_(source_ids),
                    ),
                )
                # The interrupted handshake owns the newest authenticated
                # attempt when an older completed sidecar also exists.
                .order_by(Task.created_at.desc(), Task.id.desc())
            )
        )
        .scalars()
        .all()
    )
    latest_by_slot: dict[tuple[str, str, str], Task] = {}
    for candidate in candidates:
        if not is_authenticated_backend_sidecar_shape(candidate):
            continue
        candidate_ctx = candidate.context or {}
        quality_source = candidate_ctx.get(QUALITY_GRADER_SOURCE_KEY)
        challenge_source = candidate_ctx.get(FINDING_CHALLENGE_SOURCE_KEY)
        if quality_source:
            candidate_key = (
                "awaiting_adversarial_grader"
                if candidate_ctx.get("adversarial_grader")
                else "awaiting_quality_grader"
            )
            candidate_source = str(quality_source)
        elif challenge_source:
            candidate_key = "awaiting_finding_challenge"
            candidate_source = str(challenge_source)
        else:
            continue
        latest_by_slot.setdefault(
            (str(candidate.project_id), candidate_source, candidate_key), candidate
        )

    # The sweep's source rows are only discovery snapshots. A peer completion or
    # administrative terminal transition can commit after that scan. Re-lock
    # each distinct source in deterministic Project -> Task order, refresh its
    # identity-map state, and merge onto that authoritative context rather than
    # publishing the stale snapshot back over the peer's write.
    from app.task_locking import lock_project_then_task

    slots_by_source: dict[uuid.UUID, list[str]] = {}
    source_project_ids: dict[uuid.UUID, object] = {}
    for source, key in legacy_slots:
        slots_by_source.setdefault(source.id, []).append(key)
        source_project_ids[source.id] = source.project_id

    bound = 0
    locked_any = False
    for source_id in sorted(
        slots_by_source,
        key=lambda item: (str(source_project_ids[item]), str(item)),
    ):
        project_id = source_project_ids[source_id]
        # Avoid taking locks when no authenticated candidate can fill any of the
        # source's legacy slots.
        candidate_slots = {
            key: latest_by_slot.get((str(project_id), str(source_id), key))
            for key in slots_by_source[source_id]
        }
        if not any(candidate_slots.values()):
            continue
        async with lock_project_then_task(
            session,
            project_id,
            source_id,
            skip_locked=False,
        ) as (locked_project, locked_source):
            locked_any = True
            if (
                locked_project is None
                or locked_source is None
                or locked_source.project_id != locked_project.id
                or locked_source.status != TaskStatus.EVALUATING
            ):
                continue
            source_ctx = dict(locked_source.context or {})
            source_bound = 0
            for key, sidecar in candidate_slots.items():
                if (
                    sidecar is None
                    or sidecar.project_id != locked_source.project_id
                    or source_ctx.get(key) is not True
                ):
                    continue
                source_ctx[key] = str(sidecar.id)
                source_bound += 1
            if not source_bound:
                continue
            locked_source.context = source_ctx
            flag_modified(locked_source, "context")
            session.add(locked_source)
            bound += source_bound

    if not bound and not locked_any:
        return list(evaluating)
    await session.commit()
    if bound:
        logger.warning(
            "[SIDECAR-RECOVERY] Rebound %d legacy boolean awaiting slot(s) "
            "to authenticated sidecar UUIDs",
            bound,
        )
    return (
        (
            await session.execute(
                select(Task)
                .options(
                    load_only(
                        Task.id,
                        Task.status,
                        Task.project_id,
                        Task.context,
                        raiseload=True,
                    )
                )
                .where(
                    Task.status == TaskStatus.EVALUATING,
                    awaiting_sidecar_context_predicate(session),
                )
                .order_by(Task.created_at.asc(), Task.id.asc())
                .limit(limit)
            )
        )
        .scalars()
        .all()
    )


async def lock_terminal_sidecar_for_recovery(
    session,
    sidecar: Task,
    *,
    source_id: uuid.UUID,
    awaiting_key: str,
    terminal_statuses: set[TaskStatus],
) -> tuple[Task, bool] | None:
    """Refresh recovery authority and decide whether source resume must defer.

    The legacy boolean rebind commits with ``expire_on_commit=False``.  Its
    candidate sidecar therefore remains identity-mapped, and a subsequent
    SELECT can return a terminal status cached before an operator requeued the
    row in a peer transaction.  Canonically re-lock and populate the exact row
    before its terminal verdict is applied. Refresh and lock the source under
    the already-held Project row too: terminal-lane discovery is only a
    snapshot, and deciding to resume from it can mark the source COMPLETED while
    a different awaited lane is still live. The valid path returns
    ``(sidecar, defer_source_resume)`` and keeps the transaction open for the
    caller's resolver; rejected paths release their locks.
    """

    from app.task_locking import lock_project_then_task

    sidecar_id = sidecar.id
    project_id = sidecar.project_id
    async with lock_project_then_task(
        session,
        project_id,
        sidecar_id,
        skip_locked=False,
    ) as (locked_project, locked_sidecar):
        if (
            locked_project is None
            or locked_sidecar is None
            or locked_sidecar.project_id != locked_project.id
            or locked_sidecar.status not in terminal_statuses
            or not is_authenticated_backend_sidecar_shape(locked_sidecar)
        ):
            await session.commit()
            return None
        locked_source = (
            (
                await session.execute(
                    select(Task)
                    .where(
                        Task.id == source_id,
                        Task.project_id == project_id,
                    )
                    .with_for_update()
                    .execution_options(populate_existing=True)
                )
            )
            .scalars()
            .first()
        )
        source_ctx = dict((locked_source.context if locked_source else None) or {})
        if (
            locked_source is None
            or locked_source.status != TaskStatus.EVALUATING
            or str(source_ctx.get(awaiting_key) or "") != str(locked_sidecar.id)
            or not _is_exact_authorized_lane(
                locked_source, awaiting_key, locked_sidecar
            )
        ):
            await session.commit()
            return None
        defer_source_resume = any(
            source_ctx.get(key)
            for key in AWAITING_SIDECAR_KEYS
            if key != awaiting_key
        )
        return locked_sidecar, defer_source_resume

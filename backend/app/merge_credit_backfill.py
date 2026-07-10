"""Retroactive, idempotent backfill of merge credit for landed rollups.

PR #12971 fixed *forward* propagation of merge credit across the nested-rollup +
squash-merge boundary: when a rollup lands on the integration branch, its
constituent children are re-stamped ``landed_on_integration=True`` by
rollup->child MEMBERSHIP (the lineage subtree BFS, NOT by commit ancestry —
squash-merge makes each child's merge commit an ancestor of nothing on the
integration trunk), and each affected direction's research-history outcomes are
recomputed.

That fix repairs nothing retroactively: every rollup that LANDED BEFORE the fix
left its credited children stamped ``landed_on_integration=False`` forever, so
:func:`app.landed_signal.is_merged_completion` returns ``False`` for them, so
:mod:`app.direction_outcomes` computes ``prs_merged=0`` / all-zero merge_credit
for those directions even though the rollups are ancestry-verifiable merged.
The false zero pauses/misjudges healthy directions.

This module heals that existing data. For each active project it finds LANDED
rollup parents — a task identified as a rollup parent the same way
:func:`app.rollup_state._is_rollup_parent_awaiting_merge` identifies one
(``_is_rollup_review=True`` and/or a ``rollup_pr_number`` in context) that ALSO
carries the canonical integration-landing signal
(:func:`app.landed_signal.has_landed_signal`). The rollup's own landed signal is
the ancestry gate — it was already gated by ``verify_rollup_merge_landed`` when
it landed, so we trust it and do NOT re-derive child ancestry (squash destroys
that). For each such rollup we reuse the EXISTING membership walk
:func:`app.rollup_management._propagate_landed_to_staging_merged_descendants`
verbatim (idempotent, force-drop aware, merge-unblock-gated) and then recompute
the research-history outcomes for each DISTINCT direction among the newly-stamped
descendants via :func:`app.direction_outcomes.refresh_direction_outcomes_after_merge`.

Because propagation and recompute are both idempotent (already-landed children
are skipped and yield an empty stamped list), the sweep converges: once every
landed rollup's children are stamped it becomes a cheap no-op, and it also
self-heals any FUTURE stranding the forward path misses. The per-tick project
and rollup batches advance through the FULL history via rotating keyset cursors
(see ``_project_backfill_cursor`` / ``_rollup_backfill_cursors``): the ordered
ids are random UUIDs, so without progression the rows beyond a single batch —
projects past the project batch, landed rollups past the rollup batch — would be
starved forever and the sweep could NOT converge on a large fleet/history. With
the cursors every project and every landed rollup is eventually visited across
ticks, and each cursor wraps back to the start once it reaches the end, so the
wrap re-scans everything and preserves the continuous self-heal above.
"""

import logging

from sqlalchemy import cast, func, or_
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from app import services
from app.database import resolve_dialect_name
from app.landed_signal import landed_signal_predicate
from app.models import Project, Task

logger = logging.getLogger(__name__)

# Per-tick bounds so a large history cannot stall the leader cycle. Both are
# generous relative to the steady-state (once converged the sweep is a no-op),
# but cap the worst case on a project with a long rollup history.
_MERGE_CREDIT_BACKFILL_PROJECT_BATCH = 25
_MERGE_CREDIT_BACKFILL_ROLLUP_BATCH = 50

# Rotating keyset cursors so successive ticks advance through the FULL
# project/rollup history instead of re-selecting the same first-N rows every
# time. The ordered ids are random UUIDs (Project.id / Task.id are uuid4 PKs),
# so without progression every row beyond the per-tick batch would be starved
# forever and the sweep could not converge on a large fleet/history. Each cursor
# is the last id of the batch just processed; a short (< batch) page means the
# end of the ordered set was reached, so the cursor wraps back to the start
# (None for projects, key removed for a project's rollup scan) and the next tick
# re-scans from the beginning — the wrap is what keeps the sweep's continuous
# self-heal of future stranding intact. Leader-process memory only; a leader
# restart harmlessly restarts every scan from the beginning.
_project_backfill_cursor = None
_rollup_backfill_cursors: dict = {}

# Rollup-parent context markers. Mirrors _is_rollup_parent_awaiting_merge's
# identification (_is_rollup_review=True and/or rollup_pr_number present) — but
# WITHOUT that helper's "not yet merged" clause, since a LANDED rollup carries
# pr_merged=True and we want exactly those.
_ROLLUP_REVIEW_KEY = "_is_rollup_review"
_ROLLUP_PR_NUMBER_KEY = "rollup_pr_number"


def _rollup_parent_predicate(session: AsyncSession):
    """SQL predicate matching rollup-parent tasks by context markers.

    A rollup parent carries ``_is_rollup_review=True`` and/or a
    ``rollup_pr_number``. Dialect-aware (SQLite for tests, Postgres for prod),
    mirroring :func:`app.landed_signal.landed_signal_predicate`.
    """
    dialect_name = resolve_dialect_name(session)
    if dialect_name == "sqlite":
        is_review = func.json_extract(Task.context, f"$.{_ROLLUP_REVIEW_KEY}")
        rollup_pr = func.json_extract(Task.context, f"$.{_ROLLUP_PR_NUMBER_KEY}")
        return or_(
            is_review == True,  # noqa: E712
            rollup_pr.isnot(None),
        )
    is_review = Task.context.op("@>")(cast({_ROLLUP_REVIEW_KEY: True}, JSONB))
    # ``has_key`` is SQLAlchemy's JSONB ``?`` operator, not the removed dict method.
    return or_(is_review, Task.context.has_key(_ROLLUP_PR_NUMBER_KEY))


async def backfill_landed_rollup_merge_credit(
    project_id, session: AsyncSession, *, rollup_limit: int
) -> int:
    """Backfill under-credited children of landed rollups for one project.

    Finds up to *rollup_limit* landed rollup parents in *project_id* — a
    ROTATING keyset window (``_rollup_backfill_cursors``) that advances past the
    previous window on each successive call and wraps to the start on reaching
    the end, so the whole rollup history is covered across calls rather than the
    first *rollup_limit* rows being re-selected forever — re-stamps their
    under-credited descendants by membership via the existing propagation
    helper, and recomputes each affected direction's outcomes. Returns the total
    number of descendant tasks newly stamped ``landed_on_integration=True`` this
    call (0 once the project has fully converged — the steady-state no-op).

    Idempotent and best-effort per rollup: a failure on one landed rollup is
    logged and skipped without aborting the rest.
    """
    from app.direction_outcomes import refresh_direction_outcomes_after_merge
    from app.rollup_management import (
        _propagate_landed_to_staging_merged_descendants,
    )

    rollup_cursor = _rollup_backfill_cursors.get(project_id)
    rollup_query = select(Task.id).where(
        Task.project_id == project_id,
        Task.archived.is_(False),
        landed_signal_predicate(session),
        _rollup_parent_predicate(session),
    )
    if rollup_cursor is not None:
        # Resume past the previous window (keyset pagination over the ordered id).
        rollup_query = rollup_query.where(Task.id > rollup_cursor)
    rollup_ids = list(
        (
            await session.execute(
                rollup_query.order_by(Task.id).limit(rollup_limit)
            )
        )
        .scalars()
        .all()
    )
    # Advance the per-project cursor so the NEXT call resumes past this window;
    # a short page means the end of the ordered set, so wrap to the start.
    if len(rollup_ids) < rollup_limit:
        _rollup_backfill_cursors.pop(project_id, None)
    else:
        _rollup_backfill_cursors[project_id] = rollup_ids[-1]

    total_stamped = 0
    for rollup_id in rollup_ids:
        try:
            stamped = await _propagate_landed_to_staging_merged_descendants(
                rollup_id, session
            )
            if not stamped:
                continue

            # Snapshot each distinct direction's representative descendant by
            # PLAIN id BEFORE any recompute. refresh_direction_outcomes_after_merge
            # rolls the session back on an internal error, which expires every ORM
            # instance; a subsequent iteration reading an expired attribute off a
            # held instance would lazy-load under the async session and raise
            # MissingGreenlet. Re-fetch each rep fresh via session.get instead.
            dir_reps: dict = {}
            for desc in stamped:
                dctx = desc.context or {}
                did = dctx.get("direction_id")
                rid = dctx.get("research_task_id")
                if did and rid and did not in dir_reps:
                    dir_reps[did] = desc.id

            # Persist the landed stamps before recomputing so the outcome
            # recompute (which re-queries children via is_merged_completion)
            # sees them committed.
            await session.commit()
            total_stamped += len(stamped)

            # Batch-fetch all rep tasks in one query after the commit (which
            # expires ORM instances) rather than issuing per-rep session.get.
            rep_ids = list(dir_reps.values())
            if rep_ids:
                _reps_by_id = {
                    str(r.id): r
                    for r in (
                        await session.execute(
                            select(Task).where(Task.id.in_(rep_ids))
                        )
                    ).scalars().all()
                }
            else:
                _reps_by_id = {}
            for rep_id in rep_ids:
                try:
                    rep = _reps_by_id.get(str(rep_id))
                    if rep is None:
                        continue
                    await refresh_direction_outcomes_after_merge(rep, session)
                except Exception:
                    logger.warning(
                        "[MERGE_CREDIT_BACKFILL] direction recompute failed for rep "
                        "task %s (rollup %s, project %s) — non-fatal",
                        rep_id,
                        rollup_id,
                        project_id,
                        exc_info=True,
                    )
        except Exception:
            logger.warning(
                "[MERGE_CREDIT_BACKFILL] backfill failed for landed rollup %s "
                "(project %s) — non-fatal",
                rollup_id,
                project_id,
                exc_info=True,
            )
            # A mid-rollup failure may have left uncommitted stamps; drop them so
            # the next rollup starts from a clean transaction.
            try:
                await session.rollback()
            except Exception:
                logger.debug(
                    "[MERGE_CREDIT_BACKFILL] rollback after failure raised",
                    exc_info=True,
                )

    return total_stamped


async def _sweep_merge_credit_backfill() -> None:
    """Leader-cycle sweep: backfill merge credit for landed rollups.

    Runs on a modest cadence (~600s). Iterates active (non-archived) projects,
    bounded per tick and advancing a rotating cursor across ticks so no project
    is starved, and heals each independently — a failure on one project is
    logged and skipped without blocking others. Best-effort/non-fatal, mirroring
    :func:`app.recovery_infrastructure._sweep_lane_baseline_backfill`.
    """
    global _project_backfill_cursor
    try:
        async with services.AsyncSessionLocal() as session:
            project_query = select(Project.id).where(Project.archived.is_(False))
            if _project_backfill_cursor is not None:
                # Resume past the previous batch (keyset pagination over the id).
                project_query = project_query.where(
                    Project.id > _project_backfill_cursor
                )
            project_ids = list(
                (
                    await session.execute(
                        project_query.order_by(Project.id).limit(
                            _MERGE_CREDIT_BACKFILL_PROJECT_BATCH
                        )
                    )
                )
                .scalars()
                .all()
            )

        # Advance the cursor so the next tick resumes past this batch; a short
        # page means the end of the project list, so wrap back to the start.
        if len(project_ids) < _MERGE_CREDIT_BACKFILL_PROJECT_BATCH:
            _project_backfill_cursor = None
        else:
            _project_backfill_cursor = project_ids[-1]

        total = 0
        for project_id in project_ids:
            try:
                async with services.AsyncSessionLocal() as session:
                    stamped = await backfill_landed_rollup_merge_credit(
                        project_id,
                        session,
                        rollup_limit=_MERGE_CREDIT_BACKFILL_ROLLUP_BATCH,
                    )
                    total += stamped
            except Exception:
                logger.warning(
                    "[MERGE_CREDIT_BACKFILL] backfill sweep: project=%s failed "
                    "(non-fatal)",
                    project_id,
                    exc_info=True,
                )
        if total:
            logger.info(
                "[MERGE_CREDIT_BACKFILL] backfill: re-stamped %d landed rollup "
                "descendant(s) across %d project(s)",
                total,
                len(project_ids),
            )
    except Exception:
        logger.exception("[MERGE_CREDIT_BACKFILL] merge-credit backfill sweep failed")

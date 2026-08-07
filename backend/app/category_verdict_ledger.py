"""Accessor module for the RE-GRADEABLE category/residual verdict ledger.

Persistence layer of the goal-verification redesign.  Where the legacy
``CriterionEvidence`` store was *monotonic* — a criterion, once it had any
evidence row, stayed "covered" forever even after a later candidate regressed —
this ledger records ONE verdict PER ``(version_id, candidate_sha, category_key)``
that a re-grade OVERWRITES.  A category that regresses on a newer candidate tree
flips back to ``not_met`` instead of remaining satisfied.

Two tables back this module (both in :mod:`app.release_models`):

  * :class:`~app.models.CategoryVerdict` — the per-category verdict; upserting
    one category's row never touches another category's row (within-candidate
    isolation).
  * :class:`~app.models.ResidualVerdict` — per-candidate "is any goal aspect
    uncaptured by the authored categories?".  A non-null ``uncaptured_gap``
    means the candidate is not fully met even when every category is ``met``.

The active category set is authored as
:class:`~app.models.VersionAcceptanceCriterion` rows: ``category_key`` here
corresponds to a criterion's ``criterion_key``.  :func:`is_candidate_fully_met`
resolves the ACTIVE criteria (``status == "active"``) and is fail-closed — an
active criterion with no ``met`` verdict for the candidate counts as NOT met.

All upserts use a portable SELECT-then-INSERT/UPDATE (not a dialect-specific
``ON CONFLICT``) so the same helper runs on the SQLite test gate and the
Postgres runtime.  They ``flush`` but do NOT ``commit`` — the caller owns the
transaction boundary (mirrors :func:`app.acceptance_criteria.stage_criterion`).
"""

from __future__ import annotations

import logging
import uuid
from typing import TYPE_CHECKING, Any

from sqlmodel import select

from app.models import (
    CategoryVerdict,
    ResidualVerdict,
    VersionAcceptanceCriterion,
)
from app.time_utils import _utcnow

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger(__name__)


async def upsert_category_verdict(
    session: AsyncSession,
    *,
    version_id: uuid.UUID,
    candidate_sha: str | None,
    category_key: str,
    status: str,
    tier: str,
    gap: str | None,
    citations: Any | None,
    grader_run_id: str | None,
    qualification_attempt_id: uuid.UUID | None = None,
    project_id: uuid.UUID | None = None,
) -> CategoryVerdict:
    """UPSERT the verdict for ONE ``(version_id, candidate_sha, category_key,
    qualification_attempt_id)``.

    SELECT-then-INSERT/UPDATE on the unique key so a re-grade of a single
    category rewrites ONLY its own row (latest write wins) and never touches
    another category's verdict for the same candidate (within-candidate
    isolation).  Flushes but does not commit; returns the persisted row.

    Also gives the verdict a durable consequence in the completion-debt ledger
    (best-effort, never raises): a ``not_met``/``partial`` status opens (or
    keeps open) an ``objective_gap`` debt entry keyed on ``(version_id,
    category_key)``; a ``met`` status resolves it.  ``project_id`` scopes the
    entry to its owning project when the caller has it; omitted, the entry is
    deployment-global (still uniquely identified by its dedupe key).
    """
    existing = (
        await session.execute(
            select(CategoryVerdict).where(
                CategoryVerdict.version_id == version_id,
                CategoryVerdict.candidate_sha == candidate_sha,
                CategoryVerdict.category_key == category_key,
                CategoryVerdict.qualification_attempt_id == qualification_attempt_id,
            )
        )
    ).scalar_one_or_none()

    if existing is not None:
        existing.status = status
        existing.tier = tier
        existing.gap = gap
        existing.citations = citations
        existing.grader_run_id = grader_run_id
        existing.qualification_attempt_id = qualification_attempt_id
        existing.graded_at = _utcnow()
        session.add(existing)
        await session.flush()
        await _sync_objective_gap_debt(
            session,
            version_id=version_id,
            category_key=category_key,
            status=status,
            gap=gap,
            candidate_sha=candidate_sha,
            project_id=project_id,
        )
        return existing

    row = CategoryVerdict(
        version_id=version_id,
        candidate_sha=candidate_sha,
        category_key=category_key,
        status=status,
        tier=tier,
        gap=gap,
        citations=citations,
        grader_run_id=grader_run_id,
        qualification_attempt_id=qualification_attempt_id,
        graded_at=_utcnow(),
    )
    session.add(row)
    await session.flush()
    await _sync_objective_gap_debt(
        session,
        version_id=version_id,
        category_key=category_key,
        status=status,
        gap=gap,
        candidate_sha=candidate_sha,
        project_id=project_id,
    )
    return row


async def _sync_objective_gap_debt(
    session: AsyncSession,
    *,
    version_id: uuid.UUID,
    category_key: str | None,
    status: str,
    gap: str | None,
    candidate_sha: str | None,
    project_id: uuid.UUID | None,
) -> None:
    """Give a category verdict its completion-debt consequence (best-effort).

    ``status == "met"`` resolves the open ``objective_gap`` debt for this
    category (if any).  Any other status (``not_met`` / ``partial``) opens —
    or keeps open — the debt.  Never raises: a debt-ledger hiccup must not
    break grading.
    """
    if not category_key:
        return
    try:
        from app import completion_debt

        dedupe_key = completion_debt.objective_gap_dedupe_key(version_id, category_key)
        if status == "met":
            await completion_debt.aresolve_completion_debt(
                session,
                dedupe_key,
                reason=f"category '{category_key}' graded met on candidate {candidate_sha}",
            )
        else:
            await completion_debt.arecord_completion_debt(
                session,
                dedupe_key=dedupe_key,
                kind=completion_debt.KIND_OBJECTIVE_GAP,
                title=f"Acceptance category '{category_key}' unmet",
                project_id=project_id,
                source="release_merge_grade",
                detail={
                    "version_id": str(version_id),
                    "category_key": category_key,
                    "status": status,
                    "gap": gap,
                    "candidate_sha": candidate_sha,
                },
            )
    except Exception:  # noqa: BLE001 — ledger consequence must not break grading
        logger.warning(
            "[CATEGORY_VERDICT_LEDGER] failed to sync objective_gap debt "
            "(version=%s category=%s status=%s)",
            version_id,
            category_key,
            status,
            exc_info=True,
        )


async def get_category_verdicts(
    session: AsyncSession,
    version_id: uuid.UUID,
    candidate_sha: str | None,
    qualification_attempt_id: uuid.UUID | None = None,
) -> list[CategoryVerdict]:
    """Return every category verdict recorded for one candidate tree.

    When ``qualification_attempt_id`` is provided, filter to only verdicts
    recorded by that attempt, enabling same-SHA multi-attempt provenance.
    """
    clauses = [
        CategoryVerdict.version_id == version_id,
        CategoryVerdict.candidate_sha == candidate_sha,
    ]
    if qualification_attempt_id is not None:
        clauses.append(
            CategoryVerdict.qualification_attempt_id == qualification_attempt_id
        )
    result = await session.execute(
        select(CategoryVerdict).where(*clauses)
    )
    return list(result.scalars().all())


async def is_candidate_fully_met(
    session: AsyncSession,
    version_id: uuid.UUID,
    candidate_sha: str | None,
    qualification_attempt_id: uuid.UUID | None = None,
) -> bool:
    """True iff every ACTIVE acceptance category is ``met`` and no residual gap.

    The active category set is the ``VersionAcceptanceCriterion`` rows with
    ``status == "active"`` for the version (matched by ``criterion_key`` ==
    ``CategoryVerdict.category_key``).  Fail-closed: an active criterion with no
    verdict — or a verdict whose status is not ``met`` — for this candidate
    makes the candidate NOT fully met.  Additionally a non-null/non-empty
    :class:`ResidualVerdict.uncaptured_gap` blocks (a goal aspect no category
    captures).

    When ``qualification_attempt_id`` is provided, only verdicts recorded by
    that attempt are considered — prevents two attempts each half-meeting
    different categories from producing a false-positive fully-met result.

    Note: with zero active criteria the "every active is met" clause is
    vacuously satisfied, so the result is governed solely by the residual — the
    core author should pair this with an explicit "version has criteria" check
    if an empty acceptance set must not qualify as met.
    """
    active_result = await session.execute(
        select(VersionAcceptanceCriterion.criterion_key).where(
            VersionAcceptanceCriterion.release_version_id == version_id,
            VersionAcceptanceCriterion.status == "active",
        )
    )
    active_keys = {k for k in active_result.scalars().all() if k is not None}

    verdicts = await get_category_verdicts(
        session, version_id, candidate_sha,
        qualification_attempt_id=qualification_attempt_id,
    )
    met_keys = {v.category_key for v in verdicts if v.status == "met"}

    if not active_keys.issubset(met_keys):
        return False

    # A non-empty uncaptured residual gap blocks even when every category is met.
    residual = await get_residual(
        session, version_id, candidate_sha,
        qualification_attempt_id=qualification_attempt_id,
    )
    return not residual


async def upsert_residual(
    session: AsyncSession,
    version_id: uuid.UUID,
    candidate_sha: str | None,
    uncaptured_gap: str | None,
    qualification_attempt_id: uuid.UUID | None = None,
    project_id: uuid.UUID | None = None,
) -> None:
    """UPSERT the residual "uncaptured goal aspect" for one candidate tree.

    SELECT-then-INSERT/UPDATE on the unique ``(version_id, candidate_sha,
    qualification_attempt_id)`` key.  Passing ``uncaptured_gap=None`` records/clears
    the residual to "no gap".  Flushes but does not commit.

    Also gives the residual its completion-debt consequence (best-effort,
    never raises), mirroring ``upsert_category_verdict``'s per-category
    ``objective_gap`` sync: a non-empty ``uncaptured_gap`` opens — or keeps
    open — an ``objective_gap`` debt entry keyed on the residual sentinel
    category; an empty/None gap resolves it. ``project_id`` scopes the entry
    to its owning project when the caller has it.
    """
    existing = (
        await session.execute(
            select(ResidualVerdict).where(
                ResidualVerdict.version_id == version_id,
                ResidualVerdict.candidate_sha == candidate_sha,
                ResidualVerdict.qualification_attempt_id == qualification_attempt_id,
            )
        )
    ).scalar_one_or_none()

    from app.completion_debt import OBJECTIVE_GAP_RESIDUAL_KEY as _residual_key

    if existing is not None:
        existing.uncaptured_gap = uncaptured_gap
        existing.qualification_attempt_id = qualification_attempt_id
        existing.graded_at = _utcnow()
        session.add(existing)
        await session.flush()
        await _sync_objective_gap_debt(
            session,
            version_id=version_id,
            category_key=_residual_key,
            status="met" if not uncaptured_gap else "residual",
            gap=uncaptured_gap,
            candidate_sha=candidate_sha,
            project_id=project_id,
        )
        return

    session.add(
        ResidualVerdict(
            version_id=version_id,
            candidate_sha=candidate_sha,
            uncaptured_gap=uncaptured_gap,
            qualification_attempt_id=qualification_attempt_id,
            graded_at=_utcnow(),
        )
    )
    await session.flush()
    await _sync_objective_gap_debt(
        session,
        version_id=version_id,
        category_key=_residual_key,
        status="met" if not uncaptured_gap else "residual",
        gap=uncaptured_gap,
        candidate_sha=candidate_sha,
        project_id=project_id,
    )


async def get_residual(
    session: AsyncSession,
    version_id: uuid.UUID,
    candidate_sha: str | None,
    qualification_attempt_id: uuid.UUID | None = None,
) -> str | None:
    """Return the recorded ``uncaptured_gap`` for a candidate, or None if absent.

    When ``qualification_attempt_id`` is provided, filter to the residual recorded
    by that attempt, enabling same-SHA multi-attempt provenance.
    None is returned both when no residual row exists and when the row exists
    with a null gap — both mean "no uncaptured goal aspect".
    """
    clauses = [
        ResidualVerdict.version_id == version_id,
        ResidualVerdict.candidate_sha == candidate_sha,
    ]
    if qualification_attempt_id is not None:
        clauses.append(
            ResidualVerdict.qualification_attempt_id == qualification_attempt_id
        )
    # Order by recency so that when no attempt filter is applied (multiple rows
    # possible — e.g. a NULL-attempt_id pre-PR row and a post-PR row) we get
    # the most recent verdict rather than raising MultipleResultsFound.
    existing = (
        await session.execute(
            select(ResidualVerdict)
            .where(*clauses)
            .order_by(ResidualVerdict.graded_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    if existing is None:
        return None
    return existing.uncaptured_gap

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
) -> CategoryVerdict:
    """UPSERT the verdict for ONE ``(version_id, candidate_sha, category_key)``.

    SELECT-then-INSERT/UPDATE on the unique key so a re-grade of a single
    category rewrites ONLY its own row (latest write wins) and never touches
    another category's verdict for the same candidate (within-candidate
    isolation).  Flushes but does not commit; returns the persisted row.
    """
    existing = (
        await session.execute(
            select(CategoryVerdict).where(
                CategoryVerdict.version_id == version_id,
                CategoryVerdict.candidate_sha == candidate_sha,
                CategoryVerdict.category_key == category_key,
            )
        )
    ).scalar_one_or_none()

    if existing is not None:
        existing.status = status
        existing.tier = tier
        existing.gap = gap
        existing.citations = citations
        existing.grader_run_id = grader_run_id
        existing.graded_at = _utcnow()
        session.add(existing)
        await session.flush()
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
        graded_at=_utcnow(),
    )
    session.add(row)
    await session.flush()
    return row


async def get_category_verdicts(
    session: AsyncSession,
    version_id: uuid.UUID,
    candidate_sha: str | None,
) -> list[CategoryVerdict]:
    """Return every category verdict recorded for one candidate tree."""
    result = await session.execute(
        select(CategoryVerdict).where(
            CategoryVerdict.version_id == version_id,
            CategoryVerdict.candidate_sha == candidate_sha,
        )
    )
    return list(result.scalars().all())


async def is_candidate_fully_met(
    session: AsyncSession,
    version_id: uuid.UUID,
    candidate_sha: str | None,
) -> bool:
    """True iff every ACTIVE acceptance category is ``met`` and no residual gap.

    The active category set is the ``VersionAcceptanceCriterion`` rows with
    ``status == "active"`` for the version (matched by ``criterion_key`` ==
    ``CategoryVerdict.category_key``).  Fail-closed: an active criterion with no
    verdict — or a verdict whose status is not ``met`` — for this candidate
    makes the candidate NOT fully met.  Additionally a non-null/non-empty
    :class:`ResidualVerdict.uncaptured_gap` blocks (a goal aspect no category
    captures).

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

    verdicts = await get_category_verdicts(session, version_id, candidate_sha)
    met_keys = {v.category_key for v in verdicts if v.status == "met"}

    if not active_keys.issubset(met_keys):
        return False

    # A non-empty uncaptured residual gap blocks even when every category is met.
    residual = await get_residual(session, version_id, candidate_sha)
    return not residual


async def upsert_residual(
    session: AsyncSession,
    version_id: uuid.UUID,
    candidate_sha: str | None,
    uncaptured_gap: str | None,
) -> None:
    """UPSERT the residual "uncaptured goal aspect" for one candidate tree.

    SELECT-then-INSERT/UPDATE on the unique ``(version_id, candidate_sha)`` key.
    Passing ``uncaptured_gap=None`` records/clears the residual to "no gap".
    Flushes but does not commit.
    """
    existing = (
        await session.execute(
            select(ResidualVerdict).where(
                ResidualVerdict.version_id == version_id,
                ResidualVerdict.candidate_sha == candidate_sha,
            )
        )
    ).scalar_one_or_none()

    if existing is not None:
        existing.uncaptured_gap = uncaptured_gap
        existing.graded_at = _utcnow()
        session.add(existing)
        await session.flush()
        return

    session.add(
        ResidualVerdict(
            version_id=version_id,
            candidate_sha=candidate_sha,
            uncaptured_gap=uncaptured_gap,
            graded_at=_utcnow(),
        )
    )
    await session.flush()


async def get_residual(
    session: AsyncSession,
    version_id: uuid.UUID,
    candidate_sha: str | None,
) -> str | None:
    """Return the recorded ``uncaptured_gap`` for a candidate, or None if absent.

    None is returned both when no residual row exists and when the row exists
    with a null gap — both mean "no uncaptured goal aspect".
    """
    existing = (
        await session.execute(
            select(ResidualVerdict).where(
                ResidualVerdict.version_id == version_id,
                ResidualVerdict.candidate_sha == candidate_sha,
            )
        )
    ).scalar_one_or_none()
    if existing is None:
        return None
    return existing.uncaptured_gap

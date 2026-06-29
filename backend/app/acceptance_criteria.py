"""Accessor module for version-scoped acceptance criteria and coverage computation.

Provides async helpers for creating and querying :class:`~app.models.VersionAcceptanceCriterion`
and :class:`~app.models.CriterionEvidence` rows, plus a pure-function coverage
classifier and a version-level coverage summary query.
"""

from __future__ import annotations

import logging
import secrets
import uuid
from typing import TYPE_CHECKING, Any

from sqlmodel import select

from app.models import CriterionEvidence, VersionAcceptanceCriterion

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger(__name__)

# source_kind values that indicate a criterion is fully covered.
_COVERED_SOURCE_KINDS: frozenset[str] = frozenset({"task_merge", "reachability"})


def generate_criterion_key() -> str:
    """Return a stable 'c-<8hexchars>' key for a new acceptance criterion."""
    return f"c-{secrets.token_hex(4)}"


async def create_criterion(
    session: AsyncSession,
    project_id: uuid.UUID,
    release_version_id: uuid.UUID,
    text: str,
    created_by: str | None = None,
) -> VersionAcceptanceCriterion:
    """Create and persist a new acceptance criterion for a release version."""
    criterion = VersionAcceptanceCriterion(
        project_id=project_id,
        release_version_id=release_version_id,
        criterion_key=generate_criterion_key(),
        text=text,
        created_by=created_by,
    )
    session.add(criterion)
    await session.commit()
    await session.refresh(criterion)
    return criterion


async def get_criteria_for_version(
    session: AsyncSession,
    release_version_id: uuid.UUID,
) -> list[VersionAcceptanceCriterion]:
    """Return all acceptance criteria for a version, ordered by created_at."""
    result = await session.execute(
        select(VersionAcceptanceCriterion)
        .where(VersionAcceptanceCriterion.release_version_id == release_version_id)
        .order_by(VersionAcceptanceCriterion.created_at)
    )
    return list(result.scalars().all())


async def record_evidence(
    session: AsyncSession,
    criterion_id: uuid.UUID,
    project_id: uuid.UUID,
    source_kind: str,
    source_id: str | None = None,
    detail: dict[str, Any] | None = None,
) -> CriterionEvidence:
    """Append one evidence record for a criterion (best-effort).

    Callers should not let a failure here block their main path — errors are
    logged but not re-raised.
    """
    evidence = CriterionEvidence(
        criterion_id=criterion_id,
        project_id=project_id,
        source_kind=source_kind,
        source_id=source_id,
        detail=detail,
    )
    session.add(evidence)
    try:
        await session.commit()
        await session.refresh(evidence)
    except Exception:
        logger.exception(
            "record_evidence: failed to persist evidence for criterion %s", criterion_id
        )
        await session.rollback()
    return evidence


async def get_evidence_for_criterion(
    session: AsyncSession,
    criterion_id: uuid.UUID,
) -> list[CriterionEvidence]:
    """Return all evidence records for a criterion, ordered by recorded_at."""
    result = await session.execute(
        select(CriterionEvidence)
        .where(CriterionEvidence.criterion_id == criterion_id)
        .order_by(CriterionEvidence.recorded_at)
    )
    return list(result.scalars().all())


def compute_criterion_coverage(evidence: list[CriterionEvidence]) -> str:
    """Classify criterion coverage from its evidence list.

    Returns:
      'covered'   — at least one record with source_kind in {'task_merge', 'reachability'}
      'partial'   — evidence exists but none satisfy the covered condition
      'uncovered' — no evidence at all
    """
    if not evidence:
        return "uncovered"
    if any(e.source_kind in _COVERED_SOURCE_KINDS for e in evidence):
        return "covered"
    return "partial"


async def get_version_coverage_summary(
    session: AsyncSession,
    release_version_id: uuid.UUID,
) -> dict[str, Any]:
    """Return a coverage summary dict for all criteria on a release version.

    Shape::

        {
            'total': N,
            'covered': N,
            'partial': N,
            'uncovered': N,
            'criteria': [
                {
                    'criterion_key': str,
                    'text': str,
                    'coverage': 'covered' | 'partial' | 'uncovered',
                    'evidence_count': int,
                },
                ...
            ],
        }
    """
    criteria = await get_criteria_for_version(session, release_version_id)

    counts: dict[str, int] = {"covered": 0, "partial": 0, "uncovered": 0}
    criteria_detail = []

    for criterion in criteria:
        evidence = await get_evidence_for_criterion(session, criterion.id)
        coverage = compute_criterion_coverage(evidence)
        counts[coverage] += 1
        criteria_detail.append(
            {
                "criterion_key": criterion.criterion_key,
                "text": criterion.text,
                "coverage": coverage,
                "evidence_count": len(evidence),
            }
        )

    return {
        "total": len(criteria),
        "covered": counts["covered"],
        "partial": counts["partial"],
        "uncovered": counts["uncovered"],
        "criteria": criteria_detail,
    }

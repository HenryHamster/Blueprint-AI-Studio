"""Accessor module for version-scoped acceptance criteria and coverage computation.

Provides async helpers for creating and querying :class:`~app.models.VersionAcceptanceCriterion`
and :class:`~app.models.CriterionEvidence` rows, plus a pure-function coverage
classifier and a version-level coverage summary query.
"""

from __future__ import annotations

import logging
import secrets
import uuid
from collections import defaultdict
from typing import TYPE_CHECKING, Any

from sqlmodel import select

from app.models import CriterionEvidence, VersionAcceptanceCriterion

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger(__name__)

def _evidence_proves_covered(e: CriterionEvidence) -> bool:
    """Whether a single evidence record satisfies the 'covered' condition.

    ``reachability`` is covered on its own; it represents the ancestry check
    that says the merge really reached the integration branch.
    ``task_merge`` requires ``proven=True`` in ``detail`` — a bare merge badge
    without acceptance-proof is not sufficient to qualify the criterion.
    All other source_kinds are not sufficient on their own.
    """
    if e.source_kind == "reachability":
        return True
    if e.source_kind == "task_merge":
        return isinstance(e.detail, dict) and e.detail.get("proven") is True
    return False


MAX_CRITERIA_PER_VERSION = 50


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
    """Create and persist a new acceptance criterion for a release version.

    Commits the session immediately — use :func:`stage_criterion` when calling
    from inside a larger transaction that must commit once at the end.
    """
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


async def stage_criterion(
    session: AsyncSession,
    project_id: uuid.UUID,
    release_version_id: uuid.UUID,
    text: str,
    created_by: str | None = None,
) -> VersionAcceptanceCriterion:
    """Stage a new acceptance criterion without committing the session.

    Unlike :func:`create_criterion`, this only calls ``session.add()`` — the
    caller is responsible for committing (or flushing) the session.  Use this
    from within a larger transaction (e.g. strategic-completion handlers) where
    a mid-transaction ``session.commit()`` would break the surrounding
    atomicity guarantee and cause one commit per criterion.
    """
    criterion = VersionAcceptanceCriterion(
        project_id=project_id,
        release_version_id=release_version_id,
        criterion_key=generate_criterion_key(),
        text=text,
        created_by=created_by,
    )
    session.add(criterion)
    return criterion


async def get_criterion_by_key(
    session: AsyncSession,
    release_version_id: uuid.UUID,
    criterion_key: str,
) -> VersionAcceptanceCriterion | None:
    """Return a single criterion by its criterion_key within a release version."""
    result = await session.execute(
        select(VersionAcceptanceCriterion).where(
            VersionAcceptanceCriterion.release_version_id == release_version_id,
            VersionAcceptanceCriterion.criterion_key == criterion_key,
        )
    )
    return result.scalars().first()


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


async def get_criterion_by_text(
    session: AsyncSession,
    project_id: uuid.UUID,
    release_version_id: uuid.UUID,
    text: str,
) -> VersionAcceptanceCriterion | None:
    """Return an existing criterion matching (project_id, release_version_id, text), or None."""
    result = await session.execute(
        select(VersionAcceptanceCriterion).where(
            VersionAcceptanceCriterion.project_id == project_id,
            VersionAcceptanceCriterion.release_version_id == release_version_id,
            VersionAcceptanceCriterion.text == text,
        )
    )
    return result.scalar_one_or_none()


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
      'covered'   — at least one record satisfies the covered condition
                    (``reachability`` always; ``task_merge`` only with
                    ``detail["proven"] is True``)
      'partial'   — evidence exists but none satisfy the covered condition
      'uncovered' — no evidence at all
    """
    if not evidence:
        return "uncovered"
    if any(_evidence_proves_covered(e) for e in evidence):
        return "covered"
    return "partial"


async def get_criteria_by_keys(
    session: AsyncSession,
    project_id: uuid.UUID,
    keys: list[str],
    release_version_id: uuid.UUID | None = None,
) -> list[VersionAcceptanceCriterion]:
    """Return VersionAcceptanceCriterion rows matching the given criterion_key list.

    When *release_version_id* is supplied the lookup is scoped to that version,
    preventing stale keys from a closed version from matching.  Callers that
    resolve keys on behalf of the current open version SHOULD pass it.
    """
    if not keys:
        return []
    conditions = [
        VersionAcceptanceCriterion.project_id == project_id,
        VersionAcceptanceCriterion.criterion_key.in_(keys),
    ]
    if release_version_id is not None:
        conditions.append(
            VersionAcceptanceCriterion.release_version_id == release_version_id
        )
    result = await session.execute(
        select(VersionAcceptanceCriterion).where(*conditions)
    )
    return list(result.scalars().all())


async def record_task_merge_evidence(
    session: AsyncSession,
    task: Any,
    project_id: uuid.UUID,
) -> int:
    """Record task_merge evidence for each criterion a task carries, when proven.

    No-op when ``task.criterion_ids`` is empty or the task's acceptance verdict
    is absent or not ``proven=True``.  When it does write, it stamps a
    proof-backed ``task_merge`` row (``detail["proven"] = True``) per criterion,
    plus a companion ``reachability`` row.  Coverage flips to ``covered`` on
    either signal alone — a proven ``task_merge`` row is itself sufficient;
    the companion ``reachability`` row is recorded for audit completeness.
    Best-effort: errors are logged and not re-raised so a failure here never
    blocks merge finalization.

    Returns the number of evidence rows recorded.
    """
    criterion_ids: list[str] = list(getattr(task, "criterion_ids", None) or [])
    if not criterion_ids:
        return 0
    from app.acceptance_gate import CODE_ACCEPTANCE_VERDICT_KEY, acceptance_is_proven

    verdict = (getattr(task, "context", None) or {}).get(CODE_ACCEPTANCE_VERDICT_KEY)
    if not acceptance_is_proven(verdict):
        return 0
    try:
        _release_version_id: uuid.UUID | None = None
        try:
            from app.release_version import current_open_version as _cur_open_ver

            _open_ver = await _cur_open_ver(session, project_id)
            if _open_ver is not None:
                _release_version_id = _open_ver.id
        except Exception:
            pass
        rows = await get_criteria_by_keys(
            session, project_id, criterion_ids, release_version_id=_release_version_id
        )
        task_id_str = str(getattr(task, "id", "") or "")
        recorded = 0
        for row in rows:
            await record_evidence(
                session,
                criterion_id=row.id,
                project_id=project_id,
                source_kind="task_merge",
                source_id=task_id_str or None,
                detail={"proven": True},
            )
            await record_evidence(
                session,
                criterion_id=row.id,
                project_id=project_id,
                source_kind="reachability",
                source_id=task_id_str or None,
                detail={"proven": True},
            )
            recorded += 1
        return recorded
    except Exception:
        logger.exception(
            "record_task_merge_evidence: failed for task %s",
            getattr(task, "id", None),
        )
        return 0


async def delete_criterion(
    session: AsyncSession,
    project_id: uuid.UUID,
    release_version_id: uuid.UUID,
    criterion_key: str,
) -> bool:
    """Delete a criterion scoped to the given version.

    Returns True if the row was found and deleted, False if not found.
    """
    result = await session.execute(
        select(VersionAcceptanceCriterion).where(
            VersionAcceptanceCriterion.project_id == project_id,
            VersionAcceptanceCriterion.release_version_id == release_version_id,
            VersionAcceptanceCriterion.criterion_key == criterion_key,
        )
    )
    row = result.scalar_one_or_none()
    if row is None:
        return False
    await session.delete(row)
    await session.commit()
    return True


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
    counts: dict[str, int] = {"covered": 0, "partial": 0, "uncovered": 0}
    criteria_detail = []

    result = await session.execute(
        select(VersionAcceptanceCriterion, CriterionEvidence)
        .outerjoin(
            CriterionEvidence,
            CriterionEvidence.criterion_id == VersionAcceptanceCriterion.id,
        )
        .where(VersionAcceptanceCriterion.release_version_id == release_version_id)
        .order_by(VersionAcceptanceCriterion.created_at, CriterionEvidence.recorded_at)
    )

    criteria_in_order: list[VersionAcceptanceCriterion] = []
    evidence_by_criterion: dict[uuid.UUID, list[CriterionEvidence]] = defaultdict(list)
    seen_criteria: set[uuid.UUID] = set()

    for criterion, evidence in result.all():
        if criterion.id not in seen_criteria:
            seen_criteria.add(criterion.id)
            criteria_in_order.append(criterion)
        if evidence is not None:
            evidence_by_criterion[criterion.id].append(evidence)

    for criterion in criteria_in_order:
        evidence = evidence_by_criterion.get(criterion.id, [])
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
        "total": len(criteria_in_order),
        "covered": counts["covered"],
        "partial": counts["partial"],
        "uncovered": counts["uncovered"],
        "criteria": criteria_detail,
    }

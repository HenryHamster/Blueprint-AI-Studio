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

import sqlalchemy.exc
from sqlalchemy import func
from sqlmodel import select

from app.models import CriterionEvidence, Direction, VersionAcceptanceCriterion
from app.time_utils import _utcnow

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger(__name__)

# Mirror of research_direction_state.RETIRED_DIRECTION_STATUSES. Defined locally
# (rather than imported at module load) because research_direction_state pulls in
# heavy modules and acceptance_criteria is imported early by release_qualifier —
# importing it at top-level risks an import cycle. The set is tiny and stable.
_RETIRED_DIRECTION_STATUSES = frozenset(
    {"expired", "retired", "superseded", "completed"}
)

def _evidence_proves_covered(e: CriterionEvidence) -> bool:
    """Whether a single evidence record satisfies the 'covered' condition.

    ``reachability`` is covered on its own; it represents the ancestry check
    that says the merge really reached the integration branch.
    ``operator_attestation`` is covered on its own; the operator has explicitly
    accepted the criterion, overriding any stuck-criterion deadlock.
    ``task_merge`` requires ``proven=True`` in ``detail`` — a bare merge badge
    without acceptance-proof is not sufficient to qualify the criterion.
    All other source_kinds are not sufficient on their own.
    """
    if e.source_kind == "reachability":
        return True
    if e.source_kind == "operator_attestation":
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


async def count_criteria_for_version(
    session: AsyncSession,
    release_version_id: uuid.UUID,
) -> int:
    """Return the count of acceptance criteria for a release version."""
    result = await session.execute(
        select(func.count()).where(
            VersionAcceptanceCriterion.release_version_id == release_version_id
        )
    )
    return result.scalar_one()


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
    except sqlalchemy.exc.IntegrityError:
        # Unique constraint violation: concurrent caller already recorded this
        # evidence. The constraint turning a race into a no-op is expected.
        await session.rollback()
        logger.debug(
            "record_evidence: duplicate evidence skipped for criterion %s "
            "(source_kind=%s source_id=%s)",
            criterion_id,
            source_kind,
            source_id,
        )
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
            logger.warning(
                "record_task_merge_evidence: failed to resolve open version "
                "for task %s (project %s); merge evidence not recorded",
                getattr(task, "id", None),
                project_id,
                exc_info=True,
            )
        if _release_version_id is None:
            return 0
        rows = await get_criteria_by_keys(
            session, project_id, criterion_ids, release_version_id=_release_version_id
        )
        task_id_str = str(getattr(task, "id", "") or "")
        recorded = 0
        for row in rows:
            existing = {
                (e.source_kind, e.source_id)
                for e in await get_evidence_for_criterion(session, row.id)
            }
            source_id = task_id_str or None
            wrote = False
            if ("task_merge", source_id) not in existing:
                await record_evidence(
                    session,
                    criterion_id=row.id,
                    project_id=project_id,
                    source_kind="task_merge",
                    source_id=source_id,
                    detail={"proven": True},
                )
                wrote = True
            if ("reachability", source_id) not in existing:
                await record_evidence(
                    session,
                    criterion_id=row.id,
                    project_id=project_id,
                    source_kind="reachability",
                    source_id=source_id,
                    detail={"proven": True},
                )
                wrote = True
            if wrote:
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


async def retract_criterion(
    session: AsyncSession,
    project_id: uuid.UUID,
    release_version_id: uuid.UUID,
    criterion_key: str,
    reason: str,
    retracted_by: str | None = None,
    superseded_by_key: str | None = None,
) -> VersionAcceptanceCriterion | None:
    """Retract or supersede a criterion without deleting its audit history.

    Returns the scoped row when found and ``None`` when the key does not belong
    to the requested project/version. The transition is idempotent: a criterion
    already marked non-active is returned unchanged.
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
        return None
    if row.status == "active":
        row.status = "superseded" if superseded_by_key else "retracted"
        row.retraction_reason = reason
        row.retracted_at = _utcnow()
        row.retracted_by = retracted_by
        row.superseded_by_key = superseded_by_key
        session.add(row)
        await session.commit()
        await session.refresh(row)
    return row


async def _classify_stuck_reasons(
    session: AsyncSession,
    project_id: uuid.UUID,
    uncovered_keys: list[str],
) -> dict[str, str | None]:
    """Diagnose WHY each uncovered criterion is uncovered.

    Returns a mapping ``criterion_key -> stuck_reason`` where ``stuck_reason`` is:

      ``"no_mapping"``        — no Direction in the project lists this key in its
                                ``criterion_keys``;
      ``"direction_retired"`` — at least one Direction maps this key but *every*
                                mapping Direction has a retired status
                                (``expired``/``retired``/``superseded``/``completed``);
      ``None``                — at least one *active* (non-retired) Direction maps
                                this key, so the criterion is in-progress, not stuck.

    A single bulk query fetches every project Direction, then classification
    happens in Python — no N+1. The empty-``criterion_keys`` filter and the
    per-key membership test both run on the deserialized Python list rather than
    via a DB-specific JSONB operator, so the helper is portable across Postgres
    (production) and SQLite (some tests) — ``jsonb <> varchar`` is not a valid
    Postgres operator, and JSONB containment is not available in SQLite.
    """
    if not uncovered_keys:
        return {}

    result = await session.execute(
        select(Direction).where(Direction.project_id == project_id)
    )
    directions = list(result.scalars().all())

    # Build key -> set of statuses of the directions that reference it.
    # Directions with an empty criterion_keys list contribute nothing.
    key_statuses: dict[str, set[str]] = defaultdict(set)
    for direction in directions:
        keys = direction.criterion_keys or []
        for key in keys:
            key_statuses[key].add(direction.status)

    reasons: dict[str, str | None] = {}
    for key in uncovered_keys:
        statuses = key_statuses.get(key)
        if not statuses:
            reasons[key] = "no_mapping"
        elif statuses <= _RETIRED_DIRECTION_STATUSES:
            reasons[key] = "direction_retired"
        else:
            reasons[key] = None
    return reasons


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
            'retracted': N,
            'stuck_count': N,
            'criteria': [
                {
                    'criterion_key': str,
                    'text': str,
                    'coverage': 'covered' | 'partial' | 'uncovered',
                    'status': 'active' | 'retracted' | 'superseded',
                    'evidence_count': int,
                    'stuck_reason': 'no_mapping' | 'direction_retired' | None,
                },
                ...
            ],
        }

    ``stuck_reason`` is only non-``None`` for *uncovered* criteria that have no
    active Direction mapping (see :func:`_classify_stuck_reasons`); covered and
    partial criteria always carry ``None``. ``stuck_count`` is the number of
    uncovered criteria whose ``stuck_reason`` is non-``None`` — i.e. criteria
    that need direction remapping or operator attestation to ever become
    covered. These fields are strictly additive; the pre-existing keys are
    unchanged.
    """
    counts: dict[str, int] = {"covered": 0, "partial": 0, "uncovered": 0}
    retracted_count = 0
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

    coverage_by_criterion: dict[uuid.UUID, str] = {}
    for criterion in criteria_in_order:
        evidence = evidence_by_criterion.get(criterion.id, [])
        coverage = compute_criterion_coverage(evidence)
        coverage_by_criterion[criterion.id] = coverage
        if criterion.status == "active":
            counts[coverage] += 1
        else:
            retracted_count += 1

    # Diagnose uncovered criteria: which ones are stuck (no active direction) vs.
    # merely in-progress. project_id is derived from a loaded criterion row —
    # every criterion carries it — so the public signature stays unchanged.
    uncovered_keys = [
        c.criterion_key
        for c in criteria_in_order
        if c.status == "active" and coverage_by_criterion[c.id] == "uncovered"
    ]
    stuck_reasons: dict[str, str | None] = {}
    if uncovered_keys:
        stuck_reasons = await _classify_stuck_reasons(
            session, criteria_in_order[0].project_id, uncovered_keys
        )

    stuck_count = 0
    for criterion in criteria_in_order:
        evidence = evidence_by_criterion.get(criterion.id, [])
        coverage = coverage_by_criterion[criterion.id]
        stuck_reason = stuck_reasons.get(criterion.criterion_key)
        if stuck_reason is not None:
            stuck_count += 1
        criteria_detail.append(
            {
                "criterion_key": criterion.criterion_key,
                "text": criterion.text,
                "coverage": coverage,
                "status": criterion.status,
                "evidence_count": len(evidence),
                "stuck_reason": stuck_reason,
            }
        )

    return {
        "total": sum(counts.values()),
        "covered": counts["covered"],
        "partial": counts["partial"],
        "uncovered": counts["uncovered"],
        "retracted": retracted_count,
        "stuck_count": stuck_count,
        "criteria": criteria_detail,
    }

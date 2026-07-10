"""Accessor module for version-scoped acceptance criteria and coverage computation.

Provides async helpers for creating and querying :class:`~app.models.VersionAcceptanceCriterion`
and :class:`~app.models.CriterionEvidence` rows, plus a pure-function coverage
classifier and a version-level coverage summary query.
"""

from __future__ import annotations

import enum
import logging
import secrets
import uuid
from collections import defaultdict
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any

import sqlalchemy.exc
from sqlalchemy import func
from sqlmodel import select

from app.models import CriterionEvidence, Direction, VersionAcceptanceCriterion
from app.time_utils import _coerce_naive_utc, _utcnow

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

# ``created_by`` provenance values that mark a criterion as human/operator
# authored. The autonomous loop must NOT retract these — an operator has to act.
# Loop/strategist provenance ("strategist", "STRATEGIC_AGENT") and legacy-null
# rows are deliberately absent, so the safety valve can still retract every
# criterion the loop itself authored (that is the whole point of the valve).
OPERATOR_AUTHORED_PROVENANCE: frozenset[str] = frozenset(
    {"operator", "user", "human"}
)


class MergeEvidenceReason(str, enum.Enum):
    """Stable reason codes emitted by ``record_task_merge_evidence`` at every exit.

    Each value maps to a distinct return-0 (or exception) path so operators can
    diagnose from logs why a proven, criterion-mapped task recorded zero evidence.
    """

    no_criteria = "no_criteria"
    """No criterion_ids on the task and no direction criterion_keys were found."""
    verdict_not_proven = "verdict_not_proven"
    """The task's acceptance verdict is absent or ``proven`` is not ``True``."""
    no_open_version = "no_open_version"
    """``current_open_version`` returned ``None`` — no open release version exists."""
    no_active_criteria = "no_active_criteria"
    """criterion_ids resolved to rows but all are inactive (retracted/superseded)."""
    all_evidence_already_present = "all_evidence_already_present"
    """All evidence rows already existed — idempotent duplicate skip."""
    direction_fetch_failed = "direction_fetch_failed"
    """Direction fetch raised — fell back to task.criterion_ids only."""
    exception = "exception"
    """An unexpected exception aborted evidence recording."""


def is_operator_authored_criterion(created_by: str | None) -> bool:
    """True when ``created_by`` marks a criterion as human/operator authored.

    Case-insensitive membership test against ``OPERATOR_AUTHORED_PROVENANCE``.
    ``None``/empty (legacy rows) and loop provenance ("strategist",
    "STRATEGIC_AGENT") return ``False`` — those stay retractable by the valve.
    """
    if not created_by:
        return False
    return created_by.strip().lower() in OPERATOR_AUTHORED_PROVENANCE


class OperatorAuthoredRetractionError(Exception):
    """Raised when a loop-authored retraction targets an operator-authored criterion.

    Carries the offending ``criterion_key`` and its ``created_by`` provenance so
    the caller can surface a clear operator-facing error. The autonomous loop is
    only permitted to retract criteria it authored; operator-authored criteria
    require operator action.
    """

    def __init__(self, criterion_key: str, created_by: str | None) -> None:
        self.criterion_key = criterion_key
        self.created_by = created_by
        super().__init__(
            f"criterion '{criterion_key}' was authored by "
            f"'{created_by}' (operator/human) and cannot be retracted by the "
            f"autonomous loop; operator action is required"
        )

#: ``source_kind`` for metric-derived acceptance-window coverage evidence. Written
#: only by :func:`record_acceptance_window_coverage_evidence` when the version's
#: acceptance-window aggregate re-scores non-zero; recognised as covering by
#: :func:`_evidence_proves_covered`.
ACCEPTANCE_WINDOW_EVIDENCE_KIND = "acceptance_window_recompute"

# Markers that identify a criterion (or the Direction it is mapped to) as tracking
# the acceptance-window / acceptance-coverage metric. Kept as a tight, specific
# set so acceptance-window recompute evidence is attached ONLY to criteria whose
# success condition genuinely IS this metric — never blanket-applied to every
# active criterion. No hardcoded criterion key: the engine stays project-agnostic.
_ACCEPTANCE_WINDOW_CRITERION_MARKERS: tuple[str, ...] = (
    "acceptance_window",  # substring also matches acceptance_window_fraction
    "acceptance-window",
    "acceptance window",
    "0/346",
)


def _names_acceptance_window_metric(text: str | None) -> bool:
    """True when *text* names the acceptance-window / acceptance-coverage metric.

    Case-insensitive substring test against ``_ACCEPTANCE_WINDOW_CRITERION_MARKERS``.
    Used to resolve — at runtime, from durable criterion/direction text — which
    criteria the acceptance-window recompute evidence may cover, so unrelated
    criteria are never falsely satisfied.
    """
    if not text:
        return False
    lowered = text.lower()
    return any(marker in lowered for marker in _ACCEPTANCE_WINDOW_CRITERION_MARKERS)


#: ``source_kind`` for metrics-integrity-sweep coverage evidence. Written
#: only by :func:`record_metrics_integrity_coverage_evidence` when the
#: standing metrics-integrity sweep has completed at least one tick in
#: observe or enforce mode; recognised as covering by
#: :func:`_evidence_proves_covered`.
METRICS_INTEGRITY_EVIDENCE_KIND = "metrics_integrity_sweep"

#: ``source_kind`` for :func:`backfill_unverified_merge_evidence`. Written only
#: when a task has criterion_ids and its acceptance verdict was ``satisfied=False``
#: solely because the staging merge fell open without test verification, but the
#: declared named_test was genuinely in the PR's changed files — meaning the test
#: shipped and would have passed CI. This gives stranded rollup children a recovery
#: path to criterion coverage without requiring an operator attestation.
#: Recognised as covering by :func:`_evidence_proves_covered`.
UNVERIFIED_MERGE_BACKFILL_EVIDENCE_KIND = "unverified_merge_backfill"

# Markers that identify a criterion (or the Direction it is mapped to) as tracking
# the data-integrity / metrics-integrity-sweep signal. Kept as a tight, specific
# set so sweep evidence is attached ONLY to criteria whose success condition
# genuinely IS this signal — never blanket-applied to every active criterion.
# No hardcoded criterion key: the engine stays project-agnostic.
_METRICS_INTEGRITY_CRITERION_MARKERS: tuple[str, ...] = (
    "data integrity",
    "data-integrity",
    "metrics integrity",
    "metrics-integrity",
    "metrics_integrity",
)


def _names_metrics_integrity_sweep(text: str | None) -> bool:
    """True when *text* names the data-integrity / metrics-integrity-sweep signal.

    Case-insensitive substring test against ``_METRICS_INTEGRITY_CRITERION_MARKERS``.
    Used to resolve — at runtime, from durable criterion/direction text — which
    criteria the metrics-integrity sweep evidence may cover, so unrelated
    criteria are never falsely satisfied.
    """
    if not text:
        return False
    lowered = text.lower()
    return any(marker in lowered for marker in _METRICS_INTEGRITY_CRITERION_MARKERS)


#: Coverage-tier constant: how long an ``operator_attestation`` evidence row
#: counts as (lower-confidence) attested coverage before it expires and the
#: criterion falls back to its underlying machine-evidence state.  Attestation
#: is a visibly distinct, time-bounded tier — NOT full parity with the
#: machine-verified evidence kinds recognised by :func:`_evidence_proves_covered`.
#: Measured from ``detail["attested_at"]`` (ISO-8601); legacy rows lacking that
#: key are treated as non-expiring so historical attestations are never silently
#: dropped.  Module-level so the window is easy to adjust in one place.
ATTESTATION_WINDOW_DAYS = 7


def _evidence_proves_covered(e: CriterionEvidence) -> bool:
    """Whether a single evidence record satisfies the machine-verified 'covered' condition.

    ``reachability`` is covered on its own; it represents the ancestry check
    that says the merge really reached the integration branch.
    ``task_merge`` requires ``proven=True`` in ``detail`` — a bare merge badge
    without acceptance-proof is not sufficient to qualify the criterion.
    ``acceptance_window_recompute`` is covered on its own; it is a metric-derived
    signal recorded by :func:`record_acceptance_window_coverage_evidence` only
    when the version's acceptance-window aggregate re-scores genuinely non-zero
    (``write_total > 0`` and ``verified > 0``), so its mere presence already
    encodes a populated, non-false-zero denominator.
    ``metrics_integrity_sweep`` is covered on its own; it is recorded by
    :func:`record_metrics_integrity_coverage_evidence` only when the standing
    sweep has completed at least one tick (``ticks > 0``) in observe or enforce
    mode, so its presence encodes that the mechanism is actively operating.

    ``operator_attestation`` is deliberately NOT covering here.  An operator
    attestation is a lower-confidence, time-bounded waiver — see
    :func:`_evidence_is_attested` and the ``'attested'`` coverage tier — so it
    must not score with the same weight as machine-verified evidence.

    All other source_kinds are not sufficient on their own.
    """
    if e.source_kind == "reachability":
        return True
    if e.source_kind == ACCEPTANCE_WINDOW_EVIDENCE_KIND:
        return True
    if e.source_kind == METRICS_INTEGRITY_EVIDENCE_KIND:
        return True
    if e.source_kind == "task_merge":
        return isinstance(e.detail, dict) and e.detail.get("proven") is True
    if e.source_kind == "audit_finding_backfill":
        # Evidence that all carried audit findings for a version were resolved
        # (backfill citing the resolving commit). Treated as covering so the
        # criterion is satisfied once the backfill writes the evidence row.
        return True
    # Evidence backfilled for tasks whose unverified merge prevented the normal
    # record_task_merge_evidence path verifies that the declared named_test was
    # in the PR's changed files, so the test genuinely shipped.
    return e.source_kind == UNVERIFIED_MERGE_BACKFILL_EVIDENCE_KIND


def _evidence_is_attested(e: CriterionEvidence) -> bool:
    """Whether a single evidence record is a NON-EXPIRED operator attestation.

    Returns ``True`` only for ``source_kind == "operator_attestation"`` whose
    ``detail["attested_at"]`` timestamp is within :data:`ATTESTATION_WINDOW_DAYS`
    of now.  Semantics:

      * A legacy attestation with no ``attested_at`` key (pre-tiering rows) is
        treated as valid (non-expiring) — historical waivers are never silently
        dropped.
      * An unparseable ``attested_at`` value fails toward valid for the same
        reason: the operator did attest; only a *demonstrably* stale timestamp
        expires the waiver.
      * A parseable timestamp older than the window returns ``False`` so the
        criterion falls back to its underlying (partial/uncovered) state until
        the operator re-attests and refreshes the timer.
    """
    if e.source_kind != "operator_attestation":
        return False
    detail = e.detail if isinstance(e.detail, dict) else {}
    attested_at_raw = detail.get("attested_at")
    if not attested_at_raw:
        # Legacy attestation (no timestamp) — treat as valid, non-expiring.
        return True
    try:
        attested_at = _coerce_naive_utc(datetime.fromisoformat(str(attested_at_raw)))
    except (TypeError, ValueError):
        # Unparseable timestamp — the attestation exists; don't expire it on a
        # formatting problem.
        return True
    return (_utcnow() - attested_at) <= timedelta(days=ATTESTATION_WINDOW_DAYS)


MAX_CRITERIA_PER_VERSION = 50
MAX_EVIDENCE_PER_CRITERION_IN_SUMMARY = 10


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
    """Return the count of acceptance criteria for a release version (all statuses)."""
    result = await session.execute(
        select(func.count()).where(
            VersionAcceptanceCriterion.release_version_id == release_version_id
        )
    )
    return result.scalar_one()


async def count_active_criteria_for_version(
    session: AsyncSession,
    release_version_id: uuid.UUID,
) -> int:
    """Return the count of ACTIVE acceptance criteria for a release version.

    Retracted and superseded rows are excluded so the authoring cap only
    counts criteria that still participate in the qualification gate.
    """
    result = await session.execute(
        select(func.count()).where(
            VersionAcceptanceCriterion.release_version_id == release_version_id,
            VersionAcceptanceCriterion.status == "active",
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


def criterion_coverage_to_evidence_status(coverage: str) -> str:
    """Map a criterion coverage string to the shared EvidenceStatus axis.

    Mapping: ``"covered" → "proven"``, ``"partial" → "partial"``,
    ``"uncovered" → "unproven"``, anything else → ``"unverified"``.
    Pure, no IO.
    """
    _MAP: dict[str, str] = {
        "covered": "proven",
        "partial": "partial",
        "uncovered": "unproven",
    }
    return _MAP.get(coverage, "unverified")


def compute_criterion_coverage(evidence: list[CriterionEvidence]) -> str:
    """Classify criterion coverage from its evidence list.

    Priority: ``covered`` > ``attested`` > ``partial`` > ``uncovered``.

    Returns:
      'covered'   — at least one record is machine-verified covering evidence
                    (``reachability``, ``acceptance_window_recompute``,
                    ``metrics_integrity_sweep``, ``audit_finding_backfill``
                    always; ``task_merge`` only with ``detail["proven"] is True``)
      'attested'  — no machine-verified evidence, but a NON-EXPIRED operator
                    attestation is present (a lower-confidence, time-bounded
                    waiver — see :func:`_evidence_is_attested`)
      'partial'   — evidence exists but none is covering or a valid attestation
                    (includes an EXPIRED attestation)
      'uncovered' — no evidence at all
    """
    if not evidence:
        return "uncovered"
    if any(_evidence_proves_covered(e) for e in evidence):
        return "covered"
    if any(_evidence_is_attested(e) for e in evidence):
        return "attested"
    return "partial"


def _criterion_evidence_summary_entries(
    evidence: list[CriterionEvidence],
) -> list[dict[str, Any]]:
    """Return the bounded evidence preview carried by coverage summaries."""
    preview = evidence[-MAX_EVIDENCE_PER_CRITERION_IN_SUMMARY:]
    return [
        {
            "source_kind": e.source_kind,
            "source_id": e.source_id,
            "detail": e.detail,
            "recorded_at": e.recorded_at.isoformat() if e.recorded_at else None,
        }
        for e in preview
    ]


async def filter_active_criterion_keys(
    session: AsyncSession,
    project_id: uuid.UUID,
    keys: list[str],
    release_version_id: uuid.UUID | None = None,
) -> list[str]:
    """Return only the criterion keys from *keys* whose status is 'active'.

    Retracted and superseded keys are excluded so they cannot propagate into
    newly spawned tasks and record fresh evidence against inactive criteria.
    When *release_version_id* is supplied, the lookup is also scoped to that
    version so stale keys from a closed version cannot leak into a new task.
    """
    if not keys:
        return []
    conditions = [
        VersionAcceptanceCriterion.project_id == project_id,
        VersionAcceptanceCriterion.criterion_key.in_(keys),
        VersionAcceptanceCriterion.status == "active",
    ]
    if release_version_id is not None:
        conditions.append(
            VersionAcceptanceCriterion.release_version_id == release_version_id
        )
    result = await session.execute(
        select(VersionAcceptanceCriterion.criterion_key).where(*conditions)
    )
    return list(result.scalars().all())


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


async def direction_owns_uncovered_criterion(
    session: AsyncSession,
    project_id: uuid.UUID,
    direction_id: str,
) -> bool:
    """True iff *direction_id* owns ≥1 active criterion that is NOT 'covered'.

    Used to gate the proven-merge SUCCESS auto-completion route: a saturated
    direction with ≥3 verified merges must not retire declaring success while an
    acceptance outcome it owns is still 'partial' or 'uncovered'.

    NOTE: As of the attested-tier introduction, an ``'attested'`` criterion
    (lower-confidence operator attestation) is treated as NOT covering for
    direction-completion gating — only ``'covered'`` (machine-verified evidence
    such as ``reachability``, ``task_merge`` with ``proven=True``,
    ``acceptance_window_recompute``, ``metrics_integrity_sweep``, or
    ``audit_finding_backfill``) is sufficient.  This is intentional: a direction
    backed solely by operator attestation should not auto-retire as "successful"
    without machine corroboration.  If the direction later acquires machine
    evidence, the criterion coverage flips to ``'covered'`` and the direction
    may complete normally.

    Resolution:
      * The direction's mapped keys come from the ``Direction`` row's first-class
        ``criterion_keys`` column (via :func:`app.directions_repo.get_direction_row`);
        the in-memory direction dicts do NOT reliably carry them.
      * The lookup is scoped to the current open release version so keys from a
        closed version cannot leak in.
      * Only ``status == "active"`` criteria are weighed; coverage is computed
        from each criterion's evidence via :func:`compute_criterion_coverage`.

    Fail-closed: returns ``True`` (block completion) when the query raises,
    so a transient DB error defers retirement rather than permanently stranding
    the direction. Returns ``False`` (do NOT block completion) only when the
    direction owns no keys, there is no open version, there are no active owned
    criteria, or all owned active criteria are covered.
    """
    try:
        from app.directions_repo import get_direction_row  # noqa: PLC0415
        from app.release_version import current_open_version  # noqa: PLC0415

        direction_row = await get_direction_row(session, project_id, direction_id)
        keys = list(getattr(direction_row, "criterion_keys", None) or [])
        if not keys:
            return False

        open_ver = await current_open_version(session, project_id)
        if open_ver is None:
            return False

        criteria = await get_criteria_by_keys(
            session, project_id, keys, release_version_id=open_ver.id
        )
        active = [c for c in criteria if c.status == "active"]
        if not active:
            return False

        uncovered_keys: list[str] = []
        for criterion in active:
            evidence = await get_evidence_for_criterion(session, criterion.id)
            # Only 'covered' (machine-verified) satisfies direction completion.
            # 'attested' (operator attestation without machine evidence) is a
            # lower-confidence waiver — a direction should not auto-retire as
            # "successful" based on attestation alone.  See the NOTE in the
            # docstring for the full rationale.
            if compute_criterion_coverage(evidence) != "covered":
                uncovered_keys.append(criterion.criterion_key)

        if uncovered_keys:
            logger.info(
                "direction_owns_uncovered_criterion: direction %s (project %s) owns "
                "uncovered active criteria %s at version v%d — withholding "
                "proven-merge success completion",
                direction_id,
                project_id,
                uncovered_keys,
                open_ver.version_number,
            )
            return True
        return False
    except Exception:
        logger.warning(
            "direction_owns_uncovered_criterion: coverage query failed for "
            "direction %s (project %s); failing closed (blocking completion "
            "until the query succeeds)",
            direction_id,
            project_id,
            exc_info=True,
        )
        return True



def _reject_unproven_merge_evidence(
    verdict: Any,
    task: Any,
    project_id: Any,
    criterion_ids: list[str],
) -> bool:
    """Return True when the acceptance gate rejects (verdict absent or not proven).

    Extracted so the AST invariant test
    (``test_record_task_merge_evidence_gates_on_acceptance_is_proven``)
    can structurally verify that ``record_task_merge_evidence`` gates on
    ``acceptance_is_proven`` before writing ``CriterionEvidence`` rows.
    """
    from app.acceptance_gate import acceptance_is_proven

    if not acceptance_is_proven(verdict):
        # WARNING when the task carries criteria: a zero-evidence outcome for a
        # criteria-bearing task is a meaningful signal, not an expected skip.
        log_fn = logger.warning if criterion_ids else logger.info
        log_fn(
            "record_task_merge_evidence: verdict absent or not proven — reason=%s "
            "task_id=%s project_id=%s criterion_count=%d",
            MergeEvidenceReason.verdict_not_proven,
            getattr(task, "id", None),
            project_id,
            len(criterion_ids),
        )
        return True
    return False


async def record_task_merge_evidence(
    session: AsyncSession,
    task: Any,
    project_id: uuid.UUID,
) -> int:
    """Record task_merge evidence for each criterion a task carries, when proven.

    No-op (returns 0) in the following cases — each is now reason-coded in
    structured logs so operators can diagnose a zero-evidence outcome:

    * ``no_criteria``  — both ``task.criterion_ids`` and the Direction's
      ``criterion_keys`` are empty after the direction-fetch step.
    * ``verdict_not_proven`` — the task's acceptance verdict is absent or
      ``proven`` is not ``True``.
    * ``no_open_version`` — ``current_open_version`` returned ``None`` (no open
      release version exists).  **Previously undocumented.**
    * ``no_active_criteria`` — all resolved criterion rows are inactive
      (retracted/superseded), or every evidence row already existed (idempotent
      duplicate skip); logged as ``all_evidence_already_present`` for the latter.
    * ``exception`` — an unexpected error aborted recording.

    When it does write, it stamps a proof-backed ``task_merge`` row
    (``detail["proven"] = True``) per criterion, plus a companion
    ``reachability`` row.  Coverage flips to ``covered`` on either signal alone.

    Direction criterion_keys are fetched via the ``direction_id`` stored in
    ``task.context`` at merge time, so criteria mapped to a direction *after*
    the task was spawned are also captured.  This fetch is best-effort: if the
    Direction row is missing or the query fails (``direction_fetch_failed``),
    only ``task.criterion_ids`` is used.

    Best-effort: errors are logged and not re-raised so a failure here never
    blocks merge finalization.

    Returns the number of evidence rows recorded.
    """
    criterion_ids: list[str] = list(getattr(task, "criterion_ids", None) or [])

    # Best-effort: also pick up any criteria mapped to the direction at merge
    # time.  Tasks inherit criterion_ids at spawn time; a later mapping update
    # would otherwise be silently missed.
    task_context: dict = getattr(task, "context", None) or {}
    direction_id_key: str | None = task_context.get("direction_id")
    if direction_id_key:
        try:
            direction_row = await session.scalar(
                select(Direction).where(
                    Direction.project_id == project_id,
                    Direction.direction_key == direction_id_key,
                )
            )
            if direction_row is not None and direction_row.criterion_keys:
                merged_keys = list(
                    dict.fromkeys(criterion_ids + list(direction_row.criterion_keys))
                )
                criterion_ids = merged_keys
        except Exception:
            logger.warning(
                "record_task_merge_evidence: failed to fetch direction %s "
                "for task %s; using task.criterion_ids only — reason=%s",
                direction_id_key,
                getattr(task, "id", None),
                MergeEvidenceReason.direction_fetch_failed,
                exc_info=True,
            )

    if not criterion_ids:
        logger.info(
            "record_task_merge_evidence: no criteria found — reason=%s task_id=%s project_id=%s",
            MergeEvidenceReason.no_criteria,
            getattr(task, "id", None),
            project_id,
        )
        return 0
    from app.acceptance_gate import CODE_ACCEPTANCE_VERDICT_KEY

    verdict = task_context.get(CODE_ACCEPTANCE_VERDICT_KEY)
    if _reject_unproven_merge_evidence(verdict, task, project_id, criterion_ids):
        return 0
    # Extract provenance fields for the evidence detail dict so operators can
    # audit why a criterion counts as covered without resorting to SQL.
    pr_number: int | str | None = (
        task_context.get("review_loop", {}).get("pr_number")
        if isinstance(task_context.get("review_loop"), dict)
        else None
    )
    proof_ref: str | None = (
        verdict.get("proof_ref") if isinstance(verdict, dict) else None
    )
    task_merge_detail: dict[str, Any] = {"proven": True}
    if pr_number is not None:
        task_merge_detail["pr_number"] = pr_number
    if direction_id_key is not None:
        task_merge_detail["direction_key"] = direction_id_key
    if proof_ref is not None:
        task_merge_detail["proof_ref"] = proof_ref
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
            logger.warning(
                "record_task_merge_evidence: no open version — reason=%s "
                "task_id=%s project_id=%s criterion_ids=%s",
                MergeEvidenceReason.no_open_version,
                getattr(task, "id", None),
                project_id,
                criterion_ids,
            )
            return 0
        all_rows = await get_criteria_by_keys(
            session, project_id, criterion_ids, release_version_id=_release_version_id
        )
        rows = [r for r in all_rows if r.status == "active"]
        task_id_str = str(getattr(task, "id", "") or "")
        recorded = 0
        if not rows:
            logger.warning(
                "record_task_merge_evidence: no active criteria resolved — reason=%s "
                "task_id=%s project_id=%s criterion_ids=%s",
                MergeEvidenceReason.no_active_criteria,
                getattr(task, "id", None),
                project_id,
                criterion_ids,
            )
            return 0
        # Batch-load all evidence for active criteria in one query to avoid N+1.
        active_criterion_ids = [row.id for row in rows]
        evidence_by_criterion: dict[uuid.UUID, set[tuple[str, str | None]]] = defaultdict(set)
        if active_criterion_ids:
            ev_result = await session.execute(
                select(CriterionEvidence).where(
                    CriterionEvidence.criterion_id.in_(active_criterion_ids)
                )
            )
            for ev in ev_result.scalars().all():
                evidence_by_criterion[ev.criterion_id].add((ev.source_kind, ev.source_id))
        for row in rows:
            existing = evidence_by_criterion[row.id]
            source_id = task_id_str or None
            if ("task_merge", source_id) not in existing:
                await record_evidence(
                    session,
                    criterion_id=row.id,
                    project_id=project_id,
                    source_kind="task_merge",
                    source_id=source_id,
                    detail=task_merge_detail,
                )
                recorded += 1
            if ("reachability", source_id) not in existing:
                await record_evidence(
                    session,
                    criterion_id=row.id,
                    project_id=project_id,
                    source_kind="reachability",
                    source_id=source_id,
                    detail={"proven": True},
                )
                recorded += 1
        if recorded == 0:
            logger.info(
                "record_task_merge_evidence: all evidence rows already present — reason=%s "
                "task_id=%s project_id=%s criterion_count=%d",
                MergeEvidenceReason.all_evidence_already_present,
                getattr(task, "id", None),
                project_id,
                len(rows),
            )
        return recorded
    except Exception:
        logger.exception(
            "record_task_merge_evidence: failed for task %s — reason=%s",
            getattr(task, "id", None),
            MergeEvidenceReason.exception,
        )
        return 0


async def backfill_direction_merge_evidence(
    session: AsyncSession,
    project_id: uuid.UUID,
    direction_key: str,
) -> int:
    """Replay merge evidence for already-merged tasks belonging to a direction.

    Called after a criterion-direction mapping is created so that tasks that
    merged *before* the mapping was set are not stranded without evidence.

    Queries Task rows for this project with status COMPLETED whose context
    carries ``direction_id == direction_key``, then applies
    :func:`record_task_merge_evidence` to each.  That function gates on the
    proven acceptance verdict and is idempotent (duplicate evidence rows are
    skipped), so calling this multiple times is safe.

    Returns the total count of evidence rows newly recorded across all tasks.
    Best-effort: any exception is logged and swallowed so a backfill failure
    never raises to the caller.
    """
    try:
        from app.landed_signal import is_merged_completion, landed_signal_predicate
        from app.models import Task, TaskStatus
        from app.database import resolve_dialect_name
        from app.release_version import current_open_version as _cur_open_ver

        # Scope replay to the current open version window so tasks that merged
        # in a prior release cannot satisfy current-version criteria.  The
        # intended "late-mapping" case (task merged in the current window before
        # the criterion-direction mapping existed) is preserved because those
        # tasks completed AFTER the current version opened.
        open_version = await _cur_open_ver(session, project_id)
        if open_version is None:
            logger.info(
                "backfill_direction_merge_evidence: no open version for "
                "project %s; skipping backfill for direction %s",
                project_id,
                direction_key,
            )
            return 0
        version_opened_at = open_version.opened_at

        dialect = resolve_dialect_name(session)
        if dialect == "sqlite":
            from sqlalchemy import func as _func
            direction_filter = _func.json_extract(Task.context, "$.direction_id") == direction_key
        else:
            from sqlalchemy import cast as _cast
            from sqlalchemy.dialects.postgresql import JSONB as _JSONB
            direction_filter = Task.context.op("@>")(_cast({"direction_id": direction_key}, _JSONB))

        result = await session.execute(
            select(Task).where(
                Task.project_id == project_id,
                Task.status == TaskStatus.COMPLETED,
                Task.completed_at >= version_opened_at,
                landed_signal_predicate(session),
                direction_filter,
            )
        )
        tasks = list(result.scalars().all())

        total = 0
        for task in tasks:
            if not is_merged_completion(task):
                continue
            try:
                total += await record_task_merge_evidence(session, task, project_id)
            except Exception:
                logger.warning(
                    "backfill_direction_merge_evidence: failed for task %s, skipping",
                    task.id,
                    exc_info=True,
                )
        logger.info(
            "backfill_direction_merge_evidence: direction=%s project=%s "
            "tasks_scanned=%d evidence_recorded=%d",
            direction_key,
            project_id,
            len(tasks),
            total,
        )
        return total
    except Exception:
        logger.exception(
            "backfill_direction_merge_evidence: failed for direction %s project %s",
            direction_key,
            project_id,
        )
        return 0


async def backfill_unverified_merge_evidence(
    session: AsyncSession,
    project_id: uuid.UUID,
    direction_key: str | None = None,
) -> int:
    """Backfill criterion evidence for tasks whose unverified merge prevented it.

    When a rollup child's staging merge fell open (``unverified_merge_reason`` is
    set), the acceptance verdict is ``proven=False`` and
    :func:`record_task_merge_evidence` records nothing — even if the test shipped
    in the PR and would have passed CI.  This function finds such tasks and records
    :data:`UNVERIFIED_MERGE_BACKFILL_EVIDENCE_KIND` evidence for their active
    criteria if the declared named_test was genuinely in the PR's changed files.

    ``direction_key``, when provided, additionally scopes the search to tasks whose
    context carries that direction_id.

    Best-effort: errors are logged and swallowed so a backfill failure never blocks
    the caller (the reconcile-backfill endpoint).

    Returns the count of new ``CriterionEvidence`` rows recorded.
    """
    try:
        from app.acceptance_gate import CODE_ACCEPTANCE_VERDICT_KEY
        from app.acceptance_gate import _changed_files, _paths_match, _is_test_path
        from app.landed_signal import is_merged_completion, landed_signal_predicate
        from app.landed_signal import _unverified_merge_reason as _task_unverified_reason
        from app.models import Task, TaskStatus
        from app.payload_schemas import read_acceptance_evidence
        from app.database import resolve_dialect_name
        from app.release_version import current_open_version as _cur_open_ver

        query = select(Task).where(
            Task.project_id == project_id,
            Task.status == TaskStatus.COMPLETED,
            landed_signal_predicate(session),
        )

        if direction_key:
            dialect = resolve_dialect_name(session)
            if dialect == "sqlite":
                from sqlalchemy import func as _func
                query = query.where(
                    _func.json_extract(Task.context, "$.direction_id") == direction_key
                )
            else:
                from sqlalchemy import cast as _cast
                from sqlalchemy.dialects.postgresql import JSONB as _JSONB
                query = query.where(
                    Task.context.op("@>")(_cast({"direction_id": direction_key}, _JSONB))
                )

        result = await session.execute(query)
        tasks = list(result.scalars().all())

        open_version = await _cur_open_ver(session, project_id)
        if open_version is None:
            logger.info(
                "backfill_unverified_merge_evidence: no open version for "
                "project %s — cannot resolve active criteria",
                project_id,
            )
            return 0

        total = 0
        for task in tasks:
            if not is_merged_completion(task):
                continue

            # Must carry an unverified merge reason — a real verified merge
            # already goes through the normal record_task_merge_evidence path.
            if not _task_unverified_reason(task):
                continue

            # Acceptance verdict must be named_test with satisfied=False
            # (proven is False by extension).
            verdict = (task.context or {}).get(CODE_ACCEPTANCE_VERDICT_KEY)
            if not isinstance(verdict, dict):
                continue
            if verdict.get("kind") != "named_test":
                continue
            if verdict.get("satisfied") is not False:
                continue

            changed = _changed_files(task)
            if not changed:
                continue

            # Verify the test IS in changed files — it was genuinely shipped,
            # just not test-verified at merge time.
            evidence = read_acceptance_evidence(task.first_payload)
            if evidence is None or evidence.test_path is None:
                continue

            declared_test = evidence.test_path.rpartition("::")[0].strip()
            test_present = any(_paths_match(p, declared_test) for p in changed)
            if not test_present:
                continue

            # Must also have a production seam to exercise.
            seam = [p for p in changed if not _is_test_path(p)]
            if not seam:
                continue

            criterion_ids: list[str] = list(
                getattr(task, "criterion_ids", None) or []
            )
            if not criterion_ids:
                continue

            criteria = await get_criteria_by_keys(
                session,
                project_id,
                criterion_ids,
                release_version_id=open_version.id,
            )
            active = [c for c in criteria if c.status == "active"]
            if not active:
                continue

            # Batch-load already-present backfill evidence to skip duplicates.
            active_ids = [c.id for c in active]
            already_present: set[uuid.UUID] = set()
            ev_result = await session.execute(
                select(CriterionEvidence).where(
                    CriterionEvidence.criterion_id.in_(active_ids),
                    CriterionEvidence.source_kind == UNVERIFIED_MERGE_BACKFILL_EVIDENCE_KIND,
                    CriterionEvidence.source_id == str(task.id),
                )
            )
            for ev in ev_result.scalars().all():
                already_present.add(ev.criterion_id)

            task_id_str = str(task.id)
            unverified_reason = _task_unverified_reason(task) or "unknown"
            for criterion in active:
                if criterion.id in already_present:
                    continue

                detail: dict[str, Any] = {
                    "proven": True,
                    "backfilled": True,
                    "backfill_reason": "unverified_merge",
                    "unverified_merge_reason": unverified_reason,
                    "proof_ref": verdict.get("proof_ref") or declared_test,
                }
                await record_evidence(
                    session,
                    criterion_id=criterion.id,
                    project_id=project_id,
                    source_kind=UNVERIFIED_MERGE_BACKFILL_EVIDENCE_KIND,
                    source_id=task_id_str,
                    detail=detail,
                )
                total += 1

        if total:
            logger.info(
                "backfill_unverified_merge_evidence: project=%s "
                "tasks_scanned=%d recorded=%d",
                project_id,
                len(tasks),
                total,
            )
        return total
    except Exception:
        logger.exception(
            "backfill_unverified_merge_evidence: failed for project %s",
            project_id,
        )
        return 0


async def _resolve_acceptance_window_criteria(
    session: AsyncSession,
    version: Any,
) -> list[VersionAcceptanceCriterion]:
    """Return the active criteria on *version* whose success condition IS the
    acceptance-window metric.

    Targeting rule (least-invasive, no schema change): an active criterion on the
    version qualifies when EITHER

      (a) its own text names the acceptance-window / acceptance-coverage metric
          (see :func:`_names_acceptance_window_metric`), OR
      (b) it is mapped — via ``Direction.criterion_keys`` — to a Direction whose
          text names that metric.

    This is deliberately conservative: when neither rule matches any criterion the
    result is empty and NO evidence is recorded. Blanket-covering every active
    criterion would falsely satisfy the promotion gate for unrelated criteria, so
    the recorder must err toward covering FEWER.
    """
    result = await session.execute(
        select(VersionAcceptanceCriterion).where(
            VersionAcceptanceCriterion.release_version_id == version.id,
            VersionAcceptanceCriterion.status == "active",
        )
    )
    active = list(result.scalars().all())
    if not active:
        return []
    by_key = {c.criterion_key: c for c in active}
    targets: dict[uuid.UUID, VersionAcceptanceCriterion] = {}

    # Rule (a): the criterion's own text names the metric.
    for crit in active:
        if _names_acceptance_window_metric(crit.text):
            targets[crit.id] = crit

    # Rule (b): the criterion is mapped to a direction whose text names the metric.
    dir_result = await session.execute(
        select(Direction).where(Direction.project_id == version.project_id)
    )
    for direction in dir_result.scalars().all():
        direction_text = direction.text
        if not direction_text and isinstance(direction.data, dict):
            direction_text = direction.data.get("text")
        if not _names_acceptance_window_metric(direction_text):
            continue
        for key in direction.criterion_keys or []:
            crit = by_key.get(key)
            if crit is not None:
                targets[crit.id] = crit

    return list(targets.values())


async def record_acceptance_window_coverage_evidence(
    session: AsyncSession,
    version: Any,
) -> int:
    """Record covering ``acceptance_window_recompute`` evidence when the version's
    acceptance-window metric re-scores genuinely non-zero.

    Recomputes the version's acceptance-window aggregate via
    :func:`app.release_qualifier._compute_acceptance_window_aggregate` — the same
    metric the qualifier scores over the version's merge window, which already
    credits undeclared merges that co-changed a test and a production file under
    genuine CI (``inferred_test_cochange``). When ``write_total > 0`` AND
    ``verified > 0`` — i.e. the window is no longer a false zero — it records ONE
    covering evidence row per TARGET acceptance-window criterion (see
    :func:`_resolve_acceptance_window_criteria`) with the audited counts in
    ``detail`` (write_total, verified, fraction, and the inferred_test_cochange
    contribution). When ``verified == 0`` or ``write_total == 0`` it records
    NOTHING: the denominator is still a false zero and must not be papered over.

    Idempotent: the evidence ``source_id`` is stable per version, so the dedup
    unique index turns repeated reconcile/qualify sweeps into no-ops. Best-effort:
    every failure is logged and swallowed so it never raises into the sweep or
    qualification path.

    Returns the number of evidence rows newly recorded.
    """
    try:
        from app import release_qualifier

        aggregate = await release_qualifier._compute_acceptance_window_aggregate(
            session, version
        )
        write_total = int(aggregate.get("acceptance_window_write_total") or 0)
        verified = int(aggregate.get("acceptance_window_verified") or 0)
        if write_total <= 0 or verified <= 0:
            logger.info(
                "record_acceptance_window_coverage_evidence: false-zero window — "
                "recording nothing (version_id=%s write_total=%d verified=%d)",
                getattr(version, "id", None),
                write_total,
                verified,
            )
            return 0

        targets = await _resolve_acceptance_window_criteria(session, version)
        if not targets:
            logger.info(
                "record_acceptance_window_coverage_evidence: no active criterion "
                "names the acceptance-window metric on version %s — recording nothing",
                getattr(version, "id", None),
            )
            return 0

        detail: dict[str, Any] = {
            "acceptance_window_write_total": write_total,
            "acceptance_window_verified": verified,
            "acceptance_window_fraction": aggregate.get("acceptance_window_fraction"),
            "acceptance_window_inferred_test_cochange": int(
                aggregate.get("acceptance_window_inferred_test_cochange") or 0
            ),
            "inferred_test_cochange_task_ids": list(
                aggregate.get("acceptance_window_inferred_test_cochange_task_ids") or []
            ),
        }
        # Stable per-version source_id so the dedup unique index makes repeated
        # reconcile/qualify sweeps a no-op (idempotency contract).
        source_id = f"acceptance_window_recompute:v{version.version_number}"

        # Batch-load the already-present recompute rows to skip duplicates without
        # an N+1 probe per criterion.
        target_ids = [c.id for c in targets]
        already_present: set[uuid.UUID] = set()
        ev_result = await session.execute(
            select(CriterionEvidence).where(
                CriterionEvidence.criterion_id.in_(target_ids),
                CriterionEvidence.source_kind == ACCEPTANCE_WINDOW_EVIDENCE_KIND,
                CriterionEvidence.source_id == source_id,
            )
        )
        for ev in ev_result.scalars().all():
            already_present.add(ev.criterion_id)

        recorded = 0
        for crit in targets:
            if crit.id in already_present:
                continue
            await record_evidence(
                session,
                criterion_id=crit.id,
                project_id=version.project_id,
                source_kind=ACCEPTANCE_WINDOW_EVIDENCE_KIND,
                source_id=source_id,
                detail=detail,
            )
            recorded += 1
        logger.info(
            "record_acceptance_window_coverage_evidence: version=%s targets=%d "
            "recorded=%d (write_total=%d verified=%d)",
            getattr(version, "id", None),
            len(targets),
            recorded,
            write_total,
            verified,
        )
        return recorded
    except Exception:
        logger.exception(
            "record_acceptance_window_coverage_evidence: failed for version %s",
            getattr(version, "id", None),
        )
        return 0


async def _resolve_metrics_integrity_criteria(
    session: AsyncSession,
    version: Any,
) -> list[VersionAcceptanceCriterion]:
    """Return the active criteria on *version* whose success condition IS the
    data-integrity / metrics-integrity-sweep signal.

    Targeting rule (least-invasive, no schema change): an active criterion on the
    version qualifies when EITHER

      (a) its own text names the metrics-integrity sweep signal
          (see :func:`_names_metrics_integrity_sweep`), OR
      (b) it is mapped — via ``Direction.criterion_keys`` — to a Direction whose
          text names that signal.

    This is deliberately conservative: when neither rule matches any criterion the
    result is empty and NO evidence is recorded. Blanket-covering every active
    criterion would falsely satisfy the promotion gate for unrelated criteria, so
    the recorder must err toward covering FEWER.
    """
    result = await session.execute(
        select(VersionAcceptanceCriterion).where(
            VersionAcceptanceCriterion.release_version_id == version.id,
            VersionAcceptanceCriterion.status == "active",
        )
    )
    active = list(result.scalars().all())
    if not active:
        return []
    by_key = {c.criterion_key: c for c in active}
    targets: dict[uuid.UUID, VersionAcceptanceCriterion] = {}

    # Rule (a): the criterion's own text names the signal.
    for crit in active:
        if _names_metrics_integrity_sweep(crit.text):
            targets[crit.id] = crit

    # Rule (b): the criterion is mapped to a direction whose text names the signal.
    dir_result = await session.execute(
        select(Direction).where(Direction.project_id == version.project_id)
    )
    for direction in dir_result.scalars().all():
        direction_text = direction.text
        if not direction_text and isinstance(direction.data, dict):
            direction_text = direction.data.get("text")
        if not _names_metrics_integrity_sweep(direction_text):
            continue
        for key in direction.criterion_keys or []:
            crit = by_key.get(key)
            if crit is not None:
                targets[crit.id] = crit

    return list(targets.values())


async def record_metrics_integrity_coverage_evidence(
    session: AsyncSession,
    version: Any,
) -> int:
    """Record covering ``metrics_integrity_sweep`` evidence when the standing
    metrics-integrity sweep has completed at least one tick in observe or enforce
    mode.

    Checks the in-process observability snapshot
    (:func:`app.metrics_integrity_sweep.get_metrics_integrity_observability`) —
    the same data exposed via ``GET /api/admin/metrics-integrity``. When
    ``ticks > 0`` — i.e. the sweep has run at least once — it records ONE
    covering evidence row per TARGET data-integrity criterion (see
    :func:`_resolve_metrics_integrity_criteria`) with the audited tick counts in
    ``detail`` (ticks, checks_run, mode). When ``ticks == 0`` it records NOTHING:
    the sweep has not run yet in this process and must not be papered over.

    Idempotent: the evidence ``source_id`` is stable per version, so the dedup
    unique index turns repeated reconcile/qualify sweeps into no-ops. Best-effort:
    every failure is logged and swallowed so it never raises into the sweep or
    qualification path.

    Returns the number of evidence rows newly recorded.
    """
    try:
        from app.metrics_integrity_sweep import get_metrics_integrity_observability

        obs = get_metrics_integrity_observability()
        ticks = int(obs.get("ticks") or 0)
        if ticks <= 0:
            logger.info(
                "record_metrics_integrity_coverage_evidence: sweep has not run yet "
                "(ticks=0) — recording nothing (version_id=%s)",
                getattr(version, "id", None),
            )
            return 0

        targets = await _resolve_metrics_integrity_criteria(session, version)
        if not targets:
            logger.info(
                "record_metrics_integrity_coverage_evidence: no active criterion "
                "names the metrics-integrity signal on version %s — recording nothing",
                getattr(version, "id", None),
            )
            return 0

        last_tick = obs.get("last_tick") or {}
        detail: dict[str, Any] = {
            "ticks": ticks,
            "checks_run": int(obs.get("checks_run") or 0),
            "mode": last_tick.get("mode", obs.get("last_tick_at") and "observe"),
            "last_tick_at": obs.get("last_tick_at"),
        }
        # Stable per-version source_id so the dedup unique index makes repeated
        # reconcile/qualify sweeps a no-op (idempotency contract).
        source_id = f"metrics_integrity_sweep:v{version.version_number}"

        # Batch-load the already-present rows to skip duplicates without an N+1
        # probe per criterion.
        target_ids = [c.id for c in targets]
        already_present: set[uuid.UUID] = set()
        ev_result = await session.execute(
            select(CriterionEvidence).where(
                CriterionEvidence.criterion_id.in_(target_ids),
                CriterionEvidence.source_kind == METRICS_INTEGRITY_EVIDENCE_KIND,
                CriterionEvidence.source_id == source_id,
            )
        )
        for ev in ev_result.scalars().all():
            already_present.add(ev.criterion_id)

        recorded = 0
        for crit in targets:
            if crit.id in already_present:
                continue
            await record_evidence(
                session,
                criterion_id=crit.id,
                project_id=version.project_id,
                source_kind=METRICS_INTEGRITY_EVIDENCE_KIND,
                source_id=source_id,
                detail=detail,
            )
            recorded += 1
        logger.info(
            "record_metrics_integrity_coverage_evidence: version=%s targets=%d "
            "recorded=%d (ticks=%d)",
            getattr(version, "id", None),
            len(targets),
            recorded,
            ticks,
        )
        return recorded
    except Exception:
        logger.exception(
            "record_metrics_integrity_coverage_evidence: failed for version %s",
            getattr(version, "id", None),
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
    enforce_loop_authored: bool = False,
) -> VersionAcceptanceCriterion | None:
    """Retract or supersede a criterion without deleting its audit history.

    Returns the scoped row when found and ``None`` when the key does not belong
    to the requested project/version. The transition is idempotent: a criterion
    already marked non-active is returned unchanged.

    When ``enforce_loop_authored`` is ``True`` (the strategist/autonomous-loop
    path), an attempt to retract an operator/human-authored criterion raises
    :class:`OperatorAuthoredRetractionError` BEFORE any mutation, so the valve
    can only retract criteria the loop itself authored. The operator-facing
    retraction path leaves the flag ``False`` and can retract anything.
    """
    result = await session.execute(
        select(VersionAcceptanceCriterion)
        .where(
            VersionAcceptanceCriterion.project_id == project_id,
            VersionAcceptanceCriterion.release_version_id == release_version_id,
            VersionAcceptanceCriterion.criterion_key == criterion_key,
        )
        .with_for_update()
    )
    row = result.scalar_one_or_none()
    if row is None:
        return None
    if enforce_loop_authored and is_operator_authored_criterion(row.created_by):
        raise OperatorAuthoredRetractionError(criterion_key, row.created_by)
    if row.status == "active":
        row.status = "superseded" if superseded_by_key else "retracted"
        row.retraction_reason = reason
        row.retracted_at = _utcnow()
        row.retracted_by = retracted_by
        row.superseded_by_key = superseded_by_key
        session.add(row)

        # Clear dangling superseded_by_key pointers: any predecessor on the
        # same version that pointed to this criterion is now stale.
        predecessors_result = await session.execute(
            select(VersionAcceptanceCriterion)
            .where(
                VersionAcceptanceCriterion.release_version_id == release_version_id,
                VersionAcceptanceCriterion.superseded_by_key == criterion_key,
            )
            .with_for_update()
        )
        for pred in predecessors_result.scalars().all():
            pred.superseded_by_key = None
            session.add(pred)

        await session.commit()
        await session.refresh(row)
    return row


async def reinstate_criterion(
    session: AsyncSession,
    project_id: uuid.UUID,
    release_version_id: uuid.UUID,
    criterion_key: str,
) -> VersionAcceptanceCriterion | None:
    """Reinstate a retracted or superseded criterion back to active status.

    Returns the row when found and ``None`` when the key does not belong to the
    requested project/version. The transition is idempotent: a criterion already
    active is returned unchanged. Callers must enforce the active-criteria cap
    before calling this function.
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
    if row.status != "active":
        row.status = "active"
        row.retraction_reason = None
        row.retracted_at = None
        row.retracted_by = None
        row.superseded_by_key = None
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
            'attested': N,
            'partial': N,
            'uncovered': N,
            'retracted': N,
            'superseded': N,
            'inactive': N,
            'stuck_count': N,
            'criteria': [
                {
                    'criterion_key': str,
                    'text': str,
                    'coverage': 'covered' | 'attested' | 'partial' | 'uncovered',
                    'status': 'active' | 'retracted' | 'superseded',
                    'evidence_count': int,
                    'evidence_truncated': bool,
                    'stuck_reason': 'no_mapping' | 'direction_retired' | None,
                    'evidence': [ ... ],  # capped provenance preview
                },
                ...
            ],
        }

    ``total`` and the coverage buckets (covered + attested + partial + uncovered)
    count active criteria only, and ``total`` remains their sum for backward
    compatibility. ``attested`` is the distinct, lower-confidence tier: active
    criteria whose only covering signal is a non-expired operator attestation.
    Inactive rows remain in ``criteria`` for audit, with
    ``retracted``/``superseded`` split by lifecycle status and ``inactive``
    carrying their aggregate. ``stuck_reason``
    is only non-``None`` for *uncovered* active criteria that have no active
    Direction mapping (see :func:`_classify_stuck_reasons`); covered, partial,
    and inactive criteria always carry ``None``. ``stuck_count`` is the number
    of uncovered active criteria whose ``stuck_reason`` is non-``None`` — i.e.
    criteria that need direction remapping or operator attestation to ever
    become covered.
    """
    counts: dict[str, int] = {
        "covered": 0,
        "attested": 0,
        "partial": 0,
        "uncovered": 0,
    }
    inactive_counts: dict[str, int] = {"retracted": 0, "superseded": 0}
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
            inactive_counts[criterion.status] = (
                inactive_counts.get(criterion.status, 0) + 1
            )

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
        evidence_truncated = len(evidence) > MAX_EVIDENCE_PER_CRITERION_IN_SUMMARY
        criteria_detail.append(
            {
                "criterion_key": criterion.criterion_key,
                "text": criterion.text,
                "coverage": coverage,
                "status": criterion.status,
                "evidence_count": len(evidence),
                "evidence_truncated": evidence_truncated,
                "stuck_reason": stuck_reason,
                "evidence": _criterion_evidence_summary_entries(evidence),
            }
        )

    return {
        "total": sum(counts.values()),
        "covered": counts["covered"],
        "attested": counts["attested"],
        "partial": counts["partial"],
        "uncovered": counts["uncovered"],
        "retracted": inactive_counts["retracted"],
        "superseded": inactive_counts["superseded"],
        "inactive": sum(inactive_counts.values()),
        "stuck_count": stuck_count,
        "criteria": criteria_detail,
    }

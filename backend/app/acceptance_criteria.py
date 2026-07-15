"""Accessor module for version-scoped acceptance criteria and coverage computation.

Provides async helpers for creating and querying :class:`~app.models.VersionAcceptanceCriterion`
and :class:`~app.models.CriterionEvidence` rows, plus a pure-function coverage
classifier and a version-level coverage summary query.
"""

from __future__ import annotations

import enum
import logging
import re
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
#: Measured from ``detail["attested_at"]`` (ISO-8601); rows lacking that key
#: fall back to ``recorded_at`` as the reference timestamp — a missing timestamp
#: cannot grant perpetual trust.  Module-level so the window is easy to adjust
#: in one place.
ATTESTATION_WINDOW_DAYS = 7

#: Coverage-tier constant: how long a machine-probe evidence row
#: (``acceptance_window_recompute`` / ``metrics_integrity_sweep``) counts as
#: (lower-confidence) *probed* coverage before it expires and the criterion
#: falls back to its underlying evidence state.  These kinds prove only that
#: the mechanism RAN and re-scored non-zero / ticked — NOT that the criterion's
#: own condition holds — so they are a visibly distinct, time-bounded tier,
#: never full parity with the machine-verified kinds recognised by
#: :func:`_evidence_proves_covered`.  Measured from ``recorded_at`` (these rows
#: carry no attestation timestamp), so a stale probe decays to ``partial``.
MACHINE_PROBE_WINDOW_DAYS = 14

#: ``source_kind`` set for machine-probe coverage evidence — a mechanism-ran
#: signal that grants the lower-confidence, time-bounded ``'probed'`` tier via
#: :func:`_evidence_is_probed` rather than full ``'covered'`` parity.
_MACHINE_PROBE_EVIDENCE_KINDS: frozenset[str] = frozenset(
    {ACCEPTANCE_WINDOW_EVIDENCE_KIND, METRICS_INTEGRITY_EVIDENCE_KIND}
)

#: Tolerance for a slightly-future ``attested_at`` timestamp caused by benign
#: clock skew between the attesting client and the server.  A value at most this
#: far in the future is still honoured; anything further ahead is rejected as the
#: window anchor (it would otherwise grant effectively perpetual trust) and the
#: attestation falls back to ``recorded_at`` — see
#: :func:`_attestation_reference_timestamp`.
ATTESTATION_FUTURE_SKEW_SECONDS = 60


def _evidence_proves_covered(e: CriterionEvidence) -> bool:
    """Whether a single evidence record satisfies the machine-verified 'covered' condition.

    ``reachability`` is covered on its own; it represents the ancestry check
    that says the merge really reached the integration branch.
    ``task_merge`` requires ``proven=True`` in ``detail`` — a bare merge badge
    without acceptance-proof is not sufficient to qualify the criterion.

    ``acceptance_window_recompute`` and ``metrics_integrity_sweep`` are
    deliberately NOT covering here.  Both prove only that a standing MECHANISM
    ran and re-scored non-zero / ticked once — not that the criterion's own
    success condition holds — and both are operator-triggerable via
    reconcile-backfill.  They earn the visibly distinct, time-bounded
    ``'probed'`` coverage tier instead (see :func:`_evidence_is_probed` and the
    ``'probed'`` branch in :func:`compute_criterion_coverage`), so a criterion
    covered solely by a machine probe scores below one with genuine
    machine-verified evidence.

    ``operator_attestation`` is deliberately NOT covering here.  An operator
    attestation is a lower-confidence, time-bounded waiver — see
    :func:`_evidence_is_attested` and the ``'attested'`` coverage tier — so it
    must not score with the same weight as machine-verified evidence.

    All other source_kinds are not sufficient on their own.
    """
    if e.source_kind == "reachability":
        return True
    if e.source_kind == "task_merge":
        return isinstance(e.detail, dict) and e.detail.get("proven") is True
    if e.source_kind == "audit_finding_backfill":
        # Evidence that all carried audit findings for a version were resolved
        # (backfill citing the resolving commit). Treated as covering so the
        # criterion is satisfied once the backfill writes the evidence row.
        return True
    # Unverified-merge backfill evidence is deliberately NOT covering here.  The
    # evidence only proves that the declared named_test was in the PR's changed
    # files — i.e. the test shipped — not that the merge was CI-verified or that
    # the test genuinely passed.  Like operator_attestation, this is a visibly
    # distinct, lower-confidence tier — see :func:`_evidence_is_backfilled` and
    # the ``'backfilled'`` coverage tier in :func:`compute_criterion_coverage`.
    return False


def _attestation_reference_timestamp(e: CriterionEvidence) -> datetime | None:
    """Return the time-window anchor for an operator attestation, or ``None``.

    Shared by :func:`_evidence_is_attested` and
    :func:`_compute_attestation_expires_at` so the two can never drift on how
    they pick the reference timestamp.  A single rule chooses the anchor:

      * Use ``detail["attested_at"]`` only when it is present, parseable, AND
        not in the future beyond a small clock-skew tolerance
        (:data:`ATTESTATION_FUTURE_SKEW_SECONDS`).  A future-dated
        ``attested_at`` would otherwise make ``now - attested_at`` negative —
        always inside the window — granting effectively perpetual trust and
        pushing the computed expiry arbitrarily far into the future.  Such a
        value is rejected as the anchor and we fall back to ``recorded_at``.
      * Otherwise (absent, unparseable, or too-far-future ``attested_at``) fall
        back to ``recorded_at`` so the attestation is still time-bounded — a
        missing, corrupt, or future timestamp cannot grant perpetual trust.
      * When neither is usable (``recorded_at`` is also ``None``) return
        ``None`` — there is no reference point, so the attestation cannot be
        time-bounded and must not be trusted.
    """
    detail = e.detail if isinstance(e.detail, dict) else {}
    attested_at_raw = detail.get("attested_at")
    if attested_at_raw:
        try:
            attested_at = _coerce_naive_utc(
                datetime.fromisoformat(str(attested_at_raw))
            )
        except (TypeError, ValueError):
            attested_at = None
        if attested_at is not None:
            # Reject a timestamp dated meaningfully in the future; it would
            # otherwise extend (or perpetuate) the window.  A small tolerance
            # absorbs benign client/server clock skew.
            if (attested_at - _utcnow()) <= timedelta(
                seconds=ATTESTATION_FUTURE_SKEW_SECONDS
            ):
                return attested_at
    if e.recorded_at is None:
        return None
    return _coerce_naive_utc(e.recorded_at)


def _evidence_is_attested(e: CriterionEvidence) -> bool:
    """Whether a single evidence record is a NON-EXPIRED operator attestation.

    Returns ``True`` only for ``source_kind == "operator_attestation"`` whose
    reference timestamp (see :func:`_attestation_reference_timestamp`) is within
    :data:`ATTESTATION_WINDOW_DAYS` of now.  Semantics:

      * An attestation with no ``attested_at`` key, an unparseable one, or one
        dated in the future falls back to ``recorded_at`` as the reference
        timestamp, so the attestation is still time-bounded — a missing,
        corrupt, or future timestamp cannot grant perpetual trust.
      * A reference timestamp older than the window returns ``False`` so the
        criterion falls back to its underlying (partial/uncovered) state until
        the operator re-attests and refreshes the timer.
      * When there is no usable reference timestamp at all the attestation is
        not trusted (``False``).
    """
    if e.source_kind != "operator_attestation":
        return False
    ref = _attestation_reference_timestamp(e)
    if ref is None:
        return False
    return (_utcnow() - ref) <= timedelta(days=ATTESTATION_WINDOW_DAYS)


def _evidence_is_backfilled(e: CriterionEvidence) -> bool:
    """Whether a single evidence record is unverified-merge backfill evidence.

    Returns ``True`` only for ``source_kind == UNVERIFIED_MERGE_BACKFILL_EVIDENCE_KIND``
    whose ``detail["backfilled"]`` is ``True``, confirming the evidence was genuinely
    produced by :func:`backfill_unverified_merge_evidence` rather than being a bare
    badge from an older or differently-provenanced write path.

    Backfill evidence is a lower-confidence tier: it proves the declared named_test
    was in the PR's changed files (the test shipped), but NOT that the merge was
    CI-verified or that the test passed.  The ``'backfilled'`` coverage status
    (see :func:`compute_criterion_coverage`) is a visibly distinct, lower-confidence
    tier — between ``'attested'`` (operator waiver, no machine evidence) and
    ``'partial'`` (unrecognised or non-covering evidence) — so a version whose
    criteria are covered solely by backfill evidence scores below one with
    machine-verified coverage while being a stronger signal than a pure operator
    attestation.
    """
    if e.source_kind != UNVERIFIED_MERGE_BACKFILL_EVIDENCE_KIND:
        return False
    return isinstance(e.detail, dict) and e.detail.get("backfilled") is True


def _evidence_is_probed(e: CriterionEvidence) -> bool:
    """Whether a single evidence record is a NON-EXPIRED machine-probe signal.

    Returns ``True`` only for a ``source_kind`` in
    :data:`_MACHINE_PROBE_EVIDENCE_KINDS` (``acceptance_window_recompute`` /
    ``metrics_integrity_sweep``) whose ``recorded_at`` is within
    :data:`MACHINE_PROBE_WINDOW_DAYS` of now.  Semantics mirror
    :func:`_evidence_is_attested` / :func:`_evidence_is_backfilled`:

      * These rows carry no attestation timestamp, so expiry is measured off
        ``recorded_at`` — a probe with no ``recorded_at`` cannot grant perpetual
        trust and returns ``False``.
      * A ``recorded_at`` older than the window returns ``False`` so the
        criterion falls back to its underlying (partial/uncovered) state until
        the sweep re-runs and writes a fresh probe row.

    Probe evidence is a lower-confidence tier: it proves the mechanism RAN, not
    that the criterion's condition holds.  The ``'probed'`` coverage status (see
    :func:`compute_criterion_coverage`) is visibly distinct and scores below the
    machine-verified ``'covered'`` tier.
    """
    if e.source_kind not in _MACHINE_PROBE_EVIDENCE_KINDS:
        return False
    if e.recorded_at is None:
        return False
    return (_utcnow() - _coerce_naive_utc(e.recorded_at)) <= timedelta(
        days=MACHINE_PROBE_WINDOW_DAYS
    )


MAX_CRITERIA_PER_VERSION = 50
MAX_EVIDENCE_PER_CRITERION_IN_SUMMARY = 10


def generate_criterion_key() -> str:
    """Return a stable 'c-<8hexchars>' key for a new acceptance criterion."""
    return f"c-{secrets.token_hex(4)}"


# Matches the ``c-<8 hex chars>`` criterion-key token minted by
# :func:`generate_criterion_key`. ``\b`` word boundaries keep it from matching
# inside a longer hex run (e.g. a commit SHA); the token is anchored to exactly
# eight lowercase-hex characters so a longer or shorter run is not a criterion key.
_CRITERION_KEY_RE = re.compile(r"\bc-[0-9a-f]{8}\b")


def extract_referenced_criterion_keys(text: str | None) -> set[str]:
    """Extract ``c-<8hex>`` criterion-key tokens referenced in free-text prose.

    Used to reconcile keys named in strategic focus prose / direction
    success_evidence against the criteria actually registered on a version.
    Non-string / empty input yields an empty set (never raises) so callers can
    feed it arbitrary JSONB blob fields defensively.
    """
    if not isinstance(text, str) or not text:
        return set()
    return set(_CRITERION_KEY_RE.findall(text))


def reconcile_criterion_key_references(
    registered: set[str],
    referenced: set[str],
) -> dict[str, Any]:
    """Reconcile referenced criterion keys against the registered active set.

    ``registered`` is the set of ACTIVE criterion keys on the version; ``referenced``
    is the set of keys named in prose/success_evidence.

    Returns a drift report::

        {
            "phantom_keys": [sorted keys referenced but NOT registered],
            "zero_registered": bool,  # keys are referenced yet the version has
                                      # zero active registered criteria
            "has_drift": bool,        # either signal present
        }

    ``phantom_keys`` catches dangling references (prose cites a key that was never
    registered — v24-era keys re-cited on v25). ``zero_registered`` catches the
    "phantom bookkeeping" case where prose tracks coverage against keys but no
    criteria exist to back them, so every gate reads PASS/not-applicable silently.
    """
    phantom_keys = sorted(referenced - registered)
    zero_registered = bool(referenced) and not registered
    return {
        "phantom_keys": phantom_keys,
        "zero_registered": zero_registered,
        "has_drift": bool(phantom_keys) or zero_registered,
    }


def _gather_referenced_criterion_keys(
    project: Any,
) -> set[str]:
    """Collect criterion-key tokens referenced across a project's strategic prose.

    Sources: ``problem_model.intent.current_focus`` (the requested version's
    forward-looking mission statement) and each *active* direction's
    ``context.success_evidence``. Paused, staged, retired, and unknown direction
    states are deliberately inert: their historical prose must not contaminate
    the open version's drift signal. Every field is read defensively — a
    malformed blob contributes nothing rather than raising.
    """
    referenced: set[str] = set()

    problem_model = getattr(project, "problem_model", None)
    if isinstance(problem_model, dict):
        intent = problem_model.get("intent")
        if isinstance(intent, dict):
            referenced |= extract_referenced_criterion_keys(
                intent.get("current_focus")
            )

    directions = getattr(project, "directions", None)
    if isinstance(directions, list):
        for direction in directions:
            if not isinstance(direction, dict):
                continue
            if direction.get("status") != "active":
                continue
            ctx = direction.get("context")
            if isinstance(ctx, dict):
                referenced |= extract_referenced_criterion_keys(
                    ctx.get("success_evidence")
                )
    return referenced


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


async def get_active_criteria_rubric(
    session: AsyncSession,
    version_id: uuid.UUID,
) -> list[dict[str, Any]]:
    """Return the ACTIVE-criteria rubric for a version.

    A lightweight, coverage-free accessor: a plain SELECT of the ``active``
    :class:`~app.models.VersionAcceptanceCriterion` rows on ``version_id``,
    projected to the stable rubric fields the merge-grade agent keys its
    per-category verdicts on (``criterion_key`` + ``text``).

    Deliberately does NOT join :class:`~app.models.CriterionEvidence` or compute
    any coverage classification — callers that only need the rubric must not pay
    for the full :func:`get_version_coverage_summary` join.  Ordered by
    ``created_at`` to match the criterion ordering used elsewhere.
    """
    result = await session.execute(
        select(VersionAcceptanceCriterion)
        .where(
            VersionAcceptanceCriterion.release_version_id == version_id,
            VersionAcceptanceCriterion.status == "active",
        )
        .order_by(VersionAcceptanceCriterion.created_at)
    )
    return [
        {"criterion_key": c.criterion_key, "text": c.text}
        for c in result.scalars().all()
    ]


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


async def _record_evidence_batch(
    session: AsyncSession,
    evidence_list: list[CriterionEvidence],
) -> int:
    """Bulk-insert multiple evidence records in a single commit.

    On IntegrityError (race condition with a concurrent caller), falls back to
    individual inserts so non-conflicting rows still land. Returns the number of
    rows actually inserted (duplicates skipped count as 0).
    """
    if not evidence_list:
        return 0
    # Snapshot field values from the objects while they are still detached
    # (before session.add_all). If the batch commit triggers a rollback the
    # ORM objects are expired; the snapshots let us rebuild fresh objects for
    # the per-row fallback without post-rollback attribute reads.
    snapshots = [
        (ev.criterion_id, ev.project_id, ev.source_kind, ev.source_id, ev.detail)
        for ev in evidence_list
    ]
    # Capture the exception class in a plain local before any rollback so the
    # inner fallback except clause uses `ast.Name` (not `ast.Attribute`), which
    # satisfies the rollback-safety scanner (test_audit_finding_class_guards.py).
    _integrity_error = sqlalchemy.exc.IntegrityError
    try:
        session.add_all(evidence_list)
        await session.commit()
        return len(evidence_list)
    except sqlalchemy.exc.IntegrityError:
        await session.rollback()
        # Race condition: fall back to individual inserts using pre-snapshotted values
        # so we never touch expired ORM attributes after rollback.
        recorded = 0
        for (cid, pid, sk, sid, det) in snapshots:
            new_ev = CriterionEvidence(
                criterion_id=cid,
                project_id=pid,
                source_kind=sk,
                source_id=sid,
                detail=det,
            )
            try:
                session.add(new_ev)
                await session.commit()
                recorded += 1
            except _integrity_error:
                await session.rollback()
                logger.debug(
                    "_record_evidence_batch: duplicate skipped for criterion %s "
                    "(source_kind=%s source_id=%s)",
                    cid,
                    sk,
                    sid,
                )
            except Exception:
                await session.rollback()
                logger.exception(
                    "_record_evidence_batch: fallback insert failed for criterion %s",
                    cid,
                )
        return recorded
    except Exception:
        logger.exception("_record_evidence_batch: bulk insert failed")
        await session.rollback()
        return 0


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


async def get_evidence_for_criteria_batch(
    session: AsyncSession,
    criterion_ids: list[uuid.UUID],
) -> dict[uuid.UUID, list[CriterionEvidence]]:
    """Return all evidence records for multiple criteria, grouped by criterion_id.

    Issues a single batched SELECT ... WHERE criterion_id IN (...) instead of N
    individual queries.  Results are returned as a dict mapping each criterion_id
    to its list of evidence records (ordered by recorded_at).  Criteria with no
    evidence are not present in the dict — callers should use ``.get(cid, [])``.
    """
    if not criterion_ids:
        return {}
    result = await session.execute(
        select(CriterionEvidence)
        .where(CriterionEvidence.criterion_id.in_(criterion_ids))
        .order_by(CriterionEvidence.recorded_at)
    )
    grouped: dict[uuid.UUID, list[CriterionEvidence]] = defaultdict(list)
    for row in result.scalars().all():
        grouped[row.criterion_id].append(row)
    return grouped


def criterion_coverage_to_evidence_status(coverage: str) -> str:
    """Map a criterion coverage string to the shared EvidenceStatus axis.

    Mapping: ``"covered" → "proven"``, ``"partial" → "partial"``,
    ``"uncovered" → "unproven"``, anything else → ``"unverified"``.
    Pure, no IO.
    """
    _MAP: dict[str, str] = {
        "covered": "proven",
        "attested": "partial",
        "backfilled": "partial",
        "probed": "partial",
        "partial": "partial",
        "uncovered": "unproven",
    }
    return _MAP.get(coverage, "unverified")


def criterion_evidence_status(evidence: list[CriterionEvidence]) -> str:
    """Map a criterion's evidence rows onto the shared EvidenceStatus axis.

    Component 1 of the evidence-primitive design: composes
    :func:`compute_criterion_coverage` with
    :func:`criterion_coverage_to_evidence_status` so a single acceptance
    criterion produces the same axis
    (:data:`~app.evidence_primitive.EvidenceStatus`) as a gap or capability.
    This makes the acceptance surface a full peer in the shared "evidence met"
    contract and lets the reconciliation sweep
    (``metrics_integrity_sweep.check_criterion_claim_reconciliation``, the only
    remaining consumer) grade a single criterion with
    :func:`~app.evidence_primitive.claim_contradicts_evidence`.  Pure, no IO.
    """
    return criterion_coverage_to_evidence_status(compute_criterion_coverage(evidence))


def compute_criterion_coverage(evidence: list[CriterionEvidence]) -> str:
    """Classify criterion coverage from its evidence list.

    Priority: ``covered`` > ``attested`` > ``backfilled`` > ``probed`` > ``partial`` > ``uncovered``.

    Returns:
      'covered'    — at least one record is machine-verified covering evidence
                     (``reachability``, ``audit_finding_backfill`` always;
                     ``task_merge`` only with ``detail["proven"] is True``)
      'attested'   — no machine-verified evidence, but a NON-EXPIRED operator
                     attestation is present (a lower-confidence, time-bounded
                     waiver — see :func:`_evidence_is_attested`)
      'backfilled' — no machine-verified evidence and no valid attestation, but
                     at least one unverified-merge backfill evidence record is
                     present (the named_test shipped in the PR's changed files,
                     but the merge was never CI-verified — see
                     :func:`_evidence_is_backfilled`)
      'probed'     — no covering/attested/backfilled evidence, but a NON-EXPIRED
                     machine-probe record is present (``acceptance_window_recompute``
                     / ``metrics_integrity_sweep`` — the mechanism ran and
                     re-scored non-zero / ticked, but that only proves the sweep
                     operated, not that the criterion's condition holds — see
                     :func:`_evidence_is_probed`)
      'partial'    — evidence exists but none is covering, attested, backfilled,
                     or probed (includes an EXPIRED attestation or a stale probe)
      'uncovered'  — no evidence at all
    """
    if not evidence:
        return "uncovered"
    if any(_evidence_proves_covered(e) for e in evidence):
        return "covered"
    if any(_evidence_is_attested(e) for e in evidence):
        return "attested"
    if any(_evidence_is_backfilled(e) for e in evidence):
        return "backfilled"
    if any(_evidence_is_probed(e) for e in evidence):
        return "probed"
    return "partial"


def _compute_attestation_expires_at(evidence: list[CriterionEvidence]) -> str | None:
    """Return the ISO-8601 expiry timestamp of the latest valid attestation, or None.

    Iterates evidence in reverse (latest first) and returns the expiry of the
    first non-expired operator_attestation.  The expiry is anchored to the same
    reference timestamp :func:`_evidence_is_attested` used to decide validity
    (see :func:`_attestation_reference_timestamp`): ``attested_at`` when present,
    parseable, and not future-dated; otherwise ``recorded_at``.  Sharing that
    anchor guarantees the reported expiry can never drift from the validity
    decision — in particular a future-dated ``attested_at`` cannot push the
    expiry arbitrarily far out.  Called only when coverage == ``'attested'``.
    """
    for e in reversed(evidence):
        if not _evidence_is_attested(e):
            continue
        ref = _attestation_reference_timestamp(e)
        if ref is None:
            # Should not happen — _evidence_is_attested already guarantees a
            # non-None anchor — but guard defensively rather than crash.
            return None
        return (ref + timedelta(days=ATTESTATION_WINDOW_DAYS)).isoformat()
    return None


def _evidence_tier_for_entry(e: CriterionEvidence) -> str:
    """Classify a single evidence row into its earned evidence tier."""
    if _evidence_proves_covered(e):
        return "covered"
    if _evidence_is_attested(e):
        return "attested"
    if _evidence_is_backfilled(e):
        return "backfilled"
    if _evidence_is_probed(e):
        return "probed"
    return "partial"


def _evidence_provenance_for_entry(e: CriterionEvidence) -> dict[str, Any] | None:
    """Extract runner provenance from the evidence detail dict, if present."""
    detail = e.detail if isinstance(e.detail, dict) else {}
    executing_backend = detail.get("executing_backend")
    runner_id = detail.get("runner_target_id")
    runner_hostname = detail.get("runner_hostname")
    if not executing_backend and not runner_id and not runner_hostname:
        return None
    return {
        "executing_backend": executing_backend or None,
        "runner_id": runner_id or None,
        "runner_hostname": runner_hostname or None,
    }


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
            "tier": _evidence_tier_for_entry(e),
            "provenance": _evidence_provenance_for_entry(e),
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
    such as ``reachability``, ``task_merge`` with ``proven=True``, or
    ``audit_finding_backfill``) is sufficient.  A criterion covered only by a
    lower-confidence tier (``'attested'``, ``'backfilled'``, or ``'probed'`` —
    the latter a machine-probe / sweep-ran signal) is likewise treated as NOT
    covering.  This is intentional: a direction backed solely by operator
    attestation or a mechanism-ran probe should not auto-retire as "successful"
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
        # Batch-load evidence for ALL active criteria in a single query
        # instead of issuing N individual SELECTs (one per criterion).
        evidence_map = await get_evidence_for_criteria_batch(
            session, [c.id for c in active]
        )
        for criterion in active:
            evidence = evidence_map.get(criterion.id, [])
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


async def _increment_merge_gap(
    session: AsyncSession,
    project_id: uuid.UUID,
    reason: MergeEvidenceReason,
    unmatched_ids: list[str],
) -> None:
    """Persist a cumulative merge-evidence gap counter on the open release version.

    Updates the ``merge_evidence_gaps`` JSON column so the gap is visible
    through the coverage-summary API, not only in logs.

    Best-effort: errors are logged but never re-raised so a failure here never
    blocks merge finalization.
    """
    try:
        from app.release_version import current_open_version as _cur_open_ver  # noqa: PLC0415

        open_ver = await _cur_open_ver(session, project_id)
        if open_ver is None:
            return
        gaps: dict[str, Any] = dict(open_ver.merge_evidence_gaps or {})
        reason_key = reason.value
        gaps[reason_key] = int(gaps.get(reason_key, 0)) + 1
        if unmatched_ids:
            existing: list[str] = list(gaps.get("unmatched_criterion_ids", []))
            for uid in unmatched_ids:
                if uid not in existing:
                    existing.append(uid)
            gaps["unmatched_criterion_ids"] = existing
        open_ver.merge_evidence_gaps = gaps
        await session.commit()
    except Exception:
        logger.warning(
            "_increment_merge_gap: failed to persist gap for project=%s reason=%s",
            project_id,
            reason,
            exc_info=True,
        )


async def record_task_merge_evidence(
    session: AsyncSession,
    task: Any,
    project_id: uuid.UUID,
    *,
    preloaded_direction: Any | None = None,
    preloaded_open_ver: Any | None = None,
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

    ``preloaded_direction`` and ``preloaded_open_ver`` are optional batch
    optimisation kwargs: callers that process many tasks for the same direction /
    project can preload the shared objects once and pass them here to avoid one
    DB round-trip each per task.  Falls back to individual queries when absent.

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
            if preloaded_direction is not None:
                direction_row = preloaded_direction
            else:
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
        await _increment_merge_gap(session, project_id, MergeEvidenceReason.no_criteria, [])
        return 0
    from app.acceptance_gate import CODE_ACCEPTANCE_VERDICT_KEY

    verdict = task_context.get(CODE_ACCEPTANCE_VERDICT_KEY)
    if _reject_unproven_merge_evidence(verdict, task, project_id, criterion_ids):
        await _increment_merge_gap(session, project_id, MergeEvidenceReason.verdict_not_proven, criterion_ids)
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
    # Best-effort: attach runner provenance from the task's most recent compute job.
    # Never block merge finalization on failure.
    try:
        task_id_val = getattr(task, "id", None)
        if task_id_val is not None:
            from app.models import ComputeJob

            _job_result = await session.execute(
                select(ComputeJob)
                .where(ComputeJob.task_id == task_id_val)
                .order_by(ComputeJob.created_at.desc())
                .limit(1)
            )
            _job = _job_result.scalar_one_or_none()
            if _job is not None:
                _backend = getattr(_job, "backend", None)
                if _backend:
                    task_merge_detail["executing_backend"] = _backend
                _meta = getattr(_job, "polling_metadata", None) or {}
                _runner_id = _meta.get("runner_target_id")
                _runner_hostname = _meta.get("runner_hostname")
                if _runner_id:
                    task_merge_detail["runner_target_id"] = str(_runner_id)
                if _runner_hostname:
                    task_merge_detail["runner_hostname"] = str(_runner_hostname)
    except Exception:
        logger.debug(
            "record_task_merge_evidence: could not attach runner provenance for task %s",
            getattr(task, "id", None),
            exc_info=True,
        )
    try:
        _release_version_id: uuid.UUID | None = None
        try:
            if preloaded_open_ver is not None:
                _open_ver = preloaded_open_ver
            else:
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
            await _increment_merge_gap(
                session, project_id, MergeEvidenceReason.no_active_criteria, criterion_ids
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
        evidence_batch: list[CriterionEvidence] = []
        source_id = task_id_str or None
        for row in rows:
            existing = evidence_by_criterion[row.id]
            if ("task_merge", source_id) not in existing:
                evidence_batch.append(CriterionEvidence(
                    criterion_id=row.id,
                    project_id=project_id,
                    source_kind="task_merge",
                    source_id=source_id,
                    detail=task_merge_detail,
                ))
            if ("reachability", source_id) not in existing:
                evidence_batch.append(CriterionEvidence(
                    criterion_id=row.id,
                    project_id=project_id,
                    source_kind="reachability",
                    source_id=source_id,
                    detail={"proven": True},
                ))
        recorded = await _record_evidence_batch(session, evidence_batch)
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

        # Preload the Direction row once for the whole backfill — all tasks in
        # this call share the same direction_key, so fetching per-task wastes
        # N round-trips.
        preloaded_direction = await session.scalar(
            select(Direction).where(
                Direction.project_id == project_id,
                Direction.direction_key == direction_key,
            )
        )

        dialect = resolve_dialect_name(session)
        if dialect == "sqlite":
            from sqlalchemy import func as _func
            direction_filter = _func.json_extract(Task.context, "$.direction_id") == direction_key
        else:
            from sqlalchemy import cast as _cast
            from sqlalchemy.dialects.postgresql import JSONB as _JSONB
            direction_filter = Task.context.op("@>")(_cast({"direction_id": direction_key}, _JSONB))

        _BATCH_LIMIT = 500
        total = 0
        offset = 0
        while True:
            batch_result = await session.execute(
                select(Task)
                .where(
                    Task.project_id == project_id,
                    Task.status == TaskStatus.COMPLETED,
                    Task.completed_at >= version_opened_at,
                    landed_signal_predicate(session),
                    direction_filter,
                )
                .order_by(Task.id)
                .limit(_BATCH_LIMIT)
                .offset(offset)
            )
            batch = list(batch_result.scalars().all())
            if not batch:
                break
            offset += len(batch)
            for task in batch:
                if not is_merged_completion(task):
                    continue
                try:
                    total += await record_task_merge_evidence(
                        session, task, project_id,
                        preloaded_direction=preloaded_direction,
                        preloaded_open_ver=open_version,
                    )
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
            offset,
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


async def backfill_all_directions_merge_evidence(
    session: AsyncSession,
    project_id: uuid.UUID,
    direction_keys: list[str],
) -> dict[str, int]:
    """Backfill merge evidence for multiple directions in a single Task table scan.

    Instead of calling :func:`backfill_direction_merge_evidence` once per
    direction key (each triggering its own full Task pagination), this function
    does a single paginated scan of COMPLETED tasks in the current version window
    and routes each task to its direction based on ``task.context["direction_id"]``.

    Returns a dict mapping each direction_key to the number of evidence rows newly
    recorded for tasks belonging to that direction.  Best-effort: exceptions are
    logged and swallowed so a failure never raises to the caller.
    """
    if not direction_keys:
        return {}
    try:
        from app.landed_signal import is_merged_completion, landed_signal_predicate
        from app.models import Task, TaskStatus
        from app.release_version import current_open_version as _cur_open_ver

        open_version = await _cur_open_ver(session, project_id)
        if open_version is None:
            logger.info(
                "backfill_all_directions_merge_evidence: no open version for "
                "project %s; skipping backfill",
                project_id,
            )
            return {}
        version_opened_at = open_version.opened_at
        direction_keys_set = frozenset(direction_keys)

        # Preload all needed Direction rows in one query — one per key, not one
        # per task — so record_task_merge_evidence can skip the per-task fetch.
        dir_result = await session.execute(
            select(Direction).where(
                Direction.project_id == project_id,
                Direction.direction_key.in_(list(direction_keys_set)),
            )
        )
        preloaded_directions: dict[str, Any] = {
            d.direction_key: d for d in dir_result.scalars().all()
        }

        totals: dict[str, int] = {}
        _BATCH_LIMIT = 500
        offset = 0
        total_scanned = 0
        while True:
            batch_result = await session.execute(
                select(Task)
                .where(
                    Task.project_id == project_id,
                    Task.status == TaskStatus.COMPLETED,
                    Task.completed_at >= version_opened_at,
                    landed_signal_predicate(session),
                )
                .order_by(Task.id)
                .limit(_BATCH_LIMIT)
                .offset(offset)
            )
            batch = list(batch_result.scalars().all())
            if not batch:
                break
            offset += len(batch)
            total_scanned += len(batch)

            for task in batch:
                if not is_merged_completion(task):
                    continue
                task_context: dict = getattr(task, "context", None) or {}
                direction_id_key: str | None = task_context.get("direction_id")
                if direction_id_key not in direction_keys_set:
                    continue
                try:
                    count = await record_task_merge_evidence(
                        session, task, project_id,
                        preloaded_direction=preloaded_directions.get(direction_id_key),
                        preloaded_open_ver=open_version,
                    )
                    if count:
                        totals[direction_id_key] = totals.get(direction_id_key, 0) + count
                except Exception:
                    logger.warning(
                        "backfill_all_directions_merge_evidence: failed for task %s, skipping",
                        task.id,
                        exc_info=True,
                    )

        total = sum(totals.values())
        logger.info(
            "backfill_all_directions_merge_evidence: project=%s directions=%d "
            "tasks_scanned=%d evidence_recorded=%d",
            project_id,
            len(direction_keys),
            total_scanned,
            total,
        )
        return totals
    except Exception:
        logger.exception(
            "backfill_all_directions_merge_evidence: failed for project %s",
            project_id,
        )
        return {}


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

        _BATCH_LIMIT = 500
        total = 0
        offset = 0
        open_version: Any = None
        # Criteria (key -> text) covered by backfill this run — each gets a
        # corroboration task so the 'backfilled' tier has an earn-up-or-downgrade
        # path, mirroring operator attestation.  Populated even for criteria whose
        # evidence row already existed so a pre-existing backfilled tail is closed
        # idempotently (spawn dedup guards against duplicate tasks).
        corroboration_targets: dict[str, str] = {}
        while True:
            query = (
                select(Task)
                .where(
                    Task.project_id == project_id,
                    Task.status == TaskStatus.COMPLETED,
                    landed_signal_predicate(session),
                )
                .order_by(Task.id)
                .limit(_BATCH_LIMIT)
                .offset(offset)
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

            batch_result = await session.execute(query)
            batch = list(batch_result.scalars().all())
            if not batch:
                break
            offset += len(batch)

            if open_version is None:
                open_version = await _cur_open_ver(session, project_id)
                if open_version is None:
                    logger.info(
                        "backfill_unverified_merge_evidence: no open version for "
                        "project %s — cannot resolve active criteria",
                        project_id,
                    )
                    return total

            # Batch-load all existing backfill evidence for this batch in one query.
            task_id_strs = [str(t.id) for t in batch]
            ev_batch_result = await session.execute(
                select(CriterionEvidence).where(
                    CriterionEvidence.source_kind == UNVERIFIED_MERGE_BACKFILL_EVIDENCE_KIND,
                    CriterionEvidence.source_id.in_(task_id_strs),
                )
            )
            all_present: dict[str, set[uuid.UUID]] = {}
            for ev in ev_batch_result.scalars().all():
                all_present.setdefault(ev.source_id, set()).add(ev.criterion_id)

            # First pass: identify qualifying tasks and collect their criterion_ids
            # so we can fetch all criteria in a single query instead of one per task.
            _QualifiedTask = tuple  # (task, criterion_ids, verdict, declared_test)
            qualified: list[tuple] = []
            all_batch_criterion_ids: set[str] = set()
            for task in batch:
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

                # ``partition`` (NOT ``rpartition``): the ``::node`` selector is
                # OPTIONAL on a named_test declaration (see AcceptanceEvidence's
                # validator), so a bare test-file path is legal and common.
                # ``rpartition`` returns '' as its head when the separator is
                # absent, which silently emptied ``declared_test`` for every such
                # declaration — ``_paths_match(p, "")`` is False for every changed
                # file, so the task was skipped and NO evidence was ever recorded.
                # ``partition`` yields the whole path when there is no selector,
                # matching acceptance_gate._evaluate_named_test's canonical read.
                declared_test = evidence.test_path.partition("::")[0].strip()
                if not declared_test:
                    continue
                test_present = any(_paths_match(p, declared_test) for p in changed)
                if not test_present:
                    continue

                # Must also have a production seam to exercise.
                seam = [p for p in changed if not _is_test_path(p)]
                if not seam:
                    continue

                task_criterion_ids: list[str] = list(
                    getattr(task, "criterion_ids", None) or []
                )
                if not task_criterion_ids:
                    continue

                qualified.append((task, task_criterion_ids, verdict, declared_test))
                all_batch_criterion_ids.update(task_criterion_ids)

            # Single criteria query for all qualifying tasks in this batch.
            criteria_by_key: dict[str, VersionAcceptanceCriterion] = {}
            if all_batch_criterion_ids:
                batch_criteria = await get_criteria_by_keys(
                    session,
                    project_id,
                    list(all_batch_criterion_ids),
                    release_version_id=open_version.id,
                )
                criteria_by_key = {
                    c.criterion_key: c for c in batch_criteria if c.status == "active"
                }

            # Second pass: record evidence using pre-fetched criteria.
            evidence_batch: list[CriterionEvidence] = []
            for task, task_criterion_ids, verdict, declared_test in qualified:
                active = [criteria_by_key[k] for k in task_criterion_ids if k in criteria_by_key]
                if not active:
                    continue

                already_present: set[uuid.UUID] = all_present.get(str(task.id), set())
                task_id_str = str(task.id)
                unverified_reason = _task_unverified_reason(task) or "unknown"
                for criterion in active:
                    # Queue a corroboration task for every active backfilled
                    # criterion, whether or not its evidence row is new — this
                    # earns-up or downgrades pre-existing backfill too.
                    corroboration_targets[criterion.criterion_key] = criterion.text
                    if criterion.id in already_present:
                        continue

                    detail: dict[str, Any] = {
                        "proven": True,
                        "backfilled": True,
                        "backfill_reason": "unverified_merge",
                        "unverified_merge_reason": unverified_reason,
                        "proof_ref": verdict.get("proof_ref") or declared_test,
                    }
                    evidence_batch.append(CriterionEvidence(
                        criterion_id=criterion.id,
                        project_id=project_id,
                        source_kind=UNVERIFIED_MERGE_BACKFILL_EVIDENCE_KIND,
                        source_id=task_id_str,
                        detail=detail,
                    ))

            if evidence_batch:
                batch_recorded = await _record_evidence_batch(session, evidence_batch)
                total += batch_recorded

        # Give each backfilled criterion an earn-up path: spawn a corroboration
        # task (deduped by the existing ``Verify: {key}`` title guard) so the
        # lower-confidence backfill tier is re-verified toward machine-verified
        # coverage — and downgraded to 'partial' if that verification fails —
        # exactly as an operator attestation is.
        for _ck, _ctext in corroboration_targets.items():
            await spawn_corroboration_task(
                session,
                project_id,
                _ck,
                _ctext,
                "unverified-merge backfill: named_test shipped in the PR's changed "
                "files but the merge was not CI-verified",
                spawned_by=UNVERIFIED_MERGE_BACKFILL_EVIDENCE_KIND,
            )

        if total:
            logger.info(
                "backfill_unverified_merge_evidence: project=%s "
                "tasks_scanned=%d recorded=%d",
                project_id,
                offset,
                total,
            )
        return total
    except Exception:
        logger.exception(
            "backfill_unverified_merge_evidence: failed for project %s",
            project_id,
        )
        return 0


# Depth cap for the parent-ancestor walk in
# :func:`backfill_rollup_child_changed_files`. Rollup nesting is shallow (a
# child under a rollup parent, occasionally a rollup-of-rollups), so a small
# cap bounds the walk cheaply while a visited-set guards against a cyclic
# parent_task_id chain from corrupt data.
_ROLLUP_PARENT_WALK_MAX_DEPTH = 8


async def backfill_rollup_child_changed_files(
    session: AsyncSession,
    project_id: uuid.UUID,
    direction_key: str | None = None,
) -> int:
    """Retroactively stamp a rollup parent's changed files onto landed children.

    The forward-path propagation in
    :func:`app.rollup_management._propagate_landed_to_staging_merged_descendants`
    only heals NEW landings.  Already-landed rollup children (merged via the
    parent's single PR) carry ``landed_on_integration`` but never received the
    parent's GitHub-verified ``_pr_changed_files``, so their declared
    ``named_test`` cannot be located in a diff and their acceptance verdict is
    stuck at ``declared_unsatisfied``; the unverified-merge backfill skips them
    because ``_changed_files(task)`` is empty.

    This best-effort reconcile pass finds landed COMPLETED tasks that carry NO
    ``_pr_changed_files`` but whose nearest landed ancestor (walking
    ``parent_task_id``) DOES carry a GitHub-verified changed-file list, and
    stamps that list onto the child using the SAME parent-derived semantics as
    the forward path: :func:`stamp_parent_derived_changed_files_signal`
    (``setdefault`` — never clobber a child's own list — and deliberately NO
    ``_pr_changed_files_github_verified`` sentinel, so credit is capped at the
    satisfied/unproven tier and never mints machine ``proven`` credit from the
    parent's union diff).  The acceptance verdict is then re-evaluated so a stale
    ``declared_unsatisfied`` reason flips.

    Wired to run BEFORE :func:`backfill_unverified_merge_evidence` in the
    reconcile endpoint so that, within the SAME run, a newly-stamped child is
    picked up by the unverified-merge backfill and earns criterion evidence.

    ``direction_key``, when provided, scopes the search to tasks whose context
    carries that ``direction_id`` (matching the sibling backfills).

    Idempotent (``setdefault`` — a re-run stamps nothing new) and best-effort
    (errors are logged and swallowed so a failure never blocks the caller).

    Returns the count of children newly stamped by this call.
    """
    try:
        from app import acceptance_gate as _acceptance_gate
        from app.database import resolve_dialect_name
        from app.landed_signal import is_merged_completion, landed_signal_predicate
        from app.models import Task, TaskStatus

        _BATCH_LIMIT = 500
        stamped_total = 0
        offset = 0
        # Cache ancestor lookups across the scan — sibling children share the
        # same rollup parent, so one github-verified resolution per ancestor id
        # avoids re-walking the chain for every child.
        resolved_parent_changed: dict[uuid.UUID, list[str]] = {}

        async def _github_verified_ancestor_changed(child: Task) -> list[str]:
            """Nearest landed ancestor's GitHub-verified changed files, or []."""
            visited: set[uuid.UUID] = {child.id}
            parent_id = child.parent_task_id
            depth = 0
            while parent_id is not None and depth < _ROLLUP_PARENT_WALK_MAX_DEPTH:
                if parent_id in visited:
                    break
                visited.add(parent_id)
                if parent_id in resolved_parent_changed:
                    return resolved_parent_changed[parent_id]
                ancestor = await session.get(Task, parent_id)
                if ancestor is None:
                    break
                ctx = ancestor.context or {}
                if ctx.get("_pr_changed_files_github_verified") is True:
                    changed = [
                        p
                        for p in (ctx.get("_pr_changed_files") or [])
                        if isinstance(p, str) and p.strip()
                    ]
                    resolved_parent_changed[parent_id] = changed
                    return changed
                depth += 1
                parent_id = ancestor.parent_task_id
            return []

        while True:
            query = (
                select(Task)
                .where(
                    Task.project_id == project_id,
                    Task.status == TaskStatus.COMPLETED,
                    landed_signal_predicate(session),
                )
                .order_by(Task.id)
                .limit(_BATCH_LIMIT)
                .offset(offset)
            )

            if direction_key:
                dialect = resolve_dialect_name(session)
                if dialect == "sqlite":
                    from sqlalchemy import func as _func
                    query = query.where(
                        _func.json_extract(Task.context, "$.direction_id")
                        == direction_key
                    )
                else:
                    from sqlalchemy import cast as _cast
                    from sqlalchemy.dialects.postgresql import JSONB as _JSONB
                    query = query.where(
                        Task.context.op("@>")(
                            _cast({"direction_id": direction_key}, _JSONB)
                        )
                    )

            batch = list((await session.execute(query)).scalars().all())
            if not batch:
                break
            offset += len(batch)

            # Bulk-prime the SQLAlchemy identity map with the direct parent of
            # every qualifying child in this batch.  The walk inside
            # _github_verified_ancestor_changed calls session.get(Task, id),
            # which hits the identity map on a cache hit, avoiding N×depth
            # individual point-queries.  We load only the immediate parents
            # here; deeper ancestors are loaded on demand (rare — most chains
            # are ≤1 hop deep) and are cached after the first walk.
            batch_parent_ids = {
                t.parent_task_id
                for t in batch
                if t.parent_task_id is not None
                and is_merged_completion(t)
                and not _acceptance_gate._changed_files(t)
                and t.parent_task_id not in resolved_parent_changed
            }
            if batch_parent_ids:
                await session.execute(
                    select(Task).where(Task.id.in_(list(batch_parent_ids)))
                )

            batch_stamped = 0
            for task in batch:
                if not is_merged_completion(task):
                    continue
                # Only children that lack their own changed-file context — a
                # child that already carries a list is left untouched (setdefault
                # / idempotency).
                if _acceptance_gate._changed_files(task):
                    continue
                if task.parent_task_id is None:
                    continue

                parent_changed = await _github_verified_ancestor_changed(task)
                if not parent_changed:
                    continue

                new_ctx = dict(task.context or {})
                for k, v in _acceptance_gate.stamp_parent_derived_changed_files_signal(
                    parent_changed
                ).items():
                    new_ctx.setdefault(k, v)
                task.context = new_ctx
                _acceptance_gate.record_code_acceptance_verdict(task)
                session.add(task)
                batch_stamped += 1

            if batch_stamped:
                await session.commit()
                stamped_total += batch_stamped

        if stamped_total:
            logger.info(
                "backfill_rollup_child_changed_files: project=%s "
                "tasks_scanned=%d stamped=%d",
                project_id,
                offset,
                stamped_total,
            )
        return stamped_total
    except Exception:
        logger.exception(
            "backfill_rollup_child_changed_files: failed for project %s",
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
            'backfilled': N,
            'probed': N,
            'partial': N,
            'uncovered': N,
            'retracted': N,
            'superseded': N,
            'inactive': N,
            'stuck_count': N,
            'phantom_bookkeeping': {  # criterion-key drift guard (best-effort)
                'phantom_keys': [str, ...],  # referenced but never registered
                'zero_registered': bool,     # keys referenced, zero active criteria
                'has_drift': bool,
                'referenced_keys': [str, ...],
                'registered_count': int,
            },
            'criteria': [
                {
                    'criterion_key': str,
                    'text': str,
                    'coverage': 'covered' | 'attested' | 'backfilled' | 'probed' | 'partial' | 'uncovered',
                    'status': 'active' | 'retracted' | 'superseded',
                    'evidence_count': int,
                    'evidence_truncated': bool,
                    'stuck_reason': 'no_mapping' | 'direction_retired' | None,
                    'evidence': [ ... ],  # capped provenance preview
                },
                ...
            ],
        }

    ``total`` and the coverage buckets (covered + attested + backfilled + probed
    + partial + uncovered) count active criteria only, and ``total`` remains
    their sum for backward compatibility. ``attested`` is a distinct,
    lower-confidence tier: active criteria whose only covering signal is a
    non-expired operator attestation. ``backfilled`` is a distinct,
    lower-confidence tier for criteria covered solely by unverified-merge
    backfill evidence (the test shipped in the PR's changed files but the merge
    was never CI-verified). ``probed`` is a distinct, lower-confidence,
    time-bounded tier for criteria covered solely by a non-expired machine-probe
    signal (``acceptance_window_recompute`` / ``metrics_integrity_sweep`` — the
    sweep mechanism ran, but that does not prove the criterion's own condition).
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
        "backfilled": 0,
        "probed": 0,
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

    # Downgrade 'attested'/'backfilled' to 'partial' for criteria whose
    # corroboration task FAILED or was CANCELLED — the system explicitly
    # attempted to verify the lower-confidence coverage and failed, so it must
    # not remain trusted.  Both an operator attestation and an unverified-merge
    # backfill earn a spawned ``Verify: {key}`` task (see
    # :func:`spawn_corroboration_task`) and are downgraded here identically:
    # neither tier may claim coverage once its verification attempt has failed.
    failed_corroboration: set[str] = set()
    if criteria_in_order and (
        counts.get("attested", 0) > 0 or counts.get("backfilled", 0) > 0
    ):
        failed_corroboration = await get_failed_corroboration_keys(
            session, criteria_in_order[0].project_id
        )
        if failed_corroboration:
            for criterion in criteria_in_order:
                if criterion.status != "active":
                    continue
                co = coverage_by_criterion.get(criterion.id)
                if (
                    co in ("attested", "backfilled")
                    and criterion.criterion_key in failed_corroboration
                ):
                    coverage_by_criterion[criterion.id] = "partial"
                    counts[co] -= 1
                    counts["partial"] = counts.get("partial", 0) + 1

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
                "attestation_expires_at": (
                    _compute_attestation_expires_at(evidence)
                    if coverage == "attested"
                    else None
                ),
            }
        )

    from app.release_models import ReleaseVersion  # noqa: PLC0415

    _rv = await session.get(ReleaseVersion, release_version_id)
    merge_evidence_gaps = _rv.merge_evidence_gaps if _rv is not None else None

    # Drift guard: reconcile criterion keys referenced in eligible strategic prose
    # against criteria registered on this version. A zero-criteria version (or
    # prose citing never-registered keys) otherwise reads PASS at every gate with
    # no warning — the "phantom bookkeeping" failure this surfaces loud.
    # This consumes mutable project state, so it is meaningful only for the
    # project's current open release. Historical summaries must remain stable
    # when today's focus or directions change. Within the open release every
    # eligible reference is reconciled against that release alone: a key that
    # exists only on a prior release is still dangling current-release prose,
    # not harmless history.
    phantom_bookkeeping = {
        "phantom_keys": [],
        "zero_registered": False,
        "has_drift": False,
        "referenced_keys": [],
        "registered_count": 0,
    }
    if _rv is not None and _rv.status == "open":
        try:
            from app.models import Project  # noqa: PLC0415 — avoid import cycle
            from app.release_version import current_open_version  # noqa: PLC0415

            current_version = await current_open_version(session, _rv.project_id)
            if current_version is not None and current_version.id == _rv.id:
                _project = await session.get(Project, _rv.project_id)
                registered_keys = {
                    c.criterion_key
                    for c in criteria_in_order
                    if c.status == "active"
                }
                referenced_keys = (
                    _gather_referenced_criterion_keys(_project)
                    if _project is not None
                    else set()
                )
                phantom_bookkeeping = reconcile_criterion_key_references(
                    registered_keys, referenced_keys
                )
                phantom_bookkeeping["referenced_keys"] = sorted(referenced_keys)
                phantom_bookkeeping["registered_count"] = len(registered_keys)
        except Exception:  # noqa: BLE001 — best-effort drift signal, never blocks
            logger.exception(
                "phantom-bookkeeping drift guard failed for version %s",
                release_version_id,
            )

    return {
        "total": sum(counts.values()),
        "covered": counts["covered"],
        "attested": counts["attested"],
        "backfilled": counts["backfilled"],
        "probed": counts["probed"],
        "partial": counts["partial"],
        "uncovered": counts["uncovered"],
        "retracted": inactive_counts["retracted"],
        "superseded": inactive_counts["superseded"],
        "inactive": sum(inactive_counts.values()),
        "stuck_count": stuck_count,
        "criteria": criteria_detail,
        "failed_corroboration_keys": list(failed_corroboration),
        "merge_evidence_gaps": merge_evidence_gaps,
        "phantom_bookkeeping": phantom_bookkeeping,
    }


async def get_failed_corroboration_keys(
    session: AsyncSession,
    project_id: uuid.UUID,
) -> set[str]:
    """Return criterion_keys whose corroboration task has FAILED or been
    CANCELLED **and** no newer non-terminal task exists for the same key.

    A corroboration task is spawned for both lower-confidence coverage tiers —
    operator attestation and unverified-merge backfill (see
    :func:`spawn_corroboration_task`).  This function is provenance-agnostic: it
    keys purely on ``context["corroboration_of"]``, so it returns keys for a
    failed verification regardless of which tier spawned it.

    When such a ``Verify: {criterion_key}`` task ends in FAILED or CANCELLED
    status, the lower-confidence coverage should no longer be fully trusted — the
    system explicitly attempted to verify it and that attempt failed.  Callers use
    this set to downgrade coverage from ``'attested'``/``'backfilled'`` to
    ``'partial'`` and to surface operator-attention items.

    However, if a newer corroboration attempt is spawned after a failure (e.g. the
    operator re-attests, or a later backfill reconcile re-spawns), a fresh
    non-terminal ``Verify: {criterion_key}`` task exists (the dedup in
    :func:`spawn_corroboration_task` only excludes COMPLETED/FAILED/CANCELLED).
    The old failed task remains in the database but is *stale* — this function
    excludes keys that have a newer non-terminal task so that re-verified criteria
    are not stuck at ``'partial'`` coverage indefinitely.

    **Scaling (audit finding #9).** This runs on the qualifier finalize path, so
    it must not scale with the project's total failure history. Both queries
    therefore push the ``corroboration_of`` filter into SQL and project ONLY that
    one JSON key — never the full ``context`` JSONB blob of every FAILED /
    CANCELLED task in the project. Corroboration tasks are a tiny fraction of a
    mature project's terminal tasks, so the server-side ``IS NOT NULL`` predicate
    is what keeps the row count (and the bytes on the wire) proportional to the
    corroboration tasks rather than to the whole failure history.

    Behaviour is unchanged: the JSON element is read back with its type intact and
    the same ``isinstance(key, str)`` guard applies, so a malformed
    ``corroboration_of`` (JSON null, number, object) is still ignored rather than
    coerced into a key.
    """
    from app.models import Task, TaskStatus  # noqa: PLC0415

    # The JSON element (typed) and its text form. The text form is only used for
    # the SQL existence predicate (``->> 'corroboration_of' IS NOT NULL``, which a
    # JSON null also fails — matching the isinstance guard below); the element is
    # what we read, so a non-string value is still rejected in Python instead of
    # being laundered into a key by the ``->>`` text cast.
    corroboration_key = Task.context["corroboration_of"]
    corroboration_key_text = corroboration_key.as_string()

    result = await session.execute(
        select(corroboration_key).where(
            Task.project_id == project_id,
            Task.status.in_([TaskStatus.FAILED, TaskStatus.CANCELLED]),
            corroboration_key_text.isnot(None),
        )
    )
    failed: set[str] = {key for (key,) in result.all() if isinstance(key, str)}

    # Exclude keys that have a newer non-terminal task — the criterion was
    # re-attested and a fresh verification attempt is in progress, so the old
    # failure is stale.
    if failed:
        active_titles = [f"Verify: {key}" for key in failed]
        active_result = await session.execute(
            select(corroboration_key).where(
                Task.project_id == project_id,
                Task.title.in_(active_titles),
                Task.status.not_in(
                    [TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.CANCELLED]
                ),
                corroboration_key_text.isnot(None),
            )
        )
        for (key,) in active_result.all():
            if isinstance(key, str):
                failed.discard(key)

    return failed


async def spawn_corroboration_task(
    session: AsyncSession,
    project_id: uuid.UUID,
    criterion_key: str,
    criterion_text: str,
    reason: str,
    spawned_by: str = "operator_attestation",
) -> None:
    """Spawn a research task to investigate and earn real coverage for a criterion.

    Called for the two lower-confidence coverage tiers that need re-verification:
    after an operator attests a criterion (see
    :func:`~app.routers.admin_acceptance_criteria.attest_criterion`,
    ``spawned_by="operator_attestation"``) and after unverified-merge backfill
    evidence is recorded (see :func:`backfill_unverified_merge_evidence`,
    ``spawned_by=UNVERIFIED_MERGE_BACKFILL_EVIDENCE_KIND``).  The spawned task
    carries ``criterion_ids=[criterion_key]`` so that, when it later completes and
    merges with a proven acceptance verdict, the existing
    :func:`record_task_merge_evidence` machinery writes ``task_merge`` +
    ``reachability`` evidence, transitioning the criterion from its lower tier to
    ``'covered'``.  If the task instead FAILS or is CANCELLED,
    :func:`get_failed_corroboration_keys` reports the key and coverage is
    downgraded to ``'partial'``.

    ``spawned_by`` is recorded in the task ``context`` for provenance and selects
    the prompt phrasing; it does not affect dedup (keyed on the ``Verify: {key}``
    title) or the failed-corroboration downgrade (keyed on ``corroboration_of``).

    The task is best-effort: any failure is logged and swallowed so the
    attestation or backfill itself is never blocked by a spawn failure.
    """
    try:
        from app.models import Task, TaskStatus  # noqa: PLC0415

        # Dedup: skip spawning when a non-terminal corroboration task already
        # exists for this criterion.  The attest endpoint calls this on BOTH
        # fresh attestation and re-attestation (renewal), so without this
        # guard every re-attest would create a duplicate task.
        existing = await session.execute(
            select(Task).where(
                Task.project_id == project_id,
                Task.title == f"Verify: {criterion_key}",
                Task.status.not_in(
                    [TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.CANCELLED]
                ),
            )
        )
        if existing.scalars().first() is not None:
            logger.info(
                "[AC] Corroboration task already exists for criterion %s "
                "(project %s) — skipping spawn",
                criterion_key,
                project_id,
            )
            return

        if spawned_by == UNVERIFIED_MERGE_BACKFILL_EVIDENCE_KIND:
            prompt = (
                f"Verify acceptance criterion: {criterion_text}\n\n"
                f"Unverified-merge backfill reason: {reason}\n\n"
                "The declared named_test shipped in the PR's changed files but the "
                "merge fell open without a green CI verdict, so coverage is recorded "
                "at the lower-confidence 'backfilled' tier.  Re-run or repair the "
                "declared test so the criterion can transition from backfill to real "
                "machine-verified coverage."
            )
        else:
            prompt = (
                f"Verify acceptance criterion: {criterion_text}\n\n"
                f"Operator attestation reason: {reason}\n\n"
                "Determine what test or automated check would prove this "
                "criterion is satisfied, and implement it so the criterion "
                "can transition from operator attestation to real machine-verified coverage."
            )

        task = Task(
            project_id=project_id,
            title=f"Verify: {criterion_key}",
            criterion_ids=[criterion_key],
            context={"corroboration_of": criterion_key, "spawned_by": spawned_by},
            payloads=[
                {
                    "type": "research",
                    "prompt": prompt,
                }
            ],
            created_by="SYSTEM_AUTO_REVIEW",
        )
        session.add(task)
        await session.commit()
        logger.info(
            "[AC] Corroboration task spawned for criterion %s (project %s) — task_id=%s",
            criterion_key,
            project_id,
            task.id,
        )
    except Exception:
        logger.exception(
            "[AC] Failed to spawn corroboration task for criterion %s (project %s) — "
            "attestation itself is unaffected",
            criterion_key,
            project_id,
        )

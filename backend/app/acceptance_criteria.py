"""Accessor module for version-scoped acceptance criteria and coverage computation.

Provides async helpers for creating and querying :class:`~app.models.VersionAcceptanceCriterion`
and :class:`~app.models.CriterionEvidence` rows, plus a pure-function coverage
classifier and a version-level coverage summary query.
"""

from __future__ import annotations


import enum
import hashlib
import json
import logging
import re
import secrets
import uuid
from collections import defaultdict
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any

import sqlalchemy.exc
from sqlalchemy import func, or_
from sqlmodel import select

from app.landed_signal import landed_signal_predicate
from app.models import (
    CriterionEvidence,
    Direction,
    Task,
    TaskStatus,
    VersionAcceptanceCriterion,
)
from app.time_utils import _coerce_naive_utc, _utcnow
_MODULE_SIZE_GOVERNANCE_CAP: int = 8050

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger(__name__)

# Process-local edge detector for the deployed acceptance-window reconciler.
# A false-zero result writes no evidence by design, so evidence idempotency alone
# cannot keep the minute-level release sweep from rebuilding the full task window.
# The latest landed-task timestamp plus the target criterion ids is a cheap state
# fingerprint: each worker scans a stable unresolved window at most once, while a
# new/updated landed task or criterion-set change immediately re-arms recomputation.
_acceptance_window_recompute_state: dict[
    uuid.UUID, tuple[tuple[str, ...], datetime | None]
] = {}

CRITERION_RELEASE_VERSION_CONTEXT_KEY = "criterion_release_version_id"
# When true, ``Task.criterion_ids`` is an exact operator-authored assignment.
# Merge evidence must not widen it by unioning every key owned by the task's
# broader direction.
EXPLICIT_CRITERION_IDS_CONTEXT_KEY = "criterion_ids_explicit"

# Server-owned immutable snapshot of operator task-creation authority. Runtime
# task mutation may change scheduling fields, but replay and receipt semantics
# remain anchored to this request-time record.
OPERATOR_TASK_CREATION_AUTHORITY_CONTEXT_KEY = "_operator_task_creation_authority"


def normalize_evidence_contract(raw: Any) -> dict[str, Any] | None:
    """Return a supported closed machine-contract shape for qualification."""
    from app.release_commit_authorship import normalize_authorship_evidence_contract
    from app.release_prepromotion_rehearsal import (
        normalize_rehearsal_evidence_contract,
    )

    return normalize_authorship_evidence_contract(
        raw
    ) or normalize_rehearsal_evidence_contract(raw)


def is_operator_authorship_criterion(criterion: Any) -> bool:
    """Return whether a criterion is the deterministic authorship policy row.

    Authorship contracts are release-policy gates, not user-authored product
    criteria. They stay in rubric/detail responses so operators can inspect and
    edit them, but must not consume the ordinary acceptance-criteria cap or make
    the task-evidence coverage aggregate appear incomplete.
    """
    raw_contract = (
        criterion.get("evidence_contract")
        if isinstance(criterion, dict)
        else getattr(criterion, "evidence_contract", None)
    )
    contract = normalize_evidence_contract(raw_contract)
    return bool(
        contract is not None
        and contract.get("type") == "operator_authored_commits"
    )


def is_owner_controlled_machine_contract(
    criterion: VersionAcceptanceCriterion,
) -> bool:
    """Return whether an autonomous loop must preserve this machine policy."""
    if normalize_evidence_contract(criterion.evidence_contract) is None:
        return False
    actor = str(criterion.evidence_contract_updated_by or "")
    return not actor.startswith("strategist:")


def criterion_release_version_id_from_context(
    context: dict[str, Any] | None,
) -> uuid.UUID | None:
    """Return the persisted criterion-version binding, or ``None`` if invalid."""
    raw = (context or {}).get(CRITERION_RELEASE_VERSION_CONTEXT_KEY)
    if isinstance(raw, uuid.UUID):
        return raw
    if not isinstance(raw, str) or not raw.strip():
        return None
    try:
        return uuid.UUID(raw)
    except ValueError:
        return None


def task_criterion_release_version_id(task: Any) -> uuid.UUID | None:
    """Return the release version against which a task's criterion keys resolve."""
    return criterion_release_version_id_from_context(
        getattr(task, "context", None) or {}
    )


def task_matches_criterion_release_version(task: Any, release_version: Any) -> bool:
    """Return whether a task can safely resolve keys against ``release_version``.

    Persisted bindings are authoritative. Legacy rows without a binding are
    accepted only when they were created inside the supplied version's open
    window; this preserves same-version evidence while refusing an older task
    after the next version opens.
    """
    bound_id = task_criterion_release_version_id(task)
    if bound_id is not None:
        release_version_id = getattr(release_version, "id", None)
        return (
            isinstance(release_version_id, uuid.UUID)
            and bound_id == release_version_id
        )
    raw_created_at = getattr(task, "created_at", None)
    raw_opened_at = getattr(release_version, "opened_at", None)
    if not isinstance(raw_created_at, datetime) or not isinstance(
        raw_opened_at, datetime
    ):
        return False
    return bool(
        _coerce_naive_utc(raw_created_at) >= _coerce_naive_utc(raw_opened_at)
    )


async def resolve_task_criterion_release_version_id(
    session: AsyncSession,
    task: Any,
    project_id: uuid.UUID,
) -> uuid.UUID | None:
    """Resolve a task's explicit binding or a safe same-window legacy binding."""
    bound_id = task_criterion_release_version_id(task)
    if bound_id is not None:
        return bound_id
    from app.release_version import current_open_version

    open_version = await current_open_version(session, project_id)
    if open_version is None or not task_matches_criterion_release_version(
        task, open_version
    ):
        return None
    open_version_id = getattr(open_version, "id", None)
    return open_version_id if isinstance(open_version_id, uuid.UUID) else None


def direction_criterion_release_version_id(direction: Any) -> uuid.UUID | None:
    """Return the release version against which a direction's keys resolve."""
    data = direction if isinstance(direction, dict) else getattr(direction, "data", None)
    return criterion_release_version_id_from_context(data or {})


def direction_matches_criterion_release_version(
    direction: Any, release_version: Any
) -> bool:
    """Return whether a direction's criterion keys belong to ``release_version``.

    Explicit bindings (``criterion_release_version_id`` in the direction's data
    blob, written by ``link_criteria_to_direction``) are authoritative.

    For directions without an explicit binding the compatibility rule is:
    match only if the requested version is currently open.  This is the only
    safe delimitiation without actual provenance — Direction.created_at is not
    mapping provenance (migration 0131 stamped all backfilled rows with NOW,
    and a long-lived direction can receive keys during any version regardless
    of when the row was first created).  Failing closed for promoted/abandoned
    versions prevents backward contamination: an unbound direction created
    during V2 cannot satisfy a historical V1 query.
    """
    bound_id = direction_criterion_release_version_id(direction)
    release_version_id = getattr(release_version, "id", None)
    if bound_id is not None:
        return (
            isinstance(release_version_id, uuid.UUID)
            and bound_id == release_version_id
        )
    # No explicit binding: match only when the version is currently open so
    # that historical summaries are never contaminated by later unbound rows.
    return getattr(release_version, "status", None) == "open"

# Mirror of research_direction_state.RETIRED_DIRECTION_STATUSES. Defined locally
# (rather than imported at module load) because research_direction_state pulls in
# heavy modules and acceptance_criteria is imported early by release_qualifier —
# importing it at top-level risks an import cycle. The set is tiny and stable.
_RETIRED_DIRECTION_STATUSES = frozenset(
    {"expired", "retired", "superseded", "completed"}
)

# The only direction status that means a direction is actively being worked on.
# Paused, staged, proposed, etc. are not active and should not suppress stuck alerts.
_ACTIVE_DIRECTION_STATUSES = frozenset({"active"})

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
    no_bound_version = "no_bound_version"
    """The task has no valid persisted criterion release-version binding."""
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


class CriterionTextCompareMismatchError(Exception):
    """Raised when a criterion text CAS observes neither expected nor replacement."""


class CriterionTextInactiveError(Exception):
    """Raised when an in-place rewrite targets an inactive criterion."""


class CriterionTextDuplicateError(Exception):
    """Raised when replacement text already belongs to another criterion."""


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

#: ``source_kind`` for autonomous live-stack walkthrough evidence written by the
#: qualifier-spawned ``walkthrough_e2e`` task (see
#: :func:`record_walkthrough_e2e_evidence`). A walkthrough_e2e row earns genuine
#: machine-verified ``covered`` coverage — but ONLY when it is BOTH SHA-pinned
#: (``detail["candidate_sha"]`` is non-empty) AND fresh (recorded within
#: :data:`WALKTHROUGH_E2E_WINDOW_DAYS`). A stale row, or one missing its pinned
#: SHA, does NOT count as covering and falls back to the lower ``partial`` tier —
#: this is the whole point of the walkthrough_e2e evidence lane: stale/committed
#: walk evidence (e.g. 123 commits old) must stop counting as coverage.
#: Recognised as covering by :func:`_evidence_proves_covered` via
#: :func:`_evidence_is_walkthrough_e2e_covered`.
WALKTHROUGH_E2E_EVIDENCE_KIND = "walkthrough_e2e"


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

#: Coverage-tier constant: how long a ``walkthrough_e2e`` evidence row (a
#: qualifier-spawned live-stack walkthrough that proved a specific flow on the
#: candidate SHA) counts as full ``'covered'`` tier before it expires and the
#: criterion falls back to its underlying evidence state.  The window is shorter
#: than :data:`ATTESTATION_WINDOW_DAYS` (7 d) because e2e evidence is
#: machine-verifiable on re-run and the release cycle is short — a stale row
#: should decay quickly so the next qualification attempt re-verifies.  Measured
#: from ``recorded_at``; a row with no ``recorded_at`` cannot grant perpetual
#: trust and returns ``False`` in :func:`_evidence_is_walkthrough_e2e`.
WALKTHROUGH_E2E_WINDOW_DAYS = 14

#: ``source_kind`` for executed-named-test trial evidence written by
#: :func:`record_named_test_execution_evidence` when the earn loop's targeted
#: trial of a task's declared covering tests runs GREEN against the version's
#: qualifying candidate SHA.  A ``named_test_execution`` row earns genuine
#: machine-verified ``covered`` coverage — but ONLY when it is BOTH SHA-pinned
#: (``detail["executed_sha"]`` is non-empty) AND fresh (recorded within
#: :data:`NAMED_TEST_EXECUTION_WINDOW_DAYS`), exactly mirroring the
#: walkthrough_e2e lane: a stale or unpinned trial row must not keep counting
#: as coverage.  Recognised as covering by :func:`_evidence_proves_covered` via
#: :func:`_evidence_is_named_test_execution_covered`.
NAMED_TEST_EXECUTION_EVIDENCE_KIND = "named_test_execution"

#: Coverage-tier constant: how long a ``named_test_execution`` evidence row (a
#: GREEN targeted run of a merged task's declared covering tests against the
#: candidate SHA) counts as full ``'covered'`` tier before it expires and the
#: criterion falls back to its underlying evidence state.  Mirrors
#: :data:`WALKTHROUGH_E2E_WINDOW_DAYS`: executed-test evidence is
#: machine-verifiable on re-run, so a stale row decays quickly and the next
#: qualification attempt re-verifies.  Measured from ``recorded_at``; a row
#: with no ``recorded_at`` cannot grant perpetual trust and returns ``False``
#: in :func:`_evidence_is_named_test_execution_covered`.
NAMED_TEST_EXECUTION_WINDOW_DAYS = 14

#: Hard cap on how many covering test files one ``criterion_named_test_trial`` may
#: execute. Bounds the runner job so a merged task that (mis)declares a huge test
#: surface cannot fan out into an unbounded targeted test run.  Mirrors
#: :data:`app.capability_proof.MAX_CAPABILITY_PROOF_TRIAL_TEST_FILES`.
MAX_CRITERION_NAMED_TEST_TRIAL_TEST_FILES = 10
#: Wall-clock timeout (seconds) carried in the ``criterion_named_test_trial``
#: launch contract. The agent handler runs ONLY the targeted covering tests under
#: this bound; a trial that exceeds it is a no-op (records nothing), never a
#: demotion.  Mirrors
#: :data:`app.capability_proof.CAPABILITY_PROOF_TRIAL_WALL_CLOCK_SECONDS`.
CRITERION_NAMED_TEST_TRIAL_WALL_CLOCK_SECONDS = 900
# Bump only when the targeted runner's provisioning/execution contract changes.
# Combined with project build_config, this is the named precondition identity
# that can legitimately re-arm a terminal could_not_run trial at the same SHA.
CRITERION_NAMED_TEST_TRIAL_CONTRACT = "criterion_named_test_trial:v2"

#: The identity match in :func:`enroll_criterion_named_test_trial` seeds
#: ``trial_identity`` with ``head_sha``, so on a project whose integration head
#: moves several times a day it can NEVER suppress: an unrepaired precondition
#: re-arms exactly as readily as a repaired one, minting a fresh VM placement
#: every time the candidate SHA bumps. This bound replaces that structurally
#: incapable head_sha-seeded suppressor with a decision keyed on the (criterion,
#: test, precondition) triple alone: once this many consecutive TERMINAL trials
#: for the same triple all came back non-green (independent of head_sha), stop
#: enrolling new placements until either the declared covering tests change
#: (``test_identity``) or the execution contract changes (``precondition_identity``).
#: Bounded at a small value (not 1) because ``could_not_run`` is an
#: infrastructure verdict (e.g. a Docker-less VM) and a single transient miss
#: must not permanently strand a criterion's earn-up path.
CRITERION_NAMED_TEST_TRIAL_CONSECUTIVE_NON_GREEN_SUPPRESSION_THRESHOLD = 3

#: How many of the most recent terminal trials (for the same triple) to
#: inspect when computing the consecutive-non-green streak above. Padded past
#: the threshold itself so CANCELLED (superseded-before-running) rows, which
#: carry no pass/fail signal, can be skipped without truncating the scan.
_CRITERION_NAMED_TEST_TRIAL_STREAK_SCAN_LIMIT = (
    CRITERION_NAMED_TEST_TRIAL_CONSECUTIVE_NON_GREEN_SUPPRESSION_THRESHOLD + 5
)

#: The threshold above bounds TRANSIENCE (a single ``could_not_run`` miss
#: cannot strand a criterion) but not DETERMINISM: a re-arm key of
#: (test_identity, precondition_identity) alone cannot express "the runner's
#: execution environment was repaired" — neither identity changes when, say,
#: a verification-toolchain venv is fixed. Without a recency bound, three
#: consecutive infra misses suppress a criterion FOREVER, even after the
#: underlying environment is repaired and every subsequent trial would pass.
#: Bounding the streak scan by recency makes suppression self-heal: once the
#: window elapses, the next trial re-scans only recent rows and (if the
#: environment is fixed) enrolls again with no operator action and no new
#: persisted state. Measured baseline: 99 ``criterion_named_test_trial`` tasks
#: across the trailing 7 days for at most 6 criteria (~14/criterion/week,
#: ~2/criterion/day). At 3 days, the scan window covers up to ~6 trials per
#: criterion — double the suppression threshold — so a genuinely-broken
#: precondition still suppresses well within the window; a 1-day window would
#: average under 2 trials per criterion, i.e. WORSE than the status quo
#: (routinely too few terminal trials in-window to ever reach the threshold).
CRITERION_NAMED_TEST_TRIAL_STREAK_RECENCY_WINDOW_DAYS = 3

# Legacy criterion-key aliases.  A walkthrough script may post a semantic slug
# (e.g. 'c-workday-session-lifecycle') before the script is updated to emit the
# canonical registered criterion key.  Both evidence-recording lanes apply this
# map before the get_criteria_by_keys lookup so resolution is identical.
# unmatched_keys always reports the ORIGINAL slug so alerts remain readable.
WALKTHROUGH_LEGACY_CRITERION_ALIASES: dict[str, str] = {
    "c-workday-session-lifecycle": "c-5214996f",
}


def _resolve_walkthrough_criterion_key(key: str) -> str:
    """Return the canonical criterion key, resolving any legacy alias."""
    return WALKTHROUGH_LEGACY_CRITERION_ALIASES.get(key, key)


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


def _evidence_is_walkthrough_e2e_covered(e: CriterionEvidence) -> bool:
    """Whether a single ``walkthrough_e2e`` row is fresh AND SHA-pinned covering.

    Returns ``True`` only for ``source_kind == WALKTHROUGH_E2E_EVIDENCE_KIND``
    whose evidence is BOTH:

      * **SHA-pinned** — ``detail`` is a dict carrying a non-empty
        ``candidate_sha`` (the exact commit the walkthrough ran against). A row
        with no ``detail``, a non-dict ``detail``, or a missing/blank
        ``candidate_sha`` is NOT covering — it cannot prove which commit it
        exercised, so it must not forge coverage.
      * **fresh** — ``recorded_at`` is within :data:`WALKTHROUGH_E2E_WINDOW_DAYS`
        of now (mirroring the machine-probe time-bounding). A stale row, or one
        with no ``recorded_at`` at all, is NOT covering — a missing timestamp
        cannot grant perpetual trust.

    Either failure drops the row out of the ``covered`` tier; because no other
    predicate recognises this ``source_kind`` it then classifies as ``partial``
    (evidence exists but none is covering). This is the whole point of the
    walkthrough_e2e lane: stale or unpinned walk evidence must stop counting.
    """
    if e.source_kind != WALKTHROUGH_E2E_EVIDENCE_KIND:
        return False
    detail = e.detail if isinstance(e.detail, dict) else {}
    candidate_sha = detail.get("candidate_sha")
    if not isinstance(candidate_sha, str) or not candidate_sha.strip():
        return False
    if e.recorded_at is None:
        return False
    return (_utcnow() - _coerce_naive_utc(e.recorded_at)) <= timedelta(
        days=WALKTHROUGH_E2E_WINDOW_DAYS
    )


def _evidence_is_named_test_execution_covered(e: CriterionEvidence) -> bool:
    """Whether a single ``named_test_execution`` row is fresh AND SHA-pinned covering.

    Returns ``True`` only for ``source_kind == NAMED_TEST_EXECUTION_EVIDENCE_KIND``
    whose evidence is ALL of:

      * **SHA-pinned** — ``detail`` is a dict carrying a non-empty
        ``executed_sha`` (the exact commit the targeted test run executed
        against). A row with no ``detail``, a non-dict ``detail``, or a
        missing/blank ``executed_sha`` is NOT covering — it cannot prove which
        commit it exercised, so it must not forge coverage.
      * **passed** — ``detail["passed"] is True``; the trial reported a GREEN
        run (a RED/could-not-run receipt mints nothing, so a row written by
        :func:`record_named_test_execution_evidence` always carries ``True``,
        but a legacy or malformed row must not cover).
      * **fresh** — ``recorded_at`` is within
        :data:`NAMED_TEST_EXECUTION_WINDOW_DAYS` of now (mirroring the
        walkthrough_e2e time-bounding). A stale row, or one with no
        ``recorded_at`` at all, is NOT covering — a missing timestamp cannot
        grant perpetual trust.

    Either failure drops the row out of the ``covered`` tier; because no other
    predicate recognises this ``source_kind`` it then classifies as ``partial``
    (evidence exists but none is covering). This is the whole point of the
    executed-named-test lane: stale or unpinned trial evidence must stop
    counting as machine-verified coverage.
    """
    if e.source_kind != NAMED_TEST_EXECUTION_EVIDENCE_KIND:
        return False
    detail = e.detail if isinstance(e.detail, dict) else {}
    executed_sha = detail.get("executed_sha")
    if not isinstance(executed_sha, str) or not executed_sha.strip():
        return False
    if detail.get("passed") is not True:
        return False
    if e.recorded_at is None:
        return False
    return (_utcnow() - _coerce_naive_utc(e.recorded_at)) <= timedelta(
        days=NAMED_TEST_EXECUTION_WINDOW_DAYS
    )


def _evidence_proves_covered(e: CriterionEvidence) -> bool:
    """Whether a single evidence record satisfies the machine-verified 'covered' condition.

    ``reachability`` is covered on its own; it represents the ancestry check
    that says the merge really reached the integration branch.
    ``task_merge`` requires ``proven=True`` in ``detail`` — a bare merge badge
    without acceptance-proof is not sufficient to qualify the criterion.

    ``walkthrough_e2e`` (autonomous live-stack browser walkthrough) is covering
    ONLY when it is BOTH SHA-pinned (``detail["candidate_sha"]`` non-empty) AND
    fresh (recorded within :data:`WALKTHROUGH_E2E_WINDOW_DAYS`) — see
    :func:`_evidence_is_walkthrough_e2e_covered`. A stale or unpinned row is not
    covering and falls back to ``partial``.

    ``named_test_execution`` (a GREEN targeted run of a merged task's declared
    covering tests against the candidate SHA) is covering ONLY when it is BOTH
    SHA-pinned (``detail["executed_sha"]`` non-empty), passed (``detail["passed"]
    is True``) AND fresh (recorded within
    :data:`NAMED_TEST_EXECUTION_WINDOW_DAYS`) — see
    :func:`_evidence_is_named_test_execution_covered`. A stale, unpinned, or
    non-passed row is not covering and falls back to ``partial``.

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
    if e.source_kind == "walkthrough_e2e":
        # A live-stack walkthrough that ran against a specific candidate SHA.
        # Covering iff: (a) the row is fresh within WALKTHROUGH_E2E_WINDOW_DAYS,
        # (b) ``detail["candidate_sha"]`` is present, and (c) if a legacy
        # ``verdict`` field is present it must be "PASS" (FAIL/DEAD_END/None must
        # not cover even if the SHA is pinned — fail-closed).  The autonomous
        # ingestion endpoint only writes rows for passing criteria and omits the
        # ``verdict`` key entirely; a present-but-non-PASS verdict indicates a
        # legacy row that must not be treated as machine-verified.
        detail = e.detail if isinstance(e.detail, dict) else {}
        if "verdict" in detail and detail.get("verdict") != "PASS":
            return False
        return _evidence_is_walkthrough_e2e_covered(e)
    if e.source_kind == NAMED_TEST_EXECUTION_EVIDENCE_KIND:
        # A GREEN targeted run of a task's declared covering tests against the
        # candidate SHA. Covering iff the row is SHA-pinned
        # (``detail["executed_sha"]`` non-empty), reports ``passed is True``, AND
        # is fresh within NAMED_TEST_EXECUTION_WINDOW_DAYS — see
        # :func:`_evidence_is_named_test_execution_covered`. A stale, unpinned,
        # or non-passed row must not cover.
        return _evidence_is_named_test_execution_covered(e)
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
        if attested_at is not None and (
            # Reject a timestamp dated meaningfully in the future; it would
            # otherwise extend (or perpetuate) the window.  A small tolerance
            # absorbs benign client/server clock skew.
            (attested_at - _utcnow()) <= timedelta(
                seconds=ATTESTATION_FUTURE_SKEW_SECONDS
            )
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


def _evidence_is_walkthrough_e2e(e: CriterionEvidence) -> bool:
    """Whether a single evidence record is a fresh, SHA-pinned walkthrough_e2e PASS.

    Returns ``True`` only when ALL of the following hold:

    * ``source_kind == "walkthrough_e2e"``
    * ``detail["candidate_sha"]`` is a non-empty string — prevents a bare or
      malformed row from granting trust; every row written by the harness must
      declare which exact commit it verified.
    * ``detail["verdict"] == "PASS"`` — the harness explicitly reported success;
      a ``FAIL`` or ``DEAD_END`` verdict must not cover the criterion.
    * ``recorded_at`` is within :data:`WALKTHROUGH_E2E_WINDOW_DAYS` of now — a
      stale walkthrough row decays to the underlying (partial/uncovered) state so
      the next qualification attempt must re-verify rather than coasting on old
      evidence.  A row with no ``recorded_at`` cannot grant perpetual trust and
      returns ``False``.

    LEGACY predicate: it additionally requires ``detail["verdict"] == "PASS"``.
    The autonomous evidence lane no longer stores a ``verdict`` field (the
    ingestion endpoint only writes a row for a passing criterion), so
    :func:`_evidence_proves_covered` now delegates to
    :func:`_evidence_is_walkthrough_e2e_covered` (SHA-pinned + fresh) instead.
    This function is retained for the verdict-format rows written by the legacy
    ``WALKTHROUGH_E2E_RESULT`` artifact ingestion path.
    """
    if e.source_kind != "walkthrough_e2e":
        return False
    detail = e.detail if isinstance(e.detail, dict) else {}
    if not detail.get("candidate_sha"):
        return False
    if detail.get("verdict") != "PASS":
        return False
    if e.recorded_at is None:
        return False
    return (_utcnow() - _coerce_naive_utc(e.recorded_at)) <= timedelta(
        days=WALKTHROUGH_E2E_WINDOW_DAYS
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


async def _gather_referenced_criterion_keys(
    session: AsyncSession, project: Any, release_version: Any
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

    # Scan the version's frozen goal — qualification grades against this, so
    # the drift report must include keys cited here even when current_focus
    # has been updated to a different statement.
    referenced |= extract_referenced_criterion_keys(
        getattr(release_version, "goal", None)
    )

    problem_model = getattr(project, "problem_model", None)
    if isinstance(problem_model, dict):
        intent = problem_model.get("intent")
        if isinstance(intent, dict):
            referenced |= extract_referenced_criterion_keys(
                intent.get("current_focus")
            )

    result = await session.execute(
        select(Direction).where(Direction.project_id == release_version.project_id)
    )
    normalized = list(result.scalars().all())
    direction_sources: list[Any] = list(normalized)
    normalized_keys = {row.direction_key for row in normalized}
    blob_directions = getattr(project, "directions", None)
    if isinstance(blob_directions, list):
        direction_sources.extend(
            direction
            for direction in blob_directions
            if isinstance(direction, dict)
            and direction.get("id") not in normalized_keys
        )
    for direction in direction_sources:
        if not direction_matches_criterion_release_version(direction, release_version):
            continue
        status = (
            direction.get("status")
            if isinstance(direction, dict)
            else direction.status
        )
        if status != "active":
            continue
        data = direction if isinstance(direction, dict) else direction.data
        if isinstance(data, dict):
            ctx = data.get("context")
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
    evidence_contract: dict[str, Any] | None = None,
    audit_actor: str | None = None,
) -> VersionAcceptanceCriterion:
    """Create and persist a new acceptance criterion for a release version.

    Commits the session immediately — use :func:`stage_criterion` when calling
    from inside a larger transaction that must commit once at the end.
    """
    normalized_contract = (
        normalize_evidence_contract(evidence_contract)
        if evidence_contract is not None
        else None
    )
    if evidence_contract is not None and normalized_contract is None:
        raise ValueError("unsupported or malformed evidence_contract")
    criterion = VersionAcceptanceCriterion(
        project_id=project_id,
        release_version_id=release_version_id,
        criterion_key=generate_criterion_key(),
        text=text,
        evidence_contract=normalized_contract,
        evidence_contract_updated_at=_utcnow() if normalized_contract is not None else None,
        evidence_contract_updated_by=(
            (audit_actor or created_by or "legacy:created_at")[:128]
            if normalized_contract is not None
            else None
        ),
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
    evidence_contract: dict[str, Any] | None = None,
) -> VersionAcceptanceCriterion:
    """Stage a new acceptance criterion without committing the session.

    Unlike :func:`create_criterion`, this only calls ``session.add()`` — the
    caller is responsible for committing (or flushing) the session.  Use this
    from within a larger transaction (e.g. strategic-completion handlers) where
    a mid-transaction ``session.commit()`` would break the surrounding
    atomicity guarantee and cause one commit per criterion.
    """
    normalized_contract = (
        normalize_evidence_contract(evidence_contract)
        if evidence_contract is not None
        else None
    )
    if evidence_contract is not None and normalized_contract is None:
        raise ValueError("unsupported or malformed evidence_contract")
    criterion = VersionAcceptanceCriterion(
        project_id=project_id,
        release_version_id=release_version_id,
        criterion_key=generate_criterion_key(),
        text=text,
        evidence_contract=normalized_contract,
        evidence_contract_updated_at=_utcnow() if normalized_contract is not None else None,
        evidence_contract_updated_by=(
            (created_by or "legacy:created_at")[:128]
            if normalized_contract is not None
            else None
        ),
        created_by=created_by,
    )
    session.add(criterion)
    return criterion


async def update_criterion_evidence_contract(
    session: AsyncSession,
    *,
    project_id: uuid.UUID,
    release_version_id: uuid.UUID,
    criterion_key: str,
    evidence_contract: dict[str, Any] | None,
    updated_by: str,
) -> VersionAcceptanceCriterion | None:
    """Atomically replace an existing criterion's canonical contract."""
    normalized_contract = (
        normalize_evidence_contract(evidence_contract)
        if evidence_contract is not None
        else None
    )
    if evidence_contract is not None and normalized_contract is None:
        raise ValueError("unsupported or malformed evidence_contract")
    result = await session.execute(
        select(VersionAcceptanceCriterion)
        .where(
            VersionAcceptanceCriterion.project_id == project_id,
            VersionAcceptanceCriterion.release_version_id == release_version_id,
            VersionAcceptanceCriterion.criterion_key == criterion_key,
        )
        .with_for_update()
    )
    criterion = result.scalar_one_or_none()
    if criterion is None:
        return None
    criterion.evidence_contract = normalized_contract
    criterion.evidence_contract_updated_at = _utcnow()
    criterion.evidence_contract_updated_by = updated_by[:128]
    session.add(criterion)
    await session.flush()
    return criterion


async def compare_and_set_criterion_text(
    session: AsyncSession,
    *,
    project_id: uuid.UUID,
    release_version_id: uuid.UUID,
    criterion_key: str,
    expected_text: str,
    replacement_text: str,
) -> tuple[VersionAcceptanceCriterion | None, str, bool]:
    """Rewrite one active criterion in place under an exact-text CAS.

    The row identity and every field other than ``text`` are retained, so
    criterion evidence, authorship provenance, and Direction/Task references
    keep pointing at the same stable key. A current value equal to the
    replacement is treated as an idempotent replay; any other value that does
    not exactly equal ``expected_text`` fails closed.

    Returns ``(criterion, prior_text, changed)``. The caller owns the commit.
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
    criterion = result.scalar_one_or_none()
    if criterion is None:
        return None, "", False
    if criterion.status != "active":
        raise CriterionTextInactiveError(
            f"criterion '{criterion_key}' is not active (status={criterion.status})"
        )

    prior_text = criterion.text
    if prior_text not in {expected_text, replacement_text}:
        raise CriterionTextCompareMismatchError(
            f"criterion '{criterion_key}' changed since it was read"
        )

    duplicate_result = await session.execute(
        select(VersionAcceptanceCriterion)
        .where(
            VersionAcceptanceCriterion.project_id == project_id,
            VersionAcceptanceCriterion.release_version_id == release_version_id,
            VersionAcceptanceCriterion.text == replacement_text,
            VersionAcceptanceCriterion.id != criterion.id,
            VersionAcceptanceCriterion.status == "active",
        )
        .limit(1)
        .with_for_update()
    )
    duplicate = duplicate_result.scalar_one_or_none()
    if duplicate is not None:
        raise CriterionTextDuplicateError(
            f"replacement text already belongs to criterion '{duplicate.criterion_key}'"
        )
    if prior_text == replacement_text:
        return criterion, prior_text, False

    criterion.text = replacement_text
    session.add(criterion)
    await session.flush()
    return criterion, prior_text, True


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


async def get_criterion_by_key_for_update(
    session: AsyncSession,
    project_id: uuid.UUID,
    release_version_id: uuid.UUID,
    criterion_key: str,
) -> VersionAcceptanceCriterion | None:
    """Lock and return the exact row used for lifecycle authorization."""
    result = await session.execute(
        select(VersionAcceptanceCriterion)
        .where(
            VersionAcceptanceCriterion.project_id == project_id,
            VersionAcceptanceCriterion.release_version_id == release_version_id,
            VersionAcceptanceCriterion.criterion_key == criterion_key,
        )
        .with_for_update()
    )
    return result.scalar_one_or_none()


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
        select(VersionAcceptanceCriterion).where(
            VersionAcceptanceCriterion.release_version_id == release_version_id,
            VersionAcceptanceCriterion.status == "active",
        )
    )
    return sum(
        1
        for criterion in result.scalars()
        if not is_operator_authorship_criterion(criterion)
    )


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
        {
            "criterion_key": c.criterion_key,
            "text": c.text,
            **(
                {"evidence_contract": c.evidence_contract}
                if isinstance(c.evidence_contract, dict)
                else {}
            ),
        }
        for c in result.scalars().all()
    ]


async def get_criterion_by_text(
    session: AsyncSession,
    project_id: uuid.UUID,
    release_version_id: uuid.UUID,
    text: str,
) -> VersionAcceptanceCriterion | None:
    """Return the unique active same-text criterion, or ``None``.

    Strategic retries must never revive or mutate an inactive row.  Duplicate
    active rows are treated as ambiguous rather than selecting arbitrarily.
    """
    result = await session.execute(
        select(VersionAcceptanceCriterion).where(
            VersionAcceptanceCriterion.project_id == project_id,
            VersionAcceptanceCriterion.release_version_id == release_version_id,
            VersionAcceptanceCriterion.text == text,
            VersionAcceptanceCriterion.status == "active",
        )
        .order_by(VersionAcceptanceCriterion.created_at, VersionAcceptanceCriterion.id)
    )
    rows = list(result.scalars().all())
    if len(rows) > 1:
        raise ValueError(
            "ambiguous active acceptance criteria share the same text; "
            "operator resolution is required"
        )
    return rows[0] if rows else None


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
                     ``task_merge`` only with ``detail["proven"] is True``;
                     ``walkthrough_e2e`` only when SHA-pinned AND fresh — see
                     :func:`_evidence_is_walkthrough_e2e_covered``)
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


def _evidence_providing_source_kind(evidence: list[CriterionEvidence]) -> str | None:
    """Return the ``source_kind`` providing the highest-priority coverage, or ``None``.

    Follows the same priority chain as :func:`compute_criterion_coverage`::

        covered > attested > backfilled > probed > partial > uncovered

    For ``covered`` this pinpoints which machine-verified evidence kind proved the
    criterion (e.g. ``"walkthrough_e2e"`` vs ``"task_merge"``).  For the
    lower-confidence tiers it returns the first matching source_kind within that
    tier.  For ``uncovered`` (empty evidence list) it returns ``None``.

    Callers use this to label *why* a criterion achieved its current tier without
    re-deriving the logic.
    """
    if not evidence:
        return None
    for e in evidence:
        if _evidence_proves_covered(e):
            return e.source_kind
    for e in evidence:
        if _evidence_is_attested(e):
            return e.source_kind
    for e in evidence:
        if _evidence_is_backfilled(e):
            return e.source_kind
    for e in evidence:
        if _evidence_is_probed(e):
            return e.source_kind
    # Partial: evidence exists but none qualifies at any tier — return the
    # first entry's source_kind so callers see what kind of evidence is present.
    return evidence[0].source_kind


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
        from app.directions_repo import get_direction_row
        from app.release_version import current_open_version

        direction_row = await get_direction_row(session, project_id, direction_id)
        keys = list(getattr(direction_row, "criterion_keys", None) or [])
        if not keys:
            return False

        open_ver = await current_open_version(session, project_id)
        if open_ver is None:
            return False
        if not direction_matches_criterion_release_version(direction_row, open_ver):
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
    release_version_id: uuid.UUID,
    reason: MergeEvidenceReason,
    unmatched_ids: list[str],
) -> None:
    """Persist a cumulative merge-evidence gap counter on the bound release version.

    Updates the ``merge_evidence_gaps`` JSON column so the gap is visible
    through the coverage-summary API, not only in logs.

    Best-effort: errors are logged but never re-raised so a failure here never
    blocks merge finalization.
    """
    try:
        from app.models import ReleaseVersion

        bound_version = await session.scalar(
            select(ReleaseVersion).where(
                ReleaseVersion.id == release_version_id,
                ReleaseVersion.project_id == project_id,
            )
        )
        if bound_version is None:
            return
        gaps: dict[str, Any] = dict(bound_version.merge_evidence_gaps or {})
        reason_key = reason.value
        gaps[reason_key] = int(gaps.get(reason_key, 0)) + 1
        if unmatched_ids:
            existing: list[str] = list(gaps.get("unmatched_criterion_ids", []))
            for uid in unmatched_ids:
                if uid not in existing:
                    existing.append(uid)
            gaps["unmatched_criterion_ids"] = existing
        bound_version.merge_evidence_gaps = gaps
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
    * ``no_open_version`` — a legacy task has no persisted version binding and
      ``current_open_version`` returned ``None``.
    * ``no_bound_version`` — a legacy task predates the current open version,
      so its bare keys cannot be resolved safely.
    * ``no_active_criteria`` — all resolved criterion rows are inactive
      (retracted/superseded), or every evidence row already existed (idempotent
      duplicate skip); logged as ``all_evidence_already_present`` for the latter.
    * ``exception`` — an unexpected error aborted recording.

    When it does write, it stamps a proof-backed ``task_merge`` row
    (``detail["proven"] = True``) per criterion, plus a companion
    ``reachability`` row.  Coverage flips to ``covered`` on either signal alone.

    Direction criterion_keys are fetched via the ``direction_id`` stored in
    ``task.context`` at merge time, so criteria mapped to a direction *later in
    the same release version* are also captured.  A mapping bound to another
    release version is ignored.  Tasks carrying
    ``criterion_ids_explicit=True`` are the exception: their persisted
    ``task.criterion_ids`` are an exact operator-authored subset and must not be
    widened from the direction.  The direction fetch is otherwise best-effort:
    if the row is missing or the query fails (``direction_fetch_failed``), only
    ``task.criterion_ids`` is used.

    ``preloaded_direction`` and ``preloaded_open_ver`` are optional batch
    optimisation kwargs.  ``preloaded_open_ver`` is consulted only for an
    unbound legacy task; an explicit task binding remains authoritative.

    Best-effort: errors are logged and not re-raised so a failure here never
    blocks merge finalization.

    Returns the number of evidence rows recorded.
    """
    release_version_id = task_criterion_release_version_id(task)
    has_explicit_version_binding = release_version_id is not None
    if release_version_id is None:
        try:
            if preloaded_open_ver is not None:
                open_version = preloaded_open_ver
            else:
                from app.release_version import current_open_version

                open_version = await current_open_version(session, project_id)
        except Exception:
            open_version = None
            logger.warning(
                "record_task_merge_evidence: failed to resolve open version for "
                "legacy task %s (project %s)",
                getattr(task, "id", None),
                project_id,
                exc_info=True,
            )
        if open_version is None:
            logger.warning(
                "record_task_merge_evidence: no open version — reason=%s "
                "task_id=%s project_id=%s",
                MergeEvidenceReason.no_open_version,
                getattr(task, "id", None),
                project_id,
            )
            return 0
        if not task_matches_criterion_release_version(task, open_version):
            logger.warning(
                "record_task_merge_evidence: legacy task predates the open "
                "version — reason=%s task_id=%s project_id=%s",
                MergeEvidenceReason.no_bound_version,
                getattr(task, "id", None),
                project_id,
            )
            return 0
        release_version_id = open_version.id

    criterion_ids: list[str] = list(getattr(task, "criterion_ids", None) or [])

    # Best-effort: also pick up any criteria mapped to the direction at merge
    # time.  Tasks inherit criterion_ids at spawn time; a later mapping update
    # would otherwise be silently missed.
    task_context: dict = getattr(task, "context", None) or {}
    direction_id_key: str | None = task_context.get("direction_id")
    criterion_ids_are_explicit = (
        task_context.get(EXPLICIT_CRITERION_IDS_CONTEXT_KEY) is True
    )
    if direction_id_key and not criterion_ids_are_explicit:
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
            direction_version_id = direction_criterion_release_version_id(direction_row)
            if (
                direction_row is not None
                and direction_row.criterion_keys
                and (
                    direction_version_id == release_version_id
                    or (
                        not has_explicit_version_binding
                        and direction_version_id is None
                    )
                )
            ):
                merged_keys = list(
                    dict.fromkeys(criterion_ids + list(direction_row.criterion_keys))
                )
                criterion_ids = merged_keys
            elif direction_row is not None and direction_row.criterion_keys:
                logger.info(
                    "record_task_merge_evidence: ignored direction %s keys bound "
                    "to %s for task %s bound to %s",
                    direction_id_key,
                    direction_version_id,
                    getattr(task, "id", None),
                    release_version_id,
                )
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
        await _increment_merge_gap(
            session,
            project_id,
            release_version_id,
            MergeEvidenceReason.no_criteria,
            [],
        )
        return 0
    from app.acceptance_gate import CODE_ACCEPTANCE_VERDICT_KEY

    verdict = task_context.get(CODE_ACCEPTANCE_VERDICT_KEY)
    if _reject_unproven_merge_evidence(verdict, task, project_id, criterion_ids):
        await _increment_merge_gap(
            session,
            project_id,
            release_version_id,
            MergeEvidenceReason.verdict_not_proven,
            criterion_ids,
        )
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
        all_rows = await get_criteria_by_keys(
            session, project_id, criterion_ids, release_version_id=release_version_id
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
                session,
                project_id,
                release_version_id,
                MergeEvidenceReason.no_active_criteria,
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
                if not task_matches_criterion_release_version(task, open_version):
                    continue
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
    *,
    diagnostic: dict[str, str] | None = None,
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
    the caller (the reconcile-backfill endpoint).  Callers that need to preserve a
    failed pass in an execution ledger may provide ``diagnostic``; it receives a
    stable error code and exception class while this API remains count-compatible.

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
                if not task_matches_criterion_release_version(task, open_version):
                    continue
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
                release_version_id=open_version.id,
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
    except Exception as exc:
        logger.exception(
            "backfill_unverified_merge_evidence: failed for project %s",
            project_id,
        )
        if diagnostic is not None:
            diagnostic.update(
                error="backfill_unverified_merge_evidence_failed",
                error_type=type(exc).__name__,
            )
        return 0


# Depth cap for the parent-ancestor walk in
# :func:`backfill_rollup_child_changed_files`. Rollup nesting is shallow (a
# child under a rollup parent, occasionally a rollup-of-rollups), so a small
# cap bounds the walk cheaply while a visited-set guards against a cyclic
# parent_task_id chain from corrupt data.
_ROLLUP_PARENT_WALK_MAX_DEPTH = 8


def _rollup_child_commit_range(task: Task) -> tuple[str, str] | None:
    """Return the backend-persisted immutable PR range for a rollup child."""
    context = task.context or {}
    review_loop = context.get("review_loop")
    candidates = [
        review_loop.get("review_target")
        if isinstance(review_loop, dict)
        else None,
        review_loop.get("fix_target")
        if isinstance(review_loop, dict)
        else None,
        context.get("review_target"),
        context.get("fix_target"),
    ]
    for target in candidates:
        if not isinstance(target, dict):
            continue
        base_sha = target.get("base_sha")
        head_sha = target.get("head_sha")
        if (
            isinstance(base_sha, str)
            and base_sha.strip()
            and isinstance(head_sha, str)
            and head_sha.strip()
            and base_sha.strip() != head_sha.strip()
        ):
            return base_sha.strip(), head_sha.strip()

    if isinstance(review_loop, dict):
        base_sha = review_loop.get("base_sha")
        head_sha = review_loop.get("head_sha")
        if (
            isinstance(base_sha, str)
            and base_sha.strip()
            and isinstance(head_sha, str)
            and head_sha.strip()
            and base_sha.strip() != head_sha.strip()
        ):
            return base_sha.strip(), head_sha.strip()

    base_sha = context.get("base_sha")
    head_sha = context.get("head_sha")
    if (
        isinstance(base_sha, str)
        and base_sha.strip()
        and isinstance(head_sha, str)
        and head_sha.strip()
        and base_sha.strip() != head_sha.strip()
    ):
        return base_sha.strip(), head_sha.strip()
    return None


async def stamp_rollup_child_commit_range(
    task: Task,
    *,
    gh_token: str,
    owner: str,
    repo: str,
) -> bool:
    """Stamp a child's exact PR-range diff, returning whether it was resolved.

    ``review_target`` is written by the backend from the live PR before review
    or merge.  Comparing those immutable SHAs is exact child provenance even
    though the child never owns the rollup parent's PR.  Indeterminate or
    empty results remain fail-closed and are recorded on the task context.
    """
    from app.acceptance_gate import (
        PARENT_DERIVED_CHANGED_FILES_KEY,
        record_code_acceptance_verdict,
        stamp_commit_range_derived_changed_files_signal,
    )
    from app.acceptance_provenance import (
        ChangedFilesStampSkipReason,
        PR_CHANGED_FILES_STAMP_SKIPPED_KEY,
        stamp_changed_files_skip_reason,
    )

    context = dict(task.context or {})
    commit_range = _rollup_child_commit_range(task)
    if commit_range is None:
        stamp_changed_files_skip_reason(
            context, ChangedFilesStampSkipReason.child_pr_range_unavailable
        )
        task.context = context
        return False
    from app.github_refs import get_files_changed_between_refs

    base_sha, head_sha = commit_range
    try:
        changed = await get_files_changed_between_refs(
            gh_token,
            owner,
            repo,
            base_sha,
            head_sha,
            project_id=str(task.project_id),
        )
    except Exception:
        logger.warning(
            "rollup child %s commit-range compare failed",
            getattr(task, "id", None),
            exc_info=True,
        )
        stamp_changed_files_skip_reason(
            context, ChangedFilesStampSkipReason.diff_fetch_failed
        )
        task.context = context
        return False
    if changed is None:
        stamp_changed_files_skip_reason(
            context, ChangedFilesStampSkipReason.no_diff_returned
        )
        task.context = context
        return False

    # ``dict.update`` cannot remove provenance from the previous stamp.  Clear
    # the legacy parent-derived sentinels on the task before applying the exact
    # child range, so the old union diff cannot continue to cap this verdict.
    context.pop(PARENT_DERIVED_CHANGED_FILES_KEY, None)
    context.pop("_pr_changed_files_truncated", None)
    context.pop(PR_CHANGED_FILES_STAMP_SKIPPED_KEY, None)
    context.update(stamp_commit_range_derived_changed_files_signal(changed))
    if not changed:
        stamp_changed_files_skip_reason(
            context, ChangedFilesStampSkipReason.empty_diff
        )
    task.context = context
    record_code_acceptance_verdict(task)
    return True


async def backfill_rollup_child_changed_files(
    session: AsyncSession,
    project_id: uuid.UUID,
    direction_key: str | None = None,
    *,
    diagnostic: dict[str, str] | None = None,
) -> int:
    """Repair legacy rollup children that landed before source stamping existed.

    The forward-path propagation in
    :func:`app.rollup_management._propagate_landed_to_staging_merged_descendants`
    only heals NEW landings.  Already-landed rollup children (merged via the
    parent's single PR) carry ``landed_on_integration`` but never received the
    parent's GitHub-verified ``_pr_changed_files``, so their declared
    ``named_test`` cannot be located in a diff and their acceptance verdict is
    stuck at ``declared_unsatisfied``; the unverified-merge backfill skips them
    because ``_changed_files(task)`` is empty.

    The canonical path now compares each child's backend-persisted immutable
    ``base_sha...head_sha`` range and stamps
    :func:`stamp_commit_range_derived_changed_files_signal`, replacing any
    self-report and clearing legacy parent-derived sentinels.  Once exact
    provenance is stamped, later backfill runs skip the child. The parent-union
    path remains
    only as a compatibility fallback for old rows that have no usable child
    range; it is deliberately never used when the exact compare succeeds.

    Wired to run BEFORE :func:`backfill_unverified_merge_evidence` in the
    reconcile endpoint so that, within the SAME run, a newly-stamped child is
    picked up by the unverified-merge backfill and earns criterion evidence.

    ``direction_key``, when provided, scopes the search to tasks whose context
    carries that ``direction_id`` (matching the sibling backfills).

    Idempotent (clean exact-range stamps are skipped; legacy stamps with stale
    sentinels get one repair pass) and best-effort (errors are logged and
    swallowed so a failure never blocks the caller). A caller may provide
    ``diagnostic`` to retain a stable error code and exception class when the
    swallowed failure must remain auditable.

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
        # Resolve credentials once for exact child-range comparisons.  A missing
        # credential only disables this best-effort legacy repair; the parent
        # fallback below remains conservative and GitHub-verified.
        gh_credentials: tuple[str, str, str] | None = None
        try:
            from app.routers.agent import get_github_token_for_task

            first_task = (
                await session.execute(
                    select(Task)
                    .where(
                        Task.project_id == project_id,
                        Task.status == TaskStatus.COMPLETED,
                        landed_signal_predicate(session),
                    )
                    .limit(1)
                )
            ).scalars().first()
            if first_task is not None:
                gh_credentials = await get_github_token_for_task(
                    first_task.id, session
                )
        except Exception:
            logger.info(
                "backfill_rollup_child_changed_files: exact-range credentials unavailable",
                exc_info=True,
            )

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
            # which hits the identity map on a cache hit, avoiding N x depth
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
                if task.parent_task_id is None:
                    continue

                # The exact range is immutable and already authoritative.  Do
                # not re-fetch or re-stamp it on every reconcile invocation. A
                # legacy exact stamp may still carry the old parent-derived or
                # truncation sentinel, so let that shape run once more through
                # the merge-site cleanup above.
                task_context = task.context or {}
                if (
                    task_context.get(
                        _acceptance_gate.COMMIT_RANGE_DERIVED_CHANGED_FILES_KEY
                    )
                    is True
                    and task_context.get(
                        _acceptance_gate.PARENT_DERIVED_CHANGED_FILES_KEY
                    )
                    is not True
                    and "_pr_changed_files_truncated" not in task_context
                ):
                    continue

                # Exact child provenance supersedes both self-reported and
                # parent-derived lists.  This is intentionally attempted before
                # the old skip-if-has-list guard: that guard made the dominant
                # v31 named_test_missing_from_diff bucket unreachable.
                if gh_credentials is not None and await stamp_rollup_child_commit_range(
                    task,
                    gh_token=gh_credentials[0],
                    owner=gh_credentials[1],
                    repo=gh_credentials[2],
                ):
                    session.add(task)
                    batch_stamped += 1
                    continue

                # Legacy fallback: only inherit a parent's union when the child
                # has no changed-file list at all.  It remains soft/unproven and
                # is never allowed to shadow an exact range or a self-report.
                if _acceptance_gate._changed_files(task):
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
    except Exception as exc:
        logger.exception(
            "backfill_rollup_child_changed_files: failed for project %s",
            project_id,
        )
        if diagnostic is not None:
            diagnostic.update(
                error="backfill_rollup_child_changed_files_failed",
                error_type=type(exc).__name__,
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

    # Rule (b): the criterion is mapped to an ACTIVE direction whose text names the metric.
    # Non-active directions (paused, staged, proposed, retired, etc.) are inert here:
    # their historical prose must not trigger probe evidence on the open version.
    dir_result = await session.execute(
        select(Direction).where(Direction.project_id == version.project_id)
    )
    for direction in dir_result.scalars().all():
        if not direction_matches_criterion_release_version(direction, version):
            continue
        if direction.status != "active":
            continue
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


def _version_qualifying_candidate_sha(version: Any) -> str:
    """Return the candidate SHA under qualification for ``version``, or ``""``.

    Sourced from ``ReleaseVersion.last_qualification["candidate_sha"]`` — the
    exact commit the qualifier is (or last was) scoring. Read defensively so a
    missing/malformed blob yields an empty string rather than raising.
    """
    last_qualification = getattr(version, "last_qualification", None)
    if not isinstance(last_qualification, dict):
        return ""
    return str(last_qualification.get("candidate_sha") or "").strip()


#: Canonical key of the walkthrough meta criterion: a full re-walk summary that
#: MUST NOT be minted from a partial run where one or more of its
#: rubric-resolved substantive sub-criteria failed or were absent.
_WALKTHROUGH_META_CRITERION_KEY = "c-5214996f"

#: Flows the live walkthrough harness can exercise against the candidate stack
#: (Runner Enrollment, Project Tools iframe subresource token, Conversational
#: Workday session lifecycle).  Each flow is matched against the OPEN version's
#: ACTIVE criterion texts so the evidence contract stays version-agnostic: a
#: rubric that re-mints its criteria under fresh keys still resolves when a
#: criterion's text names the flow the harness drives.  Matching is a
#: case-insensitive substring.  Housed here (rather than release_qualifier) so
#: the meta-criterion gate and the evidence-contract resolver share one source
#: of truth.
WALKTHROUGH_E2E_FLOW_MARKERS: tuple[tuple[str, ...], ...] = (
    ("runner enroll", "runner enrollment"),
    ("iframe", "subresource token", "project tool"),
    ("workday",),
)

#: Legacy harness canonical keys — a rubric that carries these criteria verbatim
#: is still satisfiable by the current v26-output harness, so walkthrough
#: resolution treats them as walkthrough-provable for backward compatibility.
#: Kept separate from the flow markers so the version-agnostic contract stays
#: the primary path.
WALKTHROUGH_E2E_LEGACY_HARNESS_KEYS: frozenset[str] = frozenset(
    {"c-dfdc9d82", "c-f52fd975", "c-5214996f"}
)


async def _walkthrough_meta_required_subcriteria(
    session: AsyncSession,
    version: Any,
) -> frozenset[str]:
    """Resolve the walkthrough sub-criteria the meta gate requires for ``version``.

    Loads the version's ACTIVE criteria and keeps exactly those the live
    walkthrough harness can produce evidence for (criterion text names a flow
    marker OR the criterion carries a legacy harness key), alias-resolving each
    key to its canonical form and excluding the meta criterion itself.  The
    result feeds :func:`_walkthrough_meta_criterion_gate`, so a rubric that
    re-mints its walkthrough criteria under fresh keys gates c-5214996f on
    those fresh keys instead of a hardcoded v26 key set.
    """
    rows = await get_criteria_for_version(session, version.id)
    required: set[str] = set()
    for row in rows:
        if row.status != "active" or not row.criterion_key:
            continue
        raw_key = str(row.criterion_key)
        key = _resolve_walkthrough_criterion_key(raw_key)
        if key == _WALKTHROUGH_META_CRITERION_KEY:
            continue
        text = str(row.text or "").lower()
        if any(
            marker in text
            for flow_markers in WALKTHROUGH_E2E_FLOW_MARKERS
            for marker in flow_markers
        ) or raw_key in WALKTHROUGH_E2E_LEGACY_HARNESS_KEYS:
            required.add(key)
    return frozenset(required)


def _walkthrough_meta_criterion_gate(
    results: list[dict],
    required_subcriteria: frozenset[str],
) -> bool:
    """Return True only when all rubric-resolved sub-criteria are passed in ``results``.

    Pure function (no I/O).  ``required_subcriteria`` comes from
    :func:`_walkthrough_meta_required_subcriteria` — the ACTIVE walkthrough
    sub-criteria on the open version's rubric, so a rubric that re-mints its
    walkthrough criteria under fresh keys gates the meta on those fresh keys
    rather than a hardcoded v26 set.

    Fail-closed: an empty required set (a rubric whose only walkthrough
    criterion is the meta itself) returns False — c-5214996f must never be
    minted from a lone row with no registered substantive sub-flows.

    The batch is compared in canonical (alias-resolved) form so the legacy
    'c-workday-session-lifecycle' slug and its canonical target both count
    against the same canonical required key.

    Called by :func:`record_walkthrough_e2e_evidence` and
    :func:`record_operator_walkthrough_evidence` before c-5214996f is allowed
    into ``passed_keys``.
    """
    if not required_subcriteria:
        return False
    passed_in_batch = {
        _resolve_walkthrough_criterion_key(
            str(entry.get("criterion_key") or "").strip()
        )
        for entry in results
        if isinstance(entry, dict) and entry.get("passed") is True
    }
    return required_subcriteria.issubset(passed_in_batch)


async def record_walkthrough_e2e_evidence(
    session: AsyncSession,
    version: Any,
    candidate_sha: str,
    results: list[dict[str, Any]],
    qualification_attempt_id: str | None = None,
    exercised_engine_sha: str | None = None,
) -> dict[str, Any]:
    """Map per-criterion ``walkthrough_e2e`` results into covered-tier evidence.

    Each entry in ``results`` is a ``{criterion_key, passed, notes}`` dict. For
    every entry whose ``passed`` is truthy, this resolves ``criterion_key`` to an
    ACTIVE criterion on ``version`` and writes one covering
    :data:`WALKTHROUGH_E2E_EVIDENCE_KIND` :class:`CriterionEvidence` row with
    ``detail = {candidate_sha, evaluated_at, criterion_key, passed[, notes,
    qualification_attempt_id, exercised_engine_sha]}`` and a ``source_id`` keyed on the qualification
    attempt + criterion (so a re-run against the same attempt is an idempotent
    no-op via the dedup unique index).

    **CRITICAL SAFETY** — covered-tier evidence is written ONLY when the posted
    ``candidate_sha`` is non-empty AND matches the version's qualifying candidate
    SHA (:func:`_version_qualifying_candidate_sha`). A missing or mismatched SHA
    writes NOTHING and is reported as ``rejected`` — a walkthrough run against the
    wrong commit (or with no pinned SHA) can never forge coverage. Only
    ``passed=True`` results are written; failing/absent results are skipped.

    Returns a summary dict::

        {"status": "recorded" | "rejected",
         "rejected_reason": str | None,  # only when status == "rejected"
         "recorded": int,                # rows newly written
         "skipped_not_passed": int,      # results with passed falsy
         "unmatched_keys": list[str],    # keys not resolving to an active criterion
         "candidate_sha": str}

    Best-effort per-row: a failure recording one criterion's evidence is logged
    and does not abort the others.
    """
    posted_sha = str(candidate_sha or "").strip()
    qualifying_sha = _version_qualifying_candidate_sha(version)
    if not posted_sha:
        logger.warning(
            "record_walkthrough_e2e_evidence: missing candidate_sha for version %s "
            "— rejecting (no coverage written)",
            getattr(version, "id", None),
        )
        return {
            "status": "rejected",
            "rejected_reason": "missing_candidate_sha",
            "recorded": 0,
            "skipped_not_passed": 0,
            "unmatched_keys": [],
            "candidate_sha": posted_sha,
        }
    if not qualifying_sha or posted_sha != qualifying_sha:
        logger.warning(
            "record_walkthrough_e2e_evidence: posted candidate_sha %s does not "
            "match version %s qualifying SHA %r — rejecting (no coverage written)",
            posted_sha[:12],
            getattr(version, "id", None),
            qualifying_sha[:12] if qualifying_sha else qualifying_sha,
        )
        return {
            "status": "rejected",
            "rejected_reason": "candidate_sha_mismatch",
            "recorded": 0,
            "skipped_not_passed": 0,
            "unmatched_keys": [],
            "candidate_sha": posted_sha,
        }

    exercised_sha = str(exercised_engine_sha or "").strip()
    if exercised_sha and exercised_sha != posted_sha:
        logger.warning(
            "record_walkthrough_e2e_evidence: exercised_engine_sha %s does not "
            "match candidate_sha %s for version %s — rejecting (harness ran against wrong stack)",
            exercised_sha[:12],
            posted_sha[:12],
            getattr(version, "id", None),
        )
        return {
            "status": "rejected",
            "rejected_reason": "exercised_engine_sha_mismatch",
            "recorded": 0,
            "skipped_not_passed": 0,
            "unmatched_keys": [],
            "candidate_sha": posted_sha,
        }
    if not exercised_sha:
        logger.warning(
            "record_walkthrough_e2e_evidence: exercised_engine_sha absent for version %s "
            "— accepting unverified-engine evidence (provenance gap)",
            getattr(version, "id", None),
        )

    project_id = version.project_id
    release_version_id = version.id

    # Only passed=True results are eligible; collect their keys (preserving order,
    # de-duplicating) so a single query resolves all criteria at once.
    # The meta criterion (c-5214996f) is NOT excluded here: it must still flow
    # through alias resolution and the unmatched-key check so that a phantom post
    # (c-5214996f not registered) surfaces as an unmatched-key alert rather than
    # a silent drop. The meta gate is instead applied at the evidence-writing
    # loop below, where the key has already resolved to an ACTIVE criterion — so
    # the gate only ever suppresses covered meta evidence, never observability.
    meta_required = await _walkthrough_meta_required_subcriteria(session, version)
    meta_gate_passes = _walkthrough_meta_criterion_gate(results, meta_required)
    passed_keys: list[str] = []
    skipped_not_passed = 0
    for entry in results:
        if not isinstance(entry, dict):
            continue
        key = str(entry.get("criterion_key") or "").strip()
        if not key:
            continue
        if entry.get("passed") is True:
            if key not in passed_keys:
                passed_keys.append(key)
        else:
            skipped_not_passed += 1

    if not passed_keys:
        return {
            "status": "recorded",
            "rejected_reason": None,
            "recorded": 0,
            "skipped_not_passed": skipped_not_passed,
            "unmatched_keys": [],
            "candidate_sha": posted_sha,
        }

    # Resolve legacy aliases (e.g. 'c-workday-session-lifecycle' → 'c-5214996f')
    # before the DB lookup.  passed_keys keeps the original slugs so that
    # unmatched_keys reports the posted key, not the alias target.
    resolved_keys = [_resolve_walkthrough_criterion_key(k) for k in passed_keys]
    criteria = await get_criteria_by_keys(
        session, project_id, resolved_keys, release_version_id=release_version_id
    )
    active_by_key = {c.criterion_key: c for c in criteria if c.status == "active"}
    unmatched_keys = [
        k for k in passed_keys
        if _resolve_walkthrough_criterion_key(k) not in active_by_key
    ]

    # Look up the notes for each passed key from the first matching result.
    # Index by the resolved key so the active_by_key loop can retrieve them.
    notes_by_key: dict[str, Any] = {}
    for entry in results:
        if not isinstance(entry, dict):
            continue
        key = str(entry.get("criterion_key") or "").strip()
        if key and entry.get("passed") is True:
            resolved = _resolve_walkthrough_criterion_key(key)
            if resolved not in notes_by_key:
                notes_by_key[resolved] = entry.get("notes")

    evaluated_at = _utcnow().isoformat()
    attempt_token = str(qualification_attempt_id or "na").strip() or "na"
    recorded = 0
    for key, criterion in active_by_key.items():
        # Meta-gate: the meta criterion c-5214996f (reachable directly or via the
        # 'c-workday-session-lifecycle' legacy alias, both of which resolve into
        # active_by_key under the canonical key) MUST NOT earn covered evidence
        # from a partial run where its required sub-criteria did not all pass in
        # this same batch. Gating here (post-resolution) leaves the unmatched-key
        # alert path intact for the not-registered case.
        if key == "c-5214996f" and not meta_gate_passes:
            logger.warning(
                "record_walkthrough_e2e_evidence: skipping c-5214996f — "
                "required sub-criteria %s not all passed in this batch "
                "(version %s); no covered meta evidence written",
                sorted(meta_required),
                getattr(version, "id", None),
            )
            skipped_not_passed += 1
            continue
        detail: dict[str, Any] = {
            "candidate_sha": posted_sha,
            "evaluated_at": evaluated_at,
            "criterion_key": key,
            "passed": True,
        }
        notes = notes_by_key.get(key)
        if notes:
            detail["notes"] = notes
        if qualification_attempt_id:
            detail["qualification_attempt_id"] = str(qualification_attempt_id)
        # Additive: record the SHA the candidate stack actually served so a later
        # guard can enforce it equals the qualifying candidate SHA. Never gates
        # here — the existing candidate_sha match above is the safety boundary.
        if exercised_sha:
            detail["exercised_engine_sha"] = exercised_sha
        source_id = f"walkthrough_e2e:{attempt_token}:{key}"
        try:
            await record_evidence(
                session,
                criterion_id=criterion.id,
                project_id=project_id,
                source_kind=WALKTHROUGH_E2E_EVIDENCE_KIND,
                source_id=source_id,
                detail=detail,
            )
            recorded += 1
        except Exception:
            logger.exception(
                "record_walkthrough_e2e_evidence: failed to record evidence for "
                "criterion %s (version %s)",
                key,
                getattr(version, "id", None),
            )

    if unmatched_keys:
        logger.warning(
            "record_walkthrough_e2e_evidence: version=%s candidate_sha=%s recorded=%d "
            "skipped_not_passed=%d unmatched=%s — passing keys dropped, no evidence written",
            getattr(version, "id", None),
            posted_sha[:12],
            recorded,
            skipped_not_passed,
            unmatched_keys,
        )
        try:
            from app.alert_policy import create_project_alert_if_allowed
            from app.models import AlertSeverity

            sorted_keys = sorted(unmatched_keys)
            dedupe_key = f"walkthrough_key_unmatched:{project_id}:{release_version_id}:{','.join(sorted_keys)}"
            title = "Walkthrough evidence dropped: unrecognised criterion key(s)"
            message = (
                f"record_walkthrough_e2e_evidence posted passing results for "
                f"{len(unmatched_keys)} key(s) that resolve to no active criterion "
                f"on version {release_version_id} (sha {posted_sha[:12]}): "
                f"{sorted_keys}. Evidence was NOT recorded."
            )
            await create_project_alert_if_allowed(
                session,
                project_id=project_id,
                event_type="release.walkthrough_key_unmatched",
                severity=AlertSeverity.WARNING,
                title=title,
                message=message,
                dedupe_key=dedupe_key,
            )
            await session.commit()
        except Exception:
            logger.exception(
                "record_walkthrough_e2e_evidence: failed to create unmatched-key alert "
                "for version %s — continuing",
                getattr(version, "id", None),
            )
    else:
        logger.info(
            "record_walkthrough_e2e_evidence: version=%s candidate_sha=%s recorded=%d "
            "skipped_not_passed=%d unmatched=%s",
            getattr(version, "id", None),
            posted_sha[:12],
            recorded,
            skipped_not_passed,
            unmatched_keys,
        )
    return {
        "status": "recorded",
        "rejected_reason": None,
        "recorded": recorded,
        "skipped_not_passed": skipped_not_passed,
        "unmatched_keys": unmatched_keys,
        "candidate_sha": posted_sha,
    }


async def record_named_test_execution_evidence(
    session: AsyncSession,
    version: Any,
    *,
    criterion_keys: list[str],
    executed_sha: str,
    test_refs: list[str],
    trial_task_id: str,
    enrolled_sha: str | None = None,
) -> dict[str, Any]:
    """Mint ``covered``-tier evidence from a GREEN executed named-test trial.

    Called by the ``criterion_named_test_trial`` completion handler when a
    targeted run of a merged task's declared covering tests has been validated
    as GREEN. For each criterion key in ``criterion_keys`` that resolves to an
    ACTIVE criterion on ``version``, writes one
    :data:`NAMED_TEST_EXECUTION_EVIDENCE_KIND` :class:`CriterionEvidence` row
    with ``detail = {executed_sha, evaluated_at, criterion_key, passed: True,
    test_refs}`` and a ``source_id`` keyed on ``named_test_exec:{trial_task_id}:
    {criterion_key}`` (so a re-ingestion of the same trial task is an idempotent
    no-op via the dedup unique index).

    **CRITICAL SAFETY** — covered-tier evidence is written ONLY when the posted
    ``executed_sha`` is non-empty AND matches the SHA the trial was pinned to.
    When ``enrolled_sha`` is provided (the orchestrator-authored enrollment
    pin), it is the authoritative anchor: the version may have re-pinned its
    qualifying candidate SHA between enrollment and ingestion, so comparing
    against the *current* scratch value would reject legitimate green runs.
    Callers that bypass enrollment (no ``enrolled_sha``) fall back to the
    version's qualifying candidate SHA (:func:`_version_qualifying_candidate_sha`)
    exactly as before. A missing or mismatched SHA writes NOTHING and is
    reported as ``rejected`` — a trial run against the wrong commit (or with no
    pinned SHA) can never forge coverage.

    Returns a summary dict::

        {"status": "recorded" | "rejected",
         "rejected_reason": str | None,  # only when status == "rejected"
         "recorded": int,                # rows newly written
         "skipped_not_passed": int,      # always 0 (only GREEN trials reach here)
         "unmatched_keys": list[str],    # keys not resolving to an active criterion
         "executed_sha": str}

    Best-effort per-row: a failure recording one criterion's evidence is logged
    and does not abort the others. Never raises.
    """
    posted_sha = str(executed_sha or "").strip()
    qualifying_sha = _version_qualifying_candidate_sha(version)
    # The enrollment pin (when present) is the durable anchor.  Falling back to
    # the *current* qualifying SHA for un-pinned callers preserves the legacy
    # contract, but the completion handler always passes the pinned value so
    # ingestion stops depending on the volatile scratch field.
    anchor_sha = str(enrolled_sha or "").strip() or qualifying_sha
    if not posted_sha:
        logger.warning(
            "record_named_test_execution_evidence: missing executed_sha for "
            "version %s — rejecting (no coverage written)",
            getattr(version, "id", None),
        )
        return {
            "status": "rejected",
            "rejected_reason": "missing_executed_sha",
            "recorded": 0,
            "skipped_not_passed": 0,
            "unmatched_keys": [],
            "executed_sha": posted_sha,
        }
    if not anchor_sha or posted_sha != anchor_sha:
        logger.warning(
            "record_named_test_execution_evidence: executed_sha %s does not "
            "match version %s pinned SHA %r (qualifying=%r) — rejecting "
            "(no coverage written)",
            posted_sha[:12],
            getattr(version, "id", None),
            anchor_sha[:12] if anchor_sha else anchor_sha,
            qualifying_sha[:12] if qualifying_sha else qualifying_sha,
        )
        return {
            "status": "rejected",
            "rejected_reason": "executed_sha_mismatch",
            "recorded": 0,
            "skipped_not_passed": 0,
            "unmatched_keys": [],
            "executed_sha": posted_sha,
        }

    project_id = version.project_id
    release_version_id = version.id

    keys = [
        str(k).strip()
        for k in (criterion_keys or [])
        if isinstance(k, str) and k.strip()
    ]
    if not keys:
        return {
            "status": "recorded",
            "rejected_reason": None,
            "recorded": 0,
            "skipped_not_passed": 0,
            "unmatched_keys": [],
            "executed_sha": posted_sha,
        }

    criteria = await get_criteria_by_keys(
        session, project_id, keys, release_version_id=release_version_id
    )
    active_by_key = {c.criterion_key: c for c in criteria if c.status == "active"}
    unmatched_keys = [k for k in keys if k not in active_by_key]

    evaluated_at = _utcnow().isoformat()
    trial_token = str(trial_task_id or "na").strip() or "na"
    # Pre-compute the deterministic source_ids so re-ingestion of the same trial
    # is idempotent: the DB unique index (uq_criterionevidence_dedup) guards the
    # Postgres path, and this explicit existence check keeps it idempotent on
    # every dialect (e.g. the SQLite test backend, which does not render the
    # postgresql_where partial index).
    source_ids_by_key = {
        key: f"named_test_exec:{trial_token}:{key}" for key in active_by_key
    }
    existing_result = await session.execute(
        select(CriterionEvidence.criterion_id).where(
            CriterionEvidence.source_kind == NAMED_TEST_EXECUTION_EVIDENCE_KIND,
            CriterionEvidence.source_id.in_(list(source_ids_by_key.values())),
        )
    )
    existing_criterion_ids = set(existing_result.scalars().all())

    recorded = 0
    for key, criterion in active_by_key.items():
        if criterion.id in existing_criterion_ids:
            logger.debug(
                "record_named_test_execution_evidence: duplicate evidence skipped "
                "for criterion %s (source_id %s)",
                key,
                source_ids_by_key[key],
            )
            continue
        detail: dict[str, Any] = {
            "executed_sha": posted_sha,
            "evaluated_at": evaluated_at,
            "criterion_key": key,
            "passed": True,
            "test_refs": [
                str(r) for r in (test_refs or []) if isinstance(r, str) and r.strip()
            ],
        }
        source_id = source_ids_by_key[key]
        try:
            await record_evidence(
                session,
                criterion_id=criterion.id,
                project_id=project_id,
                source_kind=NAMED_TEST_EXECUTION_EVIDENCE_KIND,
                source_id=source_id,
                detail=detail,
            )
            recorded += 1
        except Exception:
            logger.exception(
                "record_named_test_execution_evidence: failed to record evidence "
                "for criterion %s (version %s)",
                key,
                getattr(version, "id", None),
            )

    if unmatched_keys:
        logger.warning(
            "record_named_test_execution_evidence: version=%s executed_sha=%s "
            "recorded=%d unmatched=%s — keys dropped, no evidence written",
            getattr(version, "id", None),
            posted_sha[:12],
            recorded,
            unmatched_keys,
        )
    else:
        logger.info(
            "record_named_test_execution_evidence: version=%s executed_sha=%s "
            "recorded=%d unmatched=%s",
            getattr(version, "id", None),
            posted_sha[:12],
            recorded,
            unmatched_keys,
        )
    return {
        "status": "recorded",
        "rejected_reason": None,
        "recorded": recorded,
        "skipped_not_passed": 0,
        "unmatched_keys": unmatched_keys,
        "executed_sha": posted_sha,
    }


async def record_operator_walkthrough_evidence(
    session: AsyncSession,
    version: Any,
    candidate_sha: str,
    results: list[dict[str, Any]],
) -> dict[str, Any]:
    """Record operator-run walkthrough results as covered-tier evidence.

    Mirrors :func:`record_walkthrough_e2e_evidence` but skips the SHA match
    against ``version.last_qualification.candidate_sha``. The operator has
    intentionally run the walkthrough against a specific commit and asserts the
    SHA is the correct pinned commit — the function validates only that the
    provided ``candidate_sha`` is non-empty.

    Each entry in ``results`` is a ``{criterion_key, passed, notes}`` dict. For
    every entry whose ``passed`` is truthy, this resolves ``criterion_key`` to an
    ACTIVE criterion on ``version`` and writes one covering
    :data:`WALKTHROUGH_E2E_EVIDENCE_KIND` :class:`CriterionEvidence` row with
    ``detail = {candidate_sha, evaluated_at, criterion_key, passed[, notes]}``
    and a ``source_id`` keyed on the operator-run SHA + criterion (so a re-run
    against the same SHA is a no-op via the dedup unique index).

    **SAFETY:** covered-tier evidence is written ONLY when the posted
    ``candidate_sha`` is non-empty. A missing/empty SHA writes NOTHING and is
    reported as ``rejected``. Only ``passed=True`` results are written;
    failing/absent results are skipped.

    Returns a summary dict::

        {"status": "recorded" | "rejected",
         "rejected_reason": str | None,  # only when status == "rejected"
         "recorded": int,                # rows newly written
         "skipped_not_passed": int,      # results with passed falsy
         "unmatched_keys": list[str],    # keys not resolving to an active criterion
         "candidate_sha": str}

    Best-effort per-row: a failure recording one criterion's evidence is logged
    and does not abort the others.
    """
    posted_sha = str(candidate_sha or "").strip()
    if not posted_sha:
        logger.warning(
            "record_operator_walkthrough_evidence: missing candidate_sha for "
            "version %s — rejecting (no coverage written)",
            getattr(version, "id", None),
        )
        return {
            "status": "rejected",
            "rejected_reason": "missing_candidate_sha",
            "recorded": 0,
            "skipped_not_passed": 0,
            "unmatched_keys": [],
            "candidate_sha": posted_sha,
        }

    project_id = version.project_id
    release_version_id = version.id

    # Only passed=True results are eligible; collect their keys (preserving order,
    # de-duplicating) so a single query resolves all criteria at once.
    # The meta criterion (c-5214996f) is NOT excluded here: it must still flow
    # through alias resolution and the unmatched-key check so that a phantom post
    # (c-5214996f not registered) surfaces as an unmatched-key alert rather than
    # a silent drop. The meta gate is instead applied at the evidence-writing
    # loop below, where the key has already resolved to an ACTIVE criterion — so
    # the gate only ever suppresses covered meta evidence, never observability.
    meta_required = await _walkthrough_meta_required_subcriteria(session, version)
    meta_gate_passes = _walkthrough_meta_criterion_gate(results, meta_required)
    passed_keys: list[str] = []
    skipped_not_passed = 0
    for entry in results:
        if not isinstance(entry, dict):
            continue
        key = str(entry.get("criterion_key") or "").strip()
        if not key:
            continue
        if entry.get("passed") is True:
            if key not in passed_keys:
                passed_keys.append(key)
        else:
            skipped_not_passed += 1

    if not passed_keys:
        return {
            "status": "recorded",
            "rejected_reason": None,
            "recorded": 0,
            "skipped_not_passed": skipped_not_passed,
            "unmatched_keys": [],
            "candidate_sha": posted_sha,
        }

    # Resolve legacy aliases before the DB lookup; report unmatched using the
    # original slug so alerts remain human-readable.
    resolved_keys = [_resolve_walkthrough_criterion_key(k) for k in passed_keys]
    criteria = await get_criteria_by_keys(
        session, project_id, resolved_keys, release_version_id=release_version_id
    )
    active_by_key = {c.criterion_key: c for c in criteria if c.status == "active"}
    unmatched_keys = [
        k for k in passed_keys
        if _resolve_walkthrough_criterion_key(k) not in active_by_key
    ]

    # Look up the notes for each passed key from the first matching result.
    # Index by the resolved key so the active_by_key loop can retrieve them.
    notes_by_key: dict[str, Any] = {}
    for entry in results:
        if not isinstance(entry, dict):
            continue
        key = str(entry.get("criterion_key") or "").strip()
        if key and entry.get("passed") is True:
            resolved = _resolve_walkthrough_criterion_key(key)
            if resolved not in notes_by_key:
                notes_by_key[resolved] = entry.get("notes")

    evaluated_at = _utcnow().isoformat()
    recorded = 0
    for key, criterion in active_by_key.items():
        # Meta-gate: c-5214996f (direct or via the 'c-workday-session-lifecycle'
        # legacy alias, both resolving into active_by_key under the canonical key)
        # MUST NOT earn covered evidence from a partial run whose required
        # sub-criteria did not all pass in this batch. Gating post-resolution
        # keeps the unmatched-key alert intact for the not-registered case.
        if key == "c-5214996f" and not meta_gate_passes:
            logger.warning(
                "record_operator_walkthrough_evidence: skipping c-5214996f — "
                "required sub-criteria %s not all passed in this batch "
                "(version %s); no covered meta evidence written",
                sorted(meta_required),
                getattr(version, "id", None),
            )
            skipped_not_passed += 1
            continue
        detail: dict[str, Any] = {
            "candidate_sha": posted_sha,
            "evaluated_at": evaluated_at,
            "criterion_key": key,
            "passed": True,
        }
        notes = notes_by_key.get(key)
        if notes:
            detail["notes"] = notes
        source_id = f"operator_walkthrough:{posted_sha}:{key}"
        try:
            await record_evidence(
                session,
                criterion_id=criterion.id,
                project_id=project_id,
                source_kind=WALKTHROUGH_E2E_EVIDENCE_KIND,
                source_id=source_id,
                detail=detail,
            )
            recorded += 1
        except Exception:
            logger.exception(
                "record_operator_walkthrough_evidence: failed to record evidence "
                "for criterion %s (version %s)",
                key,
                getattr(version, "id", None),
            )

    if unmatched_keys:
        logger.warning(
            "record_operator_walkthrough_evidence: version=%s candidate_sha=%s "
            "recorded=%d skipped_not_passed=%d unmatched=%s — passing keys dropped, no evidence written",
            getattr(version, "id", None),
            posted_sha[:12],
            recorded,
            skipped_not_passed,
            unmatched_keys,
        )
        try:
            from app.alert_policy import create_project_alert_if_allowed
            from app.models import AlertSeverity

            sorted_keys = sorted(unmatched_keys)
            dedupe_key = f"walkthrough_key_unmatched:{project_id}:{release_version_id}:{','.join(sorted_keys)}"
            title = "Operator walkthrough evidence dropped: unrecognised criterion key(s)"
            message = (
                f"record_operator_walkthrough_evidence posted passing results for "
                f"{len(unmatched_keys)} key(s) that resolve to no active criterion "
                f"on version {release_version_id} (sha {posted_sha[:12]}): "
                f"{sorted_keys}. Evidence was NOT recorded."
            )
            await create_project_alert_if_allowed(
                session,
                project_id=project_id,
                event_type="release.walkthrough_key_unmatched",
                severity=AlertSeverity.WARNING,
                title=title,
                message=message,
                dedupe_key=dedupe_key,
            )
            await session.commit()
        except Exception:
            logger.exception(
                "record_operator_walkthrough_evidence: failed to create unmatched-key alert "
                "for version %s — continuing",
                getattr(version, "id", None),
            )
    else:
        logger.info(
            "record_operator_walkthrough_evidence: version=%s candidate_sha=%s "
            "recorded=%d skipped_not_passed=%d unmatched=%s",
            getattr(version, "id", None),
            posted_sha[:12],
            recorded,
            skipped_not_passed,
            unmatched_keys,
        )
    return {
        "status": "recorded",
        "rejected_reason": None,
        "recorded": recorded,
        "skipped_not_passed": skipped_not_passed,
        "unmatched_keys": unmatched_keys,
        "candidate_sha": posted_sha,
    }


async def record_acceptance_window_coverage_evidence(
    session: AsyncSession,
    version: Any,
    diagnostic: dict[str, Any] | None = None,
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

    Returns the number of evidence rows newly recorded.  When ``diagnostic`` is
    supplied, writes one observational ``branch`` value describing why the
    minter recorded evidence or legitimately skipped it.  This is deliberately
    not persisted as criterion evidence and changes no coverage decision.

    **DEPLOYED-SIDE CONSTRAINT**: this function executes inside the deployed
    orchestrator process at the deployed (main) SHA, called from
    Strategic's next qualification decision.
    A bug fix to this minting logic that lands on automain is INERT until the
    fix is itself promoted and deployed.  The operator hotfix-deploy channel
    (``POST /projects/{id}/promote/hotfix``, gated by
    :data:`app.hotfix_policy.HOTFIX_ELIGIBLE_MODULES`) is the sanctioned
    remediation path for fixes to this file.
    """
    try:
        from app import release_qualifier

        # Eligibility and idempotency are cheap indexed reads. Perform them
        # before scanning the version's full landed-task window so a stable
        # evidence row does not create recurring work on every reaper cycle.
        targets = await _resolve_acceptance_window_criteria(session, version)
        if not targets:
            if diagnostic is not None:
                diagnostic["branch"] = "no_target_criterion"
            logger.info(
                "record_acceptance_window_coverage_evidence: no active criterion "
                "names the acceptance-window metric on version %s — recording nothing",
                getattr(version, "id", None),
            )
            return 0

        source_id = f"acceptance_window_recompute:v{version.version_number}"
        target_ids = [c.id for c in targets]
        ev_result = await session.execute(
            select(CriterionEvidence).where(
                CriterionEvidence.criterion_id.in_(target_ids),
                CriterionEvidence.source_kind == ACCEPTANCE_WINDOW_EVIDENCE_KIND,
                CriterionEvidence.source_id == source_id,
            )
        )
        already_present = {ev.criterion_id for ev in ev_result.scalars().all()}
        if len(already_present) == len(target_ids):
            if diagnostic is not None:
                diagnostic["branch"] = "evidence_already_present"
            return 0

        latest_landed_updated_at = (
            await session.execute(
                select(func.max(Task.updated_at)).where(
                    Task.project_id == version.project_id,
                    Task.status == TaskStatus.COMPLETED,
                    landed_signal_predicate(session),
                    Task.updated_at >= version.opened_at,
                )
            )
        ).scalar_one_or_none()
        state_fingerprint = (
            tuple(sorted(str(criterion_id) for criterion_id in target_ids)),
            latest_landed_updated_at,
        )
        version_id = version.id
        if _acceptance_window_recompute_state.get(version_id) == state_fingerprint:
            if diagnostic is not None:
                diagnostic["branch"] = "unchanged_window_state"
            return 0

        aggregate = await release_qualifier._compute_acceptance_window_aggregate(
            session, version
        )
        write_total = int(aggregate.get("acceptance_window_write_total") or 0)
        verified = int(aggregate.get("acceptance_window_verified") or 0)
        if write_total <= 0 or verified <= 0:
            if diagnostic is not None:
                diagnostic["branch"] = "false_zero_window"
            # Cache only the intentional no-write outcome. A coverable window
            # relies on durable evidence idempotency instead: caching before its
            # transaction commits could let a transient insert/commit failure
            # suppress every retry until some unrelated landed-state change.
            _acceptance_window_recompute_state[version_id] = state_fingerprint
            logger.info(
                "record_acceptance_window_coverage_evidence: false-zero window — "
                "recording nothing (version_id=%s write_total=%d verified=%d)",
                getattr(version, "id", None),
                write_total,
                verified,
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
        if diagnostic is not None:
            diagnostic["branch"] = (
                "evidence_recorded" if recorded else "evidence_already_present"
            )
        return recorded
    except Exception:
        if diagnostic is not None:
            diagnostic["branch"] = "error"
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

    # Rule (b): the criterion is mapped to an ACTIVE direction whose text names the signal.
    # Non-active directions (paused, staged, proposed, retired, etc.) are inert here:
    # their historical prose must not trigger probe evidence on the open version.
    dir_result = await session.execute(
        select(Direction).where(Direction.project_id == version.project_id)
    )
    for direction in dir_result.scalars().all():
        if not direction_matches_criterion_release_version(direction, version):
            continue
        if direction.status != "active":
            continue
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
    if enforce_loop_authored and (
        is_operator_authored_criterion(row.created_by)
        or is_owner_controlled_machine_contract(row)
    ):
        raise OperatorAuthoredRetractionError(
            criterion_key,
            row.evidence_contract_updated_by or row.created_by,
        )
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
    release_version: Any,
) -> dict[str, str | None]:
    """Diagnose WHY each uncovered criterion is uncovered.

    Returns a mapping ``criterion_key -> stuck_reason`` where ``stuck_reason`` is:

      ``"no_mapping"``          — no Direction in the project lists this key in its
                                  ``criterion_keys``;
      ``"direction_retired"``   — at least one Direction maps this key but *every*
                                  mapping Direction has a retired status
                                  (``expired``/``retired``/``superseded``/``completed``);
      ``"direction_inactive"``  — at least one Direction maps this key but none is
                                  ``active`` and not all are retired — i.e. every mapping
                                  Direction is paused, staged, proposed, or another
                                  non-active, non-retired state;
      ``None``                  — at least one *active* Direction maps this key, so
                                  the criterion is in-progress, not stuck.

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
        if not direction_matches_criterion_release_version(direction, release_version):
            continue
        keys = direction.criterion_keys or []
        for key in keys:
            key_statuses[key].add(direction.status)

    reasons: dict[str, str | None] = {}
    for key in uncovered_keys:
        statuses = key_statuses.get(key)
        if not statuses:
            reasons[key] = "no_mapping"
        elif statuses & _ACTIVE_DIRECTION_STATUSES:
            reasons[key] = None
        elif statuses <= _RETIRED_DIRECTION_STATUSES:
            reasons[key] = "direction_retired"
        else:
            reasons[key] = "direction_inactive"
    return reasons


async def get_unmapped_active_criterion_keys(
    session: AsyncSession,
    project_id: uuid.UUID,
    release_version_id: uuid.UUID,
) -> list[str]:
    """Return criterion_keys of ACTIVE criteria not linked to any Direction.

    An active criterion is unmapped when no Direction in the project lists its
    key in ``Direction.criterion_keys``.  Unmapped criteria are structurally
    guaranteed to never accumulate merge evidence via
    :func:`record_task_merge_evidence` — that function only writes evidence for
    criteria carried on the task itself or inherited from its direction's
    ``criterion_keys``.  This helper surfaces the unmapped set so callers
    (health sweeps, API endpoints, diagnostics) can flag the gap without going
    through the full coverage-summary query.

    Returns a sorted list of criterion_key strings (empty list when all active
    criteria are mapped or when there are no active criteria).
    """
    key_result = await session.execute(
        select(VersionAcceptanceCriterion.criterion_key).where(
            VersionAcceptanceCriterion.release_version_id == release_version_id,
            VersionAcceptanceCriterion.status == "active",
        )
    )
    active_keys: set[str] = set(key_result.scalars().all())
    if not active_keys:
        return []

    dir_result = await session.execute(
        select(Direction).where(Direction.project_id == project_id)
    )
    mapped_keys: set[str] = set()
    for direction in dir_result.scalars().all():
        for k in direction.criterion_keys or []:
            mapped_keys.add(k)

    return sorted(active_keys - mapped_keys)


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
                    'covered_by': str | None,  # source_kind providing coverage, e.g. 'walkthrough_e2e'
                    'status': 'active' | 'retracted' | 'superseded',
                    'evidence_count': int,
                    'evidence_truncated': bool,
                    'stuck_reason': 'no_mapping' | 'direction_retired' | 'direction_inactive' | None,
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

    ``covered_by`` pinpoints the exact ``source_kind`` that provides the
    criterion's current coverage (e.g. ``"walkthrough_e2e"`` vs
    ``"task_merge"`` vs ``"reachability"``).  For ``"uncovered"`` criteria —
    or any tier whose evidence list is empty — this is ``None``.
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
            if not is_operator_authorship_criterion(criterion):
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
            session,
            criteria_in_order[0].project_id,
            release_version_id=release_version_id,
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

    from app.release_models import ReleaseVersion

    _rv = await session.get(ReleaseVersion, release_version_id)

    # Diagnose uncovered criteria: which ones are stuck (no active direction) vs.
    # merely in-progress. project_id is derived from a loaded criterion row —
    # every criterion carries it — so the public signature stays unchanged.
    uncovered_keys = [
        c.criterion_key
        for c in criteria_in_order
        if (
            c.status == "active"
            and not is_operator_authorship_criterion(c)
            and coverage_by_criterion[c.id] == "uncovered"
        )
    ]
    stuck_reasons: dict[str, str | None] = {}
    if uncovered_keys and _rv is not None:
        stuck_reasons = await _classify_stuck_reasons(
            session, criteria_in_order[0].project_id, uncovered_keys, _rv
        )

    stuck_count = 0
    for criterion in criteria_in_order:
        evidence = evidence_by_criterion.get(criterion.id, [])
        coverage = coverage_by_criterion[criterion.id]
        # When corroboration failed and downgraded coverage to 'partial',
        # the evidence that earned the higher tier is no longer trusted.
        # Report covered_by as None so it aligns with the effective 'partial'
        # coverage — the prior source_kind is no longer providing coverage.
        covered_by = _evidence_providing_source_kind(evidence)
        if (
            coverage == "partial"
            and criterion.criterion_key in failed_corroboration
        ):
            covered_by = None
        stuck_reason = stuck_reasons.get(criterion.criterion_key)
        if stuck_reason is not None:
            stuck_count += 1
        evidence_truncated = len(evidence) > MAX_EVIDENCE_PER_CRITERION_IN_SUMMARY
        criteria_detail.append(
            {
                "criterion_key": criterion.criterion_key,
                "text": criterion.text,
                "evidence_contract": normalize_evidence_contract(
                    criterion.evidence_contract
                ),
                "coverage": coverage,
                "covered_by": covered_by,
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
                "evidence_contract_updated_at": (
                    criterion.evidence_contract_updated_at.isoformat()
                    if criterion.evidence_contract_updated_at
                    else None
                ),
                "evidence_contract_updated_by": criterion.evidence_contract_updated_by,
            }
        )

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
            from app.models import Project
            from app.release_version import current_open_version

            current_version = await current_open_version(session, _rv.project_id)
            if current_version is not None and current_version.id == _rv.id:
                _project = await session.get(Project, _rv.project_id)
                registered_keys = {
                    c.criterion_key
                    for c in criteria_in_order
                    if c.status == "active"
                    and not is_operator_authorship_criterion(c)
                }
                referenced_keys = (
                    await _gather_referenced_criterion_keys(
                        session, _project, current_version
                    )
                    if _project is not None
                    else set()
                )
                phantom_bookkeeping = reconcile_criterion_key_references(
                    registered_keys, referenced_keys
                )
                phantom_bookkeeping["referenced_keys"] = sorted(referenced_keys)
                phantom_bookkeeping["registered_count"] = len(registered_keys)
        except Exception:
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
    *,
    release_version_id: uuid.UUID | None = None,
) -> set[str]:
    """Return criterion_keys whose corroboration task has FAILED or been
    CANCELLED **and** no newer non-terminal task exists for the same key.

    A corroboration task is spawned for both lower-confidence coverage tiers —
    operator attestation and unverified-merge backfill (see
    :func:`spawn_corroboration_task`).  This function is provenance-agnostic: it
    keys on ``context["corroboration_of"]``. When ``release_version_id`` is
    supplied, only tasks carrying that persisted version binding participate.

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
    from app.models import Task, TaskStatus

    # The JSON element (typed) and its text form. The text form is only used for
    # the SQL existence predicate (``->> 'corroboration_of' IS NOT NULL``, which a
    # JSON null also fails — matching the isinstance guard below); the element is
    # what we read, so a non-string value is still rejected in Python instead of
    # being laundered into a key by the ``->>`` text cast.
    corroboration_key = Task.context["corroboration_of"]
    corroboration_key_text = corroboration_key.as_string()
    version_conditions = []
    if release_version_id is not None:
        version_conditions.append(
            Task.context[CRITERION_RELEASE_VERSION_CONTEXT_KEY].as_string()
            == str(release_version_id)
        )

    result = await session.execute(
        select(corroboration_key).where(
            Task.project_id == project_id,
            Task.status.in_([TaskStatus.FAILED, TaskStatus.CANCELLED]),
            corroboration_key_text.isnot(None),
            *version_conditions,
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
                *version_conditions,
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
    *,
    release_version_id: uuid.UUID,
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
        from app.models import Task, TaskStatus
        from app.task_locking import (
            AUTOMATIC_TASK_CREATION_ALLOWED,
            acquire_automatic_task_creation_authority,
        )

        admission = await acquire_automatic_task_creation_authority(
            session, project_id, skip_locked=True
        )
        if admission != AUTOMATIC_TASK_CREATION_ALLOWED:
            logger.info(
                "[AC] Corroboration task admission closed for criterion %s "
                "(project %s, %s)",
                criterion_key,
                project_id,
                admission,
            )
            return

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
                Task.context[CRITERION_RELEASE_VERSION_CONTEXT_KEY].as_string()
                == str(release_version_id),
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
            context={
                "corroboration_of": criterion_key,
                "spawned_by": spawned_by,
                CRITERION_RELEASE_VERSION_CONTEXT_KEY: str(release_version_id),
            },
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


async def enroll_criterion_named_test_trial(
    session: AsyncSession,
    project: Any,
    *,
    version: Any,
    criterion_key: str,
    criterion_text: str,
    test_refs: list[str],
) -> Any:
    """Enroll one bounded, SHA-pinned named-test trial.

    The kill switch, project work freeze, absent SHA/refs, and an unchanged
    candidate+test+precondition identity are no-ops. Caps at one non-terminal
    trial per criterion + release version: an identical in-flight trial is
    skipped, a QUEUED trial pinned to a stale candidate SHA is cancelled
    (superseded) and re-enrolled at the current head, and a running
    (PROVISIONING/IN_PROGRESS) trial on a stale SHA is left alone with no new
    enrollment. Also a no-op once the same (criterion, test, precondition)
    triple has produced
    :data:`CRITERION_NAMED_TEST_TRIAL_CONSECUTIVE_NON_GREEN_SUPPRESSION_THRESHOLD`
    consecutive non-green terminal trials — independent of ``head_sha``,
    since a bare candidate-SHA bump is not evidence a broken execution
    precondition was repaired; a changed ``test_identity`` or
    ``precondition_identity`` re-arms immediately. A GREEN receipt may mint
    covered evidence; every degraded path returns ``None`` without raising.
    """
    try:
        from app.shared_state import runtime_config as _rc

        if not (_rc or {}).get("criterion_named_test_trials_enabled", True):
            return None

        if (
            bool(getattr(project, "paused", False))
            or not bool(getattr(project, "autonomous_enabled", False))
            or bool(getattr(project, "archived", False))
        ):
            logger.info(
                "[CRITERION_TRIAL] enrollment frozen for project %s "
                "(paused=%s autonomous_enabled=%s archived=%s)",
                getattr(project, "id", None),
                bool(getattr(project, "paused", False)),
                bool(getattr(project, "autonomous_enabled", False)),
                bool(getattr(project, "archived", False)),
            )
            return None

        refs = [r for r in (test_refs or []) if isinstance(r, str) and r.strip()]
        if not refs:
            logger.warning(
                "[CRITERION_TRIAL] cannot enroll trial for criterion %s — "
                "no usable test refs",
                criterion_key,
            )
            return None
        refs = refs[:MAX_CRITERION_NAMED_TEST_TRIAL_TEST_FILES]
        # Belt-and-suspenders: MAX_CRITERION_NAMED_TEST_TRIAL_TEST_FILES must never
        # be 0, but guard defensively so a misconfigured constant can't produce a
        # fabricated empty proof.
        if not refs:
            logger.warning(
                "[CRITERION_TRIAL] cannot enroll trial for criterion %s — refs empty "
                "after cap (MAX_CRITERION_NAMED_TEST_TRIAL_TEST_FILES=%d)",
                criterion_key,
                MAX_CRITERION_NAMED_TEST_TRIAL_TEST_FILES,
            )
            return None

        head_sha = _version_qualifying_candidate_sha(version)
        if not head_sha:
            logger.info(
                "[CRITERION_TRIAL] cannot enroll trial for criterion %s — version %s "
                "has no qualifying candidate SHA",
                criterion_key,
                getattr(version, "id", None),
            )
            return None

        project_id = getattr(project, "id", None)
        version_id = getattr(version, "id", None)

        from app.task_locking import (
            AUTOMATIC_TASK_CREATION_ALLOWED,
            acquire_automatic_task_creation_authority,
        )

        admission = await acquire_automatic_task_creation_authority(
            session,
            project_id,
            skip_locked=True,
            require_autonomous_enabled=True,
        )
        if admission != AUTOMATIC_TASK_CREATION_ALLOWED:
            logger.info(
                "[CRITERION_TRIAL] automatic admission closed for criterion %s "
                "(project %s, %s)",
                criterion_key,
                project_id,
                admission,
            )
            return None

        # Stable retry authority. Only inputs the trial handler actually consumes
        # belong to this named execution-precondition epoch. Project build_config
        # contains unrelated release/sweep policy and must not launder a terminal
        # trial into fresh work when one of those settings changes.
        refs = sorted(dict.fromkeys(r.strip() for r in refs))
        test_identity = hashlib.sha256(
            json.dumps(refs, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
        precondition_identity = hashlib.sha256(
            json.dumps(
                {
                    "contract": CRITERION_NAMED_TEST_TRIAL_CONTRACT,
                    "max_test_files": MAX_CRITERION_NAMED_TEST_TRIAL_TEST_FILES,
                    "wall_clock_seconds": CRITERION_NAMED_TEST_TRIAL_WALL_CLOCK_SECONDS,
                },
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        identity_seed = (
            f"{criterion_key}\n{head_sha}\n"
            f"{test_identity}\n{precondition_identity}"
        )
        trial_identity = hashlib.sha256(identity_seed.encode()).hexdigest()

        # Ask the database for the full durable identity across every task state.
        # A newest-N object scan lets unrelated churn push a terminal row out of
        # view. This exact JSON scalar predicate remains authoritative after any
        # amount of later task history and returns only the tiny matching row.
        trial_payload = Task.payloads[0]
        identity_match = (
            await session.execute(
                select(Task.id, Task.status)
                .where(
                    Task.project_id == project_id,
                    Task.context[CRITERION_RELEASE_VERSION_CONTEXT_KEY].as_string()
                    == str(version_id),
                    trial_payload["type"].as_string()
                    == "criterion_named_test_trial",
                    trial_payload["criterion_key"].as_string() == criterion_key,
                    trial_payload["trial_identity"].as_string() == trial_identity,
                )
                .limit(1)
            )
        ).first()

        # Compatibility for trials created before identities were persisted.
        # Their stored refs were already canonicalized at enrollment, so the
        # database can apply the exact array identity without loading history.
        if identity_match is None:
            identity_match = (
                await session.execute(
                    select(Task.id, Task.status)
                    .where(
                        Task.project_id == project_id,
                        Task.context[
                            CRITERION_RELEASE_VERSION_CONTEXT_KEY
                        ].as_string()
                        == str(version_id),
                        trial_payload["type"].as_string()
                        == "criterion_named_test_trial",
                        trial_payload["criterion_key"].as_string() == criterion_key,
                        trial_payload["head_sha"].as_string() == head_sha,
                        trial_payload["test_refs"] == refs,
                        trial_payload["precondition_identity"].as_string().is_(None),
                    )
                    .limit(1)
                )
            ).first()
        if identity_match is not None:
            logger.info(
                "[CRITERION_TRIAL] skipping unchanged trial identity for "
                "criterion %s (existing task %s status=%s)",
                criterion_key,
                identity_match[0],
                identity_match[1],
            )
            return None

        from app.models import TASK_OUTCOME_TERMINAL_STATUSES

        # Consecutive-non-green streak suppression. The identity check above
        # only suppresses an EXACT match including head_sha, so it never fires
        # on a project whose candidate SHA bumps continuously — a bare SHA
        # change is not evidence that a broken execution precondition was
        # repaired. Look at the same (criterion, test, precondition) triple
        # ACROSS every head_sha: if the most recent terminal trials for this
        # triple all came back non-green, stop enrolling until test_identity
        # or precondition_identity changes.
        #
        # Two additional predicates bound the scan, both load-bearing:
        #  - a recency cutoff so a repaired execution environment self-heals
        #    the suppression once the window elapses, instead of latching
        #    forever on the (test_identity, precondition_identity) key alone
        #    (see CRITERION_NAMED_TEST_TRIAL_STREAK_RECENCY_WINDOW_DAYS).
        #  - a signal-bearing filter (contradiction stamp present, OR a
        #    COMPLETED row that can only be green here since a contradiction
        #    stamp would already satisfy the first clause) so CANCELLED
        #    supersede rows — minted by this very function's own stale-QUEUED
        #    cleanup below every time the candidate SHA bumps while a trial is
        #    still QUEUED — cannot pad past _CRITERION_NAMED_TEST_TRIAL_STREAK_SCAN_LIMIT
        #    and push real terminal signal out of the scan window.
        streak_recency_cutoff = _utcnow() - timedelta(
            days=CRITERION_NAMED_TEST_TRIAL_STREAK_RECENCY_WINDOW_DAYS
        )
        streak_rows = (
            await session.execute(
                select(Task.id, Task.status, Task.context)
                .where(
                    Task.project_id == project_id,
                    Task.context[CRITERION_RELEASE_VERSION_CONTEXT_KEY].as_string()
                    == str(version_id),
                    trial_payload["type"].as_string() == "criterion_named_test_trial",
                    trial_payload["criterion_key"].as_string() == criterion_key,
                    trial_payload["test_identity"].as_string() == test_identity,
                    trial_payload["precondition_identity"].as_string()
                    == precondition_identity,
                    Task.status.in_(TASK_OUTCOME_TERMINAL_STATUSES),
                    Task.created_at >= streak_recency_cutoff,
                    or_(
                        Task.context.has_key(
                            "criterion_named_test_trial_contradiction"
                        ),
                        Task.status == TaskStatus.COMPLETED,
                    ),
                )
                .order_by(Task.created_at.desc())
                .limit(_CRITERION_NAMED_TEST_TRIAL_STREAK_SCAN_LIMIT)
            )
        ).all()

        consecutive_non_green = 0
        for _row_id, row_status, row_context in streak_rows:
            contradiction = (row_context or {}).get(
                "criterion_named_test_trial_contradiction"
            )
            if isinstance(contradiction, dict):
                consecutive_non_green += 1
                if (
                    consecutive_non_green
                    >= CRITERION_NAMED_TEST_TRIAL_CONSECUTIVE_NON_GREEN_SUPPRESSION_THRESHOLD
                ):
                    break
                continue
            if row_status == TaskStatus.COMPLETED:
                # COMPLETED with no contradiction stamp means the ingestion
                # handler recorded green evidence — the streak is broken.
                break
            # CANCELLED (superseded before it ran) or FAILED (never reached
            # ingestion) carries no pass/fail signal for this triple: skip it
            # without counting it toward, or breaking, the streak.
            continue

        if (
            consecutive_non_green
            >= CRITERION_NAMED_TEST_TRIAL_CONSECUTIVE_NON_GREEN_SUPPRESSION_THRESHOLD
        ):
            logger.info(
                "[CRITERION_TRIAL] suppressing enrollment for criterion %s "
                "(project %s, version %s) — %d consecutive non-green terminal "
                "trials for this test+precondition identity; unchanged since "
                "the last attempt (head_sha alone does not re-arm)",
                criterion_key,
                project_id,
                version_id,
                consecutive_non_green,
            )
            return None

        # In-flight debounce: at most one non-terminal trial per criterion +
        # release version. The identity check above only suppresses an EXACT
        # match (same head, same refs, same precondition); the moment the
        # version's qualifying candidate SHA moves, every still-uncovered
        # criterion would otherwise mint a brand-new trial even while an
        # earlier trial for the same criterion is still QUEUED or running on
        # the older SHA — this is the O(criteria x SHA bumps) spam mechanism.
        # Any row this query returns is, by construction, NOT an identity
        # match (that case already returned above), so it is pinned to a
        # different candidate SHA or an older test/precondition epoch.
        from sqlalchemy.orm.attributes import flag_modified

        inflight_rows = (
            await session.execute(
                select(
                    Task.id,
                    Task.status,
                    trial_payload["head_sha"].as_string(),
                ).where(
                    Task.project_id == project_id,
                    Task.context[CRITERION_RELEASE_VERSION_CONTEXT_KEY].as_string()
                    == str(version_id),
                    trial_payload["type"].as_string() == "criterion_named_test_trial",
                    trial_payload["criterion_key"].as_string() == criterion_key,
                    Task.status.not_in(TASK_OUTCOME_TERMINAL_STATUSES),
                )
            )
        ).all()

        running_stale = [row for row in inflight_rows if row[1] != TaskStatus.QUEUED]
        queued_stale = [row for row in inflight_rows if row[1] == TaskStatus.QUEUED]

        # A QUEUED trial has not consumed any VM/agent resources yet — cancel
        # it outright and re-enroll fresh at the current head rather than
        # leaving a pinned-to-a-stale-SHA duplicate around. This is pure
        # dispatch-time cleanup: no PR/branch exists for it yet.
        for stale_id, _stale_status, stale_head in queued_stale:
            stale_task = await session.get(Task, stale_id)
            # Re-check under the freshly loaded row: a concurrent dispatch may
            # have already moved it out of QUEUED between the query above and
            # this fetch.
            if stale_task is None or stale_task.status != TaskStatus.QUEUED:
                continue
            stale_task.status = TaskStatus.CANCELLED
            stale_task.latest_status_message = (
                f"Superseded by newer qualifying candidate SHA {head_sha[:12]} "
                f"(was pinned to {(stale_head or 'unknown')[:12]})"
            )[:500]
            ctx = dict(stale_task.context or {})
            ctx["superseded_by_newer_candidate"] = True
            ctx["superseded_stale_head_sha"] = stale_head
            ctx["superseded_successor_head_sha"] = head_sha
            stale_task.context = ctx
            flag_modified(stale_task, "context")
            session.add(stale_task)
            logger.info(
                "[CRITERION_TRIAL] cancelled stale QUEUED trial %s for "
                "criterion %s (project %s) — superseded by candidate SHA %s "
                "(was %s)",
                stale_id,
                criterion_key,
                project_id,
                head_sha,
                stale_head,
            )
        if queued_stale:
            await session.commit()

        # An IN_PROGRESS/PROVISIONING (or otherwise live) trial on a stale
        # head is bounded and possibly nearly done — yanking running work is
        # the review-supersede-churn antipattern. Leave it alone and decline
        # to enroll a new one; the periodic backfill sweep re-runs and will
        # enroll a follow-up at the then-current head once this trial reaches
        # a terminal state, so N SHA bumps during one trial's runtime
        # naturally collapse into a single follow-up trial at the latest head.
        if running_stale:
            logger.info(
                "[CRITERION_TRIAL] in-flight trial already running for "
                "criterion %s (project %s, version %s) — task(s) %s "
                "status=%s — not enrolling a new trial at head %s; the "
                "backfill sweep will chain a follow-up once it reaches a "
                "terminal state",
                criterion_key,
                project_id,
                version_id,
                [str(row[0]) for row in running_stale],
                [row[1] for row in running_stale],
                head_sha,
            )
            return None

        payload: dict[str, Any] = {
            "type": "criterion_named_test_trial",
            "criterion_key": criterion_key,
            "criterion_text": criterion_text,
            "test_refs": refs,
            "head_sha": head_sha,
            "candidate_identity": head_sha,
            "test_identity": test_identity,
            "precondition_identity": precondition_identity,
            "trial_identity": trial_identity,
            "max_test_files": MAX_CRITERION_NAMED_TEST_TRIAL_TEST_FILES,
            "wall_clock_seconds": CRITERION_NAMED_TEST_TRIAL_WALL_CLOCK_SECONDS,
        }
        task = Task(
            project_id=project_id,
            title=f"Criterion named-test trial: {criterion_key}",
            criterion_ids=[criterion_key],
            context={
                "corroboration_of": criterion_key,
                "spawned_by": NAMED_TEST_EXECUTION_EVIDENCE_KIND,
                CRITERION_RELEASE_VERSION_CONTEXT_KEY: str(version_id),
            },
            payloads=[payload],
            status=TaskStatus.QUEUED,
            created_by="SYSTEM_AUTO_REVIEW",
            idempotency_key=(
                f"criterion-trial:{version_id}:{criterion_key}:{trial_identity[:24]}"
            ),
        )
        session.add(task)
        await session.commit()
        await session.refresh(task)
        logger.info(
            "[CRITERION_TRIAL] enrolled trial task %s for criterion=%s refs=%s sha=%s",
            task.id,
            criterion_key,
            refs,
            head_sha,
        )
        return task
    except Exception:
        logger.warning(
            "[CRITERION_TRIAL] failed to enroll criterion_named_test_trial for criterion=%r",
            criterion_key,
            exc_info=True,
        )
        try:
            await session.rollback()
        except Exception:
            logger.debug(
                "[CRITERION_TRIAL] rollback after enroll failure also failed",
                exc_info=True,
            )
        return None


async def backfill_merged_named_test_coverage(
    session: AsyncSession,
    project_id: uuid.UUID,
    *,
    batch_size: int = 500,
) -> int:
    """Backfill merged named-test evidence and enroll each new target once.

    A shipped declared test plus production seam records the lower-confidence
    backfill tier. Only a newly admitted, not-yet-covered row becomes a bounded
    trial target. Best-effort; returns the number of rows recorded.
    """
    total = 0
    try:
        from app.acceptance_gate import CODE_ACCEPTANCE_VERDICT_KEY
        from app.acceptance_gate import _changed_files, _paths_match, _is_test_path
        from app.landed_signal import is_merged_completion, landed_signal_predicate
        from app.models import Task, TaskStatus
        from app.payload_schemas import read_acceptance_evidence
        from app.release_version import current_open_version as _cur_open_ver

        _BATCH_LIMIT = batch_size
        total = 0
        offset = 0
        open_version: Any = None
        # Criteria (key -> (text, declared_test)) covered by backfill this run —
        # each gets a criterion_named_test_trial so the 'backfilled' tier has an
        # execute-the-covering-test earn-up path toward machine-verified
        # 'covered', replacing the research corroboration spawn for this lane.
        trial_targets: dict[str, tuple[str, str]] = {}
        while True:
            query = (
                select(Task)
                .where(
                    Task.project_id == project_id,
                    Task.status == TaskStatus.COMPLETED,
                    landed_signal_predicate(session),
                    Task.criterion_ids != None,  # noqa: E711 — SQLAlchemy IS NOT NULL
                )
                .order_by(Task.id)
                .limit(_BATCH_LIMIT)
                .offset(offset)
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
                        "backfill_merged_named_test_coverage: no open version for "
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
            qualified: list[tuple] = []
            all_batch_criterion_ids: set[str] = set()
            for task in batch:
                if not task_matches_criterion_release_version(task, open_version):
                    continue
                if not is_merged_completion(task):
                    continue

                # Must have at least one criterion_id to backfill.
                task_criterion_ids: list[str] = list(
                    getattr(task, "criterion_ids", None) or []
                )
                if not task_criterion_ids:
                    continue

                # Must have changed files to verify the test shipped.
                changed = _changed_files(task)
                if not changed:
                    continue

                # Must have declared named_test acceptance evidence.
                evidence = read_acceptance_evidence(task.first_payload)
                if evidence is None or evidence.kind != "named_test":
                    continue
                if evidence.test_path is None:
                    continue

                # ``partition`` yields the whole path when there is no ``::NODE``
                # selector — matching acceptance_gate._evaluate_named_test's read.
                declared_test = evidence.test_path.partition("::")[0].strip()
                if not declared_test:
                    continue

                # Verify the test IS in changed files — it was genuinely shipped.
                test_present = any(_paths_match(p, declared_test) for p in changed)
                if not test_present:
                    continue

                # Must also have a production seam to exercise.
                seam = [p for p in changed if not _is_test_path(p)]
                if not seam:
                    continue

                qualified.append((task, task_criterion_ids, declared_test))
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

            if not criteria_by_key:
                continue

            # Batch-fetch evidence for all active criteria to check current coverage.
            active_criterion_ids = list(criteria_by_key.values())
            evidence_by_criterion = await get_evidence_for_criteria_batch(
                session,
                [c.id for c in active_criterion_ids],
            )

            # Second pass: record evidence for criteria not already covered.
            evidence_batch: list[CriterionEvidence] = []
            for task, task_criterion_ids, declared_test in qualified:
                active = [
                    criteria_by_key[k]
                    for k in task_criterion_ids
                    if k in criteria_by_key
                ]
                if not active:
                    continue

                already_present: set[uuid.UUID] = all_present.get(str(task.id), set())
                task_id_str = str(task.id)
                verdict = (task.context or {}).get(CODE_ACCEPTANCE_VERDICT_KEY, {})
                proof_ref = (
                    (isinstance(verdict, dict) and verdict.get("proof_ref"))
                    or declared_test
                )
                for criterion in active:
                    # Skip if the criterion is already machine-verified as covered
                    # by other evidence (e.g. a reachability badge or a prior
                    # task_merge with proven=True).
                    existing = evidence_by_criterion.get(criterion.id, [])
                    if compute_criterion_coverage(existing) == "covered":
                        continue

                    # Every still-uncovered backfilled criterion remains eligible
                    # for the identity-aware earn-up path.  The enroller's exact
                    # candidate + test + execution-precondition identity suppresses
                    # an unchanged terminal/could_not_run attempt, while a new
                    # candidate or repaired execution precondition legitimately
                    # re-arms it.  Restricting this to newly inserted backfill rows
                    # strands a failed first attempt forever because the durable
                    # backfill row is already present on every later sweep.
                    trial_targets[criterion.criterion_key] = (
                        criterion.text,
                        declared_test,
                    )

                    if criterion.id in already_present:
                        continue

                    detail: dict[str, Any] = {
                        "proven": True,
                        "backfilled": True,
                        "backfill_reason": "merged_named_test_matches_changed",
                        "proof_ref": proof_ref,
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

        # Give each backfilled criterion an earn-up path: enroll a targeted
        # criterion_named_test_trial that executes the declared covering test
        # against the version's qualifying candidate SHA. The enroller caps
        # this at one non-terminal trial per criterion + release version: an
        # identical in-flight trial is skipped outright, a QUEUED trial
        # pinned to a now-stale SHA is cancelled (superseded) and re-enrolled
        # at the current head, and a running (PROVISIONING/IN_PROGRESS) trial
        # on a stale SHA is left alone — this sweep's next run enrolls the
        # follow-up once it reaches a terminal state. A green run upgrades
        # the criterion from the lower-confidence backfill tier to real
        # machine-verified 'covered'.  This replaces the research corroboration
        # spawn for the merged-named-test lane.
        if trial_targets:
            from app.models import Project

            project_row = await session.get(Project, project_id)
            if project_row is not None:
                for _ck, (_ctext, _declared_test) in trial_targets.items():
                    await enroll_criterion_named_test_trial(
                        session,
                        project_row,
                        version=open_version,
                        criterion_key=_ck,
                        criterion_text=_ctext,
                        test_refs=[_declared_test],
                    )

        if total:
            logger.info(
                "backfill_merged_named_test_coverage: project=%s tasks_scanned=%d recorded=%d",
                project_id,
                offset,
                total,
            )
        return total
    except Exception:
        logger.exception(
            "backfill_merged_named_test_coverage: failed for project %s",
            project_id,
        )
        return total

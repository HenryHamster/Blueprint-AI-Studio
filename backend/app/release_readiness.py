"""Release readiness — qualification record aggregation and formatting.

Provides the operator-facing readiness verdict (whether promotion is ready) based
on recent dry-run qualification history, plus formatting helpers for API responses.

GROUND-TRUTH: the ``would_promote_rate`` signal is a *self-approval* track
record — ``would_promote`` is the qualifier's own confidence >= threshold, so
that rate is just how often the qualifier approved itself. It is NOT a measured
precision on its own.

A real ground truth IS now recorded, however: each promoted verdict's deploy is
resolved post-promotion and stamped onto ``QualificationVerdict.post_promotion_outcome``
(``serving_confirmed`` | ``deploy_failed`` | ``unconfirmed``) by
release_reconciliation. From the resolved subset
(``terminal_status == "promoted"`` and outcome in {serving_confirmed,
deploy_failed}) we derive ``validated_precision = serving_confirmed /
(serving_confirmed + deploy_failed)`` — a true post-promotion precision that
augments (never replaces) the self-approval rate. It is surfaced alongside the
self-approval keys, and ``ground_truth_validated`` flips True ONLY once the
resolved-outcome sample reaches ``_READINESS_MIN_RESOLVED_OUTCOMES``. Below that
the validated precision is still reported but ``ground_truth_validated`` stays
False and the ``caveat`` keeps the honest "self-approval only" framing so no
consumer over-claims on a thin sample.
"""

from __future__ import annotations


import contextlib
import logging
import uuid
from datetime import timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app import release_version
from app.models import (
    AuditFindingResolution,
    Project,
    QualificationVerdict,
    ReleaseVersion,
    Task,
    TaskStatus,
)
from app.promotion_ci_dispatch import promotion_ci_dispatch_gate_verdict
from app.release_scoring import (
    compute_challenged_vs_unchallenged_telemetry,
    _finding_matches_candidate_surface,
    project_audit_score,
    project_promotion_confidence,
)
from app.shared_state import resolve_project_promotion_enabled
from app.time_utils import _utcnow
_MODULE_SIZE_GOVERNANCE_CAP: int = 3550

logger = logging.getLogger(__name__)

# --------------------------------------------------------------------------- #
# v22 low-severity carried-finding backfill constants
# --------------------------------------------------------------------------- #
# All 13 v22 carried low-severity audit findings were fixed in commit
# d2cf1ebb8 / PR #13245 before AuditFindingResolution rows existed.
_V22_BACKFILL_RESOLVING_REF = "d2cf1ebb8"
_V22_BACKFILL_NOTE = (
    "Backfill: all v22 carried low-severity findings were fixed in "
    "commit d2cf1ebb8 / PR #13245 before resolution rows existed."
)
_V22_DATA_INTEGRITY_CRITERION_KEY = "c-1a3ec09d"
# Criterion that tracks the v22 live-verification claim audit (direction d-fd9c793d).
# Five v22 promotion-gated claims were code-verified PASS on 2026-07-07 and
# documented as direction notes; this backfill attaches the formal evidence row
# so evidence_progress.status advances from 'stalled' to 'covered'.
_V22_LIVE_VERIFICATION_CRITERION_KEY = "c-dd953d8c"
_V22_LIVE_VERIFICATION_SOURCE_ID = "live_verification_backfill:v22"
# This is a one-time, historical backfill: it MUST only ever touch release
# version 22 and only its LOW-severity findings.  Two hard guards enforce that:
#   * version_number == 22 — v22 has already promoted (the open version is v23+).
#     Operating on the current open version would fabricate false "resolved"
#     rows for whatever findings that version carries.
#   * severity == "low" — only low-severity findings were fixed in d2cf1ebb8.
#     Never write a resolution row for a gating (high/medium/critical) finding;
#     those must stay visible to _compute_requalification_decision so a genuine
#     carried gating finding cannot be silently suppressed from the promotion gate.
_V22_BACKFILL_VERSION_NUMBER = 22
_V22_BACKFILL_SEVERITY = "low"

# The exact count of v22 carried low-severity findings that were fixed in
# d2cf1ebb8 / PR #13245.  The backfill count guard compares the resolved
# count against this constant and warns on mismatch — a self-reported-vs-expected
# data-integrity check in the same spirit as the metrics-integrity sweep.
_V22_EXPECTED_LOW_SEVERITY_COUNT = 13

# Evergreen-backlog provenance and finding key for count-divergence enrollment.
# When the backfill's self-check detects a mismatch between the resolved count
# and the expected constant, an actionable EvergreenBacklogItem is enrolled
# (deduped, best-effort) so the signal reaches the triage surface instead of
# living only in an unmonitored log line.
_V22_BACKFILL_PROVENANCE = "v22_backfill"
_V22_BACKFILL_COUNT_DIVERGENCE_KEY = "v22_backfill_count_divergence"

# Module-level guard state observable by tests.  Updated at the end of each
# backfill_v22_carried_findings_resolved call against a v22 version; reset
# between tests via _reset_v22_backfill_guard_for_tests().
_v22_backfill_guard_state: dict[str, object] = {}


def get_v22_backfill_guard_state() -> dict[str, object]:
    """Snapshot of the most recent v22 backfill count-guard check (for tests)."""
    return dict(_v22_backfill_guard_state)


def _reset_v22_backfill_guard_for_tests() -> None:
    """Test helper — clear the process-local v22 backfill guard state."""
    global _v22_backfill_guard_state
    _v22_backfill_guard_state = {}


def _v22_count_diverges(
    resolved_count: int,
    expected_count: int = _V22_EXPECTED_LOW_SEVERITY_COUNT,
) -> bool:
    """True when the resolved low-severity count diverges from the expected count."""
    return resolved_count != expected_count


# Resolution states a ledger row may carry.  A high/medium carried finding counts
# as resolved-for-requalification when its ledger row is in EITHER of these.
_RESOLVED_LEDGER_STATES = frozenset({"resolved", "stale"})

# Carried-finding severities that GATE re-qualification readiness.  Low/info
# findings are advisory and ignored by the readiness computation.
_REQUALIFICATION_GATING_SEVERITIES = frozenset({"high", "critical", "medium"})

# Sentinel ledger key used for gating findings that carry no content key (e.g.
# challenger-independent blockers written by _handle_independent_blocker).
# All keyless gating findings for a version collapse to this single ledger row.
# Must stay in sync with the write side in release_qualifier_sweep.
_KEYLESS_FINDING_SENTINEL = "__keyless__"

# Task statuses that count as an in-flight promotion-CI remediation.
_ACTIVE_TASK_STATUSES = (
    TaskStatus.QUEUED,
    TaskStatus.PROVISIONING,
    TaskStatus.IN_PROGRESS,
    TaskStatus.EVALUATING,
)
# Bounded window for the promotion-CI remediation gate. While a remediation fix
# (spawned by the promotion-gate harvest in release_reconciliation) is active and
# was created within this window, re-qualification is deferred so the next
# promotion attempt waits for the fix to land on the integration branch. The
# window is a HARD wedge-guard: a stuck/orphaned remediation task older than this
# stops deferring, so a fix that never lands can never block re-qualify forever.
_PROMOTION_CI_REMEDIATION_GATE_SECONDS = 6 * 3600

# Maps the internal last_qualification status code to a human-readable label
# for the version-lifecycle panel.
_OUTCOME_LABELS: dict[str, str] = {
    "pending": "In progress",
    "dry_run": "Dry-run (would promote)",
    "promoted": "Promoted",
    "not_promoted": "Below threshold",
    "error": "Error",
}

# How many most-recent dry-run-regime verdicts to consider.
_READINESS_HISTORY_WINDOW = 50
# Minimum number of evaluated dry-run-regime verdicts before any "ready" signal.
_READINESS_MIN_VERDICTS = 5
# Minimum run of the MOST-RECENT consecutive clean would-promote verdicts (no
# blocker) — recent stability matters more than a good-then-bad history.
_READINESS_MIN_CLEAN_STREAK = 3
# Minimum would-promote rate across the evaluated window.
_READINESS_MIN_WOULD_PROMOTE_RATE = 0.80
# A blocker code seen at least this many times in the window is "recurring" —
# the qualifier keeps tripping on the same hard problem, so NOT ready.
_READINESS_RECURRING_BLOCKER_MIN = 2
# Confidence-spread ceiling for the advisory "stable" flag (evidence only — not
# a hard readiness gate).
_READINESS_CONFIDENCE_STDEV_MAX = 0.15

# Honest framing of what this signal can and cannot assert. ``would_promote`` is
# the qualifier's own confidence >= threshold, so the rate is self-referential:
# no post-promotion outcome is recorded against which a true precision could be
# computed. Surfaced to every consumer so none of them claims "trustworthy".
_READINESS_GROUND_TRUTH_CAVEAT = (
    "self-approval only — no post-promotion serving outcome has resolved yet, "
    "so this is the rate at which the qualifier approved itself, not a measured "
    "precision"
)

# Minimum number of RESOLVED post-promotion outcomes (serving_confirmed or
# deploy_failed) before the derived ``validated_precision`` is trusted as a real
# ground truth. Below this the precision is still reported but
# ``ground_truth_validated`` stays False and the caveat keeps self-approval framing.
_READINESS_MIN_RESOLVED_OUTCOMES = 5

# Qualification terminal statuses that recorded NO quality finding and must never
# be aggregated as failed attempts in the readiness track record.
# ``infra_failure`` was already excluded; rubric holds complete the set.  A
# rubric_changed / rubric_provenance_unknown attempt did not measure anything
# under the final rubric (its in-flight grade was invalidated by a mid-flight
# acceptance-contract change), so it must not lower the would-promote rate, break
# the clean streak, or surface its blocker as recurring.  Both ``rubric_changed``
# and ``rubric_unknown`` are listed because the latter is the terminal_status
# written for the unknown-provenance hold (see release_qualifier._record_rubric_
# provenance_outcome).
_NON_FINDING_VERDICT_STATUSES: frozenset[str] = frozenset(
    {"infra_failure", "rubric_changed", "rubric_unknown"}
)

# Governance manifest for the promotion-gate readiness block emitted by
# ``compute_promotion_readiness``.  Every field the function returns must have an
# entry here with a named ground-truth source and consumer, or a dated
# retired/pending annotation — so a new field cannot land without declaring who
# reads it.  Mirrors TIMEOUT_RATE_METRIC_MANIFEST.
PROMOTION_READINESS_METRIC_MANIFEST: dict[str, dict] = {
    # ── core readiness verdict ──
    "ready": {
        "ground_truth": "would_promote_rate >= _READINESS_MIN_WOULD_PROMOTE_RATE AND consecutive_clean_passes >= _READINESS_MIN_CLEAN_STREAK AND no recurring blockers",
        "consumer": "admin_release_version router (readiness API response), admin-ui VersionLifecyclePanel",
    },
    "reason": {
        "ground_truth": "human-readable explanation assembled from threshold comparisons",
        "consumer": "admin_release_version router (readiness API response), admin-ui VersionLifecyclePanel",
    },
    "verdicts_evaluated": {
        "ground_truth": "count of QualificationVerdict rows (promotion_enabled=False, not infra_failure) in last _READINESS_HISTORY_WINDOW",
        "consumer": "admin_release_version router (readiness API response)",
    },
    "would_promote_count": {
        "ground_truth": "count of verdicts where QualificationVerdict.would_promote is True",
        "consumer": "admin_release_version router (readiness API response)",
    },
    "would_promote_rate": {
        "ground_truth": "would_promote_count / verdicts_evaluated; QualificationVerdict.would_promote (qualifier confidence >= threshold)",
        "consumer": "admin_release_version router (readiness API response), admin-ui VersionLifecyclePanel (primary self-approval rate metric)",
    },
    "consecutive_clean_passes": {
        "ground_truth": "leading run of QualificationVerdict rows where would_promote=True and blocker is None",
        "consumer": "admin_release_version router (readiness API response), admin-ui VersionLifecyclePanel",
    },
    "consecutive_infra_errors": {
        "ground_truth": "consecutive_infra_errors from most-recent ReleaseVersion.last_qualification when status == 'infra_failure' or 'pending'",
        "consumer": "admin_release_version router (readiness API response)",
    },
    "infra_failure_count": {
        "ground_truth": "count of ReleaseVersion.last_qualification rows with status == 'infra_failure' in last _READINESS_HISTORY_WINDOW",
        "consumer": "admin_release_version router (readiness API response)",
    },
    "confidence": {
        "ground_truth": "mean/stdev/min/max of QualificationVerdict.confidence_score for evaluated window",
        "consumer": "admin_release_version router (readiness API response)",
    },
    "recurring_blockers": {
        "ground_truth": "QualificationVerdict.blocker codes appearing >= _READINESS_RECURRING_BLOCKER_MIN times in the window",
        "consumer": "admin_release_version router (readiness API response)",
    },
    "blocker_counts": {
        "ground_truth": "per-code count of QualificationVerdict.blocker across the evaluated window (excludes ci_unresolved)",
        "consumer": "admin_release_version router (readiness API response)",
    },
    "thresholds": {
        "ground_truth": "static threshold constants (_READINESS_MIN_VERDICTS, _READINESS_MIN_CLEAN_STREAK, etc.)",
        "consumer": "admin_release_version router (readiness API response), admin-ui VersionLifecyclePanel",
    },
    # ── validated precision (ground-truth post-promotion outcomes) ──
    "validated_precision": {
        "ground_truth": "serving_confirmed / (serving_confirmed + deploy_failed) from QualificationVerdict.post_promotion_outcome where terminal_status='promoted' and outcome in {serving_confirmed, deploy_failed}",
        "consumer": "admin_release_version router (readiness API response), admin-ui VersionLifecyclePanel (true post-promotion precision metric)",
    },
    "resolved_outcome_sample": {
        "ground_truth": "count of QualificationVerdict rows with terminal_status='promoted' and resolved post_promotion_outcome",
        "consumer": "admin_release_version router (readiness API response)",
    },
    "serving_confirmed_count": {
        "ground_truth": "count of promoted verdicts where post_promotion_outcome == 'serving_confirmed'",
        "consumer": "admin_release_version router (readiness API response)",
    },
    "deploy_failed_count": {
        "ground_truth": "count of promoted verdicts where post_promotion_outcome == 'deploy_failed'",
        "consumer": "admin_release_version router (readiness API response)",
    },
    "ground_truth_validated": {
        "ground_truth": "resolved_outcome_sample >= _READINESS_MIN_RESOLVED_OUTCOMES",
        "consumer": "admin_release_version router (readiness API response), admin-ui VersionLifecyclePanel (gates trusted-precision display)",
    },
    "caveat": {
        "ground_truth": "_readiness_caveat(validated): 'self-approval only' when ground_truth_validated is False; 'validated precision N%' otherwise",
        "consumer": "admin_release_version router (readiness API response), admin-ui VersionLifecyclePanel",
    },
    # ── regime annotation (_annotate_regime) ──
    "promotion_enabled": {
        "ground_truth": "resolve_project_promotion_enabled(project_id) — runtime config flag",
        "consumer": "admin_release_version router (readiness API response), admin-ui VersionLifecyclePanel",
    },
    "regime": {
        "ground_truth": "'live' when promotion_enabled else 'pre_go_live_calibration'",
        "consumer": "admin_release_version router (readiness API response), admin-ui VersionLifecyclePanel",
    },
    "is_current_readiness": {
        "ground_truth": "not promotion_enabled (False once promotion is live, since the dry-run window is then stale pre-go-live history)",
        "consumer": "admin_release_version router (readiness API response), admin-ui VersionLifecyclePanel",
    },
    "live_regime_calibration_artifact": {
        "ground_truth": "promotion_enabled == True — True iff ready=False was forced by _annotate_regime because promotion went live (not a real track-record failure); False in the dry-run window where ready reflects actual qualification history",
        "consumer": "strategic_spawning._build_cycle_start_reconciliation (cycle-start reconciliation, distinguishes forced-not-ready regime relabel from genuine not-ready verdict without requiring cross-referencing is_current_readiness)",
    },
}


def _readiness_caveat(validated: dict[str, Any]) -> str:
    """Build the ground-truth caveat from the validated-precision aggregate.

    Keeps the honest "self-approval only" framing until enough post-promotion
    outcomes have resolved; once validated, states the measured precision.
    """
    sample = validated["resolved_outcome_sample"]
    if validated["ground_truth_validated"]:
        return (
            f"ground-truth validated — post-promotion serving outcome is "
            f"recorded; validated precision "
            f"{validated['validated_precision']:.2f} over {sample} resolved "
            f"promotion(s) ({validated['serving_confirmed_count']} "
            f"serving-confirmed, {validated['deploy_failed_count']} "
            f"deploy-failed), augmenting the qualifier self-approval rate"
        )
    if sample > 0:
        return (
            f"self-approval only for now — post-promotion outcome IS recorded "
            f"but only {sample} resolved promotion(s) so far (need >= "
            f"{_READINESS_MIN_RESOLVED_OUTCOMES} to validate precision); the "
            f"would-promote rate is the qualifier approving itself, not yet a "
            f"measured precision"
        )
    return _READINESS_GROUND_TRUTH_CAVEAT


async def _compute_validated_precision(
    session: AsyncSession, project_id: uuid.UUID
) -> dict[str, Any]:
    """Derive ground-truth qualifier precision from resolved post-promotion outcomes.

    Considers only verdicts that actually promoted
    (``terminal_status == "promoted"``) whose serving outcome has RESOLVED to
    ``serving_confirmed`` or ``deploy_failed`` (``unconfirmed``/NULL ignored).
    Returns the precision, the resolved sample size, the per-outcome counts, and
    a ``ground_truth_validated`` flag set only when the sample meets the minimum.

    NOTE: this query is intentionally NOT restricted to the dry-run regime —
    promoted verdicts are recorded with ``promotion_enabled`` True, and the
    post-promotion precision is precisely the live-regime signal the dry-run
    window cannot see.
    """
    stmt = select(QualificationVerdict.post_promotion_outcome).where(
        QualificationVerdict.project_id == project_id,
        QualificationVerdict.terminal_status == "promoted",
        QualificationVerdict.post_promotion_outcome.in_(
            ("serving_confirmed", "deploy_failed")
        ),
    )
    outcomes = list((await session.execute(stmt)).scalars().all())
    serving_confirmed = sum(1 for o in outcomes if o == "serving_confirmed")
    deploy_failed = sum(1 for o in outcomes if o == "deploy_failed")
    sample = serving_confirmed + deploy_failed
    validated_precision = round(serving_confirmed / sample, 4) if sample else None
    return {
        "validated_precision": validated_precision,
        "resolved_outcome_sample": sample,
        "serving_confirmed_count": serving_confirmed,
        "deploy_failed_count": deploy_failed,
        "ground_truth_validated": sample >= _READINESS_MIN_RESOLVED_OUTCOMES,
    }


# The readiness window is hardcoded to DRY-RUN-regime verdicts (promotion_enabled
# is False) — sensible pre-go-live calibration, but stale once promotion goes
# live, because live promote/promotion-failed verdicts are recorded with
# promotion ENABLED and never enter this window. So once promotion is effectively
# enabled for the project, this summary is pre-go-live history, NOT current
# readiness, and must be labeled as such instead of frozen on aging dry-runs.
_READINESS_LIVE_REGIME_NOTE = (
    "promotion is live for this project — this is a pre-go-live dry-run "
    "calibration window, not current readiness; it stopped receiving verdicts "
    "when promotion was enabled (live outcomes are recorded with promotion on)"
)


def _annotate_regime(out: dict[str, Any], promotion_enabled: bool) -> dict[str, Any]:
    """Stamp the promotion regime onto a readiness summary.

    ``is_current_readiness`` is True only in the dry-run (pre-go-live) regime the
    window actually measures. Once promotion is effectively enabled the window is
    frozen on stale dry-runs, so we relabel it as a pre-go-live calibration window
    and force ``ready`` False — a stale dry-run streak must never read as a live
    'ready' signal.

    ``live_regime_calibration_artifact`` is True exactly when ``ready=False`` was
    forced by this function because promotion went live (a regime relabel), NOT
    because the qualification track record is genuinely not ready.  Any consumer
    that receives ``ready=False`` can check this flag without cross-referencing
    ``is_current_readiness`` to know whether the not-ready verdict is a regime
    artifact or a real track-record failure.
    """
    out["promotion_enabled"] = promotion_enabled
    out["regime"] = "live" if promotion_enabled else "pre_go_live_calibration"
    out["is_current_readiness"] = not promotion_enabled
    out["live_regime_calibration_artifact"] = promotion_enabled
    if promotion_enabled:
        out["ready"] = False
        out["reason"] = _READINESS_LIVE_REGIME_NOTE
    return out


def format_qualification_for_api(qual: dict[str, Any]) -> dict[str, Any]:
    """Return an operator-facing summary of a ``last_qualification`` record."""
    status = qual.get("status")
    _recon = qual.get("audit_reconciliation")
    _audit_leg_proof = (
        _recon.get("audit_leg_proof") if isinstance(_recon, dict) else None
    )
    _recon_out = (
        {k: v for k, v in _recon.items() if k != "audit_leg_proof"}
        if isinstance(_recon, dict)
        else _recon
    )
    return {
        "status": status,
        "outcome": _OUTCOME_LABELS.get(status, status),
        "confidence": qual.get("confidence"),
        "threshold": qual.get("threshold"),
        "sub_scores": qual.get("sub_scores"),
        # Qualification-launch merge-state evidence.  ``None`` is preserved on
        # legacy records; an object with status=unavailable is not rewritten as
        # a misleading zero.
        "stranded_approved_prs": qual.get("stranded_approved_prs"),
        "blocker": qual.get("blocker"),
        "started_at": qual.get("started_at"),
        "finalized_at": qual.get("finalized_at"),
        # Durable per-attempt evidence fields mirror the immutable verdict row
        # so the current open version keeps the same evidence trail even after
        # later re-qualifications overwrite mutable state.
        "migration_safety": qual.get("migration_safety"),
        "migration_safety_detail": qual.get("migration_safety_detail"),
        "migration_apply_safety": qual.get("migration_apply_safety"),
        "migration_apply_safety_detail": qual.get("migration_apply_safety_detail"),
        "audit_findings": qual.get("audit_findings"),
        "excluded_audit_findings": qual.get("excluded_audit_findings"),
        # Completeness signal: pre-cap totals + truncation flags so the API/UI can
        # tell whether the carried findings lists were clipped at the cap.
        "audit_findings_total": qual.get("audit_findings_total"),
        "audit_findings_truncated": qual.get("audit_findings_truncated"),
        "excluded_audit_findings_total": qual.get("excluded_audit_findings_total"),
        "excluded_audit_findings_truncated": qual.get(
            "excluded_audit_findings_truncated"
        ),
        "challenger_refuted_audit_findings": qual.get(
            "challenger_refuted_audit_findings"
        ),
        "test_erosion_detail": qual.get("test_erosion_detail"),
        "smoketest_detail": qual.get("smoketest_detail"),
        # Per-attempt smoketest enablement flag — False when the smoketest
        # dimension was disabled for this run (the sub_score is then
        # zero-weighted and the 0.0 placeholder should not be shown as an
        # active lane).  None on legacy records that predate this field.
        "smoketest_enabled": qual.get("smoketest_enabled"),
        "requested_probes": qual.get("requested_probes") or [],
        "walkthrough_e2e_task_id": qual.get("walkthrough_e2e_task_id"),
        "walkthrough_e2e_result": qual.get("walkthrough_e2e_result"),
        "audit_reconciliation": _recon_out,
        "audit_leg_proof": _audit_leg_proof,
        "promotion_pr_url": qual.get("promotion_pr_url"),
        "promotion_conflict": qual.get("promotion_conflict"),
        "promotion_error_detail": qual.get("promotion_error_detail"),
        "goal_alignment": qual.get("goal_alignment"),
        "blind_merge_grade": qual.get("blind_merge_grade"),
        # Blind merge-grade goal-coverage verdict (dir d-c0fe439f): the operator
        # sees the goal-coverage status, score, and the single biggest gap.
        "merge_grade_status": qual.get("merge_grade_status"),
        "merge_grade_coverage": qual.get("merge_grade_coverage"),
        "merge_grade_biggest_gap": qual.get("merge_grade_biggest_gap"),
        "merge_grade": qual.get("merge_grade"),
        # Open completion-debt snapshot at qualification time (additive; None when
        # the ledger was unavailable or no debt was open).
        "completion_debt": qual.get("completion_debt"),
        # Enforce-mode confidence preview (observe/off mode only; None when the mode
        # is already enforce, a hard blocker fired, or debt pressure was unavailable).
        "debt_enforce_confidence_preview": qual.get("debt_enforce_confidence_preview"),
        "debt_enforce_weight_preview": qual.get("debt_enforce_weight_preview"),
        # Per-category goal-verification verdicts + residual gap that drove the
        # criterion_coverage gate at finalize time (additive; None when no rubric
        # or grade was issued). Names WHY each category is unmet so operators and
        # agents don't have to dig into the ledger/database.
        "category_verdicts": qual.get("category_verdicts"),
        "residual_uncaptured_gap": qual.get("residual_uncaptured_gap"),
    }


def _sub(verdict: QualificationVerdict, key: str) -> Any:
    """Safely read a key from verdict.sub_scores; returns None for legacy rows."""
    return (verdict.sub_scores or {}).get(key)


def format_verdict_for_api(verdict: QualificationVerdict) -> dict[str, Any]:
    """Return an operator-facing dict for a single QualificationVerdict row.

    Surfaces the durable per-attempt evidence columns (migration-safety
    verdicts + detail, audit findings, excluded findings, erosion / smoketest /
    reconciliation detail, and promotion-attempt context) directly from the
    immutable verdict row, so operators read the attempt's own evidence instead
    of whatever survives in the latest ``last_qualification`` overwrite.  All
    evidence keys are additive and may be ``None`` on legacy rows that predate
    the evidence columns.
    """
    _vrecon = verdict.audit_reconciliation
    _v_audit_leg_proof = (
        _vrecon.get("audit_leg_proof") if isinstance(_vrecon, dict) else None
    )
    _vrecon_out = (
        {k: v for k, v in _vrecon.items() if k != "audit_leg_proof"}
        if isinstance(_vrecon, dict)
        else _vrecon
    )
    sub_scores = verdict.sub_scores
    if isinstance(sub_scores, dict):
        requested_probes = sub_scores.get("requested_probes") or []
        walkthrough_e2e_result = sub_scores.get("walkthrough_e2e_result")
        debt_enforce_confidence_preview = sub_scores.get(
            "debt_enforce_confidence_preview"
        )
        debt_enforce_weight_preview = sub_scores.get("debt_enforce_weight_preview")
        sub_scores = {
            k: v
            for k, v in sub_scores.items()
            if k
            not in {
                "acceptance_aggregate",
                "debt_enforce_confidence_preview",
                "debt_enforce_weight_preview",
                "requested_probes",
                "walkthrough_e2e_result",
            }
        }
    else:
        requested_probes = []
        walkthrough_e2e_result = None
        debt_enforce_confidence_preview = getattr(
            verdict, "debt_enforce_confidence_preview", None
        )
        debt_enforce_weight_preview = getattr(
            verdict, "debt_enforce_weight_preview", None
        )
    return {
        "id": str(verdict.id),
        "release_version_id": str(verdict.release_version_id),
        "candidate_sha": verdict.candidate_sha,
        "confidence": verdict.confidence,
        "sub_scores": sub_scores,
        "stranded_approved_prs": _sub(verdict, "stranded_approved_prs"),
        "blocker": verdict.blocker,
        "requested_probes": requested_probes,
        "walkthrough_e2e_result": walkthrough_e2e_result,
        "audit_missing_reason": verdict.audit_missing_reason,
        "would_promote": verdict.would_promote,
        "promotion_enabled": verdict.promotion_enabled,
        "terminal_status": verdict.terminal_status,
        "outcome": _OUTCOME_LABELS.get(
            verdict.terminal_status, verdict.terminal_status
        ),
        "threshold": verdict.threshold,
        "started_at": verdict.started_at.isoformat() if verdict.started_at else None,
        "finalized_at": verdict.finalized_at.isoformat()
        if verdict.finalized_at
        else None,
        "created_at": verdict.created_at.isoformat(),
        # Post-promotion serving outcome — how this verdict's promotion actually
        # played out (serving_confirmed / deploy_failed / unconfirmed); null
        # until recorded.
        "post_promotion_outcome": verdict.post_promotion_outcome,
        "post_promotion_outcome_at": (
            verdict.post_promotion_outcome_at.isoformat()
            if verdict.post_promotion_outcome_at
            else None
        ),
        # --- Durable per-attempt evidence (additive; nullable on legacy rows) --
        "migration_safety": verdict.migration_safety,
        "migration_safety_detail": verdict.migration_safety_detail,
        "migration_apply_safety": verdict.migration_apply_safety,
        "migration_apply_safety_detail": verdict.migration_apply_safety_detail,
        "audit_findings": verdict.audit_findings,
        "excluded_audit_findings": verdict.excluded_audit_findings,
        "challenger_refuted_audit_findings": verdict.challenger_refuted_audit_findings,
        "test_erosion_detail": verdict.test_erosion_detail,
        "smoketest_detail": verdict.smoketest_detail,
        # Per-attempt smoketest enablement flag read from sub_scores JSON
        # (embedded at verdict-recording time).  None on legacy rows that
        # predate the field; False means the dimension was disabled and the
        # 0.0 sub-score placeholder should not be rendered as an active lane.
        "smoketest_enabled": _sub(verdict, "smoketest_enabled"),
        "audit_reconciliation": _vrecon_out,
        "audit_leg_proof": _v_audit_leg_proof,
        # Blind merge-grade goal-coverage verdict (dir d-c0fe439f).
        "merge_grade_status": verdict.merge_grade_status,
        "merge_grade_coverage": verdict.merge_grade_coverage,
        "merge_grade_biggest_gap": verdict.merge_grade_biggest_gap,
        "merge_grade_detail": verdict.merge_grade_detail,
        "promotion_pr_url": verdict.promotion_pr_url,
        "promotion_conflict": verdict.promotion_conflict,
        "promotion_error_detail": verdict.promotion_error_detail,
        # --- Acceptance-window signal (additive; None on legacy rows without aggregate) -
        # acceptance_window_total / verified / fraction use the write-task-only population:
        # only task types that can declare acceptance evidence count in the denominator.
        # acceptance_window_total_all_merges is the raw landed count for observability.
        "acceptance_window_total": _sub(verdict, "acceptance_window_total"),
        "acceptance_window_verified": _sub(verdict, "acceptance_window_verified"),
        "acceptance_window_fraction": _sub(verdict, "acceptance_window_fraction"),
        "acceptance_window_write_total": _sub(verdict, "acceptance_window_write_total"),
        "acceptance_window_total_all_merges": _sub(verdict, "acceptance_window_total_all_merges"),
        # Additive per-bucket / per-reason breakdown of the write-task window (None on
        # legacy rows without the aggregate). Makes the fraction auditable: operators see
        # which buckets contribute to `verified`, why undeclared merges stayed undeclared,
        # and why each declared-but-unsatisfied verdict failed.
        "acceptance_window_buckets": _sub(verdict, "acceptance_window_buckets"),
        "acceptance_window_undeclared_reasons": _sub(verdict, "acceptance_window_undeclared_reasons"),
        "acceptance_window_declared_unsatisfied_reasons": _sub(verdict, "acceptance_window_declared_unsatisfied_reasons"),
        "acceptance_window_task_records": _sub(verdict, "acceptance_window_task_records"),
        # Goal-alignment cert (additive; None on legacy rows predating migration 0328).
        "goal_alignment": verdict.goal_alignment,
        # Blind merge-grade cert (additive; None on legacy rows predating migration 0327).
        "blind_merge_grade": verdict.blind_merge_grade,
        # Open completion-debt snapshot embedded in sub_scores at verdict-recording time.
        # None on legacy rows or when the ledger was unavailable at finalization.
        "completion_debt": _sub(verdict, "completion_debt"),
        # Observe-mode preview of the enforce-path confidence / weight.
        "debt_enforce_confidence_preview": debt_enforce_confidence_preview,
        "debt_enforce_weight_preview": debt_enforce_weight_preview,
        # Per-category goal-verification verdicts + residual gap persisted on the
        # immutable verdict row (additive; None on legacy rows that predate the
        # columns). Mirrors format_qualification_for_api so the verdict history
        # boundary also names WHY each category is unmet.
        "category_verdicts": verdict.category_verdicts,
        "residual_uncaptured_gap": verdict.residual_uncaptured_gap,
    }


def _empty_readiness(
    thresholds: dict[str, Any],
    *,
    infra_failure_count: int = 0,
    consecutive_infra_errors: int = 0,
    validated: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """The not-ready summary returned when there is no verdict history yet.

    ``validated`` carries the ground-truth post-promotion precision aggregate
    (see :func:`_compute_validated_precision`); the no-history self-approval
    window is empty, but resolved post-promotion outcomes may still exist, so the
    validated keys are surfaced here too to keep the dict shape stable.
    """
    if validated is None:
        validated = {
            "validated_precision": None,
            "resolved_outcome_sample": 0,
            "serving_confirmed_count": 0,
            "deploy_failed_count": 0,
            "ground_truth_validated": False,
        }
    return {
        "ready": False,
        "reason": "no qualification verdicts recorded yet — nothing to evaluate",
        "verdicts_evaluated": 0,
        "would_promote_count": 0,
        "would_promote_rate": 0.0,
        "consecutive_clean_passes": 0,
        "consecutive_infra_errors": consecutive_infra_errors,
        "infra_failure_count": infra_failure_count,
        "confidence": {
            "mean": None,
            "stdev": None,
            "min": None,
            "max": None,
            "stable": False,
        },
        "recurring_blockers": {},
        "blocker_counts": {},
        "thresholds": thresholds,
        "validated_precision": validated["validated_precision"],
        "resolved_outcome_sample": validated["resolved_outcome_sample"],
        "serving_confirmed_count": validated["serving_confirmed_count"],
        "deploy_failed_count": validated["deploy_failed_count"],
        "ground_truth_validated": validated["ground_truth_validated"],
        "caveat": _readiness_caveat(validated),
        "promotion_enabled": False,
        "regime": "pre_go_live_calibration",
        "is_current_readiness": True,
        "live_regime_calibration_artifact": False,
    }


async def get_challenged_vs_unchallenged_telemetry(
    session: AsyncSession,
    project_id: uuid.UUID,
    *,
    window: int = 100,
) -> dict[str, Any]:
    """Query QualificationVerdict history and compare challenged vs unchallenged audits.

    Fetches the most recent ``window`` verdicts for the project and delegates
    to :func:`compute_challenged_vs_unchallenged_telemetry` (pure, no I/O) to
    partition them into challenged vs unchallenged cohorts and compute the
    comparison.  Advisory and read-only — no effect on scoring or promotion.
    """
    stmt = (
        select(QualificationVerdict)
        .where(
            QualificationVerdict.project_id == project_id,
            QualificationVerdict.terminal_status.not_in(
                _NON_FINDING_VERDICT_STATUSES
            ),
        )
        .order_by(QualificationVerdict.created_at.desc())
        .limit(window)
    )
    verdicts = list((await session.execute(stmt)).scalars().all())
    verdict_dicts = [
        {
            "candidate_sha": v.candidate_sha,
            "audit_reconciliation": v.audit_reconciliation,
            "would_promote": v.would_promote,
            "terminal_status": v.terminal_status,
            "post_promotion_outcome": v.post_promotion_outcome,
            "excluded_audit_findings": v.excluded_audit_findings,
        }
        for v in verdicts
    ]
    return compute_challenged_vs_unchallenged_telemetry(verdict_dicts)


async def compute_promotion_readiness(
    session: AsyncSession,
    project_id: uuid.UUID,
    *,
    promotion_enabled: bool | None = None,
) -> dict[str, Any]:
    """Summarize the dry-run qualification track record into a readiness verdict.

    ``promotion_enabled`` is the project's EFFECTIVE live-promotion gate; it is
    resolved from runtime config when not supplied. The dry-run window this
    function evaluates only reflects *current* readiness while promotion is OFF —
    once it is on, the window is frozen pre-go-live history (see
    :func:`_annotate_regime`), so callers get ``regime``/``is_current_readiness``
    flags and a relabeled, not-ready summary rather than a stale 'ready' number.
    """
    if promotion_enabled is None:
        promotion_enabled = resolve_project_promotion_enabled(project_id)
    thresholds: dict[str, Any] = {
        "min_verdicts": _READINESS_MIN_VERDICTS,
        "min_clean_streak": _READINESS_MIN_CLEAN_STREAK,
        "min_would_promote_rate": _READINESS_MIN_WOULD_PROMOTE_RATE,
        "recurring_blocker_min": _READINESS_RECURRING_BLOCKER_MIN,
        "history_window": _READINESS_HISTORY_WINDOW,
    }

    infra_stmt = (
        select(ReleaseVersion.last_qualification)
        .where(
            ReleaseVersion.project_id == project_id,
            ReleaseVersion.last_qualification.is_not(None),
        )
        .order_by(ReleaseVersion.updated_at.desc())
        .limit(_READINESS_HISTORY_WINDOW)
    )
    infra_rows = list((await session.execute(infra_stmt)).scalars().all())
    infra_failure_count = sum(
        1
        for qual in infra_rows
        if isinstance(qual, dict) and qual.get("status") == "infra_failure"
    )
    consecutive_infra_errors = 0
    for qual in infra_rows:
        if not isinstance(qual, dict):
            continue
        if qual.get("status") == "pending":
            consecutive_infra_errors = int(qual.get("prior_infra_streak") or 0)
            break
        if qual.get("status") == "infra_failure":
            consecutive_infra_errors = int(qual.get("consecutive_infra_errors") or 1)
            break

    stmt = (
        select(QualificationVerdict)
        .where(
            QualificationVerdict.project_id == project_id,
            QualificationVerdict.promotion_enabled.is_(False),
            QualificationVerdict.terminal_status.not_in(
                _NON_FINDING_VERDICT_STATUSES
            ),
        )
        .order_by(QualificationVerdict.created_at.desc())
        .limit(_READINESS_HISTORY_WINDOW)
    )
    verdicts = list((await session.execute(stmt)).scalars().all())

    validated = await _compute_validated_precision(session, project_id)

    evaluated = len(verdicts)
    if evaluated == 0:
        out = _empty_readiness(
            thresholds,
            infra_failure_count=infra_failure_count,
            consecutive_infra_errors=consecutive_infra_errors,
            validated=validated,
        )
        if infra_failure_count:
            out["reason"] = (
                "recent qualifications have been infrastructure failures, not quality findings"
            )
        return _annotate_regime(out, promotion_enabled)

    would_promote_count = sum(1 for v in verdicts if v.would_promote)
    would_promote_rate = round(would_promote_count / evaluated, 4)

    consecutive_clean = 0
    for v in verdicts:
        if v.would_promote and not v.blocker:
            consecutive_clean += 1
        else:
            break

    blocker_counts: dict[str, int] = {}
    for v in verdicts:
        if v.blocker and v.blocker != "ci_unresolved":
            blocker_counts[v.blocker] = blocker_counts.get(v.blocker, 0) + 1
    recurring_blockers = {
        code: n
        for code, n in blocker_counts.items()
        if n >= _READINESS_RECURRING_BLOCKER_MIN
    }

    confidences = [float(v.confidence) for v in verdicts]
    conf_mean = round(sum(confidences) / evaluated, 4)
    conf_min = round(min(confidences), 4)
    conf_max = round(max(confidences), 4)
    if evaluated > 1:
        variance = sum((c - conf_mean) ** 2 for c in confidences) / evaluated
        conf_stdev = round(variance**0.5, 4)
    else:
        conf_stdev = 0.0
    conf_stable = conf_stdev <= _READINESS_CONFIDENCE_STDEV_MAX

    reasons: list[str] = []
    if evaluated < _READINESS_MIN_VERDICTS:
        reasons.append(
            f"only {evaluated} dry-run verdict(s) evaluated "
            f"(need >= {_READINESS_MIN_VERDICTS})"
        )
    if consecutive_clean < _READINESS_MIN_CLEAN_STREAK:
        reasons.append(
            f"consecutive-clean streak is {consecutive_clean} "
            f"(need >= {_READINESS_MIN_CLEAN_STREAK})"
        )
    if would_promote_rate < _READINESS_MIN_WOULD_PROMOTE_RATE:
        reasons.append(
            f"would-promote rate is {would_promote_rate:.2f} "
            f"(need >= {_READINESS_MIN_WOULD_PROMOTE_RATE:.2f})"
        )
    if recurring_blockers:
        formatted = ", ".join(
            f"{code}x{n}" for code, n in sorted(recurring_blockers.items())
        )
        reasons.append(f"recurring hard blocker(s): {formatted}")

    ready = not reasons
    if ready:
        reason = (
            f"qualifier self-approval streak clears thresholds: "
            f"{would_promote_count}/{evaluated} would-promote "
            f"(self-approval rate {would_promote_rate:.2f}), {consecutive_clean} "
            f"consecutive clean passes, no recurring blockers. "
            f"NOTE: {_readiness_caveat(validated)}"
        )
    else:
        reason = "; ".join(reasons)

    out = {
        "ready": ready,
        "reason": reason,
        "verdicts_evaluated": evaluated,
        "would_promote_count": would_promote_count,
        "would_promote_rate": would_promote_rate,
        "consecutive_clean_passes": consecutive_clean,
        "consecutive_infra_errors": consecutive_infra_errors,
        "infra_failure_count": infra_failure_count,
        "confidence": {
            "mean": conf_mean,
            "stdev": conf_stdev,
            "min": conf_min,
            "max": conf_max,
            "stable": conf_stable,
        },
        "recurring_blockers": recurring_blockers,
        "blocker_counts": blocker_counts,
        "thresholds": thresholds,
        "validated_precision": validated["validated_precision"],
        "resolved_outcome_sample": validated["resolved_outcome_sample"],
        "serving_confirmed_count": validated["serving_confirmed_count"],
        "deploy_failed_count": validated["deploy_failed_count"],
        "ground_truth_validated": validated["ground_truth_validated"],
        "caveat": _readiness_caveat(validated),
    }
    return _annotate_regime(out, promotion_enabled)


# --------------------------------------------------------------------------- #
# Audit-finding resolution ledger + re-qualification readiness signal
#
# The re-qualification trigger ("all high+medium carried audit findings
# merged-fixed or code-verified stale") needs per-finding resolution state that
# SURVIVES qualification finalize.  ``apply_finding_resolution`` upserts a durable
# ledger row (in its own AuditFindingResolution table, never in last_qualification),
# and ``compute_requalification_readiness`` reads the latest carried findings and
# joins them against the ledger to decide whether every gating finding is resolved.
# --------------------------------------------------------------------------- #


async def apply_finding_resolution(
    session: AsyncSession,
    project_id: uuid.UUID,
    release_version_id: uuid.UUID,
    finding_key: str,
    state: str,
    *,
    severity: str | None = None,
    note: str | None = None,
    resolving_ref: str | None = None,
    verified_against_sha: str | None = None,
) -> AuditFindingResolution:
    """Upsert a resolution-ledger row for one ``(release_version, finding_key)``.

    Latest write wins: a repeat write for the same ``(release_version_id,
    finding_key)`` UPDATES the existing row (state/severity/note/ref/sha +
    ``updated_at``) rather than inserting a duplicate, honoring the unique
    constraint ``uq_auditfindingresolution_version_key``.  This is the seam the
    agent write endpoint calls.  The caller owns the commit.

    The upsert is performed as a dialect-agnostic select-then-update/insert so it
    works identically under the SQLite test engine and production Postgres.
    """
    if state not in _RESOLVED_LEDGER_STATES:
        raise ValueError(
            f"invalid resolution state {state!r}; "
            f"expected one of {sorted(_RESOLVED_LEDGER_STATES)}"
        )

    existing = (
        (
            await session.execute(
                select(AuditFindingResolution).where(
                    AuditFindingResolution.release_version_id == release_version_id,
                    AuditFindingResolution.finding_key == finding_key,
                )
            )
        )
        .scalars()
        .first()
    )

    if existing is not None:
        existing.state = state
        if severity is not None:
            existing.severity = severity
        existing.note = note
        existing.resolving_ref = resolving_ref
        existing.verified_against_sha = verified_against_sha
        existing.updated_at = _utcnow()
        session.add(existing)
        return existing

    row = AuditFindingResolution(
        project_id=project_id,
        release_version_id=release_version_id,
        finding_key=finding_key,
        severity=severity,
        state=state,
        note=note,
        resolving_ref=resolving_ref,
        verified_against_sha=verified_against_sha,
    )
    session.add(row)
    return row


async def _latest_carried_audit_findings(
    session: AsyncSession,
    version: ReleaseVersion,
) -> tuple[list[dict[str, Any]], bool, dict[str, Any]]:
    """Resolve the open version's latest carried audit findings + truncation flag.

    Mirrors the agent qualification fetch: prefer the latest immutable
    ``QualificationVerdict`` row that carries ``audit_findings`` (it survives
    re-qualification overwrites), falling back to the version's mutable
    ``last_qualification.audit_findings`` blob for legacy rows.  The truncation
    flag is read from ``last_qualification`` (the verdict row does not store it as
    a column) so consumers can treat a clipped list conservatively.
    """
    from app.release_qualifier_audit import (  # noqa: PLC0415 — avoid import cycle
        get_latest_qualification_audit_findings,
    )

    from app.qualification_state_resolver import (
        qualification_read_provenance,
        resolve_newest_qualification_verdict,
    )

    selected_verdict = await resolve_newest_qualification_verdict(session, version)
    verdict = (
        selected_verdict
        if selected_verdict is not None and selected_verdict.audit_findings is not None
        else await resolve_newest_qualification_verdict(
            session, version, require_audit_findings=True
        )
    )
    findings = get_latest_qualification_audit_findings(version, verdict)
    qual = version.last_qualification or {}
    truncated = (
        bool(qual.get("audit_findings_truncated")) if isinstance(qual, dict) else False
    )
    return findings, truncated, qualification_read_provenance(version, selected_verdict)


async def _has_active_promotion_ci_remediation(
    session: AsyncSession,
    project_id: uuid.UUID,
    version_id: uuid.UUID,
) -> bool:
    """True iff a promotion-CI remediation fix task for this version is in flight.

    The promotion-gate harvest
    (``release_reconciliation._harvest_promotion_ci_failures``) spawns an
    auto-remediation task tagged with ``promotion_ci_remediation_version_id``.
    While such a task is active AND was created within
    ``_PROMOTION_CI_REMEDIATION_GATE_SECONDS``, re-qualification is deferred so
    the fix lands on the integration branch before the next promotion attempt.
    The window bound is a hard wedge-guard — a stuck task older than it stops
    deferring, so a fix that never lands cannot block re-qualify indefinitely.
    """
    cutoff = _utcnow() - timedelta(seconds=_PROMOTION_CI_REMEDIATION_GATE_SECONDS)
    rows = (
        (
            await session.execute(
                select(Task).where(
                    Task.project_id == project_id,
                    Task.auto_remediation.is_(True),
                    Task.status.in_(_ACTIVE_TASK_STATUSES),
                    Task.created_at >= cutoff,
                )
            )
        )
        .scalars()
        .all()
    )
    target = str(version_id)
    for t in rows:
        ctx = t.context or {}
        if (
            ctx.get("promotion_ci_remediation")
            and str(ctx.get("promotion_ci_remediation_version_id") or "") == target
        ):
            return True
    return False


def _compute_requalification_decision(
    carried: list[dict[str, Any]],
    truncated: bool,
    ledger_states: dict[str, str],
    open_version_id: str,
    *,
    policy: dict[str, Any] | None = None,
    candidate_surface_paths: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Pure re-qualification readiness decision (no I/O; testable without a DB).

    ``ledger_states`` maps finding_key → resolution state string (e.g. "resolved",
    "stale"). Any key absent from the map is treated as "unresolved".

    The projected audit score is computed over every carried finding, not just the
    high/medium gating subset. Resolution state removes deductions from resolved
    or stale findings, while low/info findings continue to count. Re-qualification
    is ready only when the gating findings are resolved and the projected score
    reaches the live promotion threshold.

    Truncation-safe: when ``truncated`` is True, the carried list was clipped at
    the cap and hidden findings may exist. If visible gating findings are resolved
    and the visible projection reaches the threshold, we still allow
    re-qualification — a fresh run re-scores the full set. A projection below the
    threshold never claims readiness, even on a truncated list.
    """
    valid_findings = [f for f in carried if isinstance(f, dict)]
    stale_keys = {
        str(finding.get("key") or _KEYLESS_FINDING_SENTINEL)
        for finding in valid_findings
        if candidate_surface_paths is not None
        and not _finding_matches_candidate_surface(finding, candidate_surface_paths)
    }
    resolved_keys = {
        str(key)
        for key, state in ledger_states.items()
        if state in _RESOLVED_LEDGER_STATES
    } | stale_keys
    projected_audit = project_audit_score(
        valid_findings,
        resolved_finding_keys=resolved_keys,
        candidate_surface_paths=candidate_surface_paths,
    )
    projection = project_promotion_confidence(
        projected_audit,
        policy,
    )

    gating: list[dict[str, Any]] = []
    for f in valid_findings:
        severity = str(f.get("severity") or "").lower()
        if severity not in _REQUALIFICATION_GATING_SEVERITIES:
            continue
        key = str(f.get("key") or _KEYLESS_FINDING_SENTINEL)
        state_val = ledger_states.get(key)
        state = state_val if state_val in _RESOLVED_LEDGER_STATES else "unresolved"
        if state == "unresolved" and key in stale_keys:
            state = "stale"
        gating.append(
            {
                "key": key,
                "severity": severity,
                "summary": f.get("summary"),
                "state": state,
            }
        )

    gating_total = len(gating)
    gating_resolved = sum(1 for g in gating if g["state"] in _RESOLVED_LEDGER_STATES)
    all_resolved = gating_resolved == gating_total
    projected_ready = bool(projection["projected_promotion_ready"])

    if truncated and not all_resolved:
        ready = False
        reason = (
            "carried audit findings were truncated at the cap — readiness cannot "
            f"be asserted on a partial list ({gating_resolved}/{gating_total} "
            "visible gating findings resolved)"
        )
    elif not all_resolved:
        ready = False
        reason = (
            f"{gating_total - gating_resolved} of {gating_total} carried "
            "high/medium audit finding(s) still unresolved"
        )
    elif not projected_ready:
        ready = False
        reason = (
            f"all {gating_total} carried high/medium finding(s) are resolved or "
            f"code-verified stale, but projected audit is "
            f"{projected_audit:.4f} vs required {projection['required_audit_score']:.4f}; "
            f"projected confidence is {projection['projected_confidence']:.4f} "
            f"vs threshold {projection['promotion_confidence_threshold']:.4f}"
        )
    elif truncated:
        # All visible gating findings resolved. Even though the carried list was
        # clipped at the cap, allow re-qualification — the next run re-scores the
        # full finding set. Blocking forever on truncated-but-resolved findings
        # permanently wedges the auto-redrive.
        ready = True
        reason = (
            f"all {gating_total} visible high/medium finding(s) resolved or "
            "code-verified stale; note: carried list was truncated at the cap — "
            "re-qualification will re-score the full finding set"
        )
    elif gating_total == 0:
        ready = True
        reason = "no carried high/medium audit findings — ready to re-qualify"
    elif all_resolved:
        ready = True
        reason = (
            f"all {gating_total} carried high/medium audit finding(s) resolved or "
            "code-verified stale — ready to re-qualify"
        )
    else:
        ready = False
        reason = (
            f"{gating_total - gating_resolved} of {gating_total} carried "
            "high/medium audit finding(s) still unresolved"
        )

    return {
        "requalification_ready": ready,
        "open_version_id": open_version_id,
        "audit_findings_truncated": truncated,
        "gating_total": gating_total,
        "gating_resolved": gating_resolved,
        "finding_total": len(valid_findings),
        "resolved_finding_total": sum(
            1
            for finding in valid_findings
            if str(finding.get("key") or _KEYLESS_FINDING_SENTINEL) in resolved_keys
        ),
        "projected_audit_score": projected_audit,
        "required_audit_score": projection["required_audit_score"],
        "projected_confidence": projection["projected_confidence"],
        "promotion_confidence_threshold": projection[
            "promotion_confidence_threshold"
        ],
        "projected_promotion_ready": projected_ready,
        "reason": reason,
        "findings": gating,
    }


async def auto_mark_stale_audit_findings(
    session: AsyncSession,
    project_id: uuid.UUID,
    release_version_id: uuid.UUID,
    candidate_surface_paths: dict[str, str] | None,
) -> int:
    """Persist stale rows for carried findings disproven by a complete tree."""
    if not isinstance(candidate_surface_paths, dict) or not candidate_surface_paths:
        return 0

    version = await session.get(ReleaseVersion, release_version_id)
    if version is None or version.project_id != project_id:
        return 0
    carried, truncated, _ = await _latest_carried_audit_findings(session, version)
    if truncated:
        return 0

    rows = (
        await session.execute(
            select(AuditFindingResolution).where(
                AuditFindingResolution.release_version_id == release_version_id
            )
        )
    ).scalars().all()
    resolved = {
        row.finding_key for row in rows if row.state in _RESOLVED_LEDGER_STATES
    }
    stale = {
        str(finding.get("key") or _KEYLESS_FINDING_SENTINEL): finding
        for finding in carried
        if isinstance(finding, dict)
        and str(finding.get("key") or _KEYLESS_FINDING_SENTINEL) not in resolved
        and not _finding_matches_candidate_surface(finding, candidate_surface_paths)
    }
    for key, finding in stale.items():
        path = str(finding.get("file") or finding.get("path") or "").strip()
        current_blob = candidate_surface_paths.get(path) if path else None
        await apply_finding_resolution(
            session, project_id, release_version_id, key, "stale",
            severity=str(finding.get("severity") or "").lower() or None,
            note="Carried finding blob changed on the integration branch.",
            resolving_ref="integration-surface",
            verified_against_sha=current_blob,
        )
    if stale:
        await session.commit()
    return len(stale)


async def compute_requalification_readiness(
    session: AsyncSession,
    project_id: uuid.UUID,
    *,
    candidate_surface_paths: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Decide whether the project's open version is ready to re-qualify.

    Resolves the LATEST qualification's carried audit findings for the open
    release version (preferring the latest QualificationVerdict row, falling back
    to ``last_qualification``), filters to the gating severities (critical/high +
    medium — low/info are advisory and ignored), and joins each against the
    resolution ledger by ``finding_key`` scoped to that ``release_version``.

    ``requalification_ready`` is TRUE when every gating carried finding has a
    ledger row in state ``resolved`` OR ``stale`` and the full-list projected
    confidence reaches the live promotion threshold. Convention for the
    zero-gating-findings case: the score still decides readiness.

    Truncation-safe: if the carried list was clipped at the cap
    (``audit_findings_truncated``) and any VISIBLE gating finding is unresolved,
    readiness is NOT claimed on the partial list. If every visible gating finding
    is resolved and the visible projection reaches the threshold, readiness IS
    claimed despite truncation (with a re-score caveat in the reason) — the next
    qualification run re-scores the full finding set.
    ``audit_findings_truncated`` is always surfaced so consumers can treat
    readiness conservatively.

    Returns a dict shaped:
        {
          "requalification_ready": bool,
          "open_version_id": str | None,
          "audit_findings_truncated": bool,
          "gating_total": int,
          "gating_resolved": int,
          "finding_total": int,
          "resolved_finding_total": int,
          "projected_audit_score": float,
          "required_audit_score": float,
          "projected_confidence": float,
          "promotion_confidence_threshold": float,
          "reason": str,
          "findings": [
            {"key", "severity", "summary", "state": resolved|stale|unresolved}, ...
          ],
        }
    ``requalification_ready`` is False with an explanatory reason when there is
    no open version (nothing to re-qualify).
    """
    version = await release_version.current_open_version(session, project_id)
    if version is None:
        return {
            "requalification_ready": False,
            "open_version_id": None,
            "audit_findings_truncated": False,
            "gating_total": 0,
            "gating_resolved": 0,
            "finding_total": 0,
            "resolved_finding_total": 0,
            "projected_audit_score": None,
            "required_audit_score": None,
            "projected_confidence": None,
            "promotion_confidence_threshold": None,
            "projected_promotion_ready": False,
            "reason": "no open release version — nothing to re-qualify",
            "findings": [],
        }

    # Gate: while a promotion-CI remediation fix (spawned by the promotion-gate
    # harvest for the LAST attempt's CI failures) is in flight, defer
    # re-qualification so the fix lands on the integration branch before the next
    # promotion attempt re-runs the same red CI. Bounded window (see helper) so a
    # stuck remediation can never wedge re-qualify.
    if await _has_active_promotion_ci_remediation(session, project_id, version.id):
        return {
            "requalification_ready": False,
            "open_version_id": str(version.id),
            "audit_findings_truncated": False,
            "gating_total": 0,
            "gating_resolved": 0,
            "finding_total": 0,
            "resolved_finding_total": 0,
            "projected_audit_score": None,
            "required_audit_score": None,
            "projected_confidence": None,
            "promotion_confidence_threshold": None,
            "projected_promotion_ready": False,
            "reason": (
                "promotion-CI remediation in flight — a fix for the last "
                "promotion attempt's CI failures is being applied to the "
                "integration branch; deferring re-qualification until it lands"
            ),
            "findings": [],
        }

    carried, truncated, provenance = await _latest_carried_audit_findings(
        session, version
    )

    # Load the ledger for this version once, keyed by finding_key.
    ledger_rows = list(
        (
            await session.execute(
                select(AuditFindingResolution).where(
                    AuditFindingResolution.release_version_id == version.id
                )
            )
        )
        .scalars()
        .all()
    )
    ledger_states: dict[str, str] = {row.finding_key: row.state for row in ledger_rows}

    # Resolve the same live rubric used by qualification so the readiness report
    # cannot drift when an operator changes the promotion threshold or weights.
    from app.release_qualifier import _load_policy

    project = await session.get(Project, project_id)
    policy = _load_policy(project)

    result = _compute_requalification_decision(
        carried,
        truncated,
        ledger_states,
        str(version.id),
        policy=policy,
        candidate_surface_paths=candidate_surface_paths,
    )
    result["qualification_provenance"] = provenance
    return result


# Gate-state vocabulary emitted by ``compute_promotion_ci_green_gate``.  The
# promotion_failed re-driver holds on ``red`` / unresolvable ``head_sha``, but
# must NOT require ``green``: an ordinary integration HEAD has no ci.yml run.
_PROMOTION_CI_GATE_STATES = frozenset(
    "green green_rehearsal pending red cancelled dispatched dispatch_rejected "
    "no_signal unavailable".split()
)

# GitHub ``workflow_run.event`` values whose run shape is the one that actually
# gates a merge.  A ``pull_request`` run is the ONLY shape that reproduces the
# promotion PR's checkout, so it is the only shape whose success predicts the
# promotion PR's verdict.
#
# WHY THIS DISTINCTION EXISTS.  ci.yml can be reached two ways: a real
# ``pull_request`` into ``main`` (the promotion PR) and a ``workflow_dispatch``
# on the integration branch (what this gate fires when the HEAD has no run).
# They are NOT the same experiment:
#
#   * ``actions/checkout@v4`` is bare in every ci.yml job (no ``ref``, no
#     ``fetch-depth``), so it checks out ``github.ref``.  On ``pull_request``
#     that is ``refs/pull/N/merge`` — a DETACHED synthetic merge of head into
#     base.  On ``workflow_dispatch`` it is ``refs/heads/<branch>`` — the branch
#     tip, with a local branch and an ``origin/<branch>`` remote-tracking ref.
#   * So the TREE differs whenever the base has commits the head lacks (the
#     dispatch never tests the merge result), and the REF INVENTORY differs
#     always (``origin/automain`` / ``automain`` resolve on a dispatch and do
#     not exist on a PR run, where HEAD is the only ref).
#   * Code that resolves a mainline ref by name therefore takes a different
#     branch under each shape — and the test container the backend shards use
#     binds the workspace into a ROOT container the runner user owns, so any
#     git read reached without ``-c safe.directory=`` dies with exit 128.
#
# Confirmed instance, 2026-08-25, one tree: ``workflow_dispatch`` on the
# integration branch concluded SUCCESS at 10:13 UTC; the ``pull_request`` run on
# the release branch concluded FAILURE at 10:41 UTC (backend shards 1 and 5, four
# tests in ``testing/test_size_limits_checker.py``, "git log exited 128").  Same
# code, opposite verdicts, 28 minutes apart.  The pipeline read the 10:13 green
# as permission to cut the promotion PR, and the PR was closed and its branch
# deleted when the 10:41 run went red.
#
# ``pull_request_target`` is included for completeness: it also runs on a real
# pull-request event.  It is not used by ci.yml today.
_PROMOTION_EQUIVALENT_CI_EVENTS = frozenset({"pull_request", "pull_request_target"})

# Gate states meaning "a full ci.yml matrix ran to completion and passed".  This
# is the INTEGRATION-BRANCH HEALTH question ("is the branch broken right now"),
# which a rehearsal answers perfectly well and which the drift sentinel asks.  It
# is deliberately NOT the promotion question — see
# :func:`promotion_ci_gate_proves_promotion`.
PROMOTION_CI_GREEN_STATES = frozenset({"green", "green_rehearsal"})


def promotion_ci_gate_proves_promotion(gate: dict[str, Any] | None) -> bool:
    """True only when the gate's evidence PREDICTS the promotion PR's verdict.

    The one seam callers should use before treating a green gate as authority to
    cut (or to keep) a promotion PR.  Requires BOTH a ``green`` state and the
    ``promotion_equivalent`` flag, so a future caller cannot regress this by
    string-matching ``gate_state == "green"`` against a rehearsal verdict.

    A ``green_rehearsal`` returns False.  That is not pessimism: a rehearsal
    proves the branch is not obviously broken, and nothing more.  Use
    :data:`PROMOTION_CI_GREEN_STATES` when the branch-health question is the one
    being asked.
    """
    if not isinstance(gate, dict):
        return False
    return (
        gate.get("gate_state") == "green"
        and gate.get("promotion_equivalent") is True
    )


# Branch-level dispatch coalescing window for the promotion CI gate.  ci.yml is
# keyed to a branch-level ``cancel-in-progress`` concurrency group, so a fresh
# dispatch for a NEW integration HEAD cancels whatever full-suite run is already
# in flight on the branch.  On an actively-merging branch that churns full
# 8-shard dispatches (each new HEAD's dispatch kills the prior run before it
# concludes) and starves the branch of a green conclusion — the very signal the
# standing drift sentinel needs.  When a ci.yml run is already queued/in_progress
# on the branch within this window, the gate waits for it instead of dispatching.
_PROMOTION_CI_BRANCH_COALESCE_WINDOW_SECONDS = 60 * 60


def _promotion_ci_gate_verdict(
    gate_state: str,
    *,
    head_sha: str | None = None,
    run_id: Any = None,
    html_url: str | None = None,
    reason: str = "",
    ci_event: str | None = None,
) -> dict[str, Any]:
    """Small, stable verdict dict for :func:`compute_promotion_ci_green_gate`.

    ``ci_event`` is the GitHub ``workflow_run.event`` of the run this verdict was
    derived from (``None`` when no run backs the verdict).  The two derived
    booleans are computed here, in ONE place, so every verdict carries the same
    semantics and no caller has to re-derive them from the state string:

      * ``full_matrix_passed`` — a full ci.yml matrix ran and passed.  Answers
        "is the integration branch healthy right now".
      * ``promotion_equivalent`` — the run shape matches the one a promotion PR
        gets, so its verdict PREDICTS the promotion PR's.  Answers "is it safe to
        cut the promotion PR".  Fail-closed: an absent or unrecognised
        ``ci_event`` is never promotion-equivalent, because a run whose shape we
        cannot identify cannot be claimed to reproduce anything.
    """
    return {
        "gate_state": gate_state,
        "head_sha": head_sha,
        "run_id": run_id,
        "html_url": html_url,
        "reason": reason,
        "ci_event": ci_event,
        "full_matrix_passed": gate_state in PROMOTION_CI_GREEN_STATES,
        "promotion_equivalent": (
            gate_state == "green" and ci_event in _PROMOTION_EQUIVALENT_CI_EVENTS
        ),
    }


async def compute_promotion_ci_green_gate(
    session: AsyncSession,
    project_id: uuid.UUID,
    *,
    dispatch: bool = True,
    dispatch_on_cancelled: bool = True,
) -> dict[str, Any]:
    """Resolve whether the integration-branch HEAD has a green full-suite CI run.

    This is the promotion_failed-specific full-matrix green gate.  It resolves the
    project's GitHub creds, the integration branch, and the current integration
    HEAD sha, then looks up the latest ``ci.yml`` Actions run for that HEAD sha via
    :func:`app.github.get_workflow_run_for_sha` (which filters by ``head_sha``, so a
    returned run is always FOR the current HEAD).

    Returns a small verdict dict with ``gate_state``, HEAD/run identity, and a
    reason.  When a dispatch is attempted it also preserves the structured
    dispatch outcome, retry authority, and same-authority suppression key.
    ``gate_state`` is one of:

      * ``green``      — a COMPLETED ``ci.yml`` run for the current HEAD concluded
                         success ON A ``pull_request`` RUN — the same run shape a
                         promotion PR gets, so this verdict predicts the
                         promotion PR's verdict
      * ``green_rehearsal`` — a COMPLETED ``ci.yml`` run for the current HEAD
                         concluded success, but on some OTHER event shape
                         (``workflow_dispatch``, ``push``, …).  The branch is not
                         obviously broken; this does NOT predict the promotion
                         PR.  See ``_PROMOTION_EQUIVALENT_CI_EVENTS`` for the
                         mechanism and the confirmed 2026-08-25 counter-example.
      * ``pending``    — a run for HEAD is queued / in_progress (not yet concluded)
      * ``red``        — a run for HEAD concluded non-success
      * ``cancelled``  — a run for HEAD concluded cancelled and
                         ``dispatch_on_cancelled=False`` (surfaced as a distinct
                         signal; the caller decides whether a fresh dispatch is
                         warranted — used by the drift sentinel to stop churn)
      * ``dispatched`` — no run existed for HEAD, so one was just dispatched
      * ``dispatch_rejected`` — terminal rejection; do not churn the same tuple
      * ``no_signal``  — ``dispatch=False`` and no run exists for HEAD, or a
                         cancelled run exists with ``dispatch=False``
      * ``unavailable``— GitHub creds / HEAD / API unavailable, dispatch was
                         ambiguous, or an error occurred

    When ``dispatch=True`` and there is no run for the current HEAD, first checks
    whether a ci.yml run is already queued/in-progress ANYWHERE on the
    integration branch within ``_PROMOTION_CI_BRANCH_COALESCE_WINDOW_SECONDS``.
    Because ci.yml's concurrency policy is keyed on the branch ref
    (``cancel-in-progress``), a fresh dispatch for a new HEAD would cancel the
    in-flight run before it concludes — on an actively-merging branch that
    churns full 8-shard dispatches and starves the branch of a green conclusion.
    When such a run exists the gate returns ``pending`` (coalescing onto it)
    instead of dispatching; otherwise it dispatches a fresh ``ci.yml`` run on the
    integration branch (the full 8-shard backend + frontend + agent matrix, since
    ci.yml's ``detect`` job forces all-true on a ``workflow_dispatch``) and
    returns ``dispatched``.  A completed run whose conclusion is ``cancelled`` is
    treated as absent when ``dispatch=True`` (the concurrency policy routinely
    cancels superseded integration-branch runs), UNLESS ``dispatch_on_cancelled=False``,
    in which case it surfaces as ``cancelled`` without dispatching so a caller
    that wants to avoid dispatch churn can react instead of re-dispatching the
    full matrix on every sweep.

    TWO QUESTIONS, TWO ANSWERS.  Every verdict carries two derived booleans so a
    caller states which question it is asking instead of string-matching
    ``"green"`` and getting whichever answer happens to be there:

      * ``full_matrix_passed`` — "is the integration branch healthy right now?"
        True for ``green`` AND ``green_rehearsal``.  This is the drift sentinel's
        question, and a rehearsal answers it: the sentinel's own red incidents
        also come from rehearsal runs, so retiring one with the other is
        symmetric, not a false clear.
      * ``promotion_equivalent`` — "will the promotion PR's CI agree?"  True only
        for ``green``.  Use :func:`promotion_ci_gate_proves_promotion` rather
        than reading the flag directly.

    Anything that cuts, keeps, or force-lands a promotion PR must consult the
    SECOND one.  A dispatch that reads back green is a rehearsal on a different
    checkout, and the pipeline treating it as promotion evidence is why
    promotions were being cut into a red gate and then closed with their branch
    deleted.

    IMPORTANT: this performs a GitHub round-trip and is DELIBERATELY separate from
    :func:`compute_requalification_readiness`, which stays DB-only and
    side-effect-free.  Never raises — any failure degrades to ``unavailable``.
    """
    from app import github  # noqa: PLC0415 — avoid import cycle
    from app.github_checks import CI_WORKFLOW_FILE  # noqa: PLC0415
    from app.models import Project  # noqa: PLC0415
    from app.release_promotion import (  # noqa: PLC0415
        _lts_branch_names,
        _resolve_github_with_session,
    )

    try:
        project = await session.get(Project, project_id)
        if project is None:
            return _promotion_ci_gate_verdict(
                "unavailable", reason="project not found"
            )
        resolved = await _resolve_github_with_session(session, project)
        if resolved is None:
            return _promotion_ci_gate_verdict(
                "unavailable", reason="GitHub credentials unavailable"
            )
        token, owner, repo = resolved
        integration_branch, _promotion_target = _lts_branch_names(project)
        head_sha = await github.get_branch_sha(token, owner, repo, integration_branch)
        if not head_sha:
            return _promotion_ci_gate_verdict(
                "unavailable",
                reason=f"could not resolve {integration_branch} HEAD sha",
            )

        run = await github.get_workflow_run_for_sha(
            token,
            owner,
            repo,
            head_sha,
            workflow_file=CI_WORKFLOW_FILE,
            project_id=str(project_id),
        )
        if run is not None:
            run_id = run.get("run_id")
            html_url = run.get("html_url") or ""
            status_ = run.get("status")
            conclusion = run.get("conclusion")
            ci_event = run.get("event")
            if status_ == "completed":
                if conclusion == "success":
                    # A SUCCESS is only as strong as the run shape that produced
                    # it.  Split the two, rather than reporting both as ``green``
                    # and hoping downstream reads the fine print.
                    if ci_event in _PROMOTION_EQUIVALENT_CI_EVENTS:
                        return _promotion_ci_gate_verdict(
                            "green",
                            head_sha=head_sha,
                            run_id=run_id,
                            html_url=html_url,
                            ci_event=ci_event,
                            reason=(
                                "ci.yml run for integration HEAD concluded "
                                f"success on a {ci_event} run — the same run "
                                "shape a promotion PR gets"
                            ),
                        )
                    return _promotion_ci_gate_verdict(
                        "green_rehearsal",
                        head_sha=head_sha,
                        run_id=run_id,
                        html_url=html_url,
                        ci_event=ci_event,
                        reason=(
                            "ci.yml run for integration HEAD concluded success, "
                            f"but on a {ci_event or 'unknown'} run — a rehearsal "
                            "whose checkout shape differs from a promotion PR's "
                            "(branch tip vs refs/pull/N/merge), so it shows the "
                            "branch is not broken but does NOT predict the "
                            "promotion PR's verdict"
                        ),
                    )
                if conclusion == "cancelled":
                    if not dispatch_on_cancelled:
                        return _promotion_ci_gate_verdict(
                            "cancelled",
                            head_sha=head_sha,
                            run_id=run_id,
                            html_url=html_url,
                            ci_event=ci_event,
                            reason=(
                                "ci.yml run for integration HEAD was cancelled — "
                                "surfaced as a distinct signal; caller decides "
                                "whether a fresh dispatch is warranted"
                            ),
                        )
                    if not dispatch:
                        return _promotion_ci_gate_verdict(
                            "no_signal",
                            head_sha=head_sha,
                            run_id=run_id,
                            html_url=html_url,
                            ci_event=ci_event,
                            reason=(
                                "ci.yml run for integration HEAD was cancelled "
                                "(read-only gate, no run dispatched)"
                            ),
                        )
                    # dispatch=True: treat the cancelled run as absent and
                    # fall through to the dispatch block below.
                else:
                    return _promotion_ci_gate_verdict(
                        "red",
                        head_sha=head_sha,
                        run_id=run_id,
                        html_url=html_url,
                        ci_event=ci_event,
                        # DELIBERATELY ASYMMETRIC: a rehearsal FAILURE stays
                        # ``red``.  The shapes differ, so a rehearsal success
                        # cannot be promoted to proof — but a full matrix that
                        # went red found something, and failing closed on it
                        # costs a re-check while failing open costs a deleted
                        # promotion PR.  Evidence strength is not symmetric
                        # between "it passed" and "it broke".
                        reason=(
                            "ci.yml run for integration HEAD concluded "
                            f"{conclusion or 'non-success'}"
                            + (f" on a {ci_event} run" if ci_event else "")
                        ),
                    )
            else:
                # queued / in_progress / waiting / requested — a run is in flight.
                return _promotion_ci_gate_verdict(
                    "pending",
                    head_sha=head_sha,
                    run_id=run_id,
                    html_url=html_url,
                    ci_event=ci_event,
                    reason=f"ci.yml run for integration HEAD is {status_ or 'in flight'}",
                )

        # No run exists for the current HEAD.
        if not dispatch:
            return _promotion_ci_gate_verdict(
                "no_signal",
                head_sha=head_sha,
                reason=(
                    "no ci.yml run for integration HEAD "
                    "(read-only gate, no run dispatched)"
                ),
            )

        # Branch-level dispatch coalescing.  ci.yml is keyed to a branch
        # ``cancel-in-progress`` concurrency group, so dispatching a fresh run
        # for this NEW HEAD would cancel whatever full-suite run is already in
        # flight on the integration branch.  On an actively-merging branch that
        # churns 8-shard dispatches (each new HEAD's dispatch kills the prior
        # run before it concludes) and starves the branch of a green conclusion.
        # If a run is already queued/in_progress within the coalesce window,
        # wait for it to conclude instead of cancelling it — the sentinel reads
        # ``pending`` and re-checks once the run resolves.
        inflight = await github.get_latest_inflight_workflow_run_for_branch(
            token,
            owner,
            repo,
            integration_branch,
            workflow_file=CI_WORKFLOW_FILE,
            project_id=str(project_id),
            max_age_seconds=_PROMOTION_CI_BRANCH_COALESCE_WINDOW_SECONDS,
        )
        if inflight is not None:
            return _promotion_ci_gate_verdict(
                "pending",
                head_sha=head_sha,
                run_id=inflight["run_id"],
                html_url=inflight["html_url"],
                reason=(
                    "a ci.yml run is already in flight on the integration "
                    "branch — coalescing onto it instead of cancelling it "
                    "with a fresh dispatch"
                ),
            )

        dispatch_outcome = await github.dispatch_workflow_run_outcome(
            token,
            owner,
            repo,
            CI_WORKFLOW_FILE,
            integration_branch,
            project_id=str(project_id),
        )
        return promotion_ci_dispatch_gate_verdict(
            dispatch_outcome,
            owner=owner,
            repo=repo,
            workflow_file=CI_WORKFLOW_FILE,
            ref=integration_branch,
            head_sha=head_sha,
        )
    except Exception:
        logger.exception(
            "[PROMOTION_CI_GATE] compute failed for project %s", project_id
        )
        return _promotion_ci_gate_verdict(
            "unavailable", reason="error computing promotion CI green gate"
        )


async def compute_qualification_trigger_state(
    session: AsyncSession,
    project_id: uuid.UUID,
) -> dict[str, Any]:
    """Classify whether the open version's INITIAL qualification trigger is armed.

    This is a cheap, DB-only predicate (no GitHub round-trips) that answers
    'why hasn't the initial qualification fired / would it fire now'.  It is
    the single source of truth for the initial-trigger decision so the sweep
    function and any future read-surface cannot drift apart.

    Returns a dict shaped::

        {
          "trigger_state": "armed" | "blocked",
          "open_version_id": str | None,
          "reason": str,   # machine-readable code
          "summary": str,  # human-readable explanation
        }

    ``trigger_state`` is ``"armed"`` exactly when:
      - an open release version exists for the project, AND
      - the version is in the "stabilize" phase, AND
      - no prior qualification attempt exists (``last_qualification is None``
        AND ``qualification_attempts == 0``).

    There is no pre-qualification criterion-coverage precondition: the
    acceptance contract is enforced INSIDE qualification (the sha-keyed ledger
    gate in :func:`app.release_scoring.compute_composite`), so the initial
    trigger arms on phase + no-prior-attempt alone.

    ``reason`` codes when ``trigger_state == "blocked"``:
      - ``"no_open_version"``       — no open release version
      - ``"wrong_phase"``           — version exists but is not in stabilize phase
      - ``"prior_attempt_exists"``  — last_qualification is set OR qualification_attempts > 0
    """
    version = await release_version.current_open_version(session, project_id)
    if version is None:
        return {
            "trigger_state": "blocked",
            "open_version_id": None,
            "reason": "no_open_version",
            "summary": "no open release version — nothing to qualify",
        }

    from app.qualification_state_resolver import (
        qualification_read_provenance,
        resolve_newest_qualification_verdict,
    )

    selected_verdict = await resolve_newest_qualification_verdict(session, version)
    provenance = qualification_read_provenance(version, selected_verdict)

    if version.phase != "stabilize":
        return {
            "trigger_state": "blocked",
            "open_version_id": str(version.id),
            "reason": "wrong_phase",
            "summary": (
                f"version v{version.version_number} is in phase "
                f"'{version.phase}', not 'stabilize'"
            ),
            "qualification_provenance": provenance,
        }

    has_prior_attempt = (
        version.last_qualification is not None
        or (version.qualification_attempts or 0) > 0
    )
    if has_prior_attempt:
        return {
            "trigger_state": "blocked",
            "open_version_id": str(version.id),
            "reason": "prior_attempt_exists",
            "summary": (
                f"version v{version.version_number} already has a qualification "
                "attempt recorded — initial trigger does not re-fire"
            ),
            "qualification_provenance": provenance,
        }

    return {
        "trigger_state": "armed",
        "open_version_id": str(version.id),
        "reason": "armed",
        "summary": (
            f"v{version.version_number} is stabilize-phase with no prior "
            "qualification attempt — initial trigger armed (acceptance contract "
            "is graded inside qualification)"
        ),
        "qualification_provenance": provenance,
    }


async def compute_goal_coverage_drift_signal(
    session: AsyncSession,
    project_id: uuid.UUID,
) -> dict[str, Any] | None:
    """Compute a cheap goal-coverage divergence signal for the open version.

    Best-effort and fail-safe: any failure returns None (caller degrades
    gracefully). Reads persisted merge-grade fields from the latest
    QualificationVerdict + a cheap COUNT of tasks merged since that verdict,
    then delegates to the pure ``compute_goal_coverage_drift`` scorer.

    Returns a dict with keys ``signal``, ``coverage``, ``biggest_gap``,
    ``merges_since_grade``, ``stale``, ``detail`` — or None on any error.
    """
    from app.release_scoring import compute_goal_coverage_drift  # noqa: PLC0415
    from app import release_version as _rv  # noqa: PLC0415

    try:
        version = (
            (
                await session.execute(
                    select(ReleaseVersion)
                    .where(
                        ReleaseVersion.project_id == project_id,
                        ReleaseVersion.status == "open",
                    )
                    .order_by(ReleaseVersion.opened_at.desc())
                    .limit(1)
                )
            )
            .scalars()
            .first()
        )
        if version is None:
            return None

        goal = version.goal

        # Find the latest verdict that carries a merge-grade result.
        verdict = (
            (
                await session.execute(
                    select(QualificationVerdict)
                    .where(
                        QualificationVerdict.release_version_id == version.id,
                        QualificationVerdict.merge_grade_status.is_not(None),
                    )
                    .order_by(QualificationVerdict.created_at.desc())
                    .limit(1)
                )
            )
            .scalars()
            .first()
        )

        if verdict is None:
            merges_since_open: int = 0
            if version.opened_at is not None:
                try:
                    merges_since_open = await _rv.count_merged_since(
                        session, project_id, version.opened_at
                    )
                except Exception:
                    logger.exception(
                        "[DRIFT] count_merged_since(opened_at) failed for project %s",
                        project_id,
                    )
            return compute_goal_coverage_drift(
                goal,
                merge_grade_status=None,
                merges_since_grade=merges_since_open,
            )

        merges_since: int = 0
        if verdict.finalized_at is not None:
            try:
                merges_since = await _rv.count_merged_since(
                    session, project_id, verdict.finalized_at
                )
            except Exception:
                logger.exception(
                    "[DRIFT] count_merged_since failed for project %s", project_id
                )

        return compute_goal_coverage_drift(
            goal,
            merge_grade_status=verdict.merge_grade_status,
            merge_grade_coverage=verdict.merge_grade_coverage,
            merge_grade_biggest_gap=verdict.merge_grade_biggest_gap,
            goal_alignment=verdict.goal_alignment,
            merges_since_grade=merges_since,
        )
    except Exception:
        logger.exception(
            "[DRIFT] goal_coverage_drift compute failed for project %s", project_id
        )
        return None


# --------------------------------------------------------------------------- #
# v22 carried-finding backfill
# --------------------------------------------------------------------------- #


async def _attach_v22_data_integrity_criterion_evidence(
    session: AsyncSession,
    project_id: uuid.UUID,
    version: ReleaseVersion,
    finding_count: int,
) -> bool:
    """Attach CriterionEvidence to c-1a3ec09d for the v22 backfill (dedup-guarded).

    Returns True when a new evidence row was written; False when the criterion
    is absent or the evidence was already recorded.
    """
    from app.acceptance_criteria import (  # noqa: PLC0415 — avoid load-time cycle
        get_criterion_by_key,
        get_evidence_for_criterion,
        record_evidence,
    )

    criterion = await get_criterion_by_key(
        session, version.id, _V22_DATA_INTEGRITY_CRITERION_KEY
    )
    if criterion is None:
        return False

    existing = await get_evidence_for_criterion(session, criterion.id)
    already_recorded = any(
        e.source_kind == "audit_finding_backfill"
        and e.source_id == _V22_BACKFILL_RESOLVING_REF
        for e in existing
    )
    if already_recorded:
        return False

    await record_evidence(
        session,
        criterion_id=criterion.id,
        project_id=project_id,
        source_kind="audit_finding_backfill",
        source_id=_V22_BACKFILL_RESOLVING_REF,
        detail={
            "resolving_ref": _V22_BACKFILL_RESOLVING_REF,
            "finding_count": finding_count,
            "note": _V22_BACKFILL_NOTE,
        },
    )
    return True


async def backfill_v22_carried_findings_resolved(
    session: AsyncSession,
    project_id: uuid.UUID,
    version: ReleaseVersion,
) -> int:
    """Backfill AuditFindingResolution rows for v22's carried low-severity findings.

    This is a one-time historical data attachment, NOT a general sweep.  Two hard
    guards keep it from ever fabricating resolution rows for the wrong version or
    the wrong severity (see the module-level constants for the full rationale):

    * ``version.version_number`` must equal ``_V22_BACKFILL_VERSION_NUMBER`` (22).
      v22 has already promoted, so a caller that hands us a later open version is
      a no-op — we never write "resolved" rows against v23+.
    * only findings whose severity is ``_V22_BACKFILL_SEVERITY`` ("low") are
      resolved.  Gating (high/medium/critical) findings are left untouched so
      ``_compute_requalification_decision`` still sees them.

    For each surviving low-severity finding returned by
    ``_latest_carried_audit_findings``, upserts a ``"resolved"`` ledger row citing
    commit d2cf1ebb8 / PR #13245 as the fix.  Idempotent:
    ``apply_finding_resolution`` upserts and the criterion-evidence write is
    dedup-guarded, so re-runs are safe no-ops.

    Also attaches a single ``CriterionEvidence`` row to the data-integrity
    criterion ``c-1a3ec09d`` if it exists on this version, recording the backfill
    as covering evidence.

    Returns the number of low-severity resolution rows written (0 when the
    version is not v22 or it carries no low-severity findings).
    """
    # Guard 1: only ever operate on release version 22.
    if version.version_number != _V22_BACKFILL_VERSION_NUMBER:
        return 0

    findings, _truncated, _provenance = await _latest_carried_audit_findings(
        session, version
    )
    if not findings:
        return 0

    # Guard 2: restrict to low-severity carried findings — the only ones fixed in
    # d2cf1ebb8.  Gating findings must never receive a fabricated "resolved" row.
    low_findings = [
        f
        for f in findings
        if isinstance(f, dict)
        and str(f.get("severity") or "").lower() == _V22_BACKFILL_SEVERITY
    ]
    if not low_findings:
        return 0

    count = 0
    for finding in low_findings:
        key = finding.get("key") or _KEYLESS_FINDING_SENTINEL
        await apply_finding_resolution(
            session,
            project_id=project_id,
            release_version_id=version.id,
            finding_key=key,
            state="resolved",
            severity=_V22_BACKFILL_SEVERITY,
            note=_V22_BACKFILL_NOTE,
            resolving_ref=_V22_BACKFILL_RESOLVING_REF,
        )
        count += 1

    await session.commit()

    # Attach summary evidence to the data-integrity criterion if present on this
    # version.  Dedup-guarded so repeated ticks do not append duplicate rows.
    await _attach_v22_data_integrity_criterion_evidence(
        session, project_id, version, count
    )

    # Count guard: detect divergence between the number of low-severity
    # findings actually resolved and the expected constant (13), then log a
    # warning when they differ.  Non-fatal — the backfill still completes and
    # returns the actual count.  This is a data-integrity self-check in the
    # same spirit as the metrics-integrity sweep: a self-reported number that
    # diverges from an expected constant is a signal worth investigating.
    expected = _V22_EXPECTED_LOW_SEVERITY_COUNT
    match = count == expected
    _v22_backfill_guard_state.update(
        expected=expected, resolved=count, match=match
    )
    if not match:
        logger.warning(
            "V22_BACKFILL_COUNT_DIVERGENCE: resolved %d low-severity findings, "
            "but expected %d — self-reported vs expected mismatch.  "
            "The v22 audit-finding inventory may have changed or the seed data "
            "may be inconsistent.",
            count,
            expected,
        )
        # Best-effort enrollment so the divergence signal reaches the triage
        # surface via the evergreen backlog instead of living only in a log
        # line.  Failure to enroll must never abort the backfill or the
        # return — this is a flag-only observation, not a mutating action.
        try:
            from app.evergreen_backlog import enroll_backlog_item  # noqa: PLC0415
            await enroll_backlog_item(
                session,
                project_id=project_id,
                finding_key=_V22_BACKFILL_COUNT_DIVERGENCE_KEY,
                title="v22 backfill count divergence",
                detail=(
                    f"v22 backfill resolved {count} low-severity findings, "
                    f"but expected {expected}. The v22 audit-finding inventory "
                    f"may have changed or the seed data may be inconsistent."
                ),
                provenance=_V22_BACKFILL_PROVENANCE,
            )
            await session.commit()
        except Exception:
            logger.exception(
                "Failed to enroll evergreen backlog item for v22 backfill "
                "count divergence (project %s)",
                project_id,
            )
            with contextlib.suppress(Exception):
                await session.rollback()

    return count


async def record_v22_live_verification_evidence(
    session: AsyncSession,
    project_id: uuid.UUID,
    version: ReleaseVersion,
) -> int:
    """Attach a covering evidence row to the v22 live-verification criterion c-dd953d8c.

    Five v22 promotion-gated claims (direction d-fd9c793d) were code-verified PASS
    on 2026-07-07 and documented as direction notes, but no CriterionEvidence row
    was ever written — leaving evidence_progress.status='stalled'.  This function
    records the formal backfill evidence so the strategic layer advances the
    direction's status.

    Guards:
    * Only operates on release version 22 (v22 has already promoted).
    * Writes one ``audit_finding_backfill`` CriterionEvidence row for criterion
      c-dd953d8c if it exists on this version, using a stable source_id for
      idempotency.

    Returns 1 when a new evidence row was written, 0 when skipped (wrong version,
    criterion absent, or evidence already present).
    """
    from app.acceptance_criteria import (  # noqa: PLC0415 — avoid load-time cycle
        get_criterion_by_key,
        get_evidence_for_criterion,
        record_evidence,
    )

    if version.version_number != _V22_BACKFILL_VERSION_NUMBER:
        return 0

    criterion = await get_criterion_by_key(
        session, version.id, _V22_LIVE_VERIFICATION_CRITERION_KEY
    )
    if criterion is None:
        return 0

    existing = await get_evidence_for_criterion(session, criterion.id)
    already_recorded = any(
        e.source_kind == "audit_finding_backfill"
        and e.source_id == _V22_LIVE_VERIFICATION_SOURCE_ID
        for e in existing
    )
    if already_recorded:
        return 0

    await record_evidence(
        session,
        criterion_id=criterion.id,
        project_id=project_id,
        source_kind="audit_finding_backfill",
        source_id=_V22_LIVE_VERIFICATION_SOURCE_ID,
        detail={
            "claims_verified": 5,
            "verification_date": "2026-07-07",
            "note": (
                "Five v22 promotion-gated live-verification claims "
                "(direction d-fd9c793d) code-verified PASS in direction notes."
            ),
        },
    )
    # record_evidence commits its own session — no redundant outer commit needed.
    return 1


async def _find_v22_version(
    session: AsyncSession,
    project_id: uuid.UUID,
) -> ReleaseVersion | None:
    """Return the project's release version 22 (any status), or None.

    The backfill targets v22 explicitly by ``version_number`` rather than the
    current open version: v22 has already promoted, so ``current_open_version``
    returns v23+ and would (a) never write the intended v22 rows and (b) risk
    resolving findings on a later version.
    """
    return (
        (
            await session.execute(
                select(ReleaseVersion)
                .where(
                    ReleaseVersion.project_id == project_id,
                    ReleaseVersion.version_number == _V22_BACKFILL_VERSION_NUMBER,
                )
                .order_by(ReleaseVersion.opened_at.desc())
                .limit(1)
            )
        )
        .scalars()
        .first()
    )


async def _sweep_v22_finding_backfill() -> None:
    """Leader-cycle sweep: resolve v22's low-severity carried audit findings.

    Targets release version 22 explicitly (via ``_find_v22_version``) — NOT the
    current open version — so the one-time backfill lands on v22 even though it
    has already promoted, and never touches later versions.  Idempotent:
    ``backfill_v22_carried_findings_resolved`` upserts ledger rows and
    dedup-guards its evidence write, so successive ticks are cheap no-ops once
    all findings are resolved.

    Also records live-verification evidence via
    ``record_v22_live_verification_evidence`` for criterion c-dd953d8c on every
    project that carries a v22 release version, resolving the stalled
    evidence_progress for direction d-fd9c793d.  Called every 600 s from the
    leader loop.
    """
    from app import services
    from app.models import Project

    try:
        async with services.AsyncSessionLocal() as session:
            project_ids = list(
                (
                    await session.execute(
                        select(Project.id).where(Project.archived.is_(False))
                    )
                )
                .scalars()
                .all()
            )
    except Exception:
        logger.exception("[V22_FINDING_BACKFILL] failed to enumerate projects")
        return

    for project_id in project_ids:
        try:
            async with services.AsyncSessionLocal() as session:
                version = await _find_v22_version(session, project_id)
                if version is None:
                    continue
                count = await backfill_v22_carried_findings_resolved(
                    session, project_id, version
                )
                if count:
                    logger.info(
                        "[V22_FINDING_BACKFILL] resolved %d carried finding(s) "
                        "for project %s v%d",
                        count,
                        project_id,
                        version.version_number,
                    )
                lv_recorded = await record_v22_live_verification_evidence(
                    session, project_id, version
                )
                if lv_recorded:
                    logger.info(
                        "[V22_FINDING_BACKFILL] recorded live-verification evidence "
                        "for criterion %s on project %s v%d",
                        _V22_LIVE_VERIFICATION_CRITERION_KEY,
                        project_id,
                        version.version_number,
                    )
        except Exception:
            logger.exception(
                "[V22_FINDING_BACKFILL] error backfilling project %s", project_id
            )

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

import logging
import uuid
from datetime import timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app import release_version
from app.acceptance_gate import ACCEPTANCE_AGGREGATE_BUCKETS
from app.models import (
    AuditFindingResolution,
    QualificationVerdict,
    ReleaseVersion,
    Task,
    TaskStatus,
)
from app.release_scoring import compute_challenged_vs_unchallenged_telemetry
from app.shared_state import resolve_project_promotion_enabled
from app.time_utils import _utcnow

logger = logging.getLogger(__name__)

# Resolution states a ledger row may carry.  A high/medium carried finding counts
# as resolved-for-requalification when its ledger row is in EITHER of these.
_RESOLVED_LEDGER_STATES = frozenset({"resolved", "stale"})

# Carried-finding severities that GATE re-qualification readiness.  Low/info
# findings are advisory and ignored by the readiness computation.
_REQUALIFICATION_GATING_SEVERITIES = frozenset({"high", "critical", "medium"})

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


# Zero-count acceptance aggregate — the fail-safe value used both for the
# no-open-version path and whenever the aggregate query degrades. With zero
# declared-unsatisfied tasks the readiness signal is unaffected (additive,
# never a hard block).
_EMPTY_ACCEPTANCE_AGGREGATE: dict[str, int] = {
    **{bucket: 0 for bucket in ACCEPTANCE_AGGREGATE_BUCKETS},
    "total": 0,
}


async def _compute_acceptance_aggregate(
    session: AsyncSession,
    project_id: uuid.UUID,
) -> dict[str, int]:
    """Resolve the open version's acceptance aggregate, fail-safe.

    Returns the per-version canonical acceptance bucket counts over the open
    version's window, or all-zeros when there is no open version or the query
    fails.  Never raises — any failure degrades to zero-counts so a transient
    aggregate error can never lower or hard-block readiness.
    """
    try:
        version = await release_version.current_open_version(session, project_id)
        if version is None or version.opened_at is None:
            return dict(_EMPTY_ACCEPTANCE_AGGREGATE)
        agg = await release_version.compute_version_acceptance_aggregate(
            session, project_id, since=version.opened_at
        )
        return {
            **{
                bucket: int(agg.get(bucket, 0))
                for bucket in ACCEPTANCE_AGGREGATE_BUCKETS
            },
            "total": int(agg.get("total", 0)),
        }
    except Exception:  # noqa: BLE001 — defensive: any failure must not block readiness
        logger.warning(
            "[RELEASE_READINESS] acceptance aggregate failed for project %s; "
            "degrading to zero-counts (readiness unaffected)",
            project_id,
            exc_info=True,
        )
        return dict(_EMPTY_ACCEPTANCE_AGGREGATE)


def _acceptance_keys(aggregate: dict[str, int]) -> dict[str, Any]:
    """Additive readiness keys derived from an acceptance aggregate.

    Strictly fail-safe: undeclared / legacy tasks never count against readiness.
    The keys are advisory — they augment the readiness dict but never flip
    ``ready`` (mirroring how ``validated_precision`` augments, never replaces,
    the self-approval signal).
    """
    satisfied_unproven = int(aggregate.get("satisfied_unproven", 0))
    declared_unsatisfied = int(aggregate.get("declared_unsatisfied", 0))
    degraded = int(aggregate.get("merged_unverified_degraded", 0))
    malformed = int(aggregate.get("malformed", 0))
    return {
        "acceptance_proven": int(aggregate.get("proven", 0)),
        "acceptance_satisfied_unproven": satisfied_unproven,
        "acceptance_declared_unsatisfied": declared_unsatisfied,
        "acceptance_undeclared": int(aggregate.get("undeclared", 0)),
        "acceptance_merged_unverified_degraded": degraded,
        "acceptance_malformed": malformed,
        "acceptance_unproven_present": (
            satisfied_unproven + declared_unsatisfied + degraded + malformed
        )
        > 0,
    }


def _acceptance_caveat(aggregate: dict[str, int]) -> str | None:
    """Advisory caveat when the open version contains unproven work.

    Returns ``None`` for empty, proven-only, or undeclared-only windows.
    """
    satisfied_unproven = int(aggregate.get("satisfied_unproven", 0))
    declared_unsatisfied = int(aggregate.get("declared_unsatisfied", 0))
    degraded = int(aggregate.get("merged_unverified_degraded", 0))
    malformed = int(aggregate.get("malformed", 0))
    if satisfied_unproven + declared_unsatisfied + degraded + malformed <= 0:
        return None
    return (
        "acceptance not fully proven — this version contains "
        f"{satisfied_unproven} satisfied-but-not-machine-proven task(s), "
        f"{declared_unsatisfied} declared-unsatisfied task(s), {degraded} "
        f"degraded-unverified merge(s), and {malformed} malformed recorded "
        "verdict(s) (advisory: claimed-but-not-machine-proven work does not "
        "hard-block promotion)"
    )


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
    """
    out["promotion_enabled"] = promotion_enabled
    out["regime"] = "live" if promotion_enabled else "pre_go_live_calibration"
    out["is_current_readiness"] = not promotion_enabled
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
        "audit_reconciliation": _recon_out,
        "audit_leg_proof": _audit_leg_proof,
        "promotion_pr_url": qual.get("promotion_pr_url"),
        "promotion_conflict": qual.get("promotion_conflict"),
        "promotion_error_detail": qual.get("promotion_error_detail"),
        "theme_delivery": qual.get("theme_delivery"),
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
    acceptance_aggregate = None
    sub_scores = verdict.sub_scores or {}
    if isinstance(sub_scores, dict):
        acceptance_aggregate = sub_scores.get("acceptance_aggregate")
    if acceptance_aggregate is None:
        acceptance_aggregate = getattr(verdict, "acceptance_aggregate", None)
    _vrecon = verdict.audit_reconciliation
    _v_audit_leg_proof = (
        _vrecon.get("audit_leg_proof") if isinstance(_vrecon, dict) else None
    )
    _vrecon_out = (
        {k: v for k, v in _vrecon.items() if k != "audit_leg_proof"}
        if isinstance(_vrecon, dict)
        else _vrecon
    )
    return {
        "id": str(verdict.id),
        "release_version_id": str(verdict.release_version_id),
        "candidate_sha": verdict.candidate_sha,
        "confidence": verdict.confidence,
        "sub_scores": verdict.sub_scores,
        "blocker": verdict.blocker,
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
        "audit_reconciliation": _vrecon_out,
        "audit_leg_proof": _v_audit_leg_proof,
        "promotion_pr_url": verdict.promotion_pr_url,
        "promotion_conflict": verdict.promotion_conflict,
        "promotion_error_detail": verdict.promotion_error_detail,
        # --- Acceptance-window signal (additive; None on legacy rows without aggregate) -
        # Surfaces the proven-vs-delivered distinction: how many landed merges in the
        # qualification window have verified acceptance evidence vs. raw delivered count.
        "acceptance_window_total": _sub(verdict, "acceptance_window_total"),
        "acceptance_window_verified": _sub(verdict, "acceptance_window_verified"),
        "acceptance_window_fraction": _sub(verdict, "acceptance_window_fraction"),
        # Version-window acceptance aggregate (proven-vs-merged) recorded for this
        # verdict's evaluated candidate — additive and nullable. Computed/stored
        # by the acceptance layer; surfaced here so each verdict row carries the
        # proven claim state next to the raw merged counts. ``None`` on legacy
        # rows that predate the aggregate.
        "acceptance_aggregate": acceptance_aggregate,
        # Theme-delivery cert (additive; None on legacy rows predating migration 0305).
        "theme_delivery": verdict.theme_delivery,
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
            QualificationVerdict.terminal_status != "infra_failure",
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
            QualificationVerdict.terminal_status != "infra_failure",
        )
        .order_by(QualificationVerdict.created_at.desc())
        .limit(_READINESS_HISTORY_WINDOW)
    )
    verdicts = list((await session.execute(stmt)).scalars().all())

    validated = await _compute_validated_precision(session, project_id)
    acceptance = await _compute_acceptance_aggregate(session, project_id)
    acceptance_keys = _acceptance_keys(acceptance)
    acceptance_caveat = _acceptance_caveat(acceptance)

    evaluated = len(verdicts)
    if evaluated == 0:
        out = _empty_readiness(
            thresholds,
            infra_failure_count=infra_failure_count,
            consecutive_infra_errors=consecutive_infra_errors,
            validated=validated,
        )
        out.update(acceptance_keys)
        if acceptance_caveat:
            out["acceptance_caveat"] = acceptance_caveat
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

    # Acceptance signal is additive and advisory: when the open version contains
    # merged tasks whose DECLARED acceptance evidence did not pass, surface a
    # caveat on the reason so operators/strategist see the version is not fully
    # proven. It never flips ``ready`` (mirrors validated_precision: augment,
    # never replace) and undeclared/legacy work never contributes.
    if acceptance_caveat:
        reason = f"{reason} NOTE: {acceptance_caveat}"

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
    out.update(acceptance_keys)
    if acceptance_caveat:
        out["acceptance_caveat"] = acceptance_caveat
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
) -> tuple[list[dict[str, Any]], bool]:
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

    verdict = (
        (
            await session.execute(
                select(QualificationVerdict)
                .where(
                    QualificationVerdict.release_version_id == version.id,
                    QualificationVerdict.audit_findings.is_not(None),
                )
                .order_by(QualificationVerdict.created_at.desc())
                .limit(1)
            )
        )
        .scalars()
        .first()
    )
    findings = get_latest_qualification_audit_findings(version, verdict)
    qual = version.last_qualification or {}
    truncated = (
        bool(qual.get("audit_findings_truncated")) if isinstance(qual, dict) else False
    )
    return findings, truncated


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


async def compute_requalification_readiness(
    session: AsyncSession,
    project_id: uuid.UUID,
) -> dict[str, Any]:
    """Decide whether the project's open version is ready to re-qualify.

    Resolves the LATEST qualification's carried audit findings for the open
    release version (preferring the latest QualificationVerdict row, falling back
    to ``last_qualification``), filters to the gating severities (critical/high +
    medium — low/info are advisory and ignored), and joins each against the
    resolution ledger by ``finding_key`` scoped to that ``release_version``.

    ``requalification_ready`` is TRUE exactly when every gating carried finding
    has a ledger row in state ``resolved`` OR ``stale``.  Convention for the
    zero-gating-findings case: ready=True (nothing carried blocks re-qualification).

    Truncation-safe: if the carried list was clipped at the cap
    (``audit_findings_truncated``), readiness is NOT claimed on the partial list —
    ``requalification_ready`` is forced False and ``audit_findings_truncated`` is
    surfaced so consumers can treat readiness conservatively.

    Returns a dict shaped:
        {
          "requalification_ready": bool,
          "open_version_id": str | None,
          "audit_findings_truncated": bool,
          "gating_total": int,
          "gating_resolved": int,
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
            "reason": (
                "promotion-CI remediation in flight — a fix for the last "
                "promotion attempt's CI failures is being applied to the "
                "integration branch; deferring re-qualification until it lands"
            ),
            "findings": [],
        }

    carried, truncated = await _latest_carried_audit_findings(session, version)

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
    ledger_by_key: dict[str, AuditFindingResolution] = {
        row.finding_key: row for row in ledger_rows
    }

    gating: list[dict[str, Any]] = []
    for f in carried:
        if not isinstance(f, dict):
            continue
        severity = str(f.get("severity") or "").lower()
        if severity not in _REQUALIFICATION_GATING_SEVERITIES:
            continue
        key = f.get("key")
        if not key:
            # No content key (legacy carried finding) — cannot be matched against
            # the ledger, so it must read as unresolved (conservative).
            key = ""
        ledger = ledger_by_key.get(key) if key else None
        state = (
            ledger.state
            if ledger is not None and ledger.state in _RESOLVED_LEDGER_STATES
            else "unresolved"
        )
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

    if truncated:
        # The carried list was clipped at the cap: we cannot see every gating
        # finding, so readiness must not be claimed on a partial list.
        ready = False
        reason = (
            "carried audit findings were truncated at the cap — readiness cannot "
            f"be asserted on a partial list ({gating_resolved}/{gating_total} "
            "visible gating findings resolved)"
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
        "open_version_id": str(version.id),
        "audit_findings_truncated": truncated,
        "gating_total": gating_total,
        "gating_resolved": gating_resolved,
        "reason": reason,
        "findings": gating,
    }

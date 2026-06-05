"""Release readiness — qualification record aggregation and formatting.

Provides the operator-facing readiness verdict (whether promotion is ready) based
on recent dry-run qualification history, plus formatting helpers for API responses.

GROUND-TRUTH CAVEAT: this signal is a *self-approval* track record, NOT a
validated trust measurement. ``would_promote`` is the qualifier's own
confidence >= threshold, so ``would_promote_rate`` is just the rate at which the
qualifier approved itself. There is no recorded post-promotion outcome on
QualificationVerdict (was the promoted candidate reverted, did main CI/regression
follow), and no outcome-feedback code in the release_* modules — so a qualifier
that systematically over-scores a defect class still reads ~100% here. Until such
a ground truth is recorded and a true precision/false-positive rate is derived,
this MUST be surfaced as "qualifier self-approval streak", never as a "trustworthy"
or validated judgment. The returned ``ground_truth_validated`` flag (always False
today) and ``caveat`` string exist so every consumer states this honestly.
"""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import QualificationVerdict, ReleaseVersion
from app.shared_state import resolve_project_promotion_enabled

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
    "self-approval only — no post-promotion outcome (revert / main CI / "
    "regression) is recorded, so this is the rate at which the qualifier "
    "approved itself, not a measured precision"
)

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
    return {
        "status": status,
        "outcome": _OUTCOME_LABELS.get(status, status),
        "confidence": qual.get("confidence"),
        "threshold": qual.get("threshold"),
        "sub_scores": qual.get("sub_scores"),
        "blocker": qual.get("blocker"),
        "started_at": qual.get("started_at"),
        "finalized_at": qual.get("finalized_at"),
    }


def format_verdict_for_api(verdict: QualificationVerdict) -> dict[str, Any]:
    """Return an operator-facing dict for a single QualificationVerdict row."""
    return {
        "id": str(verdict.id),
        "release_version_id": str(verdict.release_version_id),
        "candidate_sha": verdict.candidate_sha,
        "confidence": verdict.confidence,
        "sub_scores": verdict.sub_scores,
        "blocker": verdict.blocker,
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
    }


def _empty_readiness(
    thresholds: dict[str, Any],
    *,
    infra_failure_count: int = 0,
    consecutive_infra_errors: int = 0,
) -> dict[str, Any]:
    """The not-ready summary returned when there is no verdict history yet."""
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
        "ground_truth_validated": False,
        "caveat": _READINESS_GROUND_TRUTH_CAVEAT,
        "promotion_enabled": False,
        "regime": "pre_go_live_calibration",
        "is_current_readiness": True,
    }


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
        )
        .order_by(QualificationVerdict.created_at.desc())
        .limit(_READINESS_HISTORY_WINDOW)
    )
    verdicts = list((await session.execute(stmt)).scalars().all())

    evaluated = len(verdicts)
    if evaluated == 0:
        out = _empty_readiness(
            thresholds,
            infra_failure_count=infra_failure_count,
            consecutive_infra_errors=consecutive_infra_errors,
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
            f"NOTE: {_READINESS_GROUND_TRUTH_CAVEAT}"
        )
    else:
        reason = "; ".join(reasons)

    return _annotate_regime({
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
        "ground_truth_validated": False,
        "caveat": _READINESS_GROUND_TRUTH_CAVEAT,
    }, promotion_enabled)

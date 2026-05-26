"""Display and readiness aggregation helpers for release qualification."""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import QualificationVerdict

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


def _empty_readiness(thresholds: dict[str, Any]) -> dict[str, Any]:
    """The not-ready summary returned when there is no verdict history yet."""
    return {
        "ready": False,
        "reason": "no qualification verdicts recorded yet — nothing to evaluate",
        "verdicts_evaluated": 0,
        "would_promote_count": 0,
        "would_promote_rate": 0.0,
        "consecutive_clean_passes": 0,
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
    }


async def compute_promotion_readiness(
    session: AsyncSession,
    project_id: uuid.UUID,
) -> dict[str, Any]:
    """Summarize the dry-run qualification track record into a readiness verdict."""
    thresholds: dict[str, Any] = {
        "min_verdicts": _READINESS_MIN_VERDICTS,
        "min_clean_streak": _READINESS_MIN_CLEAN_STREAK,
        "min_would_promote_rate": _READINESS_MIN_WOULD_PROMOTE_RATE,
        "recurring_blocker_min": _READINESS_RECURRING_BLOCKER_MIN,
        "history_window": _READINESS_HISTORY_WINDOW,
    }

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
        return _empty_readiness(thresholds)

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
            f"qualifier looks trustworthy: {would_promote_count}/{evaluated} "
            f"would-promote (rate {would_promote_rate:.2f}), {consecutive_clean} "
            f"consecutive clean passes, no recurring blockers"
        )
    else:
        reason = "; ".join(reasons)

    return {
        "ready": ready,
        "reason": reason,
        "verdicts_evaluated": evaluated,
        "would_promote_count": would_promote_count,
        "would_promote_rate": would_promote_rate,
        "consecutive_clean_passes": consecutive_clean,
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
    }

"""Canonical persisted state for release promotion claims and managed CI waits.

This module owns the small durable vocabulary shared by promotion, the
reconciler, and stall alerts.  It deliberately contains no merge authority:
it only normalizes legacy rows and persists bounded waiter observations.
"""

from __future__ import annotations


import logging
import uuid
from datetime import datetime

from sqlalchemy.orm.attributes import flag_modified
from sqlmodel import select

from app import release_version
from app.ci_resolution import _CI_RED
from app.time_utils import _coerce_naive_utc, _utcnow
_MODULE_SIZE_GOVERNANCE_CAP: int = 369

logger = logging.getLogger(__name__)

_MANAGED_CHECK_WAIT_SNAPSHOT_DETAIL_LIMIT = 10
_MANAGED_CHECK_WAIT_DETAIL_TEXT_LIMIT = 160

_TERMINAL_405_SUBSTRINGS = (
    "pull request is closed",
    "merge commits are not allowed",
)
_RETRIABLE_405_SUBSTRINGS = (
    "required status check",
    "required checks",
    "base branch was recently updated",
    "waiting for status",
)

_LEGACY_NATIVE_PROMOTION_PENDING_KEYS = frozenset(
    {
        "auto_merge_enabled",
        "why_not_native_automerge",
        "migration_carrying",
        "alerted_at",
        "last_alerted_at",
        "alert_count",
    }
)

_PROMOTION_PENDING_ABSENT = "absent"
_PROMOTION_PENDING_MANAGED = "managed"
_PROMOTION_PENDING_CLAIM = "claim"
_PROMOTION_PENDING_MALFORMED = "malformed"


def _classify_405_message(msg: str) -> str:
    """Classify a GitHub 405 merge refusal without guessing unknown messages."""

    lower = msg.lower()
    for substring in _TERMINAL_405_SUBSTRINGS:
        if substring in lower:
            return "terminal"
    for substring in _RETRIABLE_405_SUBSTRINGS:
        if substring in lower:
            return "retriable"
    return "unknown"


def _trim_check_snapshot_text(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    value = value.strip()
    if not value:
        return None
    return value[:_MANAGED_CHECK_WAIT_DETAIL_TEXT_LIMIT]


def _managed_check_wait_snapshot(
    *,
    ci_state: str,
    classified_at: datetime,
    head_sha: str | None = None,
    check_result: dict | None = None,
    reason: str | None = None,
    detail: str | None = None,
    from_api_error: bool = False,
    api_error_sweep_count: int = 0,
) -> dict:
    """Build a bounded operator snapshot for a non-terminal check-wait sweep."""

    details = []
    raw_details = (
        check_result.get("details") if isinstance(check_result, dict) else None
    )
    if isinstance(raw_details, list):
        for raw in raw_details[:_MANAGED_CHECK_WAIT_SNAPSHOT_DETAIL_LIMIT]:
            if not isinstance(raw, dict):
                continue
            item = {
                "name": _trim_check_snapshot_text(raw.get("name")) or "unknown",
            }
            status = _trim_check_snapshot_text(raw.get("status"))
            conclusion = _trim_check_snapshot_text(raw.get("conclusion"))
            if status is not None:
                item["status"] = status
            if conclusion is not None:
                item["conclusion"] = conclusion
            details.append(item)
    else:
        raw_details = []

    snapshot = {
        "state": ci_state,
        "classified_at": classified_at.isoformat(),
        "head_sha": head_sha,
        "total_checks": len(raw_details),
        "checks": details,
        "truncated": len(raw_details) > _MANAGED_CHECK_WAIT_SNAPSHOT_DETAIL_LIMIT,
    }
    if reason:
        snapshot["reason"] = reason
    if detail:
        snapshot["detail"] = detail[:_MANAGED_CHECK_WAIT_DETAIL_TEXT_LIMIT]
    if from_api_error:
        snapshot["api_error"] = True
        snapshot["api_error_sweep_count"] = api_error_sweep_count
    return snapshot


def _parse_promotion_pending_time(value: object) -> datetime | None:
    """Parse one persisted promotion clock into Blueprint's naive-UTC form."""

    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str) and value.strip():
        try:
            parsed = datetime.fromisoformat(value.strip())
        except (ValueError, TypeError):
            return None
    else:
        return None
    return _coerce_naive_utc(parsed)


def _promotion_pending_fallback_enabled_at(
    qualification: dict,
    version: release_version.ReleaseVersion,
) -> datetime:
    """Return a deterministic clock for a legacy/corrupt promotion sub-state."""

    for value in (
        qualification.get("started_at"),
        getattr(version, "updated_at", None),
        getattr(version, "created_at", None),
    ):
        parsed = _parse_promotion_pending_time(value)
        if parsed is not None:
            return parsed
    return _utcnow()


def _normalize_legacy_promotion_pending(
    raw_pending: object,
    *,
    fallback_enabled_at: datetime | None = None,
) -> tuple[dict | None, bool]:
    """Canonicalize one persisted promotion PR onto the managed waiter.

    Compatibility is deliberately narrow: a bare startup claim has no PR
    number and is left alone; every persisted promotion PR is managed.  Native
    authority and alert fields are stripped rather than copied into new writes.
    """

    if not isinstance(raw_pending, dict):
        return None, False
    raw_pr_number = raw_pending.get("pr_number")
    if isinstance(raw_pr_number, bool):
        return None, False
    try:
        pr_number = int(raw_pr_number)
    except (TypeError, ValueError):
        return None, False
    if pr_number <= 0:
        return None, False
    normalized = dict(raw_pending)
    normalized["pr_number"] = pr_number
    normalized["managed_check_wait"] = True
    for key in _LEGACY_NATIVE_PROMOTION_PENDING_KEYS:
        normalized.pop(key, None)
    enabled_at = _parse_promotion_pending_time(normalized.get("enabled_at"))
    if enabled_at is None:
        enabled_at = _parse_promotion_pending_time(fallback_enabled_at)
    if enabled_at is not None:
        normalized["enabled_at"] = enabled_at.isoformat()
    return normalized, normalized != raw_pending


def _canonicalize_promotion_pending(
    raw_pending: object,
    *,
    qualification: dict,
    version: release_version.ReleaseVersion,
) -> tuple[dict | None, str, bool]:
    """Classify and canonicalize every durable promotion sub-state shape."""

    if raw_pending is None:
        return None, _PROMOTION_PENDING_ABSENT, False

    fallback = _promotion_pending_fallback_enabled_at(qualification, version)
    managed, managed_changed = _normalize_legacy_promotion_pending(
        raw_pending,
        fallback_enabled_at=fallback,
    )
    if managed is not None:
        return managed, _PROMOTION_PENDING_MANAGED, managed_changed

    if isinstance(raw_pending, dict) and "pr_number" not in raw_pending:
        candidate_sha = raw_pending.get("candidate_sha")
        qualified_candidate = qualification.get("candidate_sha")
        if (
            raw_pending.get("claiming") is True
            and isinstance(candidate_sha, str)
            and bool(candidate_sha.strip())
            and candidate_sha == qualified_candidate
        ):
            enabled_at = _parse_promotion_pending_time(raw_pending.get("enabled_at"))
            if enabled_at is None:
                enabled_at = fallback
            canonical_claim = {
                "claiming": True,
                "candidate_sha": candidate_sha,
                "enabled_at": enabled_at.isoformat(),
            }
            return (
                canonical_claim,
                _PROMOTION_PENDING_CLAIM,
                canonical_claim != raw_pending,
            )

    return None, _PROMOTION_PENDING_MALFORMED, True


async def _persist_managed_check_wait_snapshot(
    session,
    version_id: uuid.UUID,
    *,
    pr_number: int,
    ci_state: str,
    snapshot: dict,
) -> bool:
    """Persist additive CI telemetry without clobbering PR provenance."""

    try:
        session.expire_all()
        locked: release_version.ReleaseVersion | None = (
            (
                await session.execute(
                    select(release_version.ReleaseVersion)
                    .where(release_version.ReleaseVersion.id == version_id)
                    .with_for_update()
                    .execution_options(populate_existing=True)
                )
            )
            .scalars()
            .first()
        )
        if locked is None or locked.status != "open":
            return False
        qualification = dict(locked.last_qualification or {})
        if qualification.get("status") != "pending":
            return False
        pending, pending_kind, _normalized = _canonicalize_promotion_pending(
            qualification.get("promotion_pending"),
            qualification=qualification,
            version=locked,
        )
        if pending is None or pending_kind != _PROMOTION_PENDING_MANAGED:
            return False
        if pending.get("pr_number") != pr_number:
            return False

        updated_pending = dict(pending)
        updated_pending["ci_state"] = ci_state
        updated_pending["ci_classified_at"] = snapshot.get("classified_at")
        updated_pending["check_snapshot"] = snapshot
        if snapshot.get("api_error"):
            updated_pending["api_error_sweep_count"] = snapshot.get(
                "api_error_sweep_count", 1
            )
        else:
            updated_pending.pop("api_error_sweep_count", None)
        if ci_state != _CI_RED:
            updated_pending.pop("transient_red_count", None)
        qualification["promotion_pending"] = updated_pending
        locked.last_qualification = qualification
        flag_modified(locked, "last_qualification")
        session.add(locked)
        await session.commit()
        return True
    except Exception:
        logger.exception(
            "[RELEASE_QUALIFIER] reconcile managed_check_wait: failed to persist "
            "check snapshot for version %s",
            version_id,
        )
        try:
            await session.rollback()
        except Exception:
            logger.exception(
                "[RELEASE_QUALIFIER] reconcile managed_check_wait: rollback failed "
                "after check snapshot persist error for version %s",
                version_id,
            )
        return False


async def _increment_pending_transient_red_count(
    session,
    version_id: uuid.UUID,
    *,
    pr_number: int,
) -> int:
    """Increment and return this PR waiter's consecutive transient-red count."""

    try:
        session.expire_all()
        locked: release_version.ReleaseVersion | None = (
            (
                await session.execute(
                    select(release_version.ReleaseVersion)
                    .where(release_version.ReleaseVersion.id == version_id)
                    .with_for_update()
                    .execution_options(populate_existing=True)
                )
            )
            .scalars()
            .first()
        )
        if locked is None or locked.status != "open":
            return 0
        qualification = dict(locked.last_qualification or {})
        if qualification.get("status") != "pending":
            return 0
        pending = qualification.get("promotion_pending")
        if not isinstance(pending, dict) or pending.get("pr_number") != pr_number:
            return 0
        updated_pending = dict(pending)
        new_count = (updated_pending.get("transient_red_count") or 0) + 1
        updated_pending["transient_red_count"] = new_count
        qualification["promotion_pending"] = updated_pending
        locked.last_qualification = qualification
        flag_modified(locked, "last_qualification")
        session.add(locked)
        await session.commit()
        return new_count
    except Exception:
        logger.exception(
            "[RELEASE_QUALIFIER] transient_red_count: failed to persist for version %s",
            version_id,
        )
        try:
            await session.rollback()
        except Exception:
            logger.exception(
                "[RELEASE_QUALIFIER] transient_red_count: rollback failed for "
                "version %s",
                version_id,
            )
        return 0

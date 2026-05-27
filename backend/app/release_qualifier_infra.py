"""Infrastructure-failure branch of the release qualifier.

Extracted from ``app.release_qualifier`` to keep the qualifier monolith
focused on the quality-verdict path. This module owns the logically
distinct branch invoked when a required qualifier child (release_audit /
qualifier-smoketest) terminated as FAILED/CANCELLED — i.e. the qualifier
pipeline itself broke (agent VM crash, timeout, clone failure) rather
than the candidate failing on quality.

The branch:
  * writes an ``infra_failure`` ``last_qualification`` record (no
    ``QualificationVerdict`` row — no quality judgement was rendered);
  * returns the version to ``stabilize`` for a clean re-qualification;
  * posts a ``release_infra_failure`` operator notification; and
  * after ``_MIN_INFRA_ERROR_STREAK`` consecutive infrastructure
    failures, raises a sibling backstop ``Alert`` (the calcification
    guard never fires for infra_failure records, so without this a
    persistently broken qualifier pipeline would be invisible).

Re-exported from ``app.release_qualifier`` so existing test patches
(``rq._MIN_INFRA_ERROR_STREAK``, ``rq._handle_infra_failure``) keep
resolving via the historical import surface.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm.attributes import flag_modified

from app import release_version
from app.models import Alert, AlertSeverity, ReleaseVersion
from app.notification_dispatch import dispatch_external_notification
from app.release_promotion import _notify_operator
from app.time_utils import _utcnow

logger = logging.getLogger(__name__)


# Minimum consecutive qualifier infrastructure errors (FAILED/CANCELLED
# required child tasks) before raising a dedicated infra-failure alert.
# Mirrors _MIN_CALCIFICATION_ATTEMPTS: three consecutive infra failures
# strongly suggest the qualifier pipeline itself (agent VM / clone) is broken.
_MIN_INFRA_ERROR_STREAK = 3


async def _handle_infra_failure(
    session: AsyncSession,
    version: release_version.ReleaseVersion,
    qual: dict[str, Any],
    infra_error: str,
) -> dict[str, Any]:
    """Finalize a qualifier run that ended due to an infrastructure failure.

    Called when a required qualifier child (release_audit or qualifier
    smoketest) terminated as FAILED or CANCELLED rather than COMPLETED —
    indicating the qualifier pipeline itself broke (agent VM crash, timeout,
    clone failure), NOT a quality finding about the release candidate.

    Distinct from a quality verdict in three ways:
      1. Does NOT write a ``QualificationVerdict`` history row — no quality
         judgement was rendered so the verdict history stays clean.
      2. Returns the version to ``stabilize`` for a clean re-qualification,
         exactly like an error path (``missing_candidate_sha``), but with a
         clearly labelled ``infra_failure`` status.
      3. Posts a ``release_infra_failure`` operator notification that
         explicitly names this as an infrastructure failure — not a quality
         blocker — so the operator can distinguish pipeline noise from real
         candidate problems.

    Consecutive infra-failure backstop: after ``_MIN_INFRA_ERROR_STREAK``
    consecutive infrastructure failures the regular calcification guard is
    silent (it only triggers on ``not_promoted`` with a quality blocker).
    This backstop raises a separate Alert so a persistently broken qualifier
    pipeline surfaces to the operator.
    """
    finalized_at = _utcnow().isoformat()
    # Consecutive infra-failure streak: one more than whatever the previous
    # qualifying run reported (carried forward via prior_infra_streak stamped
    # by run_qualification; resets to 0 when a non-infra finalization wrote
    # a different status).
    prior_streak = int(qual.get("prior_infra_streak") or 0)
    current_streak = prior_streak + 1

    record: dict[str, Any] = {
        "candidate_sha": qual.get("candidate_sha"),
        "ci_green": qual.get("ci_green"),
        "smoketest_enabled": qual.get("smoketest_enabled", True),
        "status": "infra_failure",
        "error": infra_error,
        "consecutive_infra_errors": current_streak,
        "started_at": qual.get("started_at"),
        "finalized_at": finalized_at,
        "audit_task_id": qual.get("audit_task_id"),
        "smoketest_task_id": qual.get("smoketest_task_id"),
    }
    version.last_qualification = record
    flag_modified(version, "last_qualification")
    release_version.set_version_phase(
        version,
        "stabilize",
        reasoning=(
            f"qualification v{version.version_number}: infrastructure failure "
            f"({infra_error}) — not a quality finding, returning to stabilize"
        ),
    )
    session.add(version)

    # Consecutive infra-error backstop alert.  The calcification guard never
    # fires for infra_failure records (it requires not_promoted + a quality
    # blocker), so a persistently broken qualifier pipeline would be invisible
    # without this sibling backstop.  Alert at multiples of the streak
    # threshold so an intermittently broken pipeline does not spam the operator.
    if (
        current_streak >= _MIN_INFRA_ERROR_STREAK
        and current_streak % _MIN_INFRA_ERROR_STREAK == 0
    ):
        session.add(
            Alert(
                project_id=version.project_id,
                severity=AlertSeverity.WARNING,
                title=(
                    f"Release qualifier v{version.version_number} infrastructure "
                    f"failure: {current_streak} consecutive errors"
                ),
                message=(
                    f"The release qualifier for v{version.version_number} has "
                    f"failed {current_streak} consecutive time(s) due to "
                    f"infrastructure errors ({infra_error!r}) — the qualifier "
                    f"agent VM or clone pipeline is broken. This is NOT a "
                    f"quality finding about the release candidate. Inspect the "
                    f"release_audit / qualifier-smoketest agent infrastructure "
                    f"and re-queue to retry once the issue is resolved."
                ),
            )
        )
        logger.warning(
            "[RELEASE_QUALIFIER] infra-failure backstop: v%d has %d consecutive "
            "infrastructure errors (error=%s) — alert raised",
            version.version_number,
            current_streak,
            infra_error,
        )

    await _notify_operator(
        session,
        version,
        "release_infra_failure",
        (
            f"v{version.version_number} qualifier infrastructure failure — "
            f"{infra_error.replace('_', ' ')} (agent VM / clone failure, "
            f"not a quality finding). Returned to stabilize for "
            f"re-qualification. Consecutive infra errors: {current_streak}."
        ),
        extra={
            "error": infra_error,
            "consecutive_infra_errors": current_streak,
        },
    )

    asyncio.ensure_future(
        dispatch_external_notification(
            version.project_id,
            "RELEASE_INFRA_FAILURE",
            {
                "version_number": version.version_number,
                "version_id": str(version.id),
                "infra_error": infra_error,
                "consecutive_infra_errors": current_streak,
            },
        )
    )

    logger.warning(
        "[RELEASE_QUALIFIER] v%d infra failure: %s (streak=%d) — "
        "no verdict recorded, version returned to stabilize",
        version.version_number,
        infra_error,
        current_streak,
    )
    return {
        "status": "infra_failure",
        "error": infra_error,
        "consecutive_infra_errors": current_streak,
    }


async def get_infra_failure_stats(
    session: AsyncSession,
    project_id: uuid.UUID,
    *,
    window: int = 50,
) -> dict[str, Any]:
    """Return infra-failure summary for a project over the most-recent ``window`` versions.

    Infra failures are intentionally NOT written as ``QualificationVerdict`` rows
    (no quality judgement was rendered).  This function queries
    ``ReleaseVersion.last_qualification`` directly so callers can surface the
    infra-failure signal alongside verdict history without running the full
    promotion-readiness computation.

    Returns a dict with:
      ``infra_failure_count``      – count of infra_failure records in the window
      ``consecutive_infra_errors`` – current consecutive-failure streak
    """
    stmt = (
        select(ReleaseVersion.last_qualification)
        .where(
            ReleaseVersion.project_id == project_id,
            ReleaseVersion.last_qualification.is_not(None),
        )
        .order_by(ReleaseVersion.updated_at.desc())
        .limit(window)
    )
    rows = list((await session.execute(stmt)).scalars().all())

    infra_failure_count = sum(
        1 for q in rows if isinstance(q, dict) and q.get("status") == "infra_failure"
    )
    consecutive_infra_errors = 0
    for q in rows:
        if not isinstance(q, dict):
            continue
        if q.get("status") == "pending":
            consecutive_infra_errors = int(q.get("prior_infra_streak") or 0)
            break
        if q.get("status") == "infra_failure":
            consecutive_infra_errors = int(q.get("consecutive_infra_errors") or 1)
            break

    return {
        "infra_failure_count": infra_failure_count,
        "consecutive_infra_errors": consecutive_infra_errors,
    }

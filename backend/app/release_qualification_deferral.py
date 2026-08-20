"""Durable, non-error holds for release-qualification finalization."""

from __future__ import annotations

import logging
import uuid
from datetime import datetime
from typing import Any, Awaitable, Callable

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm.attributes import flag_modified

from app import pause_clock
from app.models import DeploymentState
from app.release_models import ReleaseVersion
from app.release_task_admission import finalize_admission_decision
from app.task_locking import (
    AUTOMATIC_TASK_CREATION_ALLOWED,
    AUTOMATIC_TASK_CREATION_PROJECT_UNAVAILABLE,
)
from app.time_utils import _coerce_naive_utc, _utcnow

_MODULE_SIZE_GOVERNANCE_CAP: int = 276

logger = logging.getLogger(__name__)

QUALIFICATION_FINALIZATION_DEFERRED = "qualification_finalization_deferred"
_DEFERRAL_FIELDS = frozenset(
    {
        "pending_outcome",
        "finalization_deferred_reason",
        "finalization_deferred_at",
        "finalization_deferred_pause_baseline",
    }
)


def is_finalization_deferred(qualification: dict[str, Any]) -> bool:
    return qualification.get("pending_outcome") == QUALIFICATION_FINALIZATION_DEFERRED


def finalization_snapshot_is_current(
    expected_attempt_id: uuid.UUID | None,
    peek_attempt_id: uuid.UUID | None,
    peek_candidate_sha: str | None,
    locked_attempt_id: uuid.UUID | None,
    locked_candidate_sha: str | None,
    pre_gates_ready: bool,
) -> bool:
    """Bind unlocked gate facts or a completion to the locked attempt owner."""

    if expected_attempt_id is None and not pre_gates_ready:
        return True
    return (
        locked_attempt_id == peek_attempt_id
        and locked_candidate_sha == peek_candidate_sha
        and (expected_attempt_id is None or locked_attempt_id == expected_attempt_id)
    )


def record_finalization_deferred(
    version: ReleaseVersion,
    reason: str,
    *,
    deploy_state: DeploymentState | None = None,
) -> dict[str, Any]:
    """Hold the exact pending attempt without spending crash/retry budget."""

    qualification = dict(version.last_qualification or {})
    already_deferred = is_finalization_deferred(qualification)
    deferred_at = (
        qualification.get("finalization_deferred_at") if already_deferred else None
    ) or _utcnow().isoformat()
    record = {
        **qualification,
        "status": "pending",
        "pending_outcome": QUALIFICATION_FINALIZATION_DEFERRED,
        "finalization_deferred_reason": reason,
        "finalization_deferred_at": deferred_at,
    }
    if not already_deferred:
        record.pop("finalization_deferred_pause_baseline", None)
        if deploy_state is not None:
            record["finalization_deferred_pause_baseline"] = (
                pause_clock.total_paused_seconds(deploy_state)
            )
    # This is a successful authority decision, so it breaks rather than
    # consumes a prior consecutive finalizer-crash streak. Preserve the
    # bounded finalize cause while carrying the pending attempt forward: it is
    # the only durable explanation when a later deferral overwrites the sweep
    # bookkeeping, and must not be discarded with the streak counter.
    sweep_finalize_cause = qualification.get("sweep_finalize_cause")
    record.pop("sweep_crash_streak", None)
    if sweep_finalize_cause is not None:
        record["sweep_finalize_cause"] = sweep_finalize_cause
    version.last_qualification = record
    flag_modified(version, "last_qualification")
    return {
        "status": QUALIFICATION_FINALIZATION_DEFERRED,
        "admission_reason": reason,
    }


def deferred_timeout_seconds(
    qualification: dict[str, Any],
    *,
    now: datetime | None = None,
    deploy_state: DeploymentState | None = None,
) -> float:
    """Return admission-hold time not already excluded by global pause."""

    try:
        elapsed = max(
            0.0, float(qualification.get("finalization_deferred_seconds") or 0)
        )
    except (TypeError, ValueError):
        elapsed = 0.0
    if not is_finalization_deferred(qualification):
        return elapsed
    try:
        opened_at = _coerce_naive_utc(
            datetime.fromisoformat(
                str(qualification.get("finalization_deferred_at")).replace(
                    "Z", "+00:00"
                )
            )
        )
        current = _coerce_naive_utc(now or _utcnow())
        open_wall = max(0.0, (current - opened_at).total_seconds())
        pause_baseline = qualification.get("finalization_deferred_pause_baseline")
        if deploy_state is not None and pause_baseline is not None:
            try:
                global_overlap = max(
                    0.0,
                    pause_clock.total_paused_seconds(deploy_state)
                    - float(pause_baseline),
                )
            except (TypeError, ValueError):
                global_overlap = 0.0
            open_wall = max(0.0, open_wall - global_overlap)
        return elapsed + open_wall
    except (TypeError, ValueError):
        return elapsed


def resume_finalization_deferred(
    version: ReleaseVersion,
    *,
    resumed_at: datetime | None = None,
    deploy_state: DeploymentState | None = None,
) -> bool:
    """Close the hold and bank only time outside the global pause clock."""

    qualification = dict(version.last_qualification or {})
    if not is_finalization_deferred(qualification):
        return False
    now = _coerce_naive_utc(resumed_at or _utcnow())
    elapsed = deferred_timeout_seconds(
        qualification, now=now, deploy_state=deploy_state
    )
    for key in _DEFERRAL_FIELDS:
        qualification.pop(key, None)
    qualification["finalization_deferred_seconds"] = elapsed
    qualification["finalization_resumed_at"] = now.isoformat()
    version.last_qualification = qualification
    flag_modified(version, "last_qualification")
    return True


async def prepare_finalization_admission(
    session: AsyncSession,
    version: ReleaseVersion,
    decision: str,
    children_terminal: Callable[[AsyncSession, ReleaseVersion], Awaitable[bool]],
    deploy_state: DeploymentState | None,
) -> tuple[dict[str, Any] | None, bool, bool | None]:
    """Apply a closed hold or prepare an exact pending attempt to resume."""

    qualification = dict(version.last_qualification or {})
    if decision != AUTOMATIC_TASK_CREATION_ALLOWED and not qualification.get(
        "promotion_pending"
    ):
        result = record_finalization_deferred(
            version, decision, deploy_state=deploy_state
        )
        version_number = version.version_number
        session.add(version)
        await session.commit()
        logger.info(
            "[RELEASE_QUALIFIER] v%d finalization deferred (%s)",
            version_number,
            decision,
        )
        return result, False, None
    resumed = is_finalization_deferred(qualification)
    terminal = await children_terminal(session, version) if resumed else None
    if resumed:
        resume_finalization_deferred(version, deploy_state=deploy_state)
    return None, resumed, terminal


def _coerce_attempt_id(value: Any) -> uuid.UUID | None:
    if value in (None, ""):
        return None
    try:
        return value if isinstance(value, uuid.UUID) else uuid.UUID(str(value))
    except (TypeError, ValueError, AttributeError):
        return None


async def persist_finalization_deferred(
    session: AsyncSession,
    version_id: uuid.UUID,
    project_id: uuid.UUID,
    reason: str,
    *,
    expected_attempt_id: uuid.UUID | str | None = None,
) -> dict[str, Any] | None:
    """Recover a typed child-spawn denial after partial finalize rollback."""

    # Re-establish canonical Project -> ReleaseVersion lock order.  The current
    # decision may already have changed; ``reason`` remains the provenance of
    # the actual denied spawn that caused the caller to roll back. Contention is
    # a clean no-op: the next completion/sweep retries, and an ordinary Project
    # lock holder must never escape this typed path as a finalizer crash.
    current_decision = await finalize_admission_decision(
        session, project_id, skip_locked=True
    )
    if current_decision == AUTOMATIC_TASK_CREATION_PROJECT_UNAVAILABLE:
        await session.rollback()
        return None
    version = (
        (
            await session.execute(
                select(ReleaseVersion)
                .where(ReleaseVersion.id == version_id)
                .with_for_update()
                .execution_options(populate_existing=True)
            )
        )
        .scalars()
        .first()
    )
    if version is None or version.status != "open":
        await session.rollback()
        return None
    qualification = dict(version.last_qualification or {})
    if qualification.get("status") != "pending":
        await session.rollback()
        return None
    expected_attempt_uuid = _coerce_attempt_id(expected_attempt_id)
    active_attempt_uuid = _coerce_attempt_id(
        qualification.get("qualification_attempt_id")
    ) or _coerce_attempt_id(version.active_qualification_attempt_id)
    if (
        expected_attempt_uuid is not None
        and active_attempt_uuid is not None
        and expected_attempt_uuid != active_attempt_uuid
    ):
        await session.rollback()
        return None
    deploy_state = await session.get(
        DeploymentState, 1, populate_existing=True
    )
    result = record_finalization_deferred(version, reason, deploy_state=deploy_state)
    version_number = version.version_number
    session.add(version)
    await session.commit()
    logger.info(
        "[RELEASE_QUALIFIER] v%d finalization deferred after continuation "
        "admission closed (%s)",
        version_number,
        reason,
    )
    return result

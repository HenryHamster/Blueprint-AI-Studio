"""Lifecycle stall alert checkers extracted from release_qualifier."""

from __future__ import annotations

import logging
from datetime import datetime

from sqlalchemy.ext.asyncio import AsyncSession

from app import release_version
from app.models import Alert, AlertSeverity
from app.shared_state import runtime_config as _runtime_config
from app.time_utils import _utcnow

logger = logging.getLogger(__name__)

# Minimum consecutive hard-blocker failures before raising a calcification
# alert. Three failures on the same blocker strongly suggest the issue is not
# transient and the version is genuinely stuck.
_MIN_CALCIFICATION_ATTEMPTS = 3

# Build-phase calcification thresholds — the backstop for the OTHER end of the
# lifecycle from _check_version_calcification. build→stabilize is entirely
# agent-driven; these conservative thresholds catch a genuine stall without
# nagging a normally-progressing build.
_BUILD_STALL_AGE_DAYS = 14
_BUILD_STALL_MERGED_TASKS = 40

# Stabilize-phase stall thresholds — the backstop for the stabilize→qualify
# edge of the lifecycle. EITHER trigger fires the alert.
_STABILIZE_STALL_AGE_DAYS = 14
_STABILIZE_STALL_MERGED_TASKS = 40

# How long a promotion PR may await auto-merge before the stall alert fires.
_AUTO_MERGE_STALL_SECONDS = 4 * 3600


def _release_qualifier_module():
    from app import release_qualifier as rq

    return rq


def _is_build_stalled(age_days: int, merged_count: int) -> bool:
    """Return True iff a build-phase version is past the conservative threshold."""
    rq = _release_qualifier_module()
    return (
        age_days >= rq._BUILD_STALL_AGE_DAYS
        or merged_count >= rq._BUILD_STALL_MERGED_TASKS
    )


async def _check_version_calcification(
    session: AsyncSession,
    version: release_version.ReleaseVersion,
) -> bool:
    """Post a deduplicated calcification alert when a version is stuck."""
    if version.status != "open" or version.phase != "stabilize":
        return False

    rq = _release_qualifier_module()

    attempts = version.qualification_attempts or 0
    if attempts < rq._MIN_CALCIFICATION_ATTEMPTS:
        return False

    qual = version.last_qualification or {}
    qual_status = qual.get("status")
    blocker = qual.get("blocker")

    if qual_status == "dry_run":
        return False
    if qual_status != "not_promoted" or not blocker:
        return False

    if blocker == "ci_unresolved":
        return False
    if version.calcification_alerted_blocker == blocker:
        return False

    alert_title = f"Release v{version.version_number} calcifying: {blocker}"

    age_days: int = 0
    ref_time = version.phase_changed_at or version.opened_at
    if ref_time is not None:
        age_days = max(0, (_utcnow() - ref_time).days)

    blocker_label = rq._BLOCKER_LABELS.get(blocker, blocker)
    session.add(
        Alert(
            project_id=version.project_id,
            severity=AlertSeverity.WARNING,
            title=alert_title,
            message=(
                f"Release v{version.version_number} has been stuck in the "
                f"stabilize phase for {age_days} day(s) with {attempts} "
                f"qualification attempt(s), each blocked by: {blocker_label}. "
                f"The integration trunk may be drifting from the deployed "
                f"branch. Resolve the blocker to restore promotion."
            ),
        )
    )
    await rq.post_version_calcification_message(
        session,
        version.project_id,
        version_number=version.version_number,
        version_id=str(version.id),
        qualification_attempts=attempts,
        blocker=blocker,
        blocker_label=blocker_label,
        age_days=age_days,
    )
    version.calcification_alerted_blocker = blocker
    session.add(version)
    logger.warning(
        "[RELEASE_QUALIFIER] calcification alert for v%d: %d attempts blocked by %s over %d day(s)",
        version.version_number,
        attempts,
        blocker,
        age_days,
    )
    return True


async def _check_version_ci_unresolved_stall(
    session: AsyncSession,
    version: release_version.ReleaseVersion,
) -> bool:
    """Post a deduplicated alert when CI state cannot be resolved repeatedly."""
    if version.status != "open" or version.phase != "stabilize":
        return False

    rq = _release_qualifier_module()

    attempts = version.qualification_attempts or 0
    if attempts < rq._MIN_CALCIFICATION_ATTEMPTS:
        return False

    qual = version.last_qualification or {}
    if qual.get("status") != "not_promoted":
        return False
    if qual.get("blocker") != "ci_unresolved":
        return False

    if version.calcification_alerted_blocker == "ci_unresolved":
        return False

    age_days: int = 0
    ref_time = version.phase_changed_at or version.opened_at
    if ref_time is not None:
        age_days = max(0, (_utcnow() - ref_time).days)

    session.add(
        Alert(
            project_id=version.project_id,
            severity=AlertSeverity.WARNING,
            title=f"Release v{version.version_number}: CI state unresolvable",
            message=(
                f"Release v{version.version_number} has been stuck in the "
                f"stabilize phase for {age_days} day(s) with {attempts} "
                f"qualification attempt(s) unable to resolve CI state (checks "
                f"pending, no check-runs, or GitHub I/O degraded). This is a "
                f"CI infrastructure or configuration problem — investigate CI "
                f"connectivity and check-run reporting, not the release candidate."
            ),
        )
    )
    await rq.post_version_ci_unresolved_stall_message(
        session,
        version.project_id,
        version_number=version.version_number,
        version_id=str(version.id),
        qualification_attempts=attempts,
        age_days=age_days,
    )
    version.calcification_alerted_blocker = "ci_unresolved"
    session.add(version)
    logger.warning(
        "[RELEASE_QUALIFIER] ci_unresolved stall alert for v%d: %d attempts unable to resolve CI state over %d day(s)",
        version.version_number,
        attempts,
        age_days,
    )
    return True


async def _check_version_build_stall(
    session: AsyncSession,
    version: release_version.ReleaseVersion,
) -> bool:
    """Post a deduplicated build-stall alert when a version sits too long."""
    if version.status != "open" or version.phase != "build":
        return False

    ref_time = version.phase_changed_at or version.opened_at
    if ref_time is None:
        return False

    age_days = max(0, (_utcnow() - ref_time).days)

    try:
        merged_count = await release_version.count_merged_since(
            session, version.project_id, ref_time
        )
    except Exception as exc:  # noqa: BLE001 — a count failure must not break the sweep
        logger.warning(
            "[RELEASE_QUALIFIER] build-stall merged-task count failed for v%d (non-fatal): %s",
            version.version_number,
            exc,
        )
        merged_count = 0

    rq = _release_qualifier_module()

    if not _is_build_stalled(age_days, merged_count):
        return False
    if version.build_stall_alerted_at == ref_time:
        return False

    session.add(
        Alert(
            project_id=version.project_id,
            severity=AlertSeverity.WARNING,
            title=f"Release v{version.version_number} stalled in build phase",
            message=(
                f"Release v{version.version_number} has been in the build phase "
                f"for {age_days} day(s) with {merged_count} merged task(s) and "
                f"has not advanced to stabilize. build→stabilize is agent-driven "
                f"with no automatic cadence — the strategic agent may need to "
                f"emit a version_phase_decision to move it to stabilize so it can "
                f"progress toward qualification and promotion."
            ),
        )
    )
    try:
        await rq.post_version_build_stalled_message(
            session,
            version.project_id,
            version_number=version.version_number,
            version_id=str(version.id),
            age_days=age_days,
            tasks_merged=merged_count,
        )
    except Exception as exc:  # noqa: BLE001 — messaging must never break the sweep
        logger.warning(
            "[RELEASE_QUALIFIER] failed to post build-stall message for v%d (non-fatal): %s",
            version.version_number,
            exc,
        )

    version.build_stall_alerted_at = ref_time
    session.add(version)
    logger.warning(
        "[RELEASE_QUALIFIER] build-stall alert for v%d: %d day(s), %d merged task(s) in build phase without advancing",
        version.version_number,
        age_days,
        merged_count,
    )
    return True


async def _check_version_stabilize_stall(
    session: AsyncSession,
    version: release_version.ReleaseVersion,
) -> bool:
    """Post a deduplicated stabilize-stall alert when a version sits too long."""
    if version.status != "open" or version.phase != "stabilize":
        return False

    rq = _release_qualifier_module()
    qual = version.last_qualification or {}
    qual_status = qual.get("status")
    if qual_status == "dry_run":
        return False
    if qual_status == "not_promoted" and qual.get("blocker"):
        return False

    ref_time = version.phase_changed_at or version.opened_at
    if ref_time is None:
        return False

    age_days = max(0, (_utcnow() - ref_time).days)

    try:
        merged_count = await release_version.count_merged_since(
            session, version.project_id, ref_time
        )
    except Exception as exc:  # noqa: BLE001 — a count failure must not break the sweep
        logger.warning(
            "[RELEASE_QUALIFIER] stabilize-stall merged-task count failed for v%d (non-fatal): %s",
            version.version_number,
            exc,
        )
        merged_count = 0

    if not (
        age_days >= rq._STABILIZE_STALL_AGE_DAYS
        or merged_count >= rq._STABILIZE_STALL_MERGED_TASKS
    ):
        return False
    if version.stabilize_stall_alerted_at == ref_time:
        return False

    session.add(
        Alert(
            project_id=version.project_id,
            severity=AlertSeverity.WARNING,
            title=f"Release v{version.version_number} stalled in stabilize phase",
            message=(
                f"Release v{version.version_number} has been in the stabilize "
                f"phase for {age_days} day(s) with {merged_count} merged task(s) "
                f"but has not been qualified. stabilize→qualify requires a "
                f"qualify_request from the strategic agent — consider requesting "
                f"qualification now or addressing what is preventing readiness."
            ),
        )
    )
    try:
        await rq.post_version_stabilize_stalled_message(
            session,
            version.project_id,
            version_number=version.version_number,
            version_id=str(version.id),
            age_days=age_days,
            tasks_merged=merged_count,
        )
    except Exception as exc:  # noqa: BLE001 — messaging must never break the sweep
        logger.warning(
            "[RELEASE_QUALIFIER] failed to post stabilize-stall message for v%d (non-fatal): %s",
            version.version_number,
            exc,
        )

    version.stabilize_stall_alerted_at = ref_time
    session.add(version)
    logger.warning(
        "[RELEASE_QUALIFIER] stabilize-stall alert for v%d: %d day(s), %d merged task(s) in stabilize phase without qualifying",
        version.version_number,
        age_days,
        merged_count,
    )
    return True


async def _check_version_auto_merge_stall(
    session: AsyncSession,
    version: release_version.ReleaseVersion,
) -> bool:
    """Alert when a promotion PR awaiting auto-merge has been pending too long."""
    if version.status != "open":
        return False

    qual = version.last_qualification or {}
    if qual.get("status") != "pending":
        return False

    pending = qual.get("promotion_pending")
    if not pending:
        return False

    if pending.get("alerted_at"):
        return False

    enabled_at_str = pending.get("enabled_at")
    if not enabled_at_str:
        return False

    try:
        enabled_at = datetime.fromisoformat(enabled_at_str)
    except (ValueError, TypeError):
        return False

    rq = _release_qualifier_module()
    age_seconds = (_utcnow() - enabled_at).total_seconds()
    if age_seconds < rq._AUTO_MERGE_STALL_SECONDS:
        return False

    pr_number = pending.get("pr_number")
    pr_url = pending.get("pr_url") or ""
    auto_merge_enabled = bool(pending.get("auto_merge_enabled"))
    age_hours = int(age_seconds // 3600)

    session.add(
        Alert(
            project_id=version.project_id,
            severity=AlertSeverity.WARNING,
            title=(f"Release v{version.version_number} promotion auto-merge stalled"),
            message=(
                f"Release v{version.version_number} passed qualification but "
                f"promotion PR #{pr_number} has not merged after {age_hours}h "
                f"(auto-merge "
                f"{'enabled' if auto_merge_enabled else 'NOT confirmed enabled'}"
                f"). Check the PR: {pr_url}"
            ),
        )
    )
    try:
        await rq.post_version_auto_merge_stall_message(
            session,
            version.project_id,
            version_number=version.version_number,
            version_id=str(version.id),
            pr_number=pr_number,
            pr_url=pr_url,
            auto_merge_enabled=auto_merge_enabled,
            age_hours=age_hours,
        )
    except Exception as exc:  # noqa: BLE001 — messaging must never break the sweep
        logger.warning(
            "[RELEASE_QUALIFIER] failed to post auto-merge-stall message for v%d (non-fatal): %s",
            version.version_number,
            exc,
        )

    new_qual = dict(qual)
    new_qual["promotion_pending"] = dict(pending)
    new_qual["promotion_pending"]["alerted_at"] = _utcnow().isoformat()
    version.last_qualification = new_qual
    session.add(version)

    logger.warning(
        "[RELEASE_QUALIFIER] auto-merge stall alert for v%d: PR #%s pending %dh (auto-merge %s)",
        version.version_number,
        pr_number,
        age_hours,
        "enabled" if auto_merge_enabled else "NOT confirmed enabled",
    )
    return True


async def _check_version_promotion_readiness(
    session: AsyncSession,
    version: release_version.ReleaseVersion,
) -> bool:
    """Notify operator once when the dry-run track record first reaches readiness."""
    if version.status != "open":
        return False
    rq = _release_qualifier_module()

    if bool(_runtime_config.get("promotion_enabled")):
        return False

    try:
        readiness = await rq.compute_promotion_readiness(session, version.project_id)
    except Exception as exc:  # noqa: BLE001 — failure must not break the sweep
        logger.warning(
            "[RELEASE_QUALIFIER] promotion-readiness check failed for v%d (non-fatal): %s",
            version.version_number,
            exc,
        )
        return False

    if not readiness.get("ready"):
        if version.promotion_ready_notified:
            version.promotion_ready_notified = False
            session.add(version)
            return True
        return False

    if version.promotion_ready_notified:
        return False

    try:
        await rq.post_version_promotion_readiness_message(
            session,
            version.project_id,
            version_number=version.version_number,
            version_id=str(version.id),
            verdicts_evaluated=readiness["verdicts_evaluated"],
            would_promote_rate=readiness["would_promote_rate"],
            consecutive_clean_passes=readiness["consecutive_clean_passes"],
            reason=readiness["reason"],
        )
    except Exception as exc:  # noqa: BLE001 — messaging must never break the sweep
        logger.warning(
            "[RELEASE_QUALIFIER] failed to post promotion-readiness message for v%d (non-fatal): %s",
            version.version_number,
            exc,
        )
        return False

    version.promotion_ready_notified = True
    session.add(version)
    logger.info(
        "[RELEASE_QUALIFIER] promotion-readiness notification sent for v%d: %d clean passes, rate %.2f, %d verdict(s)",
        version.version_number,
        readiness["consecutive_clean_passes"],
        readiness["would_promote_rate"],
        readiness["verdicts_evaluated"],
    )
    return True

"""Lifecycle stall alert checkers extracted from release_qualifier."""

from __future__ import annotations

import logging
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm.attributes import flag_modified

from app.release_artifacts import summarize_required_artifact_gap
from app import pause_clock, release_version
from app.models import Alert, AlertSeverity, DeploymentState
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

# A managed promotion PR includes ordinary CI, whose supported full gate can
# legitimately take 45-60+ minutes. Alert only after four hours, re-alert on the
# same cadence, and escalate at the promotion_pending 24-hour backstop.
_MANAGED_CHECK_WAIT_STALL_SECONDS = 4 * 3600
_MANAGED_CHECK_WAIT_REALERT_INTERVAL_SECONDS = 4 * 3600
_MANAGED_CHECK_WAIT_STALL_CRITICAL_SECONDS = 24 * 3600


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
    # The env-preflight blocker carries specific missing var NAMES so the operator
    # sees WHICH prerequisite to bind, not just the generic label.  Read from the
    # last qualification's sub_scores — presence-only names, never values.
    if blocker == "boot_prereq_missing":
        _missing_names = (
            (qual.get("sub_scores") or {}).get("env_preflight_missing") or []
        )
        if _missing_names:
            blocker_label = (
                f"{blocker_label} — missing: {', '.join(str(n) for n in _missing_names)}"
            )
    # Tag the calcifying blocker with WHERE its check runs so the operator knows
    # whether resolving it needs a fix that lands in the candidate tree
    # (candidate-side) or is inert until this version itself promotes
    # (deployed-side / release-machinery self-reference).
    _site_phrase = rq.blocker_execution_site_label(blocker)
    if _site_phrase:
        blocker_label = f"{blocker_label} ({_site_phrase})"
    session.add(
        Alert(
            project_id=version.project_id,
            event_type="release.calcifying",
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
            event_type="release.ci_unresolved",
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
            event_type="release.build_stalled",
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
        # Only suppress the stall alert when the calcification checker already
        # covers this version (i.e. it has accumulated enough hard-blocker
        # attempts for _check_version_calcification to fire).  Below that
        # threshold the calcification alert is silent, so the age/count gate
        # below serves as the backstop to keep the version operator-visible.
        if (version.qualification_attempts or 0) >= _MIN_CALCIFICATION_ATTEMPTS:
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
            event_type="release.stabilize_stalled",
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


def _deploy_failed_alert_title(version_number: int) -> str:
    """Deterministic title for the promotion-deploy-failed ERROR alert.

    Shared by the create path (:func:`_alert_promotion_deploy_failed`) and the
    recovery resolve path (:func:`_resolve_promotion_deploy_failed_alert`) so the
    title comparison is byte-identical and a recovered version's stale alert is
    matched and dismissed.
    """
    return f"Release v{version_number} promotion deploy FAILED"


async def _alert_promotion_deploy_failed(
    session: AsyncSession,
    version: release_version.ReleaseVersion,
    *,
    artifact_evidence: list[dict] | None = None,
    aggregate_deploy_failure_reason: str | None = None,
) -> bool:
    """Emit a loud ERROR alert when a promoted version's deploy never served.

    Fired by the post-promotion serving-confirmation sweep when a promotion is
    resolved to ``serving_status="deploy_failed"`` — the candidate merged to the
    promotion target but the deploy never came up healthy (boot crash, rollback,
    halt, or a SHA mismatch).  Deduped via ``deploy_confirm_alerted_at`` (the
    standard ``*_alerted_at`` anchor convention on this model): once the anchor
    is stamped the alert is not re-posted for the same failure.  Does not commit
    — the alert + anchor ride the caller's commit.  Returns True iff an alert
    was posted.
    """
    if version.deploy_confirm_alerted_at is not None:
        return False

    reason = (
        aggregate_deploy_failure_reason or version.deploy_failure_reason or "unknown"
    )
    _reason_text = {
        "no_new_revision": (
            "no new healthy production revision booted within the confirmation window"
        ),
        "deploy_workflow_never_dispatched": (
            "the deploy workflow was never dispatched for the promoted SHA"
        ),
        "deploy_workflow_failed": (
            "the deploy workflow ran for the promoted SHA but concluded failed"
        ),
        "deploy_workflow_succeeded_stale_revision": (
            "the deploy workflow succeeded but the promoted revision never served"
        ),
        "deploy_workflow_in_progress": (
            "the deploy workflow is still running past the confirmation deadline"
        ),
        "rolled_back": "the deploy was ROLLED BACK after the promotion",
        "halted": "the deploy was HALTED after the promotion",
        "sha_mismatch": ("the live serving SHA does not match the promoted SHA"),
    }.get(reason, "the deploy failure reason is unclassified")
    _resolution_text = "Investigate the deploy / roll forward a fix."
    _outcome_text = (
        "The promotion did NOT ship — the version is flagged deploy_failed "
        "and must not be treated as live."
    )
    _artifact_gap = summarize_required_artifact_gap(artifact_evidence)
    _artifact_text = f" Aggregate evidence: {_artifact_gap}." if _artifact_gap else ""

    session.add(
        Alert(
            project_id=version.project_id,
            event_type="release.promotion_deploy_failed",
            severity=AlertSeverity.ERROR,
            title=_deploy_failed_alert_title(version.version_number),
            message=(
                f"Release v{version.version_number} merged to the promotion "
                f"target (promoted_sha={(version.promoted_sha or '')[:12] or '?'}) "
                f"but its deploy is NOT serving: {_reason_text} "
                f"(reason={reason}). {_outcome_text} {_resolution_text}"
                f"{_artifact_text}"
            ),
        )
    )
    try:
        from app.release_promotion import _notify_operator  # noqa: PLC0415

        await _notify_operator(
            session,
            version,
            "promotion_deploy_failed",
            (
                f"v{version.version_number} promoted to the production branch but "
                f"its deploy never confirmed serving ({_reason_text}). "
                f"{_outcome_text} {_resolution_text}"
                + (f" {_artifact_gap}." if _artifact_gap else "")
            ),
            extra={
                "reason": reason,
                "promoted_sha": version.promoted_sha,
            },
        )
    except Exception as exc:  # noqa: BLE001 — messaging must never break the sweep
        logger.warning(
            "[RELEASE_QUALIFIER] failed to post deploy-failed operator message "
            "for v%d (non-fatal): %s",
            version.version_number,
            exc,
        )

    version.deploy_confirm_alerted_at = _utcnow()
    session.add(version)
    logger.error(
        "[RELEASE_QUALIFIER] deploy-failed alert for v%d (reason=%s)",
        version.version_number,
        reason,
    )
    return True


async def _resolve_promotion_deploy_failed_alert(
    session: AsyncSession,
    version: release_version.ReleaseVersion,
) -> int:
    """Dismiss any open promotion-deploy-failed ERROR alert for a recovered version.

    Companion to :func:`_alert_promotion_deploy_failed`. When a version
    previously latched to ``serving_status="deploy_failed"`` is later observed to
    actually serve its promoted SHA, the loud ERROR alert that declared the
    promotion dead no longer describes a real condition and must be resolved —
    mirroring how :func:`app.alert_dispatch._resolve_stale_staged_direction_alerts`
    dismisses its alert once the underlying stall clears.

    Matches the deterministic per-version title built by
    :func:`_deploy_failed_alert_title`. Only open (``dismissed_at IS NULL``),
    project-scoped (``task_id IS NULL``) alerts are touched, so this is a safe
    no-op when none exists and idempotent across repeated calls. Does NOT commit
    — the dismissal rides the caller's recovery transaction (the same per-version
    row lock), mirroring ``_alert_promotion_deploy_failed``'s ride-the-caller
    contract. Returns the number of alerts dismissed.
    """
    title = _deploy_failed_alert_title(version.version_number)
    result = await session.execute(
        select(Alert).where(
            Alert.project_id == version.project_id,
            Alert.task_id.is_(None),
            Alert.dismissed_at.is_(None),
            Alert.title == title,
        )
    )
    open_alerts = result.scalars().all()
    if not open_alerts:
        return 0
    now = _utcnow()
    for alert in open_alerts:
        alert.dismissed_at = now
        session.add(alert)
    logger.info(
        "[RELEASE_QUALIFIER] resolved %d open deploy-failed alert(s) for recovered v%d",
        len(open_alerts),
        version.version_number,
    )
    return len(open_alerts)


async def _check_version_managed_check_wait_stall(
    session: AsyncSession,
    version: release_version.ReleaseVersion,
) -> bool:
    """Alert when Blueprint's managed promotion PR has been pending too long."""
    if version.status != "open":
        return False

    qual = version.last_qualification or {}
    if qual.get("status") != "pending":
        return False

    from app.release_promotion_pending import (
        _PROMOTION_PENDING_MANAGED,
        _canonicalize_promotion_pending,
        _parse_promotion_pending_time,
    )

    pending, pending_kind, pending_was_normalized = _canonicalize_promotion_pending(
        qual.get("promotion_pending"),
        qualification=qual,
        version=version,
    )
    if pending is None or pending_kind != _PROMOTION_PENDING_MANAGED:
        return False
    enabled_at = _parse_promotion_pending_time(pending.get("enabled_at"))
    if enabled_at is None:  # canonicalizer is total; defensive only.
        return False
    if pending_was_normalized:
        qual = dict(qual)
        qual["promotion_pending"] = pending
        version.last_qualification = qual
        flag_modified(version, "last_qualification")
        session.add(version)

    now = _utcnow()
    deploy_state = await session.get(DeploymentState, 1)
    age_seconds = pause_clock.active_elapsed_seconds(
        deploy_state, enabled_at, pending.get("pause_baseline")
    )
    if age_seconds < _MANAGED_CHECK_WAIT_STALL_SECONDS:
        return False

    last_alerted_str = pending.get("mcw_last_alerted_at")
    if last_alerted_str:
        try:
            last_alerted = datetime.fromisoformat(last_alerted_str)
        except (ValueError, TypeError):
            last_alerted = None
        if (
            last_alerted is not None
            and (now - last_alerted).total_seconds()
            < _MANAGED_CHECK_WAIT_REALERT_INTERVAL_SECONDS
        ):
            return False

    pr_number = pending.get("pr_number")
    pr_url = pending.get("pr_url") or ""
    age_hours = int(age_seconds // 3600)
    ci_state = pending.get("ci_state", "unknown")

    critical = age_seconds >= _MANAGED_CHECK_WAIT_STALL_CRITICAL_SECONDS
    severity = AlertSeverity.ERROR if critical else AlertSeverity.WARNING
    severity_note = (
        f" This has now exceeded {_MANAGED_CHECK_WAIT_STALL_CRITICAL_SECONDS // 3600}h"
        " — manual intervention is likely required."
        if critical
        else ""
    )

    session.add(
        Alert(
            project_id=version.project_id,
            event_type="release.managed_check_wait_stalled",
            severity=severity,
            title=(
                f"Release v{version.version_number} promotion check wait stalled"
            ),
            message=(
                f"Release v{version.version_number} promotion PR #{pr_number} has "
                f"waited {age_hours}h for required checks (CI state: {ci_state}). "
                "Blueprint will revalidate the exact base/head and retry the merge "
                f"once checks pass. Check for stuck or failing checks: {pr_url}"
                f"{severity_note}"
            ),
        )
    )

    new_qual = dict(qual)
    new_pending = dict(pending)
    now_iso = now.isoformat()
    new_pending.setdefault("mcw_alerted_at", now_iso)
    new_pending["mcw_last_alerted_at"] = now_iso
    new_pending["mcw_alert_count"] = int(new_pending.get("mcw_alert_count") or 0) + 1
    new_qual["promotion_pending"] = new_pending
    version.last_qualification = new_qual
    session.add(version)

    logger.warning(
        "[RELEASE_QUALIFIER] managed_check_wait stall alert (%s, #%d) for v%d: "
        "PR #%s pending %dh (ci_state=%s)",
        "ERROR" if critical else "WARNING",
        new_pending["mcw_alert_count"],
        version.version_number,
        pr_number,
        age_hours,
        ci_state,
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
            validated_precision=readiness.get("validated_precision"),
            resolved_outcome_sample=readiness.get("resolved_outcome_sample", 0),
            ground_truth_validated=readiness.get("ground_truth_validated", False),
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

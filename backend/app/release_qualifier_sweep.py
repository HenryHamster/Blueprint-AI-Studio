"""Defense-in-depth reaper sweep for release qualifications.

Extracted from ``release_qualifier`` (the module size cap left no headroom for
routine edits).  This is the periodic sweep machinery the reaper invokes once
per cycle to force-finalize wedged / timed-out qualifications and reconcile
out-of-band promotions — a separate concern from the core async qualification
coordinator (run / finalize / promote) that stays in ``release_qualifier``.

The public surface is UNCHANGED: ``release_qualifier`` re-exports every symbol
defined here, so ``app.release_qualifier.sweep_stuck_qualifications`` (and the
``_force_finalize_*`` / ``_parse_started_at`` helpers and the tunable
constants) remain importable and test-patchable at their original paths.
"""

from __future__ import annotations


import logging
from datetime import datetime
from typing import Any

from sqlalchemy import and_, exists, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm.attributes import flag_modified

from app import release_qualification_deferral, release_version
from app.database import AsyncSessionLocal
from app.models import (
    Alert,
    AlertSeverity,
    Project,
    ReleaseDeployArtifact,
    Task,
    TaskStatus,
)
from app.release_artifacts import iter_release_artifact_providers
from app.release_authority_lock import (
    lock_project_then_version as _lock_project_then_version,
)
from app.release_promotion import _notify_operator, _resolve_github_with_session
from app.release_reconciliation import (
    _confirm_serving_or_fail,
    _reconcile_if_deploy_failed_recovered,
    _reconcile_if_managed_check_wait,
    _reconcile_if_not_promoted_but_pr_merged,
    _reconcile_if_pending_qualification_superseded,
    _reconcile_if_promotion_failed_landed,
    _reconcile_if_promotion_pending_timed_out,
)
from app.release_stall_alerts import (
    _check_version_build_stall,
    _check_version_calcification,
    _check_version_ci_unresolved_stall,
    _check_version_managed_check_wait_stall,
    _check_version_promotion_readiness,
    _check_version_stabilize_stall,
)
from app.time_utils import _utcnow
from app.vm_lifecycle import fire_and_forget_vm_delete
from app.release_qualifier_redrive import (
    _MAX_ERROR_REDRIVE_ATTEMPTS,  # noqa: F401 — re-exported for tests
    _MAX_INFRA_FAILURE_REDRIVE_ATTEMPTS,  # noqa: F401 — re-exported for tests
    _MAX_PROMOTION_FAILURE_REQUALIFY_ATTEMPTS,  # noqa: F401 — re-exported for release_qualifier
    _MAX_RUBRIC_REDRIVE_ATTEMPTS,  # noqa: F401 — re-exported for tests
    _MAX_WALKTHROUGH_CONTRACT_REDRIVE_ATTEMPTS,  # noqa: F401 — re-exported for tests
    _MAX_DEFERRED_UNSYNCED_WAIT_SECONDS,  # noqa: F401 — re-exported for tests
    _PROMOTION_MECHANICS_FAILURES,  # noqa: F401 — re-exported for tests
    _TRIGGER_ROUTING_DECISION_KEY,  # noqa: F401 — re-exported for tests
    _TRANSIENT_ERROR_CODES,  # noqa: F401 — re-exported for tests
    _redrive_deferred_unsynced_qualification,  # noqa: F401 - re-exported for tests
    _redrive_error_qualification,
    _redrive_infra_failure_qualification,
    _redrive_promotion_failed_qualification,  # noqa: F401 - re-exported for tests
    _redrive_rubric_changed_qualification,
    _record_discarded_trigger_routing,  # noqa: F401 — re-exported for tests
    _redrive_walkthrough_contract_qualification,
    run_redrive_legs,
)
from app.release_qualifier_hotfix_audit import (
    _hotfix_audit_recovery_candidate_ids,
    _hotfix_audit_was_attempted,  # noqa: F401 — re-exported for tests
    _reconcile_hotfix_audit_trail,
)
from app.release_sweep_crash_cause import (
    _MAX_SWEEP_CRASH_STREAK,
    _SWEEP_CRASH_EXCEPTION_CLASS_MAX,  # noqa: F401 — re-exported for tests
    _SWEEP_CRASH_FRAME_COUNT,  # noqa: F401 — re-exported for tests
    _SWEEP_CRASH_FRAME_MAX,  # noqa: F401 — re-exported for tests
    _SWEEP_CRASH_MESSAGE_MAX,  # noqa: F401 — re-exported for tests
    _bound_sweep_finalize_crash_cause,
    _capture_sweep_finalize_crash_cause,
    _format_sweep_finalize_crash_cause,
)
from app.release_full_suite_drift_sentinel import (
    # Re-exports -- release_qualifier_sweep stays the canonical import and
    # test-patch surface for the standing drift sentinel; the implementation
    # lives in release_full_suite_drift_sentinel.py.  noqa pins keep a
    # ruff --fix sweep from dropping them as "unused".
    _DRIFT_SENTINEL_CANCELLED_REDISPATCH_COOLDOWN_SECONDS,  # noqa: F401
    _DRIFT_SENTINEL_CANCELLED_REDISPATCH_KEY,  # noqa: F401 -- re-exported for tests
    _DRIFT_SENTINEL_INHERITED_TREE_KEY,  # noqa: F401 -- re-exported for tests
    _DRIFT_SENTINEL_RERUN_BUDGET,  # noqa: F401 -- re-exported for tests
    _DRIFT_SENTINEL_RERUN_KEY,  # noqa: F401 -- re-exported for tests
    _DRIFT_SENTINEL_TERMINAL_DISPATCH_KEY,  # noqa: F401 -- re-exported for tests
    _check_full_suite_drift_sentinel,
    _drift_sentinel_cancelled_redispatch_due,  # noqa: F401 -- re-exported for tests
    _retire_sentinel_incidents,  # noqa: F401 -- re-exported for tests
)
from app.release_qualifier_auto_trigger import (
    # Re-export -- test_promotion_read_surface reads the executor back from this
    # module with ``inspect.getsource``; the implementation lives in
    # release_qualifier_auto_trigger.py.
    _fire_auto_triggered_qualification,
)

logger = logging.getLogger(__name__)


# Matches the design-pinned JSON cap.  Raised 2150 -> 2250 to seat the
# promotion_failed / deferred_unsynced re-driver loops (the executors the
# trigger resolver's T5/T5b decisions had been dead-ending without).
_MODULE_SIZE_GOVERNANCE_CAP: int = 2150


# Audit H2: hard wall-clock bound on a single qualification.  A qualification
# spawns the current audit / goal-grade agent children,
# each of which may itself retry — but if a child heartbeats-but-hangs, gets
# stuck in a recovery/requeue loop, or otherwise never reaches a terminal
# state, the version wedges at last_qualification.status == "pending" forever
# with no signal.  sweep_stuck_qualifications force-finalizes any pending
# qualification older than this as a blocked one (blocker
# "qualification_timeout") and raises an Alert.  Six hours mirrors the
# EVALUATING wall-clock backstop in services.py — long enough for a genuinely
# progressing two-task pipeline (including retries) to finish, short enough
# that a wedged qualification surfaces within a useful operator window.
_MAX_QUALIFICATION_DURATION_SECONDS = 6 * 60 * 60

# How long a ``serving_status="deploy_failed"`` version remains eligible for
# serving RECOVERY.  A deploy_failed latch is not terminal-by-construction: a
# deploy that boot-crashed and recovered, or was merely slow past the ~60-min
# confirmation window (the real v5 case promoted 14:58 UTC and only began serving
# at 19:51 UTC — ~5h late), can later actually serve its promoted SHA.  Within
# this window the post-promotion sweep re-checks deploy_failed versions and
# recovers any whose promoted SHA is now independently observed serving; beyond
# it a genuinely dead deploy stays loudly failed forever.  24h is generous enough
# to cover the v5 ~5h-late case with wide margin.
_DEPLOY_FAILED_RECOVERY_WINDOW_SECONDS = 24 * 3600


async def _serving_confirmation_candidate_ids(session) -> list:
    """Return promoted versions whose serving evidence still needs a check.

    The legacy version scalar remains a fast path for newly promoted releases,
    but it is not the complete candidate state: optional provider rows remain
    observable after the required aggregate confirms.  Include every persisted
    row for a registered provider with a NULL/unconfirmed status so an optional
    dimension such as ``agent_image`` cannot freeze after backend serving is
    confirmed.  There is intentionally no fixed row cap: every candidate is
    visited each sweep, in deterministic promoted-at/id order, and each
    provider check already has its own confirmation deadline bound.
    """
    registered_artifact_kinds = tuple(
        provider.artifact_kind for provider in iter_release_artifact_providers()
    )
    required_artifact_kinds = tuple(
        provider.artifact_kind
        for provider in iter_release_artifact_providers()
        if provider.required
    )
    unresolved_filter = release_version.ReleaseVersion.serving_status == "unconfirmed"
    if registered_artifact_kinds:
        pending_artifact_exists = exists(
            select(ReleaseDeployArtifact.id).where(
                ReleaseDeployArtifact.version_id == release_version.ReleaseVersion.id,
                ReleaseDeployArtifact.artifact_kind.in_(registered_artifact_kinds),
                or_(
                    ReleaseDeployArtifact.serving_status.is_(None),
                    ReleaseDeployArtifact.serving_status == "unconfirmed",
                ),
            )
        )
        unresolved_filter = or_(unresolved_filter, pending_artifact_exists)
    if required_artifact_kinds:
        terminal_mismatch_exists = exists(
            select(ReleaseDeployArtifact.id).where(
                ReleaseDeployArtifact.version_id == release_version.ReleaseVersion.id,
                ReleaseDeployArtifact.artifact_kind.in_(required_artifact_kinds),
                ReleaseDeployArtifact.serving_status.in_(
                    ("deploy_failed", "confirmation_timed_out")
                ),
            )
        )
        unresolved_filter = or_(
            unresolved_filter,
            and_(
                or_(
                    release_version.ReleaseVersion.serving_status.is_(None),
                    release_version.ReleaseVersion.serving_status
                    == "serving_confirmed",
                ),
                terminal_mismatch_exists,
            ),
        )
    return list(
        (
            await session.execute(
                select(release_version.ReleaseVersion.id)
                .where(
                    release_version.ReleaseVersion.status == "promoted",
                    unresolved_filter,
                )
                .order_by(
                    release_version.ReleaseVersion.promoted_at.asc().nullsfirst(),
                    release_version.ReleaseVersion.id.asc(),
                )
            )
        )
        .scalars()
        .all()
    )


def _parse_started_at(qual: dict[str, Any]) -> datetime | None:
    """Parse ``last_qualification["started_at"]`` to a datetime, or None.

    ``started_at`` is an ISO-8601 string stamped by ``run_qualification``.
    Returns None when it is missing or unparseable.

    Audit R4-H5: a None return must NOT defeat the qualification-timeout
    backstop — ``_finalize_locked_version`` falls back to a real row timestamp
    (``version.updated_at``) when this returns None, so a corrupt
    ``started_at`` string can never wedge a qualification forever.  The
    fallback lives in the caller (not here) because this helper has no access
    to the ``ReleaseVersion`` row.
    """
    raw = qual.get("started_at")
    if not isinstance(raw, str) or not raw:
        return None
    try:
        return datetime.fromisoformat(raw)
    except ValueError:
        return None


async def _cancel_qualifier_children(
    session: AsyncSession,
    version: release_version.ReleaseVersion,
) -> None:
    """Best-effort cancel of all qualifier child tasks for a force-finalized version.

    Reads current child ids plus legacy ``smoketest_task_id`` for cleanup only, and
    ``merge_grade_task_id`` from ``version.last_qualification``, marks each
    non-terminal task as ``CANCELLED`` with ``vm_stopped_at`` set to now, and
    fire-and-forgets VM teardown.  Best-effort per child: a failure on one
    child (missing row, DB error, etc.) does not prevent cancelling the others
    or writing the force-finalize record.
    """
    qual = version.last_qualification or {}
    child_keys = [
        "audit_task_id",
        "smoketest_task_id",
        "challenge_task_id",
        "merge_grade_task_id",
        "walkthrough_e2e_task_id",
    ]
    now = _utcnow()

    # Batch-fetch all child tasks in one query instead of N+1 session.get calls.
    child_ids = [
        qual[k] for k in child_keys if isinstance(qual.get(k), str)
    ]
    children_by_id: dict[str, Task] = {}
    if child_ids:
        children_by_id = {
            str(t.id): t
            for t in (
                (await session.execute(select(Task).where(Task.id.in_(child_ids))))
                .scalars()
                .all()
            )
        }

    for child_key in child_keys:
        child_id = qual.get(child_key)
        if not child_id or not isinstance(child_id, str):
            continue
        try:
            task = children_by_id.get(child_id)
            if task is None:
                continue
            if task.status in (
                TaskStatus.COMPLETED,
                TaskStatus.FAILED,
                TaskStatus.CANCELLED,
                TaskStatus.BLOCKED,
            ):
                continue
            task.status = TaskStatus.CANCELLED
            task.vm_stopped_at = now
            session.add(task)
            fire_and_forget_vm_delete(task)
        except Exception:
            logger.exception(
                "[RELEASE_QUALIFIER] sweep: failed to cancel child task %s "
                "for version %s",
                child_id,
                version.id,
            )


async def _force_finalize_timed_out_qualification(
    session: AsyncSession,
    version: release_version.ReleaseVersion,
    age_seconds: float,
) -> None:
    """Force-finalize a qualification that has been ``pending`` too long.

    Audit H2: a qualifier child that heartbeats-but-hangs (or is stuck in a
    recovery/requeue loop) never reaches a terminal state, so neither
    ``maybe_finalize_qualification`` nor the both-children-terminal branch of
    the sweep ever fires and the version wedges at ``pending`` forever.  This
    is the unconditional escape hatch — called regardless of child state once
    the qualification exceeds ``_MAX_QUALIFICATION_DURATION_SECONDS``.

    Records ``last_qualification.status = "infra_failure"`` with
    ``blocker = "qualification_timeout"``, sends the version back to the
    ``stabilize`` phase (so the next strategic cycle re-targets it), and raises
    an ``Alert`` so the stuck qualification is operator-visible.  The version
    stays ``open`` — a timed-out qualification only ever blocks, never
    promotes, which is always safe.

    Special case: if the candidate is already reachable from the promotion
    target (out-of-band promotion), records ``blocker = "qualification_superseded"``
    instead so the next qualification engages the already-promoted exemption.
    Fail-closed: if GitHub credentials or checks fail, falls back to the generic
    ``qualification_timeout`` blocker (never fabricates a supersession).  The
    caller commits the session.
    """
    # Cancel any running child tasks before force-finalizing (best-effort).
    await _cancel_qualifier_children(session, version)

    qual = dict(version.last_qualification or {})
    age_minutes = age_seconds / 60.0
    candidate_sha: str | None = qual.get("candidate_sha")
    blocker = "qualification_timeout"
    blocker_detail_suffix = "a qualifier child never reached a terminal state"

    # Check for out-of-band supersession: if the candidate is already reachable
    # from the promotion target, prefer qualification_superseded provenance
    # (so the already-promoted exemption engages) over the generic timeout.
    # Fail-closed: GitHub credential/API failures leave the default timeout blocker.
    if candidate_sha:
        try:
            project = await session.get(Project, version.project_id)
            if project is not None:
                resolved = await _resolve_github_with_session(session, project)
                if resolved is not None:
                    from app import release_promotion

                    token, owner, repo = resolved
                    if await release_promotion._candidate_already_on_main(
                        token, owner, repo, candidate_sha, version, project=project
                    ):
                        blocker = "qualification_superseded"
                        blocker_detail_suffix = (
                            f"candidate {candidate_sha[:12]} is already reachable from "
                            f"promotion target without a qualifier-initiated promotion"
                        )
                        logger.info(
                            "[RELEASE_QUALIFIER] sweep: version v%d candidate %s "
                            "already on promotion target — recording as "
                            "qualification_superseded instead of timeout",
                            version.version_number,
                            candidate_sha[:8],
                        )
                else:
                    logger.warning(
                        "[RELEASE_QUALIFIER] sweep: no GitHub credentials for v%d — "
                        "cannot check out-of-band supersession; using qualification_timeout",
                        version.version_number,
                    )
        except Exception:
            logger.exception(
                "[RELEASE_QUALIFIER] sweep: out-of-band supersession check failed "
                "for v%d — falling back to qualification_timeout",
                version.version_number,
            )

    detail = (
        f"qualification pending {age_minutes:.0f} min "
        f"(> {_MAX_QUALIFICATION_DURATION_SECONDS // 60} min limit) — {blocker_detail_suffix}"
    )
    record = {
        **qual,
        "status": "infra_failure",
        "blocker": blocker,
        "detail": detail,
        "finalized_at": _utcnow().isoformat(),
    }
    version.last_qualification = record
    flag_modified(version, "last_qualification")
    release_version.set_version_phase(
        version,
        "stabilize",
        reasoning=(
            f"qualification v{version.version_number} timed out "
            f"({age_minutes:.0f} min) — returning to stabilize"
        ),
    )
    session.add(version)

    session.add(
        Alert(
            project_id=version.project_id,
            severity=AlertSeverity.WARNING,
            title=(f"Release qualification v{version.version_number} timed out"),
            message=(
                f"Qualification for release v{version.version_number} was "
                f"stuck pending for {age_minutes:.0f} minutes — a qualifier "
                f"required qualifier child never reached a "
                f"terminal state. Force-finalized as a blocked qualification "
                f"(blocker={blocker}); the version returns to the "
                f"stabilize phase for the next strategic cycle."
            ),
            event_type="release.qualification_timeout",
        )
    )
    logger.warning(
        "[RELEASE_QUALIFIER] sweep: version v%d qualification timed out "
        "(pending %.0f min) — force-finalized as %s",
        version.version_number,
        age_minutes,
        blocker,
    )


async def _force_finalize_sweep_crash_loop(
    session: AsyncSession,
    version: release_version.ReleaseVersion,
    streak: int,
    crash_cause: dict[str, Any] | None = None,
) -> None:
    """Force-finalize a qualification whose sweep finalize has crash-looped.

    Called when _finalize_in_dedicated_session has raised on every consecutive
    sweep attempt for this version (_MAX_SWEEP_CRASH_STREAK times).  Records
    last_qualification.status='infra_failure' with
    blocker='sweep_finalize_crash_loop', resets the version to stabilize phase,
    raises an Alert, and notifies the operator.  The caller commits the session.
    """
    qual = dict(version.last_qualification or {})
    crash_cause = _bound_sweep_finalize_crash_cause(
        crash_cause
        or qual.get("sweep_finalize_cause")
        or {
            "exception_class": "unknown",
            "message": "finalize exception cause was not captured",
            "traceback_digest": [],
        }
    )
    cause_text = _format_sweep_finalize_crash_cause(crash_cause)
    # Cancel any running child tasks before force-finalizing (best-effort).
    await _cancel_qualifier_children(session, version)

    record = {
        **qual,
        "status": "infra_failure",
        "blocker": "sweep_finalize_crash_loop",
        "detail": (
            f"sweep finalize raised on {streak} consecutive sweep(s) — "
            f"force-finalized to unblock the qualification loop; cause: {cause_text}"
        ),
        "finalized_at": _utcnow().isoformat(),
        "sweep_crash_streak": 0,
        "sweep_finalize_cause": crash_cause,
    }
    version.last_qualification = record
    flag_modified(version, "last_qualification")
    release_version.set_version_phase(
        version,
        "stabilize",
        reasoning=(
            f"qualification v{version.version_number} sweep crash-looped "
            f"({streak} consecutive finalize errors) — returning to stabilize"
        ),
    )
    session.add(version)
    session.add(
        Alert(
            project_id=version.project_id,
            severity=AlertSeverity.WARNING,
            title=f"Release qualification v{version.version_number} sweep crash-loop",
            message=(
                f"The qualification sweep for release v{version.version_number} "
                f"has raised {streak} consecutive finalize errors. "
                f"Force-finalized as sweep_finalize_crash_loop; the version "
                f"returns to the stabilize phase. Cause: {cause_text}"
            ),
            event_type="release.qualification_crash_loop",
        )
    )
    await _notify_operator(
        session,
        version,
        "release_qualification_crash_loop",
        (
            f"v{version.version_number} qualification sweep crash-looped "
            f"({streak} consecutive finalize errors) — force-finalized, "
            f"returned to stabilize. Cause: {cause_text}"
        ),
        extra={
            "consecutive_finalize_errors": streak,
            "sweep_finalize_cause": crash_cause,
        },
    )
    logger.warning(
        "[RELEASE_QUALIFIER] sweep: version v%d finalize crash-looped "
        "(%d consecutive errors) — force-finalized as sweep_finalize_crash_loop",
        version.version_number,
        streak,
    )


# Auto-qualification debounce seams; the marker key is re-exported for tests.
from app.release_qualifier_debounce import (  # noqa: E402
    _AUTOMAIN_HEAD_FIRST_SEEN_KEY,  # noqa: F401 — re-exported for tests
    compute_automain_head_quiet_seconds,  # noqa: F401 — re-exported for tests
    find_live_qualifier_audit_task_id,  # noqa: F401 — re-exported for tests
)


async def _run_lifecycle_stall_alerts(
    open_versions,
    *,
    session_factory=None,
) -> None:
    """Run the per-version lifecycle-stall alert checks on fresh row state.

    For each open version this opens its OWN dedicated session and RE-SELECTs the
    version row inside that transaction rather than merging the sweep-START
    snapshot.  The stall checks that mutate ``last_qualification``
    (notably ``_check_version_managed_check_wait_stall``)
    rebuild the verdict from ``version.last_qualification``; if that value is the
    stale sweep-start snapshot (as it was under the old ``merge(load=False)`` of
    the outer-scan object), the rebuilt verdict clobbers a ``last_qualification``
    that another writer committed between sweep start and now — the re-driver
    loops in ``sweep_stuck_qualifications`` (finalize, initial-fire, auto-trigger)
    and out-of-band completion handlers all write that column on this same row in
    their own sessions.  Re-selecting fresh makes every check operate on current
    state, so a concurrently committed verdict is never overwritten.

    A messaging failure on one version is logged and cannot affect another (each
    version is fully isolated in its own session/transaction).
    """
    factory = session_factory or AsyncSessionLocal
    for version in open_versions:
        try:
            async with factory() as alert_session:
                v = (
                    await alert_session.execute(
                        select(release_version.ReleaseVersion).where(
                            release_version.ReleaseVersion.id == version.id
                        )
                    )
                ).scalar_one_or_none()
                # The row could have been deleted between the outer scan and now;
                # nothing to alert on if it is gone.
                if v is None:
                    continue
                alerted = await _check_version_calcification(alert_session, v)
                alerted = (
                    await _check_version_ci_unresolved_stall(alert_session, v)
                    or alerted
                )
                alerted = await _check_version_build_stall(alert_session, v) or alerted
                alerted = (
                    await _check_version_stabilize_stall(alert_session, v) or alerted
                )
                alerted = (
                    await _check_version_promotion_readiness(alert_session, v)
                    or alerted
                )
                alerted = (
                    await _check_version_managed_check_wait_stall(alert_session, v)
                    or alerted
                )
                # Resolution is a first-class lifecycle result too. The
                # source-condition checkers may only dismiss an old alert and
                # return False, so commit the dedicated session after every
                # version regardless of whether a new row was raised.
                await alert_session.commit()
        except Exception:
            logger.exception(
                "[RELEASE_QUALIFIER] lifecycle-stall check failed for version %s",
                version.id,
            )


async def _recheck_ci_compliance_blocked_version(
    version_id: Any, *, session_factory=None
) -> bool:
    """Re-evaluate an open version whose qualify_request was CI-compliance-blocked.

    Closes the block→repair→requalify loop on the sweep cadence — the piece that
    no sweep previously owned.  The qualify_request block site spawns the
    cap-bump repair and records ``repair_task``; this sweep step is the backstop
    that keeps re-evaluating the version until the guardrail is clean:

      * STILL BLOCKED — ensure a cap-bump repair task is queued (deduped per
        project + HEAD) and keep the durable block marker.  Covers versions
        blocked before the block site gained its own spawn and repairs that were
        cancelled or failed.
      * BLOCK CLEARED — the integration HEAD no longer breaches the guardrails:
        drop the stale ``qualify_request_blocked`` / ``qualify_request_blocked_at``
        keys so the version row is truthful again, then arm a Strategic refresh
        so the strategist re-issues its deferred qualify_request.

    Returns True only when the block was cleared (the version row changed).  A
    still-blocked / no-op / deferral outcome returns False.  Runs in its own
    dedicated session (the sweep's per-version isolation): the version is locked
    so a concurrent explicit qualification serializes against the marker clear.
    """
    from app import ci_compliance as _ci_compliance
    from app.strategic_spawning import (
        _maybe_spawn_ci_compliance_cap_bump_fix,
        ci_compliance_qualify_gate,
    )

    factory = session_factory or AsyncSessionLocal
    async with factory() as recheck_session:
        version = await _lock_project_then_version(recheck_session, version_id)
        if version is None:
            return False
        qual = (
            version.last_qualification
            if isinstance(version.last_qualification, dict)
            else {}
        )
        blocked = qual.get("qualify_request_blocked")
        if not isinstance(blocked, dict) or blocked.get("blocker") != "ci_compliance":
            return False
        project = await recheck_session.get(Project, version.project_id)
        if project is None:
            return False
        try:
            status = await _ci_compliance.ensure_current_ci_compliance(
                recheck_session, project
            )
        except Exception:  # noqa: BLE001 — best-effort sweep; defer to next cycle
            logger.exception(
                "[RELEASE_QUALIFIER] sweep: ci_compliance re-check raised for "
                "version %s; deferring",
                version_id,
            )
            await recheck_session.rollback()
            return False
        if status.get("status") != "success":
            # Compliance cannot be resolved this cycle (GitHub outage / missing
            # credentials) — leave the marker and retry on the next sweep.
            await recheck_session.rollback()
            return False
        gate = ci_compliance_qualify_gate(project, status.get("head_sha"))
        if gate.get("blocked"):
            # Still violating — backstop the repair spawn (deduped).  The block
            # marker already points at a repair_task when the block site ran; a
            # cancelled repair is re-queued here.
            await _maybe_spawn_ci_compliance_cap_bump_fix(
                recheck_session, project, gate
            )
            await recheck_session.commit()
            return False
        # Block cleared — drop the stale marker and re-arm Strategic so the
        # deferred qualify_request is re-issued on the next cycle.
        version.last_qualification = {
            k: v
            for k, v in qual.items()
            if k not in ("qualify_request_blocked", "qualify_request_blocked_at")
        }
        flag_modified(version, "last_qualification")
        recheck_session.add(version)
        await recheck_session.commit()
        try:
            from app.strategic_trigger import request_strategic_refresh

            async with factory() as refresh_session:
                armed = await request_strategic_refresh(
                    refresh_session,
                    version.project_id,
                    "CI-compliance block cleared; qualification may proceed",
                    skip_locked=False,
                )
                if armed:
                    await refresh_session.commit()
                else:
                    await refresh_session.rollback()
        except Exception:  # noqa: BLE001 — the marker is already cleared
            logger.warning(
                "[RELEASE_QUALIFIER] sweep: ci_compliance block cleared for "
                "version %s but Strategic refresh could not be armed",
                version_id,
                exc_info=True,
            )
        return True


async def _current_integration_audited_surface(
    session: AsyncSession, project: Project
) -> dict[str, str] | None:
    """Read the audited-surface map for the live integration HEAD."""
    resolved = await _resolve_github_with_session(session, project)
    if resolved is None:
        return None
    token, owner, repo = resolved
    from app import github, release_qualifier
    from app.release_qualifier_audit_surface import audited_surface_path_map
    try:
        integration_branch, _ = release_qualifier._lts_branch_names(project)
        head_sha = await github.get_branch_sha(
            token, owner, repo, integration_branch
        )
        if not head_sha:
            return None
        entries = await github.list_repo_tree_paths_at_ref(
            token, owner, repo, head_sha, project_id=str(project.id)
        )
    except Exception:  # noqa: BLE001 — readiness remains fail-open on GitHub errors
        logger.warning(
            "[RELEASE_QUALIFIER] integration audited-surface lookup failed for %s",
            project.id,
            exc_info=True,
        )
        return None
    return None if entries is None else audited_surface_path_map(entries)


async def sweep_stuck_qualifications(
    session: AsyncSession,
    *,
    session_factory=None,
) -> int:
    """Finalize qualifications stuck ``pending`` past the normal completion path.

    ``maybe_finalize_qualification`` is the normal path: a child terminating
    fires a completion handler that finalizes the version once its sibling is
    also terminal.  But if a child dies *without* its handler running — handler
    exception, orchestrator restart between the child's terminal write and the
    handler, a child that was force-cancelled out of band — the version is left
    with ``last_qualification.status == "pending"`` forever even though its
    children have finished.

    This sweep is the defense-in-depth reaper for that case.  For every
    ``ReleaseVersion`` whose ``last_qualification.status == "pending"`` it
    handles two situations:

      * **Both required children terminal** (``COMPLETED`` / ``FAILED`` /
        ``CANCELLED``) — finalizes the version.  Legacy qualifier-smoke ids never gate this branch.

      * **Pending longer than ``_MAX_QUALIFICATION_DURATION_SECONDS``** (audit
        H2) — force-finalizes the qualification as blocked (blocker
        ``qualification_timeout``) regardless of child state and raises an
        Alert.  This is the escape hatch for a child that heartbeats-but-hangs
        and so never reaches a terminal state.

    **Per-version session isolation (replaces the old per-version commit —
    audit H5).**  ``session`` is used ONLY for the read-only scan that lists the
    candidate version ids; the sweep never writes through it.  Each version is
    then finalized via ``_finalize_in_dedicated_session``, which opens its OWN
    session, locks that one version, finalizes it, and commits — in complete
    isolation.  A failure (or a real GitHub merge) on version B therefore
    cannot roll back version A's already-committed finalization: they were
    never in the same transaction.  This is strictly stronger than the old
    "commit after each version on the shared session" pattern and removes the
    shared-session rollback/expire bookkeeping entirely.

    Idempotent and safe to call repeatedly: a version already finalized (or not
    actually ready) is no-op'd by ``_finalize_in_dedicated_session``'s own
    under-lock guards.  Returns the number of versions actually finalized.
    """
    # ``_finalize_in_dedicated_session`` stays in release_qualifier (the core
    # finalize chain); import it lazily to avoid a load-time import cycle
    # (release_qualifier imports this module at top for its re-exports).
    from app import release_qualifier

    # Only ``open`` versions can be mid-qualification — a promoted/abandoned
    # version is terminal.  This keeps the scan bounded (≈ one open version per
    # project) and avoids a JSONB-path WHERE clause that differs across the
    # Postgres / SQLite-test dialects; the pending filter is applied inside the
    # per-version dedicated session under the row lock.
    open_versions = list(
        (
            await session.execute(
                select(release_version.ReleaseVersion).where(
                    release_version.ReleaseVersion.status == "open"
                )
            )
        )
        .scalars()
        .all()
    )
    # Snapshot just the IDs — each version is re-fetched under its own
    # FOR UPDATE lock in its own dedicated session, so the id is all that is
    # carried out of the scan.
    version_ids = [v.id for v in open_versions]
    # Snapshot each version's pre-sweep qualification status.  Several sweep
    # legs below (finalize, the timeout/check-wait reconciles) transition a
    # version INTO a state the trigger resolver considers fire-eligible in the
    # SAME cycle (e.g. promotion_pending_timeout -> promotion_failed).  The
    # auto-trigger must not fire on a transition this cycle just made — those
    # transitions document an explicit "re-arm next cycle" intent — so the
    # executor defers any version whose status changed since this snapshot.
    pre_sweep_statuses: dict = {
        v.id: (v.last_qualification or {}).get("status") for v in open_versions
    }

    # Evidence upkeep is deployed-side bookkeeping, not a release decision.
    # Keep it on the sweep after removing scheduler-owned qualification starts:
    # the minter is idempotent and records only when the acceptance window is
    # populated, while Strategic remains solely responsible for acting on it.
    from app.acceptance_criteria import record_acceptance_window_coverage_evidence

    for version_id in version_ids:
        factory = session_factory or AsyncSessionLocal
        try:
            async with factory() as evidence_session:
                evidence_version = await evidence_session.get(
                    release_version.ReleaseVersion, version_id
                )
                if evidence_version is None:
                    continue
                recorded = await record_acceptance_window_coverage_evidence(
                    evidence_session, evidence_version
                )
                if recorded:
                    await evidence_session.commit()
                else:
                    await evidence_session.rollback()
        except Exception:
            logger.exception(
                "[RELEASE_QUALIFIER] sweep: acceptance evidence refresh failed "
                "for version %s",
                version_id,
            )

    # The durable rehearsal dispatcher runs before this sweep. Once it has
    # persisted a candidate-bound receipt, resume only the qualification that
    # was explicitly parked waiting for that receipt. This path is independent
    # of the general auto-trigger knob: it completes an already-authorized
    # attempt and remains inert for missing, invalid, or stale evidence.
    from app.release_prepromotion_rehearsal import (
        redrive_rehearsal_ready_qualification,
    )

    for version_id in version_ids:
        try:
            redriven = await redrive_rehearsal_ready_qualification(
                version_id,
                session_factory=session_factory,
            )
        except Exception:
            logger.exception(
                "[RELEASE_QUALIFIER] sweep: rehearsal-ready re-drive failed "
                "for version %s",
                version_id,
            )
            continue
        if redriven:
            logger.info(
                "[RELEASE_QUALIFIER] sweep re-drove rehearsal-ready version %s",
                version_id,
            )

    # Hotfixes may already be promoted, so this recovery scan is deliberately
    # independent of the open-version qualification loops below.  Candidate
    # discovery filters ordinary history and fully-reconciled hotfixes before
    # this loop opens any dedicated sessions or takes version locks.
    hotfix_version_ids = await _hotfix_audit_recovery_candidate_ids(session)

    now = _utcnow()
    finalized = 0
    for version_id in hotfix_version_ids:
        try:
            if await _reconcile_hotfix_audit_trail(
                version_id, session_factory=session_factory
            ):
                finalized += 1
        except Exception:
            logger.exception(
                "[RELEASE_QUALIFIER] sweep: hotfix audit reconciliation failed for "
                "version %s",
                version_id,
            )
    for version_id in version_ids:
        # Each version finalizes in its own dedicated session (concern d + the
        # audit-H5 isolation guarantee): a failure here is contained and cannot
        # touch another version's already-committed work.
        try:
            result = await release_qualifier._finalize_in_dedicated_session(
                version_id,
                session_factory=session_factory,
                timeout_now=now,
            )
        except Exception as exc:
            crash_cause = _capture_sweep_finalize_crash_cause(exc)
            logger.exception(
                "[RELEASE_QUALIFIER] sweep: failed to finalize stuck "
                "qualification for version %s",
                version_id,
            )
            # Track consecutive sweep crashes on this version.  When the streak
            # reaches _MAX_SWEEP_CRASH_STREAK, force-finalize to unblock the loop
            # so the same crash cannot recur on every subsequent sweep.
            _factory = session_factory or AsyncSessionLocal
            try:
                async with _factory() as crash_session:
                    locked_v = await _lock_project_then_version(
                        crash_session, version_id
                    )
                    if (
                        locked_v is not None
                        and isinstance(locked_v.last_qualification, dict)
                        and locked_v.last_qualification.get("status") == "pending"
                    ):
                        streak = (
                            locked_v.last_qualification.get("sweep_crash_streak") or 0
                        ) + 1
                        if streak >= _MAX_SWEEP_CRASH_STREAK:
                            await _force_finalize_sweep_crash_loop(
                                crash_session, locked_v, streak, crash_cause
                            )
                        else:
                            locked_v.last_qualification = {
                                **locked_v.last_qualification,
                                "sweep_crash_streak": streak,
                                "sweep_finalize_cause": crash_cause,
                            }
                            flag_modified(locked_v, "last_qualification")
                            crash_session.add(locked_v)
                        await crash_session.commit()
            except Exception:
                logger.exception(
                    "[RELEASE_QUALIFIER] sweep: crash-streak update failed "
                    "for version %s",
                    version_id,
                )
            continue
        if (
            result is not None
            and result.get("status")
            != release_qualification_deferral.QUALIFICATION_FINALIZATION_DEFERRED
        ):
            finalized += 1

    if finalized:
        logger.info(
            "[RELEASE_QUALIFIER] sweep finalized %d stuck qualification(s)",
            finalized,
        )

    # Re-drive qualifications stranded at ``status=error`` due to a transient
    # GitHub failure (sha_error, no_credentials).  A single outage parks the
    # version indefinitely because the strategist reads a standing error as
    # settled.  This loop re-enters run_qualification for each open,
    # stabilize-phase version with a transient error code, subject to a bounded
    # attempt cap (_MAX_ERROR_REDRIVE_ATTEMPTS) so a permanently broken path
    # does not retry forever.
    for version_id in version_ids:
        try:
            redriven = await _redrive_error_qualification(
                version_id,
                session_factory=session_factory,
            )
        except Exception:
            logger.exception(
                "[RELEASE_QUALIFIER] sweep: error re-drive failed "
                "for version %s",
                version_id,
            )
            continue
        if redriven:
            logger.info(
                "[RELEASE_QUALIFIER] sweep re-drove error version %s "
                "(transient error cleared — qualification spawned)",
                version_id,
            )

    # Re-drive qualifications stranded in ``infra_failure`` — the only
    # non-conclusive outcome with no scheduled re-entry.  A transient
    # infrastructure failure (qualifier child timeout, GitHub API unavailable,
    # migration-identity gate unreachable) records ``status=infra_failure`` and
    # parks the version indefinitely because the strategist treats it as
    # "re-qualify needed" but only acts on a fresh strategic cycle; no
    # automatic retry fires.  This loop re-enters run_qualification for each
    # open, stabilize-phase ``infra_failure`` version, subject to a per-episode
    # cap (_MAX_INFRA_FAILURE_REDRIVE_ATTEMPTS) so a persistently broken
    # infra path does not retry forever.  Runs AFTER the error re-driver loop
    # (``status=error`` re-drives above) because both are transient-outcome
    # re-drivers operating on the same version_ids snapshot.
    for version_id in version_ids:
        try:
            redriven = await _redrive_infra_failure_qualification(
                version_id,
                session_factory=session_factory,
            )
        except Exception:
            logger.exception(
                "[RELEASE_QUALIFIER] sweep: infra_failure re-drive failed "
                "for version %s",
                version_id,
            )
            continue
        if redriven:
            logger.info(
                "[RELEASE_QUALIFIER] sweep re-drove infra_failure version %s "
                "(transient infra cleared — qualification spawned)",
                version_id,
            )

    # Re-drive qualifications stranded at ``status=rubric_changed`` /
    # ``rubric_provenance_unknown``.  A mid-flight rubric change invalidates the
    # in-flight attempt — it measured nothing under the final rubric — so a
    # FRESH grade against the CURRENT rubric is the recovery path.  Until now
    # the only resume path was the knob-gated auto-trigger leg (``observe`` in
    # production), so a rubric hold parked silently with no self-heal.  This
    # loop is the unconditional relaunch, bounded by
    # _MAX_RUBRIC_REDRIVE_ATTEMPTS so a rubric that keeps changing cannot spin
    # attempts indefinitely (the cap raises an operator Alert and halts).  Runs
    # AFTER the error/infra re-drivers (same version_ids snapshot) and BEFORE
    # the auto-trigger leg, which explicitly excludes rubric holds — the bespoke
    # re-driver owns them now.
    for version_id in version_ids:
        try:
            redriven = await _redrive_rubric_changed_qualification(
                version_id,
                session_factory=session_factory,
            )
        except Exception:
            logger.exception(
                "[RELEASE_QUALIFIER] sweep: rubric_changed re-drive failed "
                "for version %s",
                version_id,
            )
            continue
        if redriven:
            logger.info(
                "[RELEASE_QUALIFIER] sweep re-drove rubric_changed version %s "
                "(fresh grade spawned against the current rubric)",
                version_id,
            )

    # Re-drive qualifications stranded at ``status=not_promoted`` /
    # ``blocker=walkthrough_e2e_contract_failed``.  The walkthrough
    # evidence-contract blocker is a DURABLE deployed-side verdict (the serving
    # validator rejected the report) with NO scheduled re-entry: the existing
    # not_promoted re-drive covers only criterion_coverage_incomplete,
    # reconciliation covers only ci_unresolved, and the auto-trigger executor
    # explicitly excludes not_promoted blockers other than the acceptance
    # grade.  This loop is the missing scheduled path — re-enter
    # run_qualification (with auto_trigger=True) so a conforming fix on
    # automain or a rubric/goal change releases the blocked candidate for a
    # fresh qualification, bounded by
    # _MAX_WALKTHROUGH_CONTRACT_REDRIVE_ATTEMPTS so a deployed site that keeps
    # rejecting reports cannot spin attempts forever (the cap raises an
    # operator Alert and halts).  Runs AFTER the error/infra/rubric re-drivers
    # (same version_ids snapshot) and BEFORE the auto-trigger leg, which
    # excludes this blocker — the bespoke re-driver owns it now.
    for version_id in version_ids:
        try:
            redriven = await _redrive_walkthrough_contract_qualification(
                version_id,
                session_factory=session_factory,
            )
        except Exception:
            logger.exception(
                "[RELEASE_QUALIFIER] sweep: walkthrough contract re-drive "
                "failed for version %s",
                version_id,
            )
            continue
        if redriven:
            logger.info(
                "[RELEASE_QUALIFIER] sweep re-drove walkthrough "
                "contract-failed version %s (fresh qualification spawned)",
                version_id,
            )

    # Re-evaluate open versions whose qualify_request was deferred by a
    # CI-compliance block.  The block site spawns a cap-bump repair; this loop
    # is the sweep's re-evaluation leg: while the guardrail is still red it
    # backstops the repair spawn (deduped); once the HEAD is clean it drops the
    # stale block marker and arms a Strategic refresh so the deferred
    # qualify_request is re-issued.  Runs after the transient-outcome re-drivers
    # so a version that is BOTH infra/error and block-marked is re-driven first.
    for version_id in version_ids:
        try:
            unblocked = await _recheck_ci_compliance_blocked_version(
                version_id,
                session_factory=session_factory,
            )
        except Exception:
            logger.exception(
                "[RELEASE_QUALIFIER] sweep: ci_compliance re-check failed for "
                "version %s",
                version_id,
            )
            continue
        if unblocked:
            logger.info(
                "[RELEASE_QUALIFIER] sweep: version %s CI-compliance block "
                "cleared — stale marker dropped, Strategic refresh armed",
                version_id,
            )

    # Reconcile open versions whose promotion-failure candidate is already on
    # main.  A promotion_failed version is kept open so the next strategic cycle
    # can retry, but if the blocked PR later lands out-of-band (operator merge,
    # an operator later merges it, or a transient CI block clears) nothing calls the
    # idempotency check again.  This loop fills that gap: for each open version
    # with last_qualification.status=="promotion_failed" it checks whether the
    # candidate is now reachable from main and, if so, records the promotion as
    # successful and opens the next version.
    for version_id in version_ids:
        try:
            reconciled = await _reconcile_if_promotion_failed_landed(
                version_id,
                session_factory=session_factory,
            )
        except Exception:
            logger.exception(
                "[RELEASE_QUALIFIER] sweep: reconcile check failed for version %s",
                version_id,
            )
            continue
        if reconciled:
            finalized += 1
            logger.info(
                "[RELEASE_QUALIFIER] sweep reconciled version %s "
                "(promotion_failed candidate landed on main)",
                version_id,
            )

    # Reconcile open versions recorded as not_promoted/ci_unresolved whose
    # release PR actually merged. Incident 2026-06-09: a transient DB error
    # rolled back the promotion_pending marker, a subsequent finalize saw
    # ci_unresolved (transient, not red) and recorded not_promoted, but the PR
    # merged 15 s later. This loop is the safety net for that class of ledger
    # desync. Only ci_unresolved is reconciled — hard blockers (ci_not_green,
    # migration collision, audit quality) are left untouched.
    for version_id in version_ids:
        try:
            reconciled = await _reconcile_if_not_promoted_but_pr_merged(
                version_id,
                session_factory=session_factory,
            )
        except Exception:
            logger.exception(
                "[RELEASE_QUALIFIER] sweep: not_promoted/ci_unresolved reconcile "
                "check failed for version %s",
                version_id,
            )
            continue
        if reconciled:
            finalized += 1
            logger.info(
                "[RELEASE_QUALIFIER] sweep reconciled version %s "
                "(not_promoted/ci_unresolved candidate found on main)",
                version_id,
            )

    # Reconcile every open version with a persisted promotion PR. The managed
    # waiter canonicalizes legacy rows, records out-of-band success, checks CI,
    # re-runs the migration guard, revalidates the exact base/head pair, and
    # retries the merge itself. It also records terminal red-CI, migration, and
    # protection failures. Runs BEFORE the
    # pending-superseded loop (which handles pending WITHOUT promotion_pending)
    # and BEFORE the timeout backstop so a just-merged PR is never mis-classified
    # as timed-out in the same sweep.
    for version_id in version_ids:
        try:
            reconciled = await _reconcile_if_managed_check_wait(
                version_id,
                session_factory=session_factory,
            )
        except Exception:
            logger.exception(
                "[RELEASE_QUALIFIER] sweep: managed_check_wait reconcile failed "
                "for version %s",
                version_id,
            )
            continue
        if reconciled:
            finalized += 1
            logger.info(
                "[RELEASE_QUALIFIER] sweep reconciled version %s "
                "(managed_check_wait reached terminal state)",
                version_id,
            )

    # Reconcile open versions whose pending qualification candidate landed
    # out-of-band (operator force-promotion).  A pending qualification with no
    # promotion_pending sub-key has no scheduled finalizer: if the candidate is
    # already reachable from the promotion target, record the correct supersession
    # provenance promptly rather than waiting for the 6h qualification_timeout
    # backstop which would stamp the wrong blocker.  Runs AFTER the
    # managed promotion loop (which handles pending WITH promotion_pending) so a
    # normally-landed promotion is never mis-classified as superseded.
    for version_id in version_ids:
        try:
            reconciled = await _reconcile_if_pending_qualification_superseded(
                version_id,
                session_factory=session_factory,
            )
        except Exception:
            logger.exception(
                "[RELEASE_QUALIFIER] sweep: pending_superseded reconcile check "
                "failed for version %s",
                version_id,
            )
            continue
        if reconciled:
            finalized += 1
            logger.info(
                "[RELEASE_QUALIFIER] sweep reconciled version %s "
                "(pending qualification superseded out of band)",
                version_id,
            )

    # Audit C9: hard wall-clock deadline on the promotion_pending sub-state.
    # The promotion_pending state intentionally bypasses the 6h qualification
    # timeout (the qualifier children already terminated), but nothing replaced
    # that backstop for the sub-state itself: if the promotion PR stays open
    # with pending/absent CI (or every GitHub call fails) the
    # managed reconciliation cannot reach a terminal state, the version would
    # otherwise strand indefinitely and block the whole self-improve loop.
    # This pure-DB check force-fails a promotion_pending older than the cap as
    # promotion_pending_timeout, clearing the sub-state so the version returns to
    # stabilize and the qualification-timeout backstop + retry machinery re-arm
    # next cycle.  Runs AFTER the landed/closed reconcile loops (so a PR that has
    # actually merged is recorded as a success first) and BEFORE the lifecycle
    # alerts below.
    for version_id in version_ids:
        try:
            timed_out = await _reconcile_if_promotion_pending_timed_out(
                version_id,
                session_factory=session_factory,
            )
        except Exception:
            logger.exception(
                "[RELEASE_QUALIFIER] sweep: promotion_pending timeout check "
                "failed for version %s",
                version_id,
            )
            continue
        if timed_out:
            finalized += 1
            logger.info(
                "[RELEASE_QUALIFIER] sweep force-failed version %s "
                "(promotion_pending stranded past the wall-clock cap)",
                version_id,
            )

    # Post-promotion serving confirmation.  A promoted version is PROVISIONAL
    # (serving_status == "unconfirmed") until deploy_health confirms the
    # promoted SHA is actually serving in production.  For each such version,
    # derive the outcome from the DeploymentState singleton (the existing
    # deploy-health serving signal — no new always-on monitor) in its own
    # dedicated session, and either confirm it serving or land a distinct loud
    # deploy-failed state.  Unlike the loops above this scans PROMOTED versions
    # (the rest target ``open`` ones), so it runs its own narrow query.
    unconfirmed_ids = await _serving_confirmation_candidate_ids(session)
    for version_id in unconfirmed_ids:
        try:
            resolved = await _confirm_serving_or_fail(
                version_id,
                session_factory=session_factory,
                now=now,
            )
        except Exception:
            logger.exception(
                "[RELEASE_QUALIFIER] sweep: serving-confirmation check failed "
                "for version %s",
                version_id,
            )
            continue
        if resolved:
            finalized += 1
            logger.info(
                "[RELEASE_QUALIFIER] sweep resolved post-promotion serving "
                "status for version %s",
                version_id,
            )

    # Post-promotion deploy-failure RECOVERY.  A serving_status="deploy_failed"
    # latch is not terminal-by-construction: a deploy that boot-crashed and then
    # recovered (or was merely slow past the confirmation window — the real v5
    # case served ~5h late) can later actually serve its promoted SHA.  Within a
    # bounded recovery window, re-check promoted+deploy_failed versions and flip
    # any whose promoted SHA is now INDEPENDENTLY observed serving back to a
    # truthful serving_confirmed state (clearing the version status, the operator
    # alert, the strategist crisis directive, and the verdict precision sample).
    # Beyond the window a genuinely dead deploy stays loudly failed forever.  Like
    # the unconfirmed loop this scans PROMOTED versions, so it runs its own narrow
    # query; each version recovers in its own dedicated session.
    deploy_failed_ids = list(
        (
            await session.execute(
                select(release_version.ReleaseVersion.id).where(
                    release_version.ReleaseVersion.status == "promoted",
                    release_version.ReleaseVersion.serving_status.in_(
                        ("deploy_failed", "confirmation_timed_out")
                    ),
                )
            )
        )
        .scalars()
        .all()
    )
    for version_id in deploy_failed_ids:
        try:
            recovered = await _reconcile_if_deploy_failed_recovered(
                version_id,
                session_factory=session_factory,
                now=now,
                recovery_window_seconds=_DEPLOY_FAILED_RECOVERY_WINDOW_SECONDS,
            )
        except Exception:
            logger.exception(
                "[RELEASE_QUALIFIER] sweep: deploy-failed recovery check failed "
                "for version %s",
                version_id,
            )
            continue
        if recovered:
            finalized += 1
            logger.info(
                "[RELEASE_QUALIFIER] sweep recovered deploy-failed version %s "
                "(promoted SHA confirmed serving after a prior latch)",
                version_id,
            )

    # Serving confirmation predates the authorship receipt on some releases.
    # Retry only a bounded promoted/serving-confirmed subset; the receipt writer
    # itself is idempotent and CAS-protected, so this also upgrades legacy and
    # retryable receipts without changing serving or promotion truth.
    try:
        from app.release_commit_authorship import reconcile_serving_authorship_evidence

        await reconcile_serving_authorship_evidence(
            session_factory=session_factory,
            limit=25,
        )
    except Exception:
        logger.exception(
            "[RELEASE_QUALIFIER] sweep: serving authorship reconciliation failed"
        )

    # Standing full-suite drift sentinel: canonical and ON by default, dispatch
    # ci.yml on the integration branch for each open version (the only opt-out
    # is an explicit build_config False), alert+harvest when a run concludes
    # red.  This catches full-suite CI drift before it accumulates invisibly
    # across hundreds of targeted-CI merges and surfaces at a promotion-gate
    # failure.  Each version runs in its own dedicated session via the function
    # itself.
    for version_id in version_ids:
        try:
            await _check_full_suite_drift_sentinel(
                version_id,
                session_factory=session_factory,
            )
        except Exception:
            logger.exception(
                "[RELEASE_QUALIFIER] sweep: full-suite drift sentinel failed "
                "for version %s",
                version_id,
            )

    # Lifecycle-stall checks: alert when an open version is stuck at either end
    # of the lifecycle — calcifying in stabilize (repeated hard-blocker
    # failures) or stalled in build (sitting too long without advancing).  Each
    # version gets its own dedicated session (same isolation pattern as the
    # finalize loop above) so a messaging failure on one version can't affect
    # another.  Extracted to a helper so the fresh-per-version re-read is
    # independently testable.
    await _run_lifecycle_stall_alerts(open_versions, session_factory=session_factory)

    # Re-drive the two statuses whose resolver decisions the auto-trigger
    # executor below discards as re-driver-owned: ``promotion_failed`` (T5,
    # trigger ``blockers_cleared``) and ``deferred_unsynced`` (T5b).  Runs AFTER
    # the reconcile legs (so a candidate that actually landed is recorded as
    # promoted first) and BEFORE the auto-trigger executor, which keeps only the
    # coverage/candidate triggers.  The dispatch itself lives beside the
    # re-drivers it dispatches — see ``run_redrive_legs``.
    await run_redrive_legs(
        version_ids,
        session_factory=session_factory,
        pre_sweep_statuses=pre_sweep_statuses,
    )

    # Qualification trigger auto-fire.  The resolver
    # (``qualification_triggers.resolve_qualification_trigger``) decides fire/hold
    # for open versions but nothing executed that decision — this loop is the
    # executor.  It runs AFTER every other sweep leg (finalize, transient-outcome
    # re-drivers, ci_compliance recheck, reconciles, drift sentinel, lifecycle
    # alerts) so a version is auto-fired only when no other leg owns it first: a
    # just-spawned qualification reads back as in-flight and is held by the
    # resolver, and the bespoke error/infra re-drivers have already re-entered
    # ``run_qualification`` for those statuses.  Gated on the
    # ``auto_qualification_triggers`` knob ("on" fires, "observe" logs would-fire,
    # "off" no-ops).
    for version_id in version_ids:
        try:
            fired = await _fire_auto_triggered_qualification(
                version_id,
                session_factory=session_factory,
                pre_sweep_status=pre_sweep_statuses.get(version_id),
            )
        except Exception:
            logger.exception(
                "[RELEASE_QUALIFIER] sweep: qualification auto-trigger failed "
                "for version %s",
                version_id,
            )
            continue
        if fired:
            logger.info(
                "[RELEASE_QUALIFIER] sweep auto-fired qualification for version %s",
                version_id,
            )

    return finalized

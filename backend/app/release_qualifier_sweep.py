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

from sqlalchemy import exists, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm.attributes import flag_modified

from app import release_version
from app.database import AsyncSessionLocal
from app.models import (
    Alert,
    AlertSeverity,
    Project,
    ReleaseDeployArtifact,
    Task,
    TaskStatus,
)
from app.release_artifacts import required_release_artifact_kinds
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
    _TRANSIENT_ERROR_CODES,  # noqa: F401 — re-exported for tests
    _redrive_error_qualification,
    _redrive_infra_failure_qualification,
    _redrive_rubric_changed_qualification,
    _redrive_walkthrough_contract_qualification,
)

logger = logging.getLogger(__name__)


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

# Consecutive sweep-finalize crashes before force-finalizing the pending
# qualification and raising an operator alert.  Mirrors _MIN_CALCIFICATION_ATTEMPTS:
# three consecutive crashes strongly suggest a persistent bug in the finalize
# path rather than a transient error, and the loop must self-recover.
_MAX_SWEEP_CRASH_STREAK = 3

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


def _hotfix_audit_was_attempted(qual: dict[str, Any]) -> bool:
    """Recognize promoted and durably persisted hotfix wait states."""
    promotion_pending = qual.get("promotion_pending")
    persisted_wait = (
        qual.get("status") == "pending"
        and isinstance(promotion_pending, dict)
        and promotion_pending.get("pr_number") is not None
    )
    return qual.get("status") == "promoted" or persisted_wait


async def _reconcile_hotfix_audit_trail(
    version_id: Any, *, session_factory=None
) -> bool:
    """Restore a hotfix's debt/Alert after its request session was lost.

    The hotfix promotion commits in its own session because ``promote`` releases
    its lock mid-flight.  Re-read the committed provenance under a fresh
    transaction and repair only the missing artifact while holding the version
    lock.  The request path uses this same function, so it cannot race the
    periodic recovery writer.
    """
    from app import release_qualifier
    from app.completion_debt import (
        hotfix_deploy_alert_dedupe_key,
        hotfix_deploy_debt_dedupe_key,
    )
    from app.models import CompletionDebtEntry

    factory = session_factory or AsyncSessionLocal
    async with factory() as audit_session:
        version = (
            (
                await audit_session.execute(
                    select(release_version.ReleaseVersion)
                    .where(release_version.ReleaseVersion.id == version_id)
                    .with_for_update()
                )
            )
            .scalars()
            .first()
        )
        qual = dict(version.last_qualification or {}) if version else {}
        candidate_sha = qual.get("candidate_sha")
        if not (
            qual.get("hotfix_promote") is True
            and isinstance(candidate_sha, str)
            and candidate_sha
            and _hotfix_audit_was_attempted(qual)
        ):
            return False
        debt_key = hotfix_deploy_debt_dedupe_key(version.project_id, candidate_sha)
        alert_key = hotfix_deploy_alert_dedupe_key(version.project_id, candidate_sha)
        debt = (
            await audit_session.execute(
                select(CompletionDebtEntry).where(
                    CompletionDebtEntry.dedupe_key == debt_key
                )
            )
        ).scalars().first()
        alert = (
            await audit_session.execute(
                select(Alert).where(
                    Alert.project_id == version.project_id,
                    Alert.dedupe_key == alert_key,
                )
            )
        ).scalars().first()
        if debt is not None and alert is not None:
            return False
        await release_qualifier._record_hotfix_audit_trail(
            audit_session,
            version.project_id,
            candidate_sha=candidate_sha,
            actor=qual.get("hotfix_promote_by"),
            reason=qual.get("hotfix_promote_reason"),
            scope_modules=qual.get("hotfix_promote_scope_modules"),
            diff_file_list=qual.get("hotfix_promote_diff_file_list"),
            prior_deployed_sha=qual.get("hotfix_promote_prior_deployed_sha"),
            record_debt=debt is None,
            record_alert=alert is None,
        )
        await audit_session.commit()
        return True


async def _hotfix_audit_recovery_candidate_ids(session: AsyncSession) -> list[Any]:
    """Return hotfix versions whose debt or Alert is missing.

    The first query uses JSON provenance to exclude ordinary release history.
    Two bulk artifact reads then exclude completed hotfixes before the sweep
    opens a dedicated transaction or takes a row lock.  The under-lock helper
    remains the authority because request completion can race this snapshot.
    """
    from app.completion_debt import (
        hotfix_deploy_alert_dedupe_key,
        hotfix_deploy_debt_dedupe_key,
    )
    from app.models import CompletionDebtEntry

    hotfix_rows = list(
        (
            await session.execute(
                select(
                    release_version.ReleaseVersion.id,
                    release_version.ReleaseVersion.project_id,
                    release_version.ReleaseVersion.last_qualification,
                ).where(
                    release_version.ReleaseVersion.status.in_(("open", "promoted")),
                    release_version.ReleaseVersion.last_qualification[
                        "hotfix_promote"
                    ]
                    .as_boolean()
                    .is_(True),
                )
            )
        ).all()
    )
    candidates: list[tuple[Any, str, str]] = []
    for version_id, project_id, raw_qual in hotfix_rows:
        qual = dict(raw_qual or {})
        candidate_sha = qual.get("candidate_sha")
        if not (
            isinstance(candidate_sha, str)
            and candidate_sha
            and _hotfix_audit_was_attempted(qual)
        ):
            continue
        candidates.append(
            (
                version_id,
                hotfix_deploy_debt_dedupe_key(project_id, candidate_sha),
                hotfix_deploy_alert_dedupe_key(project_id, candidate_sha),
            )
        )
    if not candidates:
        return []

    debt_keys = {debt_key for _, debt_key, _ in candidates}
    alert_keys = {alert_key for _, _, alert_key in candidates}
    existing_debt_keys = set(
        (
            await session.execute(
                select(CompletionDebtEntry.dedupe_key).where(
                    CompletionDebtEntry.dedupe_key.in_(debt_keys)
                )
            )
        ).scalars()
    )
    existing_alert_keys = set(
        (
            await session.execute(
                select(Alert.dedupe_key).where(Alert.dedupe_key.in_(alert_keys))
            )
        ).scalars()
    )
    return [
        version_id
        for version_id, debt_key, alert_key in candidates
        if debt_key not in existing_debt_keys or alert_key not in existing_alert_keys
    ]


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
) -> None:
    """Force-finalize a qualification whose sweep finalize has crash-looped.

    Called when _finalize_in_dedicated_session has raised on every consecutive
    sweep attempt for this version (_MAX_SWEEP_CRASH_STREAK times).  Records
    last_qualification.status='infra_failure' with
    blocker='sweep_finalize_crash_loop', resets the version to stabilize phase,
    raises an Alert, and notifies the operator.  The caller commits the session.
    """
    qual = dict(version.last_qualification or {})
    # Cancel any running child tasks before force-finalizing (best-effort).
    await _cancel_qualifier_children(session, version)

    record = {
        **qual,
        "status": "infra_failure",
        "blocker": "sweep_finalize_crash_loop",
        "detail": (
            f"sweep finalize raised on {streak} consecutive sweep(s) — "
            "force-finalized to unblock the qualification loop"
        ),
        "finalized_at": _utcnow().isoformat(),
        "sweep_crash_streak": 0,
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
                f"returns to the stabilize phase. Inspect the release_qualifier "
                f"logs for the root cause."
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
            f"returned to stabilize."
        ),
        extra={"consecutive_finalize_errors": streak},
    )
    logger.warning(
        "[RELEASE_QUALIFIER] sweep: version v%d finalize crash-looped "
        "(%d consecutive errors) — force-finalized as sweep_finalize_crash_loop",
        version.version_number,
        streak,
    )


# Durable per-HEAD rerun accounting for the drift sentinel's infra-flake path.
# Stored as ``{"head_sha": str, "reruns": int}`` in project_telemetry: a new
# integration HEAD resets the budget, the same HEAD does not.
_DRIFT_SENTINEL_RERUN_KEY = "_drift_sentinel_rerun"
_DRIFT_SENTINEL_RERUN_BUDGET = 1

# Durable per-HEAD re-dispatch accounting for the drift sentinel's cancelled-run
# path.  Stored as ``{"head_sha": str, "redispatched_at": str}`` in
# project_telemetry: ci.yml's branch-keyed cancel-in-progress concurrency policy
# cancels in-flight integration-branch runs, so a dispatched sentinel run
# routinely reads back as cancelled.  Without a bounded re-dispatch the sentinel
# would give up on the HEAD permanently, which on an actively-merging branch can
# starve the full-suite signal of a green conclusion forever (every fresh run is
# cancelled by the next merge, and the sentinel never tries again for that SHA).
# A cooldown-bounded re-dispatch lets a quiet window on the branch conclude a
# green run; a new integration commit still dispatches afresh through the normal
# no-run path.
_DRIFT_SENTINEL_CANCELLED_REDISPATCH_KEY = "_drift_sentinel_cancelled_redispatch"
_DRIFT_SENTINEL_CANCELLED_REDISPATCH_COOLDOWN_SECONDS = 30 * 60


def _drift_sentinel_cancelled_redispatch_due(
    marker: dict | None,
    head_sha: str | None,
    now: datetime,
) -> bool:
    """Decide whether a fresh full-suite dispatch is due for a cancelled HEAD.

    Due when there is no prior marker, when the integration HEAD advanced past
    the marker's recorded sha, or when the cooldown has elapsed since the last
    re-dispatch.  An unparseable / missing timestamp errs toward re-dispatching
    (the safe direction — a missing green signal must not starve forever).
    """
    if not isinstance(marker, dict):
        return True
    if marker.get("head_sha") != head_sha:
        return True
    redispatched_at = marker.get("redispatched_at")
    if not redispatched_at:
        return True
    try:
        prev = datetime.fromisoformat(redispatched_at)
    except (ValueError, TypeError):
        return True
    return (
        now - prev
    ).total_seconds() >= _DRIFT_SENTINEL_CANCELLED_REDISPATCH_COOLDOWN_SECONDS


async def _check_full_suite_drift_sentinel(
    version_id,
    *,
    session_factory=None,
) -> bool:
    """Dispatch and evaluate the standing full-suite CI sentinel on automain.

    Resolves the integration-branch HEAD and dispatches a ci.yml full-matrix
    run if none exists for that SHA.  A concluded-red run raises a deduplicated
    Alert (keyed on project+SHA so repeated sweeps do not double-alert) and
    spawns harvest remediation via the same path used by the promotion-failed
    re-driver.

    Canonical and ON by default: every open version fires the sentinel
    regardless of phase, so full-suite CI drift is caught before it accumulates
    across targeted-CI merges.  The only opt-out is an explicit
    ``build_config.full_suite_drift_sentinel_enabled: False`` (temporary
    rollback path).
    Best-effort: any exception is swallowed and logged.  Returns True when a
    dispatch or alert/harvest occurred.
    """
    from app import release_qualifier
    from app.release_readiness import compute_promotion_ci_green_gate
    from app.release_reconciliation import _harvest_promotion_ci_failures

    factory = session_factory or AsyncSessionLocal
    async with factory() as session:
        try:
            version = await session.get(release_version.ReleaseVersion, version_id)
            if version is None or version.status != "open":
                return False
            project = await session.get(Project, version.project_id)
            if project is None:
                return False
            # Only an explicit ``False`` disables the sentinel; the key's
            # absence (the canonical default) and ``True`` both keep it armed.
            if not (project.build_config or {}).get(
                "full_suite_drift_sentinel_enabled", True
            ):
                return False

            gate = await compute_promotion_ci_green_gate(
                session,
                version.project_id,
                dispatch=True,
                # A cancelled run must surface as its own state, not collapse into
                # an immediate re-dispatch — otherwise the ci.yml concurrency
                # policy's cancel-in-progress on the integration branch drives a
                # fresh full 8-shard dispatch on every sweep with no signal.
                dispatch_on_cancelled=False,
            )
            gate_state = gate.get("gate_state")
            head_sha = gate.get("head_sha") or ""

            if gate_state == "dispatched":
                logger.info(
                    "[DRIFT_SENTINEL] dispatched full-suite CI run for project %s "
                    "HEAD %s",
                    version.project_id,
                    head_sha[:8] if head_sha else "?",
                )
                return True

            if gate_state == "cancelled":
                # ci.yml's branch-keyed cancel-in-progress concurrency policy
                # cancels in-flight integration-branch runs, so a dispatched
                # full-suite run legitimately reads back as cancelled.  The old
                # behaviour raised a deduplicated operator Alert and never
                # re-dispatched for the HEAD; on an actively-merging branch that
                # can starve the full-suite signal of a green conclusion forever
                # (every fresh dispatch is promptly cancelled by the next merge,
                # and the sentinel had already given up on that SHA).  Instead
                # re-dispatch once per HEAD per cooldown window, so a quiet window
                # on the branch lets the fresh run conclude and green becomes
                # reachable for the same HEAD.  A new integration commit still
                # dispatches afresh through the normal no-run path.
                from app.project_telemetry import (
                    read_telemetry,
                    upsert_telemetry,
                )

                _marker = (
                    await read_telemetry(
                        session,
                        version.project_id,
                        _DRIFT_SENTINEL_CANCELLED_REDISPATCH_KEY,
                    )
                    or {}
                )
                _now = _utcnow()
                _cancelled_unresolved = False
                if _drift_sentinel_cancelled_redispatch_due(_marker, head_sha, _now):
                    # Treat the cancelled run as absent (dispatch_on_cancelled=True)
                    # so the gate dispatches a fresh full-suite run for the SAME
                    # HEAD — this is the seam that lets a quiet head reach green.
                    gate = await compute_promotion_ci_green_gate(
                        session,
                        version.project_id,
                        dispatch=True,
                        dispatch_on_cancelled=True,
                    )
                    gate_state = gate.get("gate_state")
                    head_sha = gate.get("head_sha") or ""
                    if gate_state == "dispatched":
                        await upsert_telemetry(
                            session,
                            version.project_id,
                            _DRIFT_SENTINEL_CANCELLED_REDISPATCH_KEY,
                            {
                                "head_sha": head_sha,
                                "redispatched_at": _now.isoformat(),
                            },
                        )
                        await session.commit()
                        logger.info(
                            "[DRIFT_SENTINEL] re-dispatched full-suite CI run after "
                            "cancellation for project %s HEAD %s",
                            version.project_id,
                            head_sha[:8] if head_sha else "?",
                        )
                        return True
                    if gate_state in ("green", "pending"):
                        # A run for the HEAD is green or in flight — nothing left
                        # to do; the sentinel will read it on the next sweep.
                        return False
                    if gate_state != "red":
                        # unavailable / no_signal: the re-dispatch could not be
                        # attempted, so the cancellation is unresolved.  Raise the
                        # operator alert (deduped on project+SHA) and let the next
                        # sweep retry the re-dispatch once the cooldown allows.
                        _cancelled_unresolved = True
                    # gate_state == "red": the re-dispatched run concluded red —
                    # fall through to the shared red handler (alert + harvest).
                else:
                    # Cooldown has not elapsed for this HEAD — do NOT re-dispatch
                    # yet.  Surface the cancelled run as a deduplicated operator
                    # alert; the sentinel re-arms once the cooldown elapses.
                    _cancelled_unresolved = True

                if _cancelled_unresolved:
                    _dedupe_key = (
                        f"drift_sentinel:cancelled:{version.project_id}:{head_sha}"
                    )
                    existing = (
                        (
                            await session.execute(
                                select(Alert).where(
                                    Alert.project_id == version.project_id,
                                    Alert.dedupe_key == _dedupe_key,
                                    Alert.dismissed_at.is_(None),
                                )
                            )
                        )
                        .scalars()
                        .first()
                    )
                    if existing is None:
                        run_url = gate.get("html_url") or str(
                            gate.get("run_id") or ""
                        )
                        session.add(
                            Alert(
                                project_id=version.project_id,
                                severity=AlertSeverity.WARNING,
                                title=(
                                    f"Full-suite CI run cancelled on integration "
                                    f"branch ({head_sha[:8] if head_sha else '?'})"
                                ),
                                message=(
                                    f"Standing drift sentinel: the ci.yml run for "
                                    f"integration HEAD "
                                    f"{head_sha[:12] if head_sha else '?'} was "
                                    "cancelled (the ci.yml concurrency policy "
                                    "cancels in-flight integration-branch runs). "
                                    "A fresh run will be re-dispatched after the "
                                    "cooldown window, so a quiet HEAD can still "
                                    "reach green."
                                    + (f" Run: {run_url}." if run_url else "")
                                ),
                                dedupe_key=_dedupe_key,
                                event_type="release.ci_unresolved",
                            )
                        )
                        await session.commit()
                        logger.warning(
                            "[DRIFT_SENTINEL] full-suite CI run cancelled for "
                            "project %s HEAD %s — alert raised, re-dispatch "
                            "cooldown in effect",
                            version.project_id,
                            head_sha[:8] if head_sha else "?",
                        )
                    return True

            if gate_state == "unavailable":
                # The gate could not resolve creds / HEAD / API.  This is
                # self-healing on the next sweep, but must not remain invisible
                # while full-suite CI drift accumulates.  Keep one active alert
                # per project so every new automain merge does not create a new
                # operator incident for the same unresolved integration signal.
                reason = str(gate.get("reason") or "unknown reason")
                dedupe_key = f"drift_sentinel:unavailable:{version.project_id}"
                head_label = head_sha[:8] if head_sha else "?"
                head_detail = head_sha[:12] if head_sha else "?"
                title = (
                    f"Full-suite CI unavailable on integration branch "
                    f"({head_label})"
                )
                message = (
                    f"Standing drift sentinel could not evaluate ci.yml "
                    f"for automain HEAD {head_detail}: {reason}. "
                    "Full-suite CI drift is currently unverified; "
                    "inspect GitHub connectivity and retry."
                )
                existing = (
                    (
                        await session.execute(
                            select(Alert).where(
                                Alert.project_id == version.project_id,
                                Alert.dedupe_key == dedupe_key,
                                Alert.dismissed_at.is_(None),
                            )
                        )
                    )
                    .scalars()
                    .first()
                )
                if existing is None:
                    session.add(
                        Alert(
                            project_id=version.project_id,
                            severity=AlertSeverity.WARNING,
                            title=title,
                            message=message,
                            dedupe_key=dedupe_key,
                            event_type="release.ci_unresolved",
                        )
                    )
                    await session.commit()
                    logger.warning(
                        "[DRIFT_SENTINEL] full-suite CI gate unavailable for project %s "
                        "HEAD %s — alert raised: %s",
                        version.project_id,
                        head_sha[:8] if head_sha else "?",
                        reason,
                    )
                else:
                    existing.severity = AlertSeverity.WARNING
                    existing.title = title
                    existing.message = message
                    existing.event_type = "release.ci_unresolved"
                    existing.occurrence_count = int(existing.occurrence_count or 1) + 1
                    existing.last_seen_at = _utcnow()
                    session.add(existing)
                    await session.commit()
                    logger.warning(
                        "[DRIFT_SENTINEL] full-suite CI gate unavailable for project %s "
                        "HEAD %s — alert refreshed: %s",
                        version.project_id,
                        head_label,
                        reason,
                    )
                return True

            if gate_state != "red":
                return False

            # Red: raise a deduplicated alert + spawn harvest remediation.
            dedupe_key = f"drift_sentinel:red:{version.project_id}:{head_sha}"
            existing = (
                (
                    await session.execute(
                        select(Alert).where(
                            Alert.project_id == version.project_id,
                            Alert.dedupe_key == dedupe_key,
                            Alert.dismissed_at.is_(None),
                        )
                    )
                )
                .scalars()
                .first()
            )
            if existing is None:
                run_url = gate.get("html_url") or str(gate.get("run_id") or "")
                session.add(
                    Alert(
                        project_id=version.project_id,
                        severity=AlertSeverity.ERROR,
                        title=(
                            f"Full-suite CI red on integration branch "
                            f"({head_sha[:8] if head_sha else '?'})"
                        ),
                        message=(
                            f"Standing drift sentinel: ci.yml concluded failure for "
                            f"integration HEAD {head_sha[:12] if head_sha else '?'}."
                            + (f" Run: {run_url}." if run_url else "")
                            + " Harvest remediation spawning."
                        ),
                        dedupe_key=dedupe_key,
                        event_type="release.ci_unresolved",
                    )
                )
                await session.commit()
                logger.warning(
                    "[DRIFT_SENTINEL] full-suite CI red for project %s HEAD %s — "
                    "alert raised, harvest spawning",
                    version.project_id,
                    head_sha[:8] if head_sha else "?",
                )

            # Harvest is best-effort and self-gating (per-hour/concurrent caps).
            integration_branch, _ = release_qualifier._lts_branch_names(project)
            from app.project_telemetry import (
                read_telemetry,
                read_telemetry_or_doc,
                upsert_telemetry,
            )

            auto_rem = (
                await read_telemetry_or_doc(
                    session,
                    version.project_id,
                    project.design_doc,
                    "_auto_remediation",
                    {},
                )
                or {}
            )

            # ── Per-HEAD rerun budget ──────────────────────────────────────────
            # This sentinel runs on EVERY sweep while the gate is red, and the
            # Alert dedupe_key only suppresses duplicate alerts — it does not
            # gate the harvest.  Without a budget, an infra-flake verdict (whose
            # log patterns are broad substrings matched against the whole
            # aggregated failure log) would re-run the full 8-shard matrix once
            # per CI cycle forever and never escalate a misclassified genuine
            # regression to a fix task — the very ci_not_green latch the sentinel
            # exists to clear.  Budget is keyed on head_sha, so a new commit
            # legitimately gets a fresh attempt; exhausting it makes the harvest
            # fall through to the fix spawn (same shape as ci_completion's
            # ci_infra_rerun_done / _CI_RERUN_BUDGET gates).
            _rerun_state = (
                await read_telemetry(
                    session, version.project_id, _DRIFT_SENTINEL_RERUN_KEY
                )
                or {}
            )
            _consumed = (
                int(_rerun_state.get("reruns") or 0)
                if _rerun_state.get("head_sha") == head_sha
                else 0
            )
            _budget = max(0, _DRIFT_SENTINEL_RERUN_BUDGET - _consumed)

            outcome = await _harvest_promotion_ci_failures(
                project_id=version.project_id,
                version_id=version.id,
                version_number=version.version_number,
                owner=project.github_owner or "",
                repo=project.github_repo or "",
                ci_source_sha=head_sha,
                integration_branch=integration_branch,
                auto_remediation_config=auto_rem,
                selfheal_enabled=True,
                # The sentinel harvests a run on the LIVE integration branch, so
                # an infra flake should re-run its failed jobs to clear the red
                # gate for the same HEAD — but only while budget remains.  A 0
                # here tells the harvest to escalate the flake to a fix spawn.
                flake_rerun_budget=_budget,
            )
            if outcome == "flake_rerun":
                await upsert_telemetry(
                    session,
                    version.project_id,
                    _DRIFT_SENTINEL_RERUN_KEY,
                    {"head_sha": head_sha, "reruns": _consumed + 1},
                )
                await session.commit()
                logger.info(
                    "[DRIFT_SENTINEL] consumed rerun budget %d/%d for project %s "
                    "HEAD %s",
                    _consumed + 1,
                    _DRIFT_SENTINEL_RERUN_BUDGET,
                    version.project_id,
                    head_sha[:8] if head_sha else "?",
                )
            return True
        except Exception:
            logger.exception(
                "[DRIFT_SENTINEL] sentinel check failed for version %s", version_id
            )
            try:
                await session.rollback()
            except Exception:
                logger.exception(
                    "[DRIFT_SENTINEL] rollback failed for version %s", version_id
                )
            return False


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
                if alerted:
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
        version = (
            (
                await recheck_session.execute(
                    select(release_version.ReleaseVersion)
                    .where(release_version.ReleaseVersion.id == version_id)
                    .with_for_update()
                )
            )
            .scalars()
            .first()
        )
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


async def _fire_auto_triggered_qualification(
    version_id: Any,
    *,
    session_factory=None,
    pre_sweep_status: str | None = None,
) -> bool:
    """Execute the qualification-trigger resolver's fire decision for one version.

    ``qualification_triggers.resolve_qualification_trigger`` is the pure decision
    module that maps an open version's state to fire/hold, but until now nothing
    EXECUTED it: the only production consumers were read surfaces (strategic fetch,
    admin lifecycle), the ``auto_qualification_triggers`` knob defaulted to
    ``observe``, and ``run_qualification`` refused any non-explicit fresh
    qualification (``explicit_request=False`` ⇒ ``{"status":
    "explicit_request_required"}``).  The result was a trigger engine that decided
    "armed" forever without ever spawning a qualification.  This helper is the
    sweep's executor for that decision: it fires ``run_qualification`` through the
    sweep's OWN sanctioned path (``auto_trigger=True``), which the ownership guard
    admits as a distinct source — never by claiming ``explicit_request``.

    For each open, stabilize-phase version it mirrors the read surface's resolver
    inputs (candidate-fully-met from the ledger, requalification readiness, and a
    best-effort automain HEAD/CI state for the advanced-candidate legs), then:

      * knob == "off"       — no-op (never even resolves).
      * knob == "observe"   — resolve and LOG a would-fire decision, but do not run.
      * knob == "on" + fire — call ``run_qualification(..., auto_trigger=True)``.
      * fire == False       — hold; nothing to do this cycle.

    Guardrails (mirroring the qualify_request path so the auto-trigger can never
    bypass the CI-compliance guardrail or race a bespoke re-driver):

      * A durable ``qualify_request_blocked`` marker with blocker ``ci_compliance``
        is skipped — ``_recheck_ci_compliance_blocked_version`` owns that loop and
        must be the one to clear it.
      * The STORED ci-compliance gate record is checked (fail-open: no record ⇒ not
        blocked) using the same cheap, no-GitHub-scan read the error/infra re-drivers
        use — a fresh ``ensure_current_ci_compliance`` scan stays the strategic
        qualify_request path's job.
      * Verdict re-drives are NOT auto-trigger work: ``error`` / ``infra_failure``
        are owned by the dedicated re-drivers above (which re-enter
        ``run_qualification`` with a bounded attempt cap), and
        ``rubric_changed`` / ``rubric_provenance_unknown`` are owned by the
        dedicated rubric re-driver above (``_redrive_rubric_changed_qualification``)
        — auto-firing them here would bypass its re-drive cap.  And
        ``deferred_unsynced`` / ``promotion_failed`` / ``not_promoted`` with any
        blocker other than the acceptance grade are Strategic qualify_request
        decisions — the existing contract forbids the scheduler from starting a
        new review on those.  The resolver surfaces them to Strategic; the sweep
        only fires the initial start and attempt-scoped recovery.
      * ``pre_sweep_status`` — when provided, a version whose qualification
        status differs from that snapshot is deferred.  An earlier sweep leg
        (finalize, promotion_pending_timeout / managed-check-wait reconcile)
        just wrote this cycle's outcome; those transitions re-arm the trigger
        on the NEXT cycle, and firing now would race the fresh state.

    Best-effort like every other sweep leg: any exception is swallowed and logged so
    one version can never wedge the sweep.  Returns True only when a qualification
    was actually started (``run_qualification`` returned ``{"status": "spawned"}``).
    """
    from app import release_qualifier
    from app.qualification_triggers import resolve_qualification_trigger
    from app.runtime_config_state import get_auto_qualification_triggers_mode

    knob = get_auto_qualification_triggers_mode()
    if knob == "off":
        return False

    factory = session_factory or AsyncSessionLocal
    async with factory() as fire_session:
        version = await fire_session.get(release_version.ReleaseVersion, version_id)
        if version is None:
            return False
        if version.status != "open" or version.phase != "stabilize":
            return False
        qual = (
            version.last_qualification
            if isinstance(version.last_qualification, dict)
            else {}
        )
        status = qual.get("status")
        # Defer when an earlier sweep leg transitioned this version THIS cycle
        # (finalize, the promotion_pending_timeout / managed-check-wait
        # reconciles).  Those transitions settle to a fire-eligible state but
        # document a deliberate "re-arm next cycle" — firing now would race the
        # just-written outcome (and, on the error path, overwrite its error
        # detail).  A version whose status is unchanged since the sweep started
        # is a settled state the resolver may fire on.
        if pre_sweep_status is not None and status != pre_sweep_status:
            logger.info(
                "[RELEASE_QUALIFIER] sweep: auto-trigger deferring version %s — "
                "qualification status moved %r -> %r during this sweep cycle",
                version_id,
                pre_sweep_status,
                status,
            )
            return False
        # Verdict re-drives the scheduler must NOT execute.  ``error`` /
        # ``infra_failure`` / ``rubric_changed`` / ``rubric_provenance_unknown``
        # are owned by the dedicated re-drivers above (which re-enter
        # ``run_qualification`` with a bounded attempt cap); the rest are
        # Strategic delivery decisions — the existing contract is explicit that the
        # sweep must leave them for Strategic to re-issue ("Resolved findings do
        # not authorize the scheduler to start a new review", "A cleared deferral
        # remains a Strategic delivery decision, not sweep work", "Green CI is
        # evidence for Strategic, not scheduler qualification authority").  The
        # resolver surfaces these to Strategic via the read surfaces; the sweep's
        # auto-trigger authority is the INITIAL start (status None) plus
        # attempt-scoped recovery (dry-run advance, and the acceptance-grade
        # ``criterion_coverage_incomplete`` re-grade).
        if status in (
            "error",
            "infra_failure",
            "deferred_unsynced",
            "promotion_failed",
            "rubric_changed",
            "rubric_provenance_unknown",
        ):
            return False
        if status == "not_promoted" and qual.get("blocker") != "criterion_coverage_incomplete":
            return False
        # A CI-compliance deferral is durable and owned by the recheck loop above.
        blocked = qual.get("qualify_request_blocked")
        if isinstance(blocked, dict) and blocked.get("blocker") == "ci_compliance":
            return False
        project = await fire_session.get(Project, version.project_id)
        if project is None:
            return False
        from app.strategic_spawning import ci_compliance_qualify_gate

        if ci_compliance_qualify_gate(project).get("blocked"):
            logger.info(
                "[RELEASE_QUALIFIER] sweep: deferring auto-trigger for v%d — "
                "stored CI-compliance record shows the integration HEAD breaches "
                "the guardrails",
                version.version_number,
            )
            return False
        try:
            from app import category_verdict_ledger as _cvl
            from app.release_readiness import compute_requalification_readiness

            _is_candidate_fully_met = await _cvl.is_candidate_fully_met(
                fire_session, version.id, qual.get("candidate_sha")
            )
            _readiness = await compute_requalification_readiness(
                fire_session, version.project_id
            )
        except Exception:  # noqa: BLE001 — best-effort sweep; defer to next cycle
            logger.exception(
                "[RELEASE_QUALIFIER] sweep: auto-trigger readiness resolution "
                "failed for version %s; deferring",
                version_id,
            )
            await fire_session.rollback()
            return False
        # Best-effort automain HEAD / CI state for the advanced-candidate legs
        # (T1 candidate-advanced, T4 post_dry_run_advance).  The gate never raises
        # and degrades to ``unavailable`` (automain_sha=None) without credentials,
        # which simply disables those legs — the same conservative behaviour as the
        # read surface, which passes None to avoid any GitHub I/O.
        automain_sha = None
        automain_ci_state = None
        try:
            from app.release_readiness import compute_promotion_ci_green_gate

            _gate = await compute_promotion_ci_green_gate(
                fire_session, version.project_id, dispatch=False
            )
            automain_sha = _gate.get("head_sha")
            _gstate = _gate.get("gate_state")
            if _gstate in ("red", "green", "pending", "no_checks"):
                automain_ci_state = _gstate
        except Exception:  # noqa: BLE001 — best-effort; degrade to None
            logger.warning(
                "[RELEASE_QUALIFIER] sweep: automain HEAD resolution failed for "
                "version %s; auto-trigger running without the advanced-candidate "
                "legs",
                version_id,
                exc_info=True,
            )
        decision = resolve_qualification_trigger(
            phase=version.phase or "",
            status=status,
            error_code=qual.get("error") if status == "error" else None,
            qualification_attempts=version.qualification_attempts or 0,
            error_redrive_count=int(qual.get("error_redrive_count") or 0),
            is_candidate_fully_met=_is_candidate_fully_met,
            requalification_ready=_readiness.get("requalification_ready"),
            candidate_sha=qual.get("candidate_sha"),
            automain_sha=automain_sha,
            automain_ci_state=automain_ci_state,
            blocker=qual.get("blocker"),
            in_flight=status == "pending",
            promotion_check_wait=bool(qual.get("promotion_pending")),
            consecutive_infra_errors=int(qual.get("consecutive_infra_errors") or 0),
            rubric_redrive_count=int(qual.get("rubric_redrive_count") or 0),
        )
        if not decision.fire:
            return False
        if knob == "observe":
            logger.info(
                "[RELEASE_QUALIFIER] sweep: auto-trigger OBSERVE would fire "
                "qualification for version %s (trigger=%s reason=%s)",
                version_id,
                decision.trigger,
                decision.reason,
            )
            return False
        result = await release_qualifier.run_qualification(
            fire_session,
            version.project_id,
            version,
            auto_trigger=True,
        )
        await fire_session.commit()
        if result and result.get("status") == "spawned":
            logger.info(
                "[RELEASE_QUALIFIER] sweep auto-fired qualification for version "
                "%s (trigger=%s reason=%s)",
                version_id,
                decision.trigger,
                decision.reason,
            )
            return True
        logger.info(
            "[RELEASE_QUALIFIER] sweep auto-trigger fired run_qualification for "
            "version %s but it did not spawn (status=%s)",
            version_id,
            (result or {}).get("status"),
        )
        return False


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
        except Exception:
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
                    locked_v = (
                        (
                            await crash_session.execute(
                                select(release_version.ReleaseVersion)
                                .where(release_version.ReleaseVersion.id == version_id)
                                .with_for_update()
                            )
                        )
                        .scalars()
                        .first()
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
                                crash_session, locked_v, streak
                            )
                        else:
                            locked_v.last_qualification = {
                                **locked_v.last_qualification,
                                "sweep_crash_streak": streak,
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
        if result is not None:
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
    required_artifact_kinds = required_release_artifact_kinds()
    unresolved_filter = release_version.ReleaseVersion.serving_status == "unconfirmed"
    if required_artifact_kinds:
        pending_artifact_exists = exists(
            select(ReleaseDeployArtifact.id).where(
                ReleaseDeployArtifact.version_id == release_version.ReleaseVersion.id,
                ReleaseDeployArtifact.artifact_kind.in_(required_artifact_kinds),
                or_(
                    ReleaseDeployArtifact.serving_status.is_(None),
                    ReleaseDeployArtifact.serving_status == "unconfirmed",
                ),
            )
        )
        # NOTE: We do NOT compare artifact row count against len(required_artifact_kinds).
        # Per-project observable arming intentionally creates only the observable provider
        # subset (e.g. only promotion_merge for non-deploy GitHub projects). Comparing
        # against the global required count would always match external projects as
        # "missing rows" and re-enter _confirm_serving_or_fail forever after resolution.
        # The serving_status=="unconfirmed" scalar catches newly-promoted versions before
        # any rows exist; pending_artifact_exists catches versions with unresolved rows.
        unresolved_filter = or_(
            unresolved_filter,
            pending_artifact_exists,
        )
    unconfirmed_ids = list(
        (
            await session.execute(
                select(release_version.ReleaseVersion.id).where(
                    release_version.ReleaseVersion.status == "promoted",
                    unresolved_filter,
                )
            )
        )
        .scalars()
        .all()
    )
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

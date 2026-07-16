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
    AuditFindingResolution,
    Project,
    ReleaseDeployArtifact,
    Task,
    TaskStatus,
)
from app.release_artifacts import required_release_artifact_kinds
from app.release_promotion import _notify_operator, _resolve_github_with_session
from app.release_reconciliation import (
    _confirm_serving_or_fail,
    _reconcile_if_ci_unresolved_now_green,
    _reconcile_if_deploy_failed_recovered,
    _reconcile_if_managed_check_wait,
    _reconcile_if_not_promoted_but_pr_merged,
    _reconcile_if_pending_auto_merge_landed,
    _reconcile_if_pending_qualification_superseded,
    _reconcile_if_promotion_failed_landed,
    _reconcile_if_promotion_pending_timed_out,
)
from app.release_stall_alerts import (
    _check_version_auto_merge_stall,
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
    _TRANSIENT_ERROR_CODES,  # noqa: F401 — re-exported for tests
    _redrive_deferred_unsynced_qualification,
    _redrive_dry_run_candidate,
    _redrive_error_qualification,
    _redrive_infra_failure_qualification,
    _redrive_not_promoted_qualification,
    _redrive_promotion_failed_qualification,
    _redrive_rubric_qualification,
)

logger = logging.getLogger(__name__)


# Audit H2: hard wall-clock bound on a single qualification.  A qualification
# spawns two heavy agent-VM children (release_audit + qualifier-smoketest),
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
        and (
            "auto_merge_enabled" in promotion_pending
            or promotion_pending.get("managed_check_wait") is True
        )
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

    Reads ``audit_task_id``, ``smoketest_task_id``, ``challenge_task_id``, and
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
    ]
    now = _utcnow()

    for child_key in child_keys:
        child_id = qual.get(child_key)
        if not child_id or not isinstance(child_id, str):
            continue
        try:
            task = await session.get(Task, child_id)
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
                f"child (release_audit / qualifier-smoketest) never reached a "
                f"terminal state. Force-finalized as a blocked qualification "
                f"(blocker={blocker}); the version returns to the "
                f"stabilize phase for the next strategic cycle."
            ),
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


async def _reconcile_if_criterion_coverage_now_complete(
    version_id,
    *,
    session_factory=None,
) -> bool:
    """Re-qualify a not_promoted/criterion_coverage_incomplete version once coverage clears.

    ``criterion_coverage_incomplete`` (release_scoring) means active version
    acceptance criteria were uncovered when the candidate was scored — a state
    that a criterion retraction / supersession (or fresh coverage evidence) can
    clear WITHOUT the candidate SHA changing.  ``run_qualification`` caches the
    ``not_promoted`` verdict by candidate SHA, so neither the strategist nor the
    admin endpoint re-enters once coverage clears; the version strands
    not_promoted with no operator in the loop — the exact deadlock the
    acceptance-contract safety valve exists to break.

    This is the scheduled trigger, mirroring
    :func:`_reconcile_if_ci_unresolved_now_green`.  For an open, stabilize-phase
    version whose ``last_qualification`` is ``not_promoted`` with blocker
    ``criterion_coverage_incomplete``, when automain still points at the exact
    candidate SHA AND the live coverage summary shows the coverage gate would no
    longer fire (every active criterion covered, or zero active criteria remain),
    it re-invokes ``run_qualification`` on the same candidate.
    ``run_qualification``'s own ``criterion_coverage_recovered_same_sha``
    exception then permits the fresh re-audit instead of serving the cached
    verdict; every other gate still applies to that re-audit.

    Returns True iff a new qualification was spawned.
    """
    from app import github, release_qualifier
    from app.acceptance_criteria import (
        backfill_direction_merge_evidence,
        get_version_coverage_summary,
        record_acceptance_window_coverage_evidence,
        record_metrics_integrity_coverage_evidence,
    )
    from app.models import Direction

    factory = session_factory or AsyncSessionLocal
    async with factory() as session:
        try:
            version = await session.get(release_version.ReleaseVersion, version_id)
            if version is None:
                return False
            if version.status != "open" or version.phase != "stabilize":
                return False
            qual = version.last_qualification or {}
            if qual.get("status") != "not_promoted":
                return False
            if qual.get("blocker") != "criterion_coverage_incomplete":
                return False
            candidate_sha = qual.get("candidate_sha")
            if not candidate_sha:
                return False

            # Live coverage gate: mirror release_scoring — the gate fires when
            # genuinely uncovered criteria remain (``total - covered - attested > 0``).
            # Criteria covered only by a (non-expired) operator attestation are NOT
            # genuinely uncovered — they clear the hard blocker and let the re-drive
            # proceed even though they will incur a confidence penalty at score time.
            coverage = await get_version_coverage_summary(session, version.id)
            total = int(coverage.get("total") or 0)
            covered = int(coverage.get("covered") or 0)
            attested = int(coverage.get("attested") or 0)
            backfilled = int(coverage.get("backfilled") or 0)

            # Mirror release_scoring.compute_composite: 'backfilled' clears the
            # coverage gate alongside covered/attested.
            if total > 0 and (total - covered - attested - backfilled) > 0:
                # Before giving up, attempt to backfill merge evidence for any
                # uncovered criteria that have an active direction mapping.  Tasks
                # that merged before the criterion-direction mapping was created
                # were never given a chance to record evidence; replaying them here
                # lets the gate re-evaluate on fresher data without requiring a
                # human to call the manual reconcile-backfill endpoint.
                uncovered_with_mapping = [
                    c["criterion_key"]
                    for c in coverage.get("criteria", [])
                    if c.get("coverage") == "uncovered"
                    and c.get("status") == "active"
                    and c.get("stuck_reason") is None
                ]
                if uncovered_with_mapping:
                    result = await session.execute(
                        select(Direction).where(
                            Direction.project_id == version.project_id,
                            Direction.status.notin_(
                                ["expired", "retired", "superseded", "completed"]
                            ),
                        )
                    )
                    active_directions = list(result.scalars().all())
                    keys_to_backfill: set[str] = set()
                    for direction in active_directions:
                        dkeys = set(direction.criterion_keys or [])
                        if dkeys & set(uncovered_with_mapping):
                            keys_to_backfill.add(direction.direction_key)
                    for dk in keys_to_backfill:
                        await backfill_direction_merge_evidence(
                            session, version.project_id, dk
                        )
                    if keys_to_backfill:
                        coverage = await get_version_coverage_summary(
                            session, version.id
                        )
                        total = int(coverage.get("total") or 0)
                        covered = int(coverage.get("covered") or 0)
                        attested = int(coverage.get("attested") or 0)
                        backfilled = int(coverage.get("backfilled") or 0)
                        logger.info(
                            "[RELEASE_QUALIFIER] reconcile criterion_coverage_now_complete: "
                            "backfilled evidence for %d direction(s) on v%d "
                            "(total=%d covered=%d attested=%d backfilled=%d after backfill)",
                            len(keys_to_backfill),
                            version.version_number,
                            total,
                            covered,
                            attested,
                            backfilled,
                        )

                # Re-score the acceptance-window metric and, when the window is
                # genuinely populated and non-zero, record covering evidence for
                # the criteria that track that metric. This closes the false
                # acceptance-coverage 0/N by letting the coverage gate re-evaluate
                # on fresher data in the same pass, without an operator in the loop.
                window_recorded = await record_acceptance_window_coverage_evidence(
                    session, version
                )
                if window_recorded:
                    coverage = await get_version_coverage_summary(session, version.id)
                    total = int(coverage.get("total") or 0)
                    covered = int(coverage.get("covered") or 0)
                    attested = int(coverage.get("attested") or 0)
                    backfilled = int(coverage.get("backfilled") or 0)
                    logger.info(
                        "[RELEASE_QUALIFIER] reconcile criterion_coverage_now_complete: "
                        "recorded %d acceptance-window recompute evidence row(s) on v%d "
                        "(total=%d covered=%d attested=%d backfilled=%d after recompute)",
                        window_recorded,
                        version.version_number,
                        total,
                        covered,
                        attested,
                        backfilled,
                    )

                # Record covering evidence for the data-integrity criterion when
                # the standing metrics-integrity sweep has run at least once in
                # observe mode (ticks > 0). Mirrors the acceptance-window pattern:
                # the sweep's observability confirms the mechanism is operating and
                # the evidence row closes the coverage gate without operator action.
                mis_recorded = await record_metrics_integrity_coverage_evidence(
                    session, version
                )
                if mis_recorded:
                    coverage = await get_version_coverage_summary(session, version.id)
                    total = int(coverage.get("total") or 0)
                    covered = int(coverage.get("covered") or 0)
                    attested = int(coverage.get("attested") or 0)
                    backfilled = int(coverage.get("backfilled") or 0)
                    logger.info(
                        "[RELEASE_QUALIFIER] reconcile criterion_coverage_now_complete: "
                        "recorded %d metrics-integrity sweep evidence row(s) on v%d "
                        "(total=%d covered=%d attested=%d backfilled=%d after mis-sweep evidence)",
                        mis_recorded,
                        version.version_number,
                        total,
                        covered,
                        attested,
                        backfilled,
                    )

            # Goal-verification redesign — LOCKSTEP with run_qualification's
            # ``criterion_coverage_recovered_same_sha`` guard (they are a matched
            # pair: this is the scheduled trigger, that is the on-entry gate).  The
            # coverage-count arithmetic (``covered + attested + backfilled >=
            # total``) is retired; the gate now clears iff the sha-keyed ledger
            # reports the candidate FULLY MET — every active category ``met`` AND
            # the residual clean.  The evidence-recording machinery above is now
            # vestigial (a criterion retraction/supersession or a merge-grade
            # re-grade is what flips a ledger verdict), but is left in place as a
            # no-op for the integrator to prune.  When the sibling ledger module is
            # absent in this checkout, stay cached (safe: never a wrong re-drive).
            try:
                from app import category_verdict_ledger as _cvl
            except Exception:  # noqa: BLE001 — sibling ledger not yet landed
                _cvl = None
            if _cvl is None:
                return False
            if not await _cvl.is_candidate_fully_met(
                session, version.id, candidate_sha
            ):
                return False

            project = await session.get(Project, version.project_id)
            if project is None:
                return False
            resolved = await _resolve_github_with_session(session, project)
            if resolved is None:
                logger.warning(
                    "[RELEASE_QUALIFIER] reconcile criterion_coverage_now_complete: "
                    "no GitHub credentials for v%d — skipping",
                    version.version_number,
                )
                return False
            token, owner, repo = resolved
            integration_branch, _promotion_target = (
                release_qualifier._lts_branch_names(project)
            )
            try:
                automain_sha = await github.get_branch_sha(
                    token, owner, repo, integration_branch
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "[RELEASE_QUALIFIER] reconcile criterion_coverage_now_complete: "
                    "could not resolve %s HEAD SHA for v%d (%s) — skipping",
                    integration_branch,
                    version.version_number,
                    exc,
                )
                return False
            # Only the SAME-SHA wedge is ours: a different HEAD is a fresh
            # candidate the stale-not-promoted re-drive already owns.
            if automain_sha != candidate_sha:
                return False

            logger.info(
                "[RELEASE_QUALIFIER] reconcile criterion_coverage_now_complete: "
                "coverage cleared for candidate %s of v%d on unchanged automain "
                "(total=%d covered=%d attested=%d) — spawning fresh qualification",
                candidate_sha[:8],
                version.version_number,
                total,
                covered,
                attested,
            )
            result = await release_qualifier.run_qualification(
                session, version.project_id, version
            )
            if result and result.get("status") == "spawned":
                await session.commit()
                return True
            await session.rollback()
            return False
        except Exception:
            logger.exception(
                "[RELEASE_QUALIFIER] reconcile criterion_coverage_now_complete: "
                "unexpected error for version %s",
                version_id,
            )
            try:
                await session.rollback()
            except Exception:
                logger.exception(
                    "[RELEASE_QUALIFIER] reconcile criterion_coverage_now_complete: "
                    "rollback failed for version %s after unexpected error",
                    version_id,
                )
            return False


async def _fire_initial_qualification_if_coverage_complete(
    version_id,
    *,
    session_factory=None,
) -> bool:
    """Fire the FIRST qualification for a stabilize-phase version once coverage is complete.

    This is the initial-trigger complement to
    :func:`_reconcile_if_criterion_coverage_now_complete` (which handles the
    RE-drive path for versions that already have a ``not_promoted`` attempt).
    Here we fire the FIRST qualification for a version that has never been
    qualified at all (``last_qualification is None`` AND
    ``qualification_attempts == 0``) once criterion coverage is complete.

    Uses :func:`~app.release_readiness.compute_qualification_trigger_state` as
    the single classifier so the read-surface and sweep behavior cannot drift.
    Resolves the automain HEAD SHA and invokes ``run_qualification`` on the
    same pattern as :func:`_reconcile_if_criterion_coverage_now_complete`.

    Returns True iff a new qualification was spawned.
    """
    from app import github, release_qualifier
    from app.release_readiness import compute_qualification_trigger_state

    factory = session_factory or AsyncSessionLocal
    async with factory() as session:
        try:
            version = await session.get(release_version.ReleaseVersion, version_id)
            if version is None:
                return False

            trigger = await compute_qualification_trigger_state(
                session, version.project_id
            )
            if trigger.get("trigger_state") != "armed":
                return False
            # Guard: only act on the version we were asked about (the trigger
            # resolves the open version for the project; confirm it matches).
            if str(version.id) != trigger.get("open_version_id"):
                return False

            project = await session.get(Project, version.project_id)
            if project is None:
                return False
            resolved = await _resolve_github_with_session(session, project)
            if resolved is None:
                logger.warning(
                    "[RELEASE_QUALIFIER] fire_initial_qualification: "
                    "no GitHub credentials for v%d — skipping",
                    version.version_number,
                )
                return False
            token, owner, repo = resolved
            integration_branch, _promotion_target = (
                release_qualifier._lts_branch_names(project)
            )
            try:
                automain_sha = await github.get_branch_sha(
                    token, owner, repo, integration_branch
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "[RELEASE_QUALIFIER] fire_initial_qualification: "
                    "could not resolve %s HEAD SHA for v%d (%s) — skipping",
                    integration_branch,
                    version.version_number,
                    exc,
                )
                return False
            if not automain_sha:
                return False

            logger.info(
                "[RELEASE_QUALIFIER] fire_initial_qualification: "
                "initial trigger armed for v%d on %s — spawning initial "
                "qualification (acceptance contract graded inside qualification)",
                version.version_number,
                automain_sha[:8],
            )
            result = await release_qualifier.run_qualification(
                session, version.project_id, version
            )
            if result and result.get("status") == "spawned":
                await session.commit()
                return True
            await session.rollback()
            return False
        except Exception:
            logger.exception(
                "[RELEASE_QUALIFIER] fire_initial_qualification: "
                "unexpected error for version %s",
                version_id,
            )
            try:
                await session.rollback()
            except Exception:
                logger.exception(
                    "[RELEASE_QUALIFIER] fire_initial_qualification: "
                    "rollback failed for version %s after unexpected error",
                    version_id,
                )
            return False


async def _auto_stale_not_promoted_findings(
    version_id,
    *,
    session_factory=None,
) -> int:
    """Write 'stale' ledger rows for unresolved gating findings when automain advances.

    The ``_redrive_not_promoted_qualification`` gate requires every gating
    audit finding (critical/high/medium) to have a ledger row in state
    ``resolved`` or ``stale`` before it will re-enter the qualifier.  No
    automated write path populates those rows: the only existing write path is
    the admin endpoint where an operator manually resolves each finding.  So
    once automain advances past the ``not_promoted`` candidate SHA, the version
    strands indefinitely — the re-drive gate blocks even though the findings
    are effectively stale (they were audited against an old HEAD).

    This function is the missing automated write path.  For each sweep it
    checks a stabilize-phase ``not_promoted`` version and, when automain HEAD
    has advanced past the ``candidate_sha``, writes a ``stale`` row for every
    gating finding that has NO existing ledger entry.  Existing entries are
    left untouched (idempotent; preserves operator ``resolved`` records).

    Short-circuits when:
      - The version's carried findings list was truncated (conservative — a
        partial list could miss a finding that should stay unresolved).
      - GitHub credential resolution fails (no credentials configured).
      - ``get_branch_sha`` raises (transient network error; next sweep retries).
      - automain HEAD equals ``candidate_sha`` (no advance — nothing to stale).

    Returns the count of new ``stale`` rows written (0 on no-op).

    Called by ``sweep_stuck_qualifications`` BEFORE the
    ``_redrive_not_promoted_qualification`` loop so the ledger is populated
    when ``compute_requalification_readiness`` is consulted in that same cycle.
    """
    from app import github, release_qualifier
    from app.release_readiness import (
        _KEYLESS_FINDING_SENTINEL,
        _REQUALIFICATION_GATING_SEVERITIES,
        _latest_carried_audit_findings,
        apply_finding_resolution,
    )

    factory = session_factory or AsyncSessionLocal
    async with factory() as session:
        version = await session.get(release_version.ReleaseVersion, version_id)
        if version is None:
            return 0
        qual = version.last_qualification
        if (
            not isinstance(qual, dict)
            or qual.get("status") != "not_promoted"
            or version.phase != "stabilize"
        ):
            return 0

        # Short-circuit on truncated findings — partial list could misrepresent
        # which findings need staleing.
        if qual.get("audit_findings_truncated"):
            return 0

        # Load the project for GitHub credential resolution.
        project = await session.get(release_version.Project, version.project_id)
        if project is None:
            return 0

        # Resolve GitHub credentials.
        resolved = await release_qualifier._resolve_github_with_session(
            session, project
        )
        if resolved is None:
            return 0
        token, owner, repo = resolved

        # Determine the integration branch (automain).
        integration_branch, _promotion_target = release_qualifier._lts_branch_names(
            project
        )

        # Fetch current integration-branch HEAD.
        try:
            head_sha = await github.get_branch_sha(
                token, owner, repo, integration_branch
            )
        except Exception:
            logger.warning(
                "[RELEASE_QUALIFIER] auto_stale: failed to resolve %s HEAD "
                "for version %s — will retry on next sweep",
                integration_branch,
                version_id,
            )
            return 0

        candidate_sha = qual.get("candidate_sha") or ""
        if not head_sha:
            return 0

        head_advanced = head_sha != candidate_sha

        # Load the latest carried gating findings via the same resolution path
        # that compute_requalification_readiness uses.
        findings, truncated = await _latest_carried_audit_findings(session, version)
        if truncated:
            return 0

        gating_findings = [
            f
            for f in findings
            if isinstance(f, dict)
            and str(f.get("severity") or "").lower()
            in _REQUALIFICATION_GATING_SEVERITIES
        ]
        if not gating_findings:
            return 0

        # When HEAD has advanced and at least one gating finding is file-anchored,
        # fetch the files changed between candidate_sha and head_sha.  We only
        # auto-stale file-anchored findings whose file actually changed — staleing a
        # finding whose file is untouched would force a needless re-audit cycle on
        # every automain merge, burning audit tokens even when none of the flagged
        # code was touched.  Findings without a file field have no code anchor so
        # they are staled unconditionally when HEAD advances.
        changed_files: "set[str] | None" = None
        if head_advanced and any(
            str(f.get("file") or "").strip() for f in gating_findings
        ):
            raw_files = await github.get_files_changed_between_refs(
                token, owner, repo, candidate_sha, head_sha
            )
            if raw_files is None:
                logger.warning(
                    "[RELEASE_QUALIFIER] auto_stale: cannot determine changed "
                    "files between %s and %s for version %s — skipping "
                    "file-anchored stales this cycle",
                    candidate_sha[:12],
                    head_sha[:12],
                    version_id,
                )
                # Leave changed_files as None to signal: skip file-anchored
                # findings this cycle; next sweep will retry.
            else:
                changed_files = set(raw_files)

        # Load existing ledger keys for this version (idempotency — skip any
        # finding that already has a row so operator 'resolved' entries are
        # never overwritten).
        existing_keys = {
            row.finding_key
            for row in (
                await session.execute(
                    select(AuditFindingResolution).where(
                        AuditFindingResolution.release_version_id == version.id
                    )
                )
            )
            .scalars()
            .all()
        }

        written = 0
        for f in gating_findings:
            # Findings without a content key (e.g. challenger-independent blockers
            # persisted by _handle_independent_blocker) use a sentinel so they can
            # be tracked in the ledger and resolved by the readiness check.
            key = f.get("key") or _KEYLESS_FINDING_SENTINEL
            if key in existing_keys:
                continue
            # Keyed findings are anchored to specific code — only stale them when
            # HEAD has genuinely advanced past the candidate SHA so the finding is
            # no longer relevant to the current integration branch.
            # Keyless findings have no code anchor (they are challenger-confirmed
            # independent blockers without a hash key). When HEAD has NOT advanced
            # they can still be auto-staled: re-qualification on the same HEAD will
            # re-confirm or refute the finding, preventing permanent strand when
            # the finding was a false positive.
            if not head_advanced and key != _KEYLESS_FINDING_SENTINEL:
                continue
            # File-anchored findings: only stale when the finding's specific file
            # changed between candidate_sha and head_sha.  If we could not
            # determine the changed-file set (API error), skip this cycle so the
            # next sweep can retry rather than staling still-live findings.
            finding_file = str(f.get("file") or "").strip()
            if head_advanced and finding_file:
                if changed_files is None:
                    continue  # API error; retry next sweep
                if finding_file not in changed_files:
                    continue  # File unchanged; finding may still be live
            note = (
                "auto-staled: automain advanced past not_promoted candidate SHA"
                if head_advanced
                else (
                    "auto-staled: keyless gating blocker on current HEAD — "
                    "no code anchor; re-qualification will re-confirm or refute"
                )
            )
            await apply_finding_resolution(
                session,
                version.project_id,
                version.id,
                key,
                "stale",
                severity=str(f.get("severity") or "").lower() or None,
                note=note,
                verified_against_sha=head_sha,
            )
            written += 1

        if written:
            if head_advanced:
                notify_msg = (
                    f"v{version.version_number} auto-staled {written} finding(s): "
                    f"automain advanced past candidate {candidate_sha[:12]} "
                    f"to {head_sha[:12]}. A fresh qualification will be triggered."
                )
                log_msg = (
                    "[RELEASE_QUALIFIER] auto_stale: wrote %d stale ledger row(s) "
                    "for version %s (automain %s advanced past candidate %s)"
                )
                log_args = (written, version_id, head_sha[:12], candidate_sha[:12])
            else:
                notify_msg = (
                    f"v{version.version_number} auto-staled {written} keyless "
                    f"finding(s) on current HEAD {head_sha[:12]}: no code anchor — "
                    "re-qualification will re-confirm or refute."
                )
                log_msg = (
                    "[RELEASE_QUALIFIER] auto_stale: wrote %d stale ledger row(s) "
                    "for version %s (keyless findings on current HEAD %s — no code anchor)"
                )
                log_args = (written, version_id, head_sha[:12])
            await _notify_operator(
                session,
                version,
                "release_qualification_auto_stale",
                notify_msg,
                extra={
                    "written": written,
                    "candidate_sha": candidate_sha,
                    "head_sha": head_sha,
                },
            )
            await session.commit()
            logger.info(log_msg, *log_args)

        return written


async def _promote_dry_run_pass_if_enabled(
    version_id,
    *,
    session_factory=None,
) -> bool:
    """Promote a dry-run-pass candidate once promotion is enabled for the project.

    When ``_finalize_qualification`` records ``status='dry_run'`` with
    ``would_promote=True``, promotion was disabled at finalize time.  If the
    operator subsequently enables promotion (fleet-wide or per-project), nothing
    automatically acts on the standing dry-run pass — the version strands open in
    the stabilize phase indefinitely.

    This re-driver fires for open, stabilize-phase versions whose
    ``last_qualification.status == 'dry_run'`` and ``would_promote=True`` when
    ``resolve_project_promotion_enabled`` now returns True.  It verifies the
    stored candidate SHA still equals the current integration-branch HEAD —
    stale candidates (automain has advanced) are skipped and will re-qualify via
    normal paths.  When the candidate is current it enters the normal promotion
    machinery via ``_force_promote_in_dedicated_session``, which preserves all
    promotion-in-flight idempotency guards.

    Returns True when a promotion was initiated, False otherwise.
    """
    from app import github, release_qualifier
    from app.runtime_config_state import resolve_project_promotion_enabled

    factory = session_factory or AsyncSessionLocal
    async with factory() as session:
        version = await session.get(release_version.ReleaseVersion, version_id)
        if version is None:
            return False
        qual = version.last_qualification
        if (
            not isinstance(qual, dict)
            or qual.get("status") != "dry_run"
            or not qual.get("would_promote")
            or version.phase != "stabilize"
            or version.status != "open"
        ):
            return False

        # Only act when promotion is now enabled for this project.
        if not resolve_project_promotion_enabled(version.project_id):
            return False

        candidate_sha = qual.get("candidate_sha")
        if not candidate_sha:
            return False

        project = await session.get(Project, version.project_id)
        if project is None:
            return False
        resolved = await _resolve_github_with_session(session, project)
        if resolved is None:
            logger.warning(
                "[RELEASE_QUALIFIER] dry_run promote: no GitHub credentials "
                "for v%d — skipping",
                version.version_number,
            )
            return False
        token, owner, repo = resolved
        integration_branch, _promotion_target = release_qualifier._lts_branch_names(
            project
        )
        try:
            head_sha = await github.get_branch_sha(token, owner, repo, integration_branch)
        except Exception:
            logger.warning(
                "[RELEASE_QUALIFIER] dry_run promote: failed to resolve %s HEAD "
                "for v%d — skipping",
                integration_branch,
                version.version_number,
            )
            return False

        if head_sha != candidate_sha:
            logger.info(
                "[RELEASE_QUALIFIER] dry_run promote: v%d candidate %s is stale "
                "(automain HEAD is %s) — skipping, will re-qualify on new HEAD",
                version.version_number,
                candidate_sha[:8],
                head_sha[:8] if head_sha else "unknown",
            )
            return False

        candidate_diff_hash = qual.get("candidate_diff_hash")
        version_number = version.version_number

    result = await release_qualifier._force_promote_in_dedicated_session(
        version_id,
        candidate_sha,
        candidate_diff_hash,
        "sweep_dry_run_promote",
        session_factory=session_factory,
    )
    promoted = result.get("status") not in release_qualifier._FORCE_PROMOTE_NOOP_STATUSES
    if promoted:
        logger.info(
            "[RELEASE_QUALIFIER] dry_run promote: initiated promotion for v%d "
            "candidate %s (result=%s)",
            version_number,
            candidate_sha[:8],
            result.get("status"),
        )
    return promoted


async def _check_full_suite_drift_sentinel(
    version_id,
    *,
    session_factory=None,
) -> bool:
    """Dispatch and evaluate the standing full-suite CI sentinel on automain.

    When ``build_config.full_suite_drift_sentinel_enabled`` is True, resolves
    the integration-branch HEAD and dispatches a ci.yml full-matrix run if none
    exists for that SHA.  A concluded-red run raises a deduplicated Alert
    (keyed on project+SHA so repeated sweeps do not double-alert) and spawns
    harvest remediation via the same path used by the promotion-failed re-driver.

    Only fires for open, stabilize-phase versions; ignores all others.
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
            if version is None or version.phase != "stabilize" or version.status != "open":
                return False
            project = await session.get(Project, version.project_id)
            if project is None:
                return False
            if not (project.build_config or {}).get("full_suite_drift_sentinel_enabled"):
                return False

            gate = await compute_promotion_ci_green_gate(
                session, version.project_id, dispatch=True
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
            auto_rem = (project.design_doc or {}).get("_auto_remediation") or {}
            await _harvest_promotion_ci_failures(
                project_id=version.project_id,
                version_id=version.id,
                version_number=version.version_number,
                owner=project.github_owner or "",
                repo=project.github_repo or "",
                ci_source_sha=head_sha,
                integration_branch=integration_branch,
                auto_remediation_config=auto_rem,
                selfheal_enabled=True,
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


async def _run_auto_qualification_trigger_sweep(
    open_versions: list,
    session: AsyncSession,
    *,
    session_factory=None,
) -> None:
    """Observe-mode event-driven qualification trigger sweep.

    For each open version: gather qualification inputs, call
    ``resolve_qualification_trigger``, and either log the decision (observe mode)
    or call ``run_qualification`` (on mode).  This never replaces the bespoke
    re-driver loops above — it runs in addition, providing a unified log surface
    and a path to eventual consolidation.

    Controlled by the ``auto_qualification_triggers`` runtime-config knob:
      "off"     — skip entirely
      "observe" — log decisions, spawn nothing (default Phase 0)
      "on"      — call run_qualification for fire=True decisions
    """
    from app.runtime_config_state import get_auto_qualification_triggers_mode

    mode = get_auto_qualification_triggers_mode()
    if mode == "off":
        return

    from app import release_qualifier
    from app.qualification_triggers import resolve_qualification_trigger
    from app.release_readiness import compute_requalification_readiness

    _factory = session_factory or AsyncSessionLocal

    for version in open_versions:
        try:
            await _run_auto_qualification_trigger_for_version(
                version,
                session,
                mode=mode,
                release_qualifier=release_qualifier,
                resolve_qualification_trigger=resolve_qualification_trigger,
                compute_requalification_readiness=compute_requalification_readiness,
                session_factory=_factory,
            )
        except Exception:
            logger.exception(
                "[AUTO_QUAL_TRIGGER] sweep failed for version %s", version.id
            )


async def _run_auto_qualification_trigger_for_version(
    version,
    session: AsyncSession,
    *,
    mode: str,
    release_qualifier,
    resolve_qualification_trigger,
    compute_requalification_readiness,
    session_factory,
) -> None:
    """Run the trigger resolver for one open version and act per mode."""
    from app import github

    qual = version.last_qualification or {}
    status = qual.get("status") if qual else None
    error_code = qual.get("error") if status == "error" else None
    blocker = qual.get("blocker") if qual else None
    candidate_sha = qual.get("candidate_sha") if qual else None
    qualification_attempts = version.qualification_attempts or 0
    # PER-ERROR-EPISODE counter (not the lifetime column) — see the T2 cap in
    # qualification_triggers: capping on the lifetime count strands a version at
    # status=error forever.
    error_redrive_count = int(qual.get("error_redrive_count") or 0) if qual else 0

    # Determine in-flight and promotion-in-flight guards cheaply from stored state.
    in_flight = status == "pending"
    promotion_in_flight = bool(qual.get("promotion_pending")) if qual else False

    # Gather the sha-keyed acceptance-ledger verdict for the last-evaluated
    # candidate (DB-only, cheap). Only the not_promoted/criterion_coverage
    # redrive leg consults it; the initial/error fire ignores it. Fail-closed to
    # False (not fully met) so a ledger-fetch error never spuriously redrives.
    is_candidate_fully_met = False
    try:
        from app import category_verdict_ledger as _cvl

        is_candidate_fully_met = await _cvl.is_candidate_fully_met(
            session, version.id, candidate_sha
        )
    except Exception:
        logger.warning(
            "[AUTO_QUAL_TRIGGER] ledger fetch failed for version %s — treating "
            "candidate as not fully met",
            version.id,
        )
        is_candidate_fully_met = False

    # Gather requalification_ready (DB-only, cheap).
    requalification_ready: bool | None = None
    try:
        readiness = await compute_requalification_readiness(
            session, version.project_id
        )
        requalification_ready = readiness.get("requalification_ready")
    except Exception:
        logger.warning(
            "[AUTO_QUAL_TRIGGER] readiness fetch failed for version %s — using None",
            version.id,
        )

    # Candidate-advancement paths need the integration-branch HEAD + its CI state.
    # This is the only path that requires GitHub I/O in the sweep.
    #
    # BOTH advancement legs need it, not just T3:
    #   * T3  — dry_run superseded by a new automain HEAD.
    #   * T1  — a ``not_promoted`` version blocked on the acceptance grade. Its
    #           CategoryVerdict rows are SHA-KEYED and nothing re-grades a PAST
    #           candidate, so the not-met verdict is frozen; only a NEW candidate
    #           can change it. Without automain_sha here that leg could never fire
    #           (candidate_superseded would be permanently False) — i.e. the escape
    #           would be dead code and the version would sit blocked.
    automain_sha: str | None = None
    automain_ci_state: str | None = None
    if status in ("dry_run", "not_promoted") and candidate_sha:
        project = (
            (
                await session.execute(
                    select(Project).where(Project.id == version.project_id)
                )
            )
            .scalars()
            .first()
        )
        if project is not None:
            resolved = await release_qualifier._resolve_github_with_session(
                session, project
            )
            if resolved is not None:
                token, owner, repo = resolved
                integration_branch, _ = release_qualifier._lts_branch_names(project)
                try:
                    automain_sha = await github.get_branch_sha(
                        token, owner, repo, integration_branch
                    )
                except Exception:
                    logger.warning(
                        "[AUTO_QUAL_TRIGGER] could not resolve %s HEAD for version %s",
                        integration_branch,
                        version.id,
                    )
                # Only check CI state when the SHA has actually advanced.
                if automain_sha and automain_sha != candidate_sha:
                    # ``_resolve_candidate_ci`` — the qualifier-scoped STRICT CI
                    # resolver — is defined in ``release_qualifier``, NOT in
                    # ``app.ci_resolution`` (which holds the shared merge-path
                    # helpers).  Bind it from the injected module, and bind it
                    # OUTSIDE the try below: a missing symbol is a WIRING BUG,
                    # and swallowing it as a benign "CI fetch failed" warning is
                    # exactly how the CI-red suppression underneath this call
                    # stayed dead code (every sweep raised ImportError, left
                    # automain_ci_state None, and the red candidate auto-fired).
                    # Only genuine GitHub I/O failure may degrade to "unknown".
                    _resolve_candidate_ci = release_qualifier._resolve_candidate_ci
                    try:
                        ci_result = await _resolve_candidate_ci(
                            token, owner, repo, automain_sha
                        )
                    except Exception:
                        logger.warning(
                            "[AUTO_QUAL_TRIGGER] CI state fetch failed for %s "
                            "(version %s) — treating as unknown",
                            automain_sha[:8] if automain_sha else "?",
                            version.id,
                        )
                    else:
                        automain_ci_state = ci_result.get("state")

    decision = resolve_qualification_trigger(
        phase=version.phase or "",
        status=status,
        error_code=error_code,
        qualification_attempts=qualification_attempts,
        error_redrive_count=error_redrive_count,
        is_candidate_fully_met=is_candidate_fully_met,
        requalification_ready=requalification_ready,
        candidate_sha=candidate_sha,
        automain_sha=automain_sha,
        automain_ci_state=automain_ci_state,
        blocker=blocker,
        in_flight=in_flight,
        promotion_in_flight=promotion_in_flight,
    )

    if decision.fire:
        if mode == "observe":
            logger.info(
                "[AUTO_QUAL_TRIGGER] observe: would fire '%s' for version %s "
                "(v%d, status=%s, trigger=%s)",
                decision.trigger,
                version.id,
                version.version_number,
                status,
                decision.trigger,
            )
        else:  # mode == "on"
            logger.info(
                "[AUTO_QUAL_TRIGGER] firing '%s' for version %s "
                "(v%d, status=%s)",
                decision.trigger,
                version.id,
                version.version_number,
                status,
            )
            async with session_factory() as fire_session:
                try:
                    locked_v = (
                        (
                            await fire_session.execute(
                                select(release_version.ReleaseVersion).where(
                                    release_version.ReleaseVersion.id == version.id
                                )
                            )
                        )
                        .scalars()
                        .first()
                    )
                    if locked_v is None:
                        return
                    result = await release_qualifier.run_qualification(
                        fire_session, locked_v.project_id, locked_v
                    )
                    if result and result.get("status") == "spawned":
                        await fire_session.commit()
                        logger.info(
                            "[AUTO_QUAL_TRIGGER] spawned qualification for version %s "
                            "(trigger=%s)",
                            version.id,
                            decision.trigger,
                        )
                    else:
                        await fire_session.rollback()
                        logger.info(
                            "[AUTO_QUAL_TRIGGER] run_qualification returned '%s' "
                            "for version %s (trigger=%s) — no spawn",
                            (result or {}).get("status"),
                            version.id,
                            decision.trigger,
                        )
                except Exception:
                    logger.exception(
                        "[AUTO_QUAL_TRIGGER] run_qualification failed for version %s",
                        version.id,
                    )
                    try:
                        await fire_session.rollback()
                    except Exception:
                        pass
    else:
        logger.debug(
            "[AUTO_QUAL_TRIGGER] %s: hold for version %s (v%d, reason=%s, blocking=%s)",
            mode,
            version.id,
            version.version_number,
            decision.reason,
            decision.blocking,
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
    (``_check_version_auto_merge_stall`` / ``_check_version_managed_check_wait_stall``)
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
                    await _check_version_auto_merge_stall(alert_session, v) or alerted
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
        ``CANCELLED``) — finalizes the version.  The qualifier-smoketest child
        is optional (audit H3): when the qualifying run recorded
        ``smoketest_enabled == False`` only the audit child gates this branch.

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

    # Re-drive qualifications stranded in ``deferred_unsynced``.  The
    # qualify-on-synced gate defers a candidate the promotion target is not yet
    # absorbed into (a ``main -> integration`` back-merge is pending), but
    # ``run_qualification`` only re-enters via a fresh strategic
    # ``qualify_request`` or the admin ``/qualify`` endpoint — nothing
    # re-evaluates a STANDING deferral.  So once the back-merge lands (making the
    # candidate synced) the version can sit in ``deferred_unsynced`` forever, and
    # the gate's bounded fall-through never fires because it is checked only on
    # re-entry.  This loop is the scheduled re-driver: each sweep it re-invokes
    # ``run_qualification`` for any open ``deferred_unsynced`` version, which
    # re-snapshots the integration HEAD and either proceeds (now synced /
    # fall-through elapsed) or re-defers while preserving ``first_deferred_at`` so
    # the fall-through accumulates.  ``run_qualification`` is self-defending, so a
    # version that changed state since the scan is a safe no-op.
    for version_id in version_ids:
        try:
            redriven = await _redrive_deferred_unsynced_qualification(
                version_id,
                session_factory=session_factory,
            )
        except Exception:
            logger.exception(
                "[RELEASE_QUALIFIER] sweep: deferred_unsynced re-drive failed "
                "for version %s",
                version_id,
            )
            continue
        if redriven:
            logger.info(
                "[RELEASE_QUALIFIER] sweep re-drove deferred_unsynced version %s "
                "(now synced — qualification spawned)",
                version_id,
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

    # A rubric-changed / unknown-provenance attempt intentionally proves
    # nothing.  Re-drive it on the next lifecycle sweep so it snapshots the
    # current rubric and launches a fresh blind grade instead of stranding in
    # stabilize until an operator manually retries.
    for version_id in version_ids:
        try:
            redriven = await _redrive_rubric_qualification(
                version_id,
                session_factory=session_factory,
            )
        except Exception:
            logger.exception(
                "[RELEASE_QUALIFIER] sweep: rubric re-drive failed for version %s",
                version_id,
            )
            continue
        if redriven:
            logger.info(
                "[RELEASE_QUALIFIER] sweep re-drove rubric outcome for version %s "
                "(fresh rubric snapshot qualification spawned)",
                version_id,
            )

    # Auto-stale not_promoted gating findings when automain has advanced.
    # ``_redrive_not_promoted_qualification`` requires every gating finding to
    # have a ledger row in state ``resolved`` or ``stale``, but no automated
    # path writes those rows — only the operator admin endpoint does.  So once
    # automain advances past the not_promoted candidate SHA the version strands
    # indefinitely.  This loop writes ``stale`` rows for all unresolved gating
    # findings when HEAD has advanced, populating the ledger so the re-drive
    # loop below can fire in the SAME sweep cycle.  Runs BEFORE the re-drive
    # loop; skips truncated finding lists and existing ledger entries.
    for version_id in version_ids:
        try:
            staled = await _auto_stale_not_promoted_findings(
                version_id,
                session_factory=session_factory,
            )
        except Exception:
            logger.exception(
                "[RELEASE_QUALIFIER] sweep: auto_stale failed for version %s",
                version_id,
            )
            continue
        if staled:
            logger.info(
                "[RELEASE_QUALIFIER] sweep auto-staled %d finding(s) for "
                "version %s (automain advanced past candidate SHA)",
                staled,
                version_id,
            )

    # Re-drive quiescent ``not_promoted`` versions whose only blocker was the
    # transient ``ci_unresolved`` state.  If automain still equals the stored
    # candidate SHA and CI has since gone green, Guard 3 must not treat the
    # stale transient verdict as settled; this reconciliation path triggers the
    # fresh qualification before the generic stale-not-promoted loop below.
    for version_id in version_ids:
        try:
            reconciled = await _reconcile_if_ci_unresolved_now_green(
                version_id,
                session_factory=session_factory,
            )
        except Exception:
            logger.exception(
                "[RELEASE_QUALIFIER] sweep: ci_unresolved-now-green reconcile "
                "failed for version %s",
                version_id,
            )
            continue
        if reconciled:
            finalized += 1
            logger.info(
                "[RELEASE_QUALIFIER] sweep re-drove ci_unresolved version %s "
                "(CI green on unchanged automain — qualification spawned)",
                version_id,
            )

    # Re-drive quiescent ``not_promoted`` versions whose only blocker was the
    # ``criterion_coverage_incomplete`` gate, once a criterion retraction /
    # supersession (or fresh evidence) has cleared coverage on the UNCHANGED
    # automain SHA.  The acceptance-contract safety valve: an uncoverable
    # criterion that is retracted must let the version re-audit and clear the
    # gate without a SHA change or operator action, rather than stranding
    # not_promoted.  Mirrors the ci_unresolved reconciler above; runs before the
    # generic stale-not-promoted loop.
    for version_id in version_ids:
        try:
            reconciled = await _reconcile_if_criterion_coverage_now_complete(
                version_id,
                session_factory=session_factory,
            )
        except Exception:
            logger.exception(
                "[RELEASE_QUALIFIER] sweep: criterion_coverage-now-complete "
                "reconcile failed for version %s",
                version_id,
            )
            continue
        if reconciled:
            finalized += 1
            logger.info(
                "[RELEASE_QUALIFIER] sweep re-drove criterion_coverage version %s "
                "(coverage cleared on unchanged automain — qualification spawned)",
                version_id,
            )

    # Re-drive qualifications stranded in ``not_promoted`` once blockers clear.
    # When a stabilize-phase version receives a conclusive ``not_promoted`` verdict,
    # ``run_qualification`` caches it by candidate SHA; neither the strategist nor
    # the admin endpoint re-enters automatically once the standing verdict reads
    # "settled".  So once all gating audit findings are ledger-resolved AND
    # ``automain`` advances past the ``not_promoted`` SHA, the version strands
    # indefinitely with a stale verdict.  This loop is the scheduled re-driver:
    # for any open ``not_promoted`` stabilize-phase version it first checks
    # readiness (all gating findings resolved), then re-invokes
    # ``run_qualification`` which under its own FOR UPDATE lock re-resolves the
    # candidate SHA — if the SHA advanced, the cached-verdict guard is a miss and
    # a fresh qualification spawns; if unchanged, it returns ``cached_verdict``
    # and this is a no-op.  ``run_qualification`` is fully self-defending, so a
    # version that changed state since the scan is a safe no-op.
    for version_id in version_ids:
        try:
            redriven = await _redrive_not_promoted_qualification(
                version_id,
                session_factory=session_factory,
            )
        except Exception:
            logger.exception(
                "[RELEASE_QUALIFIER] sweep: not_promoted re-drive failed "
                "for version %s",
                version_id,
            )
            continue
        if redriven:
            logger.info(
                "[RELEASE_QUALIFIER] sweep re-drove not_promoted version %s "
                "(blockers resolved and HEAD advanced — qualification spawned)",
                version_id,
            )

    # Reconcile open versions whose promotion-failure candidate is already on
    # main.  A promotion_failed version is kept open so the next strategic cycle
    # can retry, but if the blocked PR later lands out-of-band (operator merge,
    # a stalled auto-merge fires, a transient CI block clears) nothing calls the
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

    # Re-drive open versions stranded in ``promotion_failed``.  After a promotion
    # PR's CI goes red, the version is recorded as ``promotion_failed`` and
    # returned to stabilize — but no scheduled path triggers re-qualification once
    # the underlying CI failure is fixed (or was an infra flake).  This loop
    # re-enters the qualifier for each open stabilize-phase ``promotion_failed``
    # version, gated by ``compute_requalification_readiness`` (which already defers
    # while a ``promotion_ci_remediation`` fix task is in flight) and by the
    # circuit-breaker cap on consecutive promotion failures.  Runs AFTER
    # ``_reconcile_if_promotion_failed_landed`` (which handles the version if the
    # candidate already landed on main) so an out-of-band promotion is recorded
    # before we attempt to re-qualify.
    for version_id in version_ids:
        try:
            redriven = await _redrive_promotion_failed_qualification(
                version_id,
                session_factory=session_factory,
            )
        except Exception:
            logger.exception(
                "[RELEASE_QUALIFIER] sweep: promotion_failed re-drive failed "
                "for version %s",
                version_id,
            )
            continue
        if redriven:
            logger.info(
                "[RELEASE_QUALIFIER] sweep re-drove promotion_failed version %s "
                "(re-qualification spawned after automain advanced)",
                version_id,
            )

    # Dry-run-pass candidates, once the operator enables promotion.  A candidate
    # that passes qualification while ``promotion_enabled=False`` records a
    # conclusive ``dry_run`` verdict (``would_promote=True``); nothing acts on it
    # when promotion is later enabled (stall alerts treat ``dry_run`` as healthy,
    # and the same-SHA ``qualify_request`` no-ops via the cached-verdict guard).
    # Two triggers cover this state and MUST NOT both fire for the same version
    # (audit 8989794e): the cheap, deterministic direct-promote path
    # (``_promote_dry_run_pass_if_enabled``) handles a candidate that is STILL the
    # integration-branch HEAD — it already passed every gate, so it only needs
    # promoting — while the expensive, non-deterministic full re-qualification
    # (``_redrive_dry_run_candidate``) is reserved for the genuine re-qualification
    # case where automain has advanced past the candidate SHA.  The cheap path
    # runs FIRST and records which versions it promoted; the expensive re-driver
    # then SKIPS those, so a still-current candidate is never needlessly (and
    # non-deterministically) re-audited after it was already promoted.  Both run
    # AFTER the ``promotion_failed`` re-driver so a version that exited ``dry_run``
    # via another path is handled there first.
    dry_run_promoted_ids: set = set()
    for version_id in version_ids:
        try:
            promoted = await _promote_dry_run_pass_if_enabled(
                version_id,
                session_factory=session_factory,
            )
        except Exception:
            logger.exception(
                "[RELEASE_QUALIFIER] sweep: dry_run promote failed for version %s",
                version_id,
            )
            continue
        if promoted:
            dry_run_promoted_ids.add(version_id)
            finalized += 1
            logger.info(
                "[RELEASE_QUALIFIER] sweep promoted dry-run-pass version %s "
                "(promotion now enabled — candidate current)",
                version_id,
            )
    for version_id in version_ids:
        # Skip any candidate the cheap direct-promote path already handled this
        # cycle — re-qualifying a still-current candidate is wasteful and risks a
        # non-deterministic audit flipping a previously-passing candidate to fail.
        if version_id in dry_run_promoted_ids:
            continue
        try:
            redriven = await _redrive_dry_run_candidate(
                version_id,
                session_factory=session_factory,
            )
        except Exception:
            logger.exception(
                "[RELEASE_QUALIFIER] sweep: dry_run re-drive failed "
                "for version %s",
                version_id,
            )
            continue
        if redriven:
            logger.info(
                "[RELEASE_QUALIFIER] sweep re-drove dry_run version %s "
                "(promotion enabled — re-qualification spawned)",
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

    # Reconcile open versions whose promotion PR is awaiting auto-merge but
    # has already landed on main (normal path: required CI passed, auto-merge
    # fired) or was closed without merging (can never auto-merge, drives
    # version to promotion_failed so retry machinery applies).
    for version_id in version_ids:
        try:
            reconciled = await _reconcile_if_pending_auto_merge_landed(
                version_id,
                session_factory=session_factory,
            )
        except Exception:
            logger.exception(
                "[RELEASE_QUALIFIER] sweep: pending_auto_merge reconcile check "
                "failed for version %s",
                version_id,
            )
            continue
        if reconciled:
            finalized += 1
            logger.info(
                "[RELEASE_QUALIFIER] sweep reconciled version %s "
                "(pending_auto_merge drove to terminal state)",
                version_id,
            )

    # Reconcile open versions with a migration-carrying promotion PR in
    # managed_check_wait mode.  These versions have
    # last_qualification.promotion_pending.managed_check_wait=True and were NOT
    # delegated to GitHub native auto-merge (because the pre-merge Alembic guard
    # must be re-run after CI passes).  This rung: (a) records success when the
    # candidate lands out-of-band, (b) re-runs the guard and retries the merge
    # when CI turns green, and (c) records terminal failures for red CI,
    # migration guard blocks, and non-check protection blocks.  Runs BEFORE the
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
    # pending_auto_merge loop (which handles pending WITH promotion_pending) so a
    # normally-landed auto-merge is never mis-classified as superseded.
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
    # _reconcile_if_pending_auto_merge_landed terminal-state path is a no-op and
    # the version strands indefinitely, blocking the whole self-improve loop.
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

    # Fire the INITIAL qualification for stabilize-phase versions that have
    # never been qualified once their criterion coverage is complete.  Every
    # other loop in this sweep is a RE-driver; this is the only trigger that
    # fires the first qualification automatically — filling the gap where the
    # strategist never emitted qualify_request.  Runs AFTER all RE-driver loops
    # so a version that transitioned state during this sweep cycle is handled
    # by the appropriate re-driver first.
    for version_id in version_ids:
        try:
            fired = await _fire_initial_qualification_if_coverage_complete(
                version_id,
                session_factory=session_factory,
            )
        except Exception:
            logger.exception(
                "[RELEASE_QUALIFIER] sweep: initial qualification fire failed "
                "for version %s",
                version_id,
            )
            continue
        if fired:
            finalized += 1
            logger.info(
                "[RELEASE_QUALIFIER] sweep fired initial qualification for "
                "version %s (coverage complete — first qualification spawned)",
                version_id,
            )

    # Standing full-suite drift sentinel: on a build_config opt-in, dispatch
    # ci.yml on the integration branch for each stabilize-phase open version,
    # alert+harvest when a run concludes red.  This catches full-suite CI drift
    # before it accumulates invisibly across hundreds of targeted-CI merges and
    # surfaces at a promotion-gate failure.  Each version runs in its own
    # dedicated session via the function itself.
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

    # Event-driven qualification trigger resolver (Phase 0 — observe mode).
    # For each open version: gather inputs, call resolve_qualification_trigger,
    # and in "observe" mode log the would-fire decision without actually spawning.
    # In "on" mode, call run_qualification for matched versions.
    # This loop runs LAST so all re-driver paths above have already handled any
    # version that transitioned state during this sweep cycle.
    await _run_auto_qualification_trigger_sweep(
        open_versions,
        session,
        session_factory=session_factory,
    )

    # Lifecycle-stall checks: alert when an open version is stuck at either end
    # of the lifecycle — calcifying in stabilize (repeated hard-blocker
    # failures) or stalled in build (sitting too long without advancing).  Each
    # version gets its own dedicated session (same isolation pattern as the
    # finalize loop above) so a messaging failure on one version can't affect
    # another.  Extracted to a helper so the fresh-per-version re-read is
    # independently testable.
    await _run_lifecycle_stall_alerts(open_versions, session_factory=session_factory)

    return finalized

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
    ReleaseDeployArtifact,
)
from app.release_artifacts import required_release_artifact_kinds
from app.release_promotion import _notify_operator
from app.release_reconciliation import (
    _confirm_serving_or_fail,
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
    promotes, which is always safe.  The caller commits the session.
    """
    qual = dict(version.last_qualification or {})
    age_minutes = age_seconds / 60.0
    detail = (
        f"qualification pending {age_minutes:.0f} min "
        f"(> {_MAX_QUALIFICATION_DURATION_SECONDS // 60} min limit) — a "
        f"qualifier child never reached a terminal state"
    )
    record = {
        **qual,
        "status": "infra_failure",
        "blocker": "qualification_timeout",
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
                f"(blocker=qualification_timeout); the version returns to the "
                f"stabilize phase for the next strategic cycle."
            ),
        )
    )
    logger.warning(
        "[RELEASE_QUALIFIER] sweep: version v%d qualification timed out "
        "(pending %.0f min) — force-finalized as qualification_timeout",
        version.version_number,
        age_minutes,
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


async def _redrive_deferred_unsynced_qualification(
    version_id,
    *,
    session_factory=None,
) -> bool:
    """Re-enter the qualifier for a version stranded in ``deferred_unsynced``.

    The qualify-on-synced gate (``release_qualifier._check_lts_sync_gate``)
    DEFERS a candidate the promotion target is not yet absorbed into — a
    ``main -> integration`` back-merge is still pending — and records
    ``last_qualification.status == "deferred_unsynced"``.  The deferral is meant
    to be retryable, but ``run_qualification`` only ever re-enters via a fresh
    strategic ``qualify_request`` or the admin ``POST /qualify`` endpoint; there
    is no scheduled re-driver.  So once the strategist stops re-emitting (its own
    status reads a standing ``deferred_unsynced`` as "qualification in
    progress"), the version strands there indefinitely — even after the
    back-merge has made it synced — and the gate's OWN bounded fall-through never
    fires, because that fall-through is evaluated only ON RE-ENTRY.

    This is that missing scheduled re-driver.  For an open, stabilize-phase
    version whose ``last_qualification.status == "deferred_unsynced"`` it
    re-invokes ``run_qualification`` in a dedicated session.
    ``run_qualification`` re-snapshots the integration-branch HEAD and either
    PROCEEDS — now synced, or the bounded fall-through window has elapsed —
    spawning the real audit, or RE-DEFERS (still behind) while preserving
    ``first_deferred_at`` so the fall-through clock keeps accumulating across
    sweeps.  ``run_qualification`` is fully self-defending (it re-checks phase /
    in-flight / promotion / cached-verdict under its own ``FOR UPDATE`` row
    lock), so a version that changed state since the scan is a safe no-op, and a
    still-unsynced candidate spawns nothing (the gate returns before the spawn).

    Returns True only when the re-entry actually STARTED a qualification
    (``status == "spawned"``) — i.e. the deferral cleared — so the sweep can
    count and log it.  A continued deferral or any guard no-op returns False.

    **Rollback on non-spawned result**: when ``run_qualification`` returns any
    status other than ``"spawned"`` (e.g. ``sha_error``, ``no_credentials``,
    still-deferred), the session is rolled back rather than committed.  This
    preserves the pre-existing ``last_qualification`` (including
    ``first_deferred_at``) so the version stays in ``deferred_unsynced`` and
    remains eligible for the next sweep.  Without the rollback an error result
    would overwrite ``last_qualification`` with a non-deferred status, silently
    removing the version from the re-drive loop.
    """
    # ``run_qualification`` lives in release_qualifier (the core coordinator);
    # import it lazily to avoid the load-time import cycle (release_qualifier
    # imports this module at top for its re-exports).
    from app import release_qualifier

    factory = session_factory or AsyncSessionLocal
    async with factory() as redrive_session:
        version = await redrive_session.get(release_version.ReleaseVersion, version_id)
        if version is None:
            return False
        qual = version.last_qualification
        if (
            not isinstance(qual, dict)
            or qual.get("status") != "deferred_unsynced"
            or version.phase != "stabilize"
        ):
            return False
        result = await release_qualifier.run_qualification(
            redrive_session, version.project_id, version
        )
        if result and result.get("status") == "spawned":
            await redrive_session.commit()
            return True
        # run_qualification returned a non-spawned status (sha_error, no_credentials,
        # still deferred, etc.) — rollback so last_qualification is not overwritten
        # with an error state that would drop this version from the re-drive loop
        # (only deferred_unsynced versions are picked up by the next sweep).
        await redrive_session.rollback()
        return False


async def _redrive_not_promoted_qualification(
    version_id,
    *,
    session_factory=None,
) -> bool:
    """Re-enter the qualifier for a ``not_promoted`` version whose blockers cleared.

    When a stabilize-phase version receives a conclusive ``not_promoted`` verdict,
    ``run_qualification`` caches that verdict by candidate SHA and will not re-run
    as long as the candidate SHA is unchanged.  But once the gating audit findings
    are ledger-resolved AND ``automain`` has advanced past the ``not_promoted``
    candidate's SHA, the stale verdict is no longer valid — a fresh qualification
    against the new HEAD is warranted.  Neither the strategic ``qualify_request``
    path nor the admin endpoint drive this re-entry automatically: the strategist
    reads the standing ``not_promoted`` status as "qualification settled" and stops
    emitting, so the version strands indefinitely even after all blockers clear.

    This is that missing scheduled re-driver.  For each sweep it checks an open,
    stabilize-phase version whose ``last_qualification.status == "not_promoted"``
    and:

      1. Calls ``compute_requalification_readiness`` to verify ALL gating
         high/medium audit findings are ledger-resolved (``resolved`` or ``stale``).
         If not, returns False immediately — the blockers have NOT cleared.
         Versions with zero gating findings (e.g. blocked by ``ci_not_green``)
         pass this check: the hard blockers may be resolved by a code fix, and
         HEAD advancement is the signal that warrants a fresh qualification.
      2. Re-invokes ``run_qualification``, which under its own ``FOR UPDATE`` lock
         re-resolves the candidate SHA.  If ``automain`` has NOT advanced (same
         SHA), ``run_qualification`` returns ``cached_verdict`` and this returns
         False — nothing to do.  If ``automain`` HAS advanced, the cached-verdict
         guard is a miss (new SHA) and a fresh qualification spawns.

    Returns True only when the re-entry actually STARTED a new qualification
    (``status == "spawned"``).  Any guard no-op or readiness miss returns False.

    ``run_qualification`` is fully self-defending (phase check, in-flight check,
    cached-verdict check, all under a ``FOR UPDATE`` lock), so a version that
    changed state between the scan and the re-drive is a safe no-op.
    """
    # Both ``run_qualification`` and ``compute_requalification_readiness`` live
    # in modules that import this one (directly or transitively), so import
    # them lazily to avoid load-time import cycles.
    from app import release_qualifier
    from app.release_readiness import compute_requalification_readiness

    factory = session_factory or AsyncSessionLocal
    async with factory() as redrive_session:
        version = await redrive_session.get(release_version.ReleaseVersion, version_id)
        if version is None:
            return False
        qual = version.last_qualification
        if (
            not isinstance(qual, dict)
            or qual.get("status") != "not_promoted"
            or version.phase != "stabilize"
        ):
            return False
        # Readiness guard: all gating audit findings must be ledger-resolved.
        # compute_requalification_readiness returns requalification_ready=True in two
        # cases: (1) zero carried high/medium audit findings (e.g. version blocked by
        # non-audit blocker like ci_not_green that has since cleared), or (2) all
        # gating findings ledger-resolved.  In both cases the version is eligible for
        # re-qualification against the current automain HEAD.
        readiness = await compute_requalification_readiness(
            redrive_session, version.project_id
        )
        if not readiness.get("requalification_ready"):
            return False
        # The real backstop against redundant re-runs is run_qualification's cached-
        # verdict guard: under its FOR UPDATE lock, it re-resolves the candidate SHA.
        # If automain has NOT advanced (same SHA), run_qualification returns cached_
        # verdict (no-op).  If automain HAS advanced, the cached-verdict miss spawns a
        # fresh qualification.
        result = await release_qualifier.run_qualification(
            redrive_session, version.project_id, version
        )
        if result and result.get("status") == "spawned":
            await redrive_session.commit()
            return True
        # run_qualification returned a non-spawned status (error, cached_verdict,
        # already_pending, etc.) — rollback so last_qualification is not corrupted
        # and the identity map is expired for subsequent session reads.
        await redrive_session.rollback()
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
        if not head_sha or head_sha == candidate_sha:
            # HEAD has not advanced — stale rows would be premature.
            return 0

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
            and f.get("key")
        ]
        if not gating_findings:
            return 0

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
            key = f["key"]
            if key in existing_keys:
                continue
            await apply_finding_resolution(
                session,
                version.project_id,
                version.id,
                key,
                "stale",
                severity=str(f.get("severity") or "").lower() or None,
                note=("auto-staled: automain advanced past not_promoted candidate SHA"),
                verified_against_sha=head_sha,
            )
            written += 1

        if written:
            await _notify_operator(
                session,
                version,
                "release_qualification_auto_stale",
                (
                    f"v{version.version_number} auto-staled {written} finding(s): "
                    f"automain advanced past candidate {candidate_sha[:12]} "
                    f"to {head_sha[:12]}. A fresh qualification will be triggered."
                ),
                extra={
                    "written": written,
                    "candidate_sha": candidate_sha,
                    "head_sha": head_sha,
                },
            )
            await session.commit()
            logger.info(
                "[RELEASE_QUALIFIER] auto_stale: wrote %d stale ledger row(s) "
                "for version %s (automain %s advanced past candidate %s)",
                written,
                version_id,
                head_sha[:12],
                candidate_sha[:12],
            )

        return written


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

    now = _utcnow()
    finalized = 0
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
            finalized += 1
            logger.info(
                "[RELEASE_QUALIFIER] sweep re-drove deferred_unsynced version %s "
                "(now synced — qualification spawned)",
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
            finalized += 1
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

    # Lifecycle-stall checks: alert when an open version is stuck at either end
    # of the lifecycle — calcifying in stabilize (repeated hard-blocker
    # failures) or stalled in build (sitting too long without advancing).  The
    # two checks are mutually exclusive per version (each early-returns on the
    # wrong phase) but both run so whichever applies fires.  Each version gets
    # its own dedicated session (same isolation pattern as the finalize loop
    # above) so a messaging failure on one version can't affect another.
    factory = session_factory or AsyncSessionLocal
    for version in open_versions:
        try:
            async with factory() as alert_session:
                v = await alert_session.get(release_version.ReleaseVersion, version.id)
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

    return finalized

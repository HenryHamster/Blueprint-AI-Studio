"""Scheduled re-drivers for stranded release qualifications.

Extracted from ``release_qualifier_sweep`` (the sweep module's size cap left no
headroom for routine edits).  These are the per-status re-entry helpers the
sweep (``sweep_stuck_qualifications``) invokes once per cycle to re-drive a
version stranded in a non-conclusive qualification state back into
``run_qualification`` once its blocking condition has actually cleared.

This module implements EXACTLY these six re-drivers, and nothing else:

    ``_redrive_error_qualification``               status=error (transient codes)
    ``_redrive_infra_failure_qualification``       status=infra_failure
    ``_redrive_rubric_changed_qualification``      status=rubric_changed /
                                                   rubric_provenance_unknown
    ``_redrive_walkthrough_contract_qualification`` status=not_promoted with
                                                   blocker=walkthrough_e2e_
                                                   contract_failed
    ``_redrive_promotion_failed_qualification``    status=promotion_failed
    ``_redrive_deferred_unsynced_qualification``   status=deferred_unsynced

Each is bounded by its own attempt (or wall-clock) cap and escalates to an
operator Alert at that cap rather than spinning.

NOT here, and deliberately so: standing ``dry_run`` and candidate-advanced
``not_promoted`` re-entry.  Those are the sweep executor's own triggers
(``post_dry_run_advance`` and ``candidate_advanced`` in
``_fire_auto_triggered_qualification``), and the force-finalize / reconcile
machinery plus the sweep orchestrator stay in ``release_qualifier_sweep``.

An earlier version of this docstring claimed re-drivers for ``deferred_unsynced``,
``promotion_failed`` and ``not_promoted`` that had been deleted (commit
a9ed8d6e) and never replaced.  That claim, plus the matching "owned by its
dedicated re-driver" comment on the sweep executor's trigger filter, is why a
version whose promotion PR went red stayed parked forever with nobody noticing:
the resolver armed it, the executor discarded the decision as re-driver-owned,
and no re-driver existed.  Two of those three are now implemented (the third,
``not_promoted``, is genuinely executor-owned).  Keep this list literal.

The public surface is UNCHANGED: ``release_qualifier_sweep`` re-imports every
symbol defined here, so ``app.release_qualifier_sweep._redrive_*`` (and the
tunable caps) remain importable and test-patchable at their original paths, and
``release_qualifier`` continues to re-export them from the sweep module.
"""

from __future__ import annotations


import logging
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm.attributes import flag_modified

from app import release_version
from app.database import AsyncSessionLocal
from app.models import Alert, AlertSeverity, Project
from app.time_utils import _utcnow

# First cap for this module: it crossed the 1500-line coverage threshold
# when the two missing re-drivers were restored.  The value is the canonical
# policy headroom for a newly tracked file of this size
# (``check_size_limits.recommended_cap_for_line_count``), not a hand-picked
# number -- a hand-tight cap would put the file straight into the
# metrics-integrity ``near_cap_size_warning`` band on the day it landed.
_MODULE_SIZE_GOVERNANCE_CAP: int = 2050

logger = logging.getLogger(__name__)

# Maximum number of sweep-driven re-entries for a ``status=error`` version.
# A single transient GitHub outage (sha_error, no_credentials) or a walkthrough
# execution-site outage (spawn_failed) should clear on the next sweep; a
# persistent failure is an operator issue.  Cap at 5 so a permanently broken
# credential path does not retry forever (a capped walkthrough spawn_failed can
# still re-arm once per execution-site capability change — see
# ``_redrive_error_qualification``).
_MAX_ERROR_REDRIVE_ATTEMPTS = 5

# Maximum number of sweep-driven re-entries for a ``status=infra_failure`` version.
# ``infra_failure`` is documented as non-conclusive — it records a transient
# infrastructure problem (qualifier child timeout, unavailable GitHub check, etc.)
# rather than a real quality verdict.  Cap at 3 to prevent a persistently broken
# infrastructure path from retrying forever; an operator escalation is warranted
# once this many consecutive re-drives still result in infra_failure.
_MAX_INFRA_FAILURE_REDRIVE_ATTEMPTS = 3

# Time-based backoff schedule for infra-failure re-drives keyed on the
# candidate-agnostic same-(signature, engine_sha) streak.  The streak counts
# consecutive trailing infra_attempt_history entries that share BOTH the
# current infra_failure_signature AND the current engine_sha — it does NOT
# reset when the candidate SHA advances, so a persistent infra condition that
# survives multiple automain promotions is still gated.
#
# Index == streak length (clamped to the last entry):
#   0 → immediate (first same-signature attempt — no backoff)
#   1 → 60 s  (second consecutive hit — short cooldown, might self-resolve)
#   2 → 300 s (third hit — 5 min, wait for infra to stabilise)
#   3+ → 1800 s (four or more hits — 30 min, clearly structural)
_INFRA_BACKOFF_SCHEDULE: tuple[int, ...] = (0, 60, 300, 1800)


def _infra_backoff_seconds(same_signature_streak: int) -> int:
    """Minimum wait (seconds) before re-driving the same-(signature, engine_sha) failure.

    Pure helper — no I/O.  ``same_signature_streak`` is the count of trailing
    infra_attempt_history entries that share BOTH the current failure signature
    AND the current engine_sha (candidate-agnostic: SHA changes do not reset it).
    """
    if same_signature_streak <= 0:
        return 0
    idx = min(same_signature_streak, len(_INFRA_BACKOFF_SCHEDULE) - 1)
    return _INFRA_BACKOFF_SCHEDULE[idx]


def _is_walkthrough_qualification(qual: dict | None) -> bool:
    """True when a qualification record belongs to the walkthrough_e2e probe leg.

    A walkthrough infra failure carries a ``walkthrough_e2e``-prefixed ``error``
    or ``infra_failure_signature``.  A walkthrough ``spawn_failed`` record
    carries NO signature (the probe never spawned — the site check failed
    first); there the probe is named in ``requested_probes`` instead.
    """
    if not isinstance(qual, dict):
        return False
    error = str(qual.get("error") or "")
    if error.startswith("walkthrough_e2e"):
        return True
    signature = str(qual.get("infra_failure_signature") or "")
    if signature.startswith("walkthrough_e2e"):
        return True
    if error == "spawn_failed":
        probes = qual.get("requested_probes")
        if isinstance(probes, (list, tuple, set, frozenset)) and (
            "walkthrough_e2e" in probes
        ):
            return True
    return False


def _walkthrough_execution_site_changed(qual: dict | None) -> bool:
    """True when a capped WALKTHROUGH_e2e failure was recorded by an engine
    revision that is no longer serving.

    The walkthrough product probe's failures are execution-site failures
    (candidate-stack boot, Docker/Compose/Chromium availability on the runner).
    A deploy that changes the walkthrough dispatch capability (e.g. the
    supported-site repair) lands under a NEW serving engine sha, so a capped
    record whose ``engine_sha`` differs from the current serving sha means the
    repair may have taken effect.  The redrive uses this to re-arm the cap
    exactly once.  Applies to both walkthrough infra-failure records (signature
    prefix) and walkthrough ``spawn_failed`` records (probe named in
    ``requested_probes``).  Non-walkthrough legs have no site dependency and
    never re-arm; a record with no detectable engine change fails closed (no
    re-arm).
    """
    if not _is_walkthrough_qualification(qual):
        return False
    last_engine_sha = qual.get("engine_sha")
    if not last_engine_sha:
        for _entry in reversed(qual.get("infra_attempt_history") or []):
            if isinstance(_entry, dict) and _entry.get("engine_sha"):
                last_engine_sha = _entry.get("engine_sha")
                break
    if not last_engine_sha:
        return False
    from app.deployed_sha import get_serving_sha

    current = get_serving_sha()
    return bool(current) and last_engine_sha != current


_RUBRIC_REDRIVE_STATUSES: frozenset[str] = frozenset(
    {"rubric_changed", "rubric_provenance_unknown"}
)

# Maximum number of sweep-driven re-launches for a version whose acceptance
# rubric changed (or could not be reconstructed) mid-flight.  A rubric change
# invalidates the in-flight attempt — it measured nothing under the final
# rubric — so a FRESH grade against the CURRENT rubric is always the recovery
# path.  But a rubric that keeps changing must not spin attempts indefinitely:
# cap the automatic relaunches so the second rubric-change re-drive (count == 2)
# raises an operator Alert and halts.  Mirrors ``qualification_triggers._MAX_
# RUBRIC_REDRIVE_ATTEMPTS`` (the resolver's cap for the read surfaces).
_MAX_RUBRIC_REDRIVE_ATTEMPTS = 2

# Maximum number of sweep-driven re-entries for a ``status=not_promoted``
# version blocked on ``walkthrough_e2e_contract_failed``.
#
# The walkthrough evidence-contract blocker is EXECUTION_SITE_DEPLOYED: the
# report-key validation was performed by the DEPLOYED qualifier against the
# DEPLOYED rubric, so a fix on automain is inert until a conforming change
# lands and the version's next candidate carries it.  The recovery signal is a
# change in the qualification inputs — an automain advance (the conforming fix)
# or a rubric/goal change (the contract_fingerprint drifts) — which is exactly
# when ``run_qualification``'s cached-verdict guard releases the blocked
# candidate for a fresh qualification.  The cap bounds how many such automatic
# re-entries the sweep performs before it halts and raises an operator Alert: a
# deployed site that keeps rejecting the candidate's reports (or a team that
# keeps shipping reports that violate the contract) must surface to a human
# instead of spinning fresh qualification attempts forever.
_MAX_WALKTHROUGH_CONTRACT_REDRIVE_ATTEMPTS = 3

# Transient error codes the sweep is allowed to re-drive.  Permanent errors
# (e.g. project misconfiguration) should not be blindly retried.
#
# ``spawn_failed`` is transient because its most common cause is a
# WALKTHROUGH_e2e execution-site outage — no live runner proved the
# candidate-stack site (docker info + Compose + Chromium) at spawn time, a
# fleet condition that clears when a site-proven runner comes online or a
# supported-site repair deploys.  The re-drive is still bounded by
# ``_MAX_ERROR_REDRIVE_ATTEMPTS``, and a CAPPED walkthrough spawn_failed
# re-arms exactly once per execution-site capability change (see
# ``_redrive_error_qualification``), so a genuinely permanent spawn defect
# cannot retry forever.
_TRANSIENT_ERROR_CODES: frozenset[str] = frozenset(
    {"sha_error", "no_credentials", "spawn_failed"}
)


_MAX_PROMOTION_FAILURE_REQUALIFY_ATTEMPTS = 3
"""Circuit-breaker cap on autonomous re-qualify attempts after a promotion_failed.

When a version's ``last_qualification.promotion_failure_count`` reaches this cap
the automatic re-driver stops and raises an operator-visible Alert instead of
looping forever.  Each _record_promotion_failure call increments the counter,
so this caps the TOTAL number of autonomous promotion attempts (fix→qualify→
promote→CI-red) before human review is required.
"""


async def _escalate_promotion_failure_loop(
    session,
    version,
    *,
    commit: bool = True,
) -> bool:
    """Persist the operator-visible halt for an exhausted promotion retry cap."""
    qual = version.last_qualification
    if not isinstance(qual, dict) or qual.get("promotion_loop_escalated"):
        return False
    count = int(qual.get("promotion_failure_count") or 0)
    alert = Alert(
        project_id=version.project_id,
        severity=AlertSeverity.ERROR,
        title=f"Release v{version.version_number} promotion re-drive loop halted",
        message=(
            f"Release v{version.version_number} reached the autonomous promotion "
            f"re-drive cap ({count}/{_MAX_PROMOTION_FAILURE_REQUALIFY_ATTEMPTS}) "
            "after repeated promotion failures. Automatic re-qualification is "
            "halted; operator review or a changed audited candidate is required."
        ),
        event_type="release.qualification_crash_loop",
    )
    session.add(alert)
    version.last_qualification = {
        **qual,
        "promotion_loop_escalated": True,
    }
    flag_modified(version, "last_qualification")
    session.add(version)
    if commit:
        await session.commit()
    logger.warning(
        "[RELEASE_QUALIFIER] v%d promotion_failure_count=%d reached cap=%d — "
        "raised operator Alert and halted auto re-drive",
        version.version_number,
        count,
        _MAX_PROMOTION_FAILURE_REQUALIFY_ATTEMPTS,
    )
    return True


async def _escalate_promotion_failure_loop_in_dedicated_session(
    version_id,
    *,
    session_factory=None,
) -> bool:
    """Lock and escalate a capped promotion-failed version safely."""
    factory = session_factory or AsyncSessionLocal
    async with factory() as session:
        version = await session.get(release_version.ReleaseVersion, version_id)
        if version is None:
            return False
        qual = version.last_qualification
        if (
            not isinstance(qual, dict)
            or qual.get("status") != "promotion_failed"
            or int(qual.get("promotion_failure_count") or 0)
            < _MAX_PROMOTION_FAILURE_REQUALIFY_ATTEMPTS
        ):
            return False
        return await _escalate_promotion_failure_loop(session, version)


async def _redrive_error_qualification(
    version_id,
    *,
    session_factory=None,
) -> bool:
    """Re-enter the qualifier for a ``status=error`` version caused by a transient failure.

    A single transient GitHub outage (``sha_error``, ``no_credentials``) or a
    walkthrough execution-site outage (``spawn_failed`` — no live runner proved
    the candidate-stack site at spawn time) records ``last_qualification.status
    == "error"`` and parks the version indefinitely because nothing schedules a
    retry — neither the strategist (which reads a standing ``error`` state as
    settled) nor any other sweep loop.

    This re-driver fires for open, stabilize-phase versions whose
    ``last_qualification.status == "error"`` AND whose ``error`` code is in
    ``_TRANSIENT_ERROR_CODES`` AND whose ``last_qualification["error_redrive_count"]``
    is below ``_MAX_ERROR_REDRIVE_ATTEMPTS``.  The attempt cap prevents a
    permanently broken path from retrying forever.

    A CAPPED walkthrough ``spawn_failed`` re-arms exactly once when the
    execution-site capability changed (the engine revision that recorded the
    record is no longer serving), mirroring the infra-failure re-arm in
    ``_redrive_infra_failure_qualification``: a fleet that had no site-proven
    runner gets one genuine attempt under the newly-deployed dispatch site before
    the cap re-applies.  Non-walkthrough ``spawn_failed`` records and the GitHub
    outage codes never re-arm — their cap is final.

    The cap is tracked in ``last_qualification["error_redrive_count"]`` (a
    per-error-episode counter), NOT in the lifetime ``qualification_attempts``
    column.  Using the lifetime counter was a bug: any version with 5+ prior
    qualification attempts of ANY kind would be blocked from all transient-error
    re-drives, stranding it silently at status=error regardless of how many
    error episodes it had actually seen.

    Returns True only when the re-entry actually STARTED a new qualification
    (``status == "spawned"``).  Any guard no-op returns False.

    ``run_qualification`` is fully self-defending (phase check, in-flight check,
    all under a ``FOR UPDATE`` lock), so a version that changed state between the
    scan and the re-drive is a safe no-op.
    """
    from app import release_qualifier

    factory = session_factory or AsyncSessionLocal
    async with factory() as redrive_session:
        version = await redrive_session.get(release_version.ReleaseVersion, version_id)
        if version is None:
            return False
        qual = version.last_qualification
        if (
            not isinstance(qual, dict)
            or qual.get("status") != "error"
            or qual.get("error") not in _TRANSIENT_ERROR_CODES
            or version.phase != "stabilize"
        ):
            return False
        # Bounded attempts: do not retry a permanently broken path.
        # Use the per-episode counter stored in last_qualification, NOT the
        # lifetime qualification_attempts column.  The lifetime counter accrues
        # across all qualification episodes (spawns, not just error re-drives),
        # so a long-lived version with many prior attempts would be incorrectly
        # blocked from transient-error re-drives on its very first error episode.
        error_redrive_count = qual.get("error_redrive_count", 0)
        if error_redrive_count >= _MAX_ERROR_REDRIVE_ATTEMPTS:
            # One-shot re-arm for a CAPPED walkthrough spawn_failed once the
            # execution-site capability changed.  A spawn_failed that never had
            # a site-proven runner is an execution-site failure (docker info +
            # Compose + Chromium unavailable on the fleet at spawn time).  When
            # the engine revision that recorded it is no longer serving, a
            # supported-site repair or newly-online runner may have taken effect
            # — give the re-drive EXACTLY ONE genuine attempt before the cap
            # re-applies.  The re-arm is one-shot: the re-drive writes a fresh
            # spawn_failed under the CURRENT engine sha, so the next cap check
            # (after the counter accumulates again) sees no engine change and
            # halts.  Non-walkthrough errors never re-arm — their cap is final.
            _rearm_after_site_change = (
                qual.get("error") == "spawn_failed"
                and _walkthrough_execution_site_changed(qual)
            )
            if _rearm_after_site_change:
                logger.warning(
                    "[RELEASE_QUALIFIER] sweep: v%d capped walkthrough "
                    "spawn_failed re-armed once after execution-site capability "
                    "change (record engine_sha=%s no longer serving)",
                    version.version_number,
                    str(qual.get("engine_sha") or "?")[:12],
                )
                error_redrive_count = 0
            else:
                if not qual.get("error_redrive_loop_escalated"):
                    alert = Alert(
                        project_id=version.project_id,
                        severity=AlertSeverity.ERROR,
                        title=f"Release v{version.version_number} transient re-drive loop halted",
                        message=(
                            f"Release v{version.version_number} reached the transient "
                            f"error re-drive cap ({error_redrive_count}/"
                            f"{_MAX_ERROR_REDRIVE_ATTEMPTS}) for "
                            f"{qual.get('error')}. Automatic re-qualification is "
                            "halted; operator review or a repaired credential/site "
                            "is required."
                        ),
                        event_type="release.qualification_crash_loop",
                    )
                    redrive_session.add(alert)
                    version.last_qualification = {
                        **qual,
                        "error_redrive_loop_escalated": True,
                    }
                    flag_modified(version, "last_qualification")
                    redrive_session.add(version)
                    await redrive_session.commit()
                    logger.warning(
                        "[RELEASE_QUALIFIER] sweep: v%d error_redrive_count=%d "
                        "reached cap=%d — raised operator Alert and halted "
                        "auto re-drive",
                        version.version_number,
                        error_redrive_count,
                        _MAX_ERROR_REDRIVE_ATTEMPTS,
                    )
                return False
        # The CI-compliance qualify gate must not be bypassed by a re-drive.  If
        # the integration HEAD currently breaches the guardrails (per the latest
        # stored evaluation), defer until a repair lands — the sweep's
        # ``_recheck_ci_compliance_blocked_version`` keeps re-evaluating.  Uses
        # the stored record (no fresh GitHub scan under the sweep session); an
        # empty/stale store fails open to the existing re-drive behavior.
        from app.strategic_spawning import ci_compliance_qualify_gate

        project = await redrive_session.get(Project, version.project_id)
        if project is not None and ci_compliance_qualify_gate(project).get("blocked"):
            logger.info(
                "[RELEASE_QUALIFIER] sweep: deferring error re-drive for v%d — "
                "integration HEAD breaches CI-compliance guardrails",
                version.version_number,
            )
            return False
        result = await release_qualifier.run_qualification(
            redrive_session,
            version.project_id,
            version,
            qualification_attempt_id=qual.get("qualification_attempt_id")
            or version.active_qualification_attempt_id,
            retry_episode=int(qual.get("retry_episode") or 0) + 1,
            retry_episode_cap=_MAX_ERROR_REDRIVE_ATTEMPTS,
            requested_probes=qual.get("requested_probes"),
            explicit_request=False,
        )
        # COMMIT on every outcome — do NOT roll back the non-spawned paths.
        #
        # The transient-error early returns (``sha_error`` / ``no_credentials``)
        # write a new last_qualification and bump ``qualification_attempts`` via
        # an atomic UPDATE on THIS session.  Rolling back would discard both.
        # Committing is safe because the version STARTS in an ``error`` state
        # that is meant to be overwritten:
        #   * a repeated transient error rewrites last_qualification to a fresh
        #     ``sha_error`` / ``no_credentials`` (still in _TRANSIENT_ERROR_CODES,
        #     so it stays eligible for the next sweep); and
        #   * any genuine transition (spawned / deferred_unsynced / not_promoted /
        #     a self-defending guard no-op) is a legitimate new state we want to
        #     keep, not revert to a stale ``error``.
        await redrive_session.commit()
        # If still in a transient-error state, persist the per-episode retry
        # counter so the cap advances across sweeps.  A spawned result naturally
        # resets the counter (the new last_qualification won't carry
        # error_redrive_count, so the next error episode starts fresh at 0).
        if result and result.get("status") in _TRANSIENT_ERROR_CODES:
            await redrive_session.refresh(version)
            new_qual = version.last_qualification
            if isinstance(new_qual, dict):
                version.last_qualification = {
                    **new_qual,
                    "error_redrive_count": error_redrive_count + 1,
                }
                flag_modified(version, "last_qualification")
                await redrive_session.commit()
        return bool(result and result.get("status") == "spawned")

async def _redrive_infra_failure_qualification(
    version_id,
    *,
    session_factory=None,
) -> bool:
    """Re-enter the qualifier for a ``status=infra_failure`` version.

    ``infra_failure`` is documented as non-conclusive: it records a transient
    infrastructure problem (qualifier child timeout, unavailable migration-identity
    gate, GitHub API unreachable, etc.) rather than a real quality verdict.  But it
    has no scheduled re-entry — neither the strategist (which treats a standing
    ``infra_failure`` as "re-qualify needed" but only emits ``qualify_request`` on a
    fresh strategic cycle) nor the admin endpoint drives an automatic retry.  So a
    version strands in ``infra_failure`` until the operator manually re-enters or the
    strategist cycle fires.  Live v22 stranded exactly this way on 2026-07-06 when a
    ``migration_identity_check_unavailable`` blocker was recorded during a GitHub
    outage.

    This is the missing scheduled re-driver.  For an open, stabilize-phase version
    whose ``last_qualification.status == "infra_failure"`` and whose PERSISTED
    ``last_qualification["consecutive_infra_errors"]`` streak is below
    ``_MAX_INFRA_FAILURE_REDRIVE_ATTEMPTS``, re-invokes ``run_qualification``.
    ``run_qualification`` already treats ``infra_failure`` as non-conclusive (it does
    not serve a cached verdict for that status), so a fresh qualification spawns on the
    current integration-branch HEAD.

    The attempt cap keys on the PERSISTED ``last_qualification["consecutive_infra_errors"]``
    streak that the finalize path (``_handle_infra_failure``) maintains — a
    counter that resets to 0 only when a non-infra verdict lands — NOT the
    lifetime ``qualification_attempts`` column (which would silently block
    re-drives for long-lived versions) and NOT the synchronous
    ``run_qualification`` return value (which never reports ``infra_failure``:
    that verdict is only ever written asynchronously at finalize, when the
    qualifier children terminate).  The streak accumulates across advancing
    candidate SHAs so that repeated cancellations on a moving automain still
    trip the cap and fire an operator Alert.

    Commit behavior: COMMIT on every outcome (same as ``_redrive_error_qualification``).
    ``run_qualification`` writes a new ``last_qualification`` on every path it takes,
    and a rollback would discard that write plus any ``qualification_attempts`` bump.
    The streak is carried forward across the spawn→finalize cycle by
    ``run_qualification`` (``prior_infra_streak``) and re-incremented by the next
    infra_failure finalize, so the cap advances across sweeps with no post-commit
    bookkeeping here.

    Returns True only when the re-entry actually STARTED a new qualification
    (``status == "spawned"``).  Any guard no-op or cap hit returns False.
    """
    from app import release_qualifier

    factory = session_factory or AsyncSessionLocal
    async with factory() as redrive_session:
        # Row-lock the version so this autonomous re-drive serializes against a
        # concurrently-committing explicit qualification instead of reading a
        # stale ``infra_failure`` marker mid-flight (2026-08-05: an explicit
        # qualification and this re-driver double-spawned duplicate
        # release_audit tasks two minutes apart).
        version = (
            await redrive_session.execute(
                select(release_version.ReleaseVersion)
                .where(release_version.ReleaseVersion.id == version_id)
                .with_for_update()
            )
        ).scalar_one_or_none()
        if version is None:
            return False
        qual = version.last_qualification
        if (
            not isinstance(qual, dict)
            or qual.get("status") != "infra_failure"
            or version.phase != "stabilize"
        ):
            return False
        # A live qualifier audit means a qualification is ALREADY in flight —
        # the ``infra_failure`` marker is just stale (the explicit path only
        # rewrites ``last_qualification`` when its spawn transaction commits).
        # Re-driving now would double-spawn the audit lane, so defer to the
        # running attempt; if it dies, its own finalize/stall machinery updates
        # the marker and a later sweep re-drives cleanly.
        from app.models import Task, TaskStatus

        _live_audit_id = (
            await redrive_session.execute(
                select(Task.id)
                .where(
                    Task.project_id == version.project_id,
                    Task.status.in_(
                        [
                            TaskStatus.QUEUED,
                            TaskStatus.PROVISIONING,
                            TaskStatus.IN_PROGRESS,
                            TaskStatus.EVALUATING,
                            TaskStatus.BLOCKED,
                            TaskStatus.PARKED,
                        ]
                    ),
                    Task.payloads[0]["type"]
                    .as_string()
                    .in_(["release_audit", "release_audit_challenge"]),
                )
                .limit(1)
            )
        ).scalar_one_or_none()
        if _live_audit_id is not None:
            logger.info(
                "[RELEASE_QUALIFIER] sweep: skipping infra_failure re-drive for "
                "v%d — qualifier audit task %s is already live",
                version.version_number,
                _live_audit_id,
            )
            return False
        # Bounded attempts: stop re-driving a persistently broken infra path.
        # Phase 1b (per-dimension failure isolation): when the failing dimension
        # is recorded on the infra_failure record, consult that dimension's OWN
        # per-dimension streak from the QualificationDimensionResult ledger
        # instead of the attempt-global ``consecutive_infra_errors`` — so a
        # persistent migration-gate GitHub outage (migration_identity streak=3)
        # does not exhaust the redrive budget that an audit or smoketest failure
        # would draw on.  Falls back to the attempt-level streak when the
        # failing dimension is absent or the ledger read fails (preserving
        # pre-Phase-1b behaviour in those edge cases).
        _failing_dim = qual.get("failing_dimension")
        _legacy_smoke_failure = release_qualifier._legacy_smoke_infra_lineage(qual)
        _dim_streak: int | None = 0 if _legacy_smoke_failure else None
        if _failing_dim and not _legacy_smoke_failure:
            try:
                from app.models import QualificationDimensionResult

                _csha = qual.get("candidate_sha")
                if _csha:
                    _dim_row = (
                        await redrive_session.execute(
                            select(QualificationDimensionResult).where(
                                QualificationDimensionResult.release_version_id
                                == version.id,
                                QualificationDimensionResult.candidate_sha == _csha,
                                QualificationDimensionResult.dimension == _failing_dim,
                            )
                        )
                    ).scalar_one_or_none()
                    if _dim_row is not None and isinstance(_dim_row.detail, dict):
                        _s = _dim_row.detail.get("consecutive_infra_errors")
                        if isinstance(_s, int) and not isinstance(_s, bool):
                            _dim_streak = _s
            except Exception:  # noqa: BLE001 — best-effort; fall back to attempt-level
                _dim_streak = None
        effective_streak = (
            _dim_streak
            if _dim_streak is not None
            else int(qual.get("consecutive_infra_errors") or 0)
        )
        # Unconditional deadlock backstop for legacy-smoke lineage.
        # _legacy_smoke_infra_lineage zeroes effective_streak (via _dim_streak=0
        # above), so consecutive_infra_errors never accumulates and the cap below
        # never fires for smoke-lineage failures.  When retry_episode has reached
        # the declared cap, forcibly override effective_streak so the existing
        # Alert + halt path fires and the operator is notified.  Guard on
        # _episode_cap > 0 so versions with no declared cap are not affected.
        if _legacy_smoke_failure:
            _episode = int(qual.get("retry_episode") or 0)
            _episode_cap = int(qual.get("retry_episode_cap") or 0)
            if _episode_cap > 0 and _episode >= _episode_cap:
                effective_streak = _MAX_INFRA_FAILURE_REDRIVE_ATTEMPTS
        # One-shot re-arm for a capped WALKTHROUGH infra failure once the
        # execution-site capability changed.  The walkthrough probe's failures
        # are execution-site failures (candidate-stack boot, Docker/Compose/
        # Chromium availability on the runner).  When the engine revision that
        # recorded the streak is no longer serving, the supported-site repair
        # may have taken effect — give the newly-dispatched site EXACTLY ONE
        # genuine attempt before the cap re-applies.  A re-arm attempt that
        # fails again re-caps (the streak carries forward under the NEW engine
        # sha), so the cap is never silently circumvented forever.
        _rearm_after_site_change = (
            effective_streak >= _MAX_INFRA_FAILURE_REDRIVE_ATTEMPTS
            and _walkthrough_execution_site_changed(qual)
        )
        if effective_streak >= _MAX_INFRA_FAILURE_REDRIVE_ATTEMPTS:
            if _rearm_after_site_change:
                logger.warning(
                    "[RELEASE_QUALIFIER] sweep: v%d capped walkthrough "
                    "infra-failure re-armed once after execution-site "
                    "capability change (record engine_sha=%s no longer serving)",
                    version.version_number,
                    str(qual.get("engine_sha") or "?")[:12],
                )
                effective_streak = 0
            else:
                # Raise an operator Alert (once) so the cap is visible.  Guard
                # against duplicate alerts with a flag on last_qualification.
                if not qual.get("infra_failure_loop_escalated"):
                    # The Alert model has no JSON ``extra`` column, so structured
                    # evidence must live in a persisted field (``message``) to survive
                    # readback — a prior ``extra={...}`` kwarg was silently dropped.
                    # Surface the failing dimension and infra-failure signature so an
                    # operator can see WHICH gate is looping and WHY.
                    _failing_dim_label = _failing_dim or "unknown"
                    _signature = qual.get("infra_failure_signature") or "unavailable"
                    _last_error = qual.get("error") or "unavailable"
                    alert = Alert(
                        project_id=version.project_id,
                        severity=AlertSeverity.ERROR,
                        title=(
                            f"Release v{version.version_number} infra-failure "
                            f"re-drive loop halted"
                        ),
                        message=(
                            f"Release v{version.version_number} has failed "
                            f"{effective_streak} consecutive infra-failure "
                            f"re-drive(s) in the '{_failing_dim_label}' dimension "
                            f"(failure signature: {_signature}; last error: "
                            f"{_last_error}) — autonomous re-qualification halted. "
                            f"Human review required."
                        ),
                        event_type="release.qualification_crash_loop",
                    )
                    redrive_session.add(alert)
                    version.last_qualification = {
                        **qual,
                        "infra_failure_loop_escalated": True,
                    }
                    flag_modified(version, "last_qualification")
                    redrive_session.add(version)
                    await redrive_session.commit()
                    logger.warning(
                        "[RELEASE_QUALIFIER] sweep: v%d consecutive_infra_errors=%d "
                        "reached cap=%d — raised operator Alert and halted "
                        "auto-requalify",
                        version.version_number,
                        effective_streak,
                        _MAX_INFRA_FAILURE_REDRIVE_ATTEMPTS,
                    )
                return False
        # Time-based backoff keyed on candidate-agnostic same-(signature,
        # engine_sha) streak.  Unlike ``consecutive_infra_errors`` (which resets
        # when the candidate SHA advances), this streak counts trailing
        # infra_attempt_history entries that share BOTH the current signature AND
        # the current engine_sha — so a persistent infra condition that survives
        # multiple automain promotions is still rate-limited.  Resets naturally
        # when the signature or engine_sha changes (the trailing run breaks).
        # A re-armed walkthrough skips the stale same-engine streak (the site
        # changed, so the old attempts' backoff must not delay the new site).
        _cur_sig = qual.get("infra_failure_signature")
        _cur_eng = qual.get("engine_sha")
        _attempt_history = [
            row
            for row in (qual.get("infra_attempt_history") or [])
            if isinstance(row, dict)
        ]
        _same_sig_streak = 0
        if not _legacy_smoke_failure and not _rearm_after_site_change:
            for _entry in reversed(_attempt_history):
                if (
                    _entry.get("signature") == _cur_sig
                    and _entry.get("engine_sha") == _cur_eng
                ):
                    _same_sig_streak += 1
                else:
                    break
        _required_wait = _infra_backoff_seconds(_same_sig_streak)
        if _required_wait > 0:
            _finalized_at_str = qual.get("finalized_at")
            if _finalized_at_str:
                try:
                    _finalized_at = datetime.fromisoformat(_finalized_at_str)
                    _elapsed = (_utcnow() - _finalized_at).total_seconds()
                    if _elapsed < _required_wait:
                        logger.debug(
                            "[RELEASE_QUALIFIER] redrive backoff: v%d "
                            "same-sig/engine streak=%d requires %ds wait, "
                            "elapsed=%.0fs — skipping this sweep",
                            version.version_number,
                            _same_sig_streak,
                            _required_wait,
                            _elapsed,
                        )
                        return False
                except (ValueError, TypeError):
                    pass  # malformed timestamp → do not gate, allow redrive
        # Same CI-compliance gate as the error re-driver: never spawn a fresh
        # qualification while the integration HEAD breaches the guardrails (per
        # the latest stored evaluation).  The sweep's block re-check re-evaluates
        # until the guardrail is clean, then this re-drive resumes normally.
        from app.strategic_spawning import ci_compliance_qualify_gate

        project = await redrive_session.get(Project, version.project_id)
        if project is not None and ci_compliance_qualify_gate(project).get("blocked"):
            logger.info(
                "[RELEASE_QUALIFIER] sweep: deferring infra_failure re-drive for "
                "v%d — integration HEAD breaches CI-compliance guardrails",
                version.version_number,
            )
            return False
        result = await release_qualifier.run_qualification(
            redrive_session,
            version.project_id,
            version,
            qualification_attempt_id=qual.get("qualification_attempt_id")
            or version.active_qualification_attempt_id,
            retry_episode=int(qual.get("retry_episode") or 0) + 1,
            retry_episode_cap=_MAX_INFRA_FAILURE_REDRIVE_ATTEMPTS,
            requested_probes=qual.get("requested_probes"),
            explicit_request=False,
        )
        # COMMIT on every outcome — do NOT roll back the non-spawned paths.
        # run_qualification writes a new last_qualification (and bumps
        # qualification_attempts) on every path it takes; rolling back would
        # discard those writes and leave the version in the old infra_failure
        # state.
        await redrive_session.commit()
        return bool(result and result.get("status") == "spawned")


async def _redrive_rubric_changed_qualification(
    version_id,
    *,
    session_factory=None,
) -> bool:
    """Auto-relaunch a qualification held because the acceptance rubric changed.

    A mid-flight rubric change (or an unreconstructable launched rubric)
    records ``last_qualification.status == "rubric_changed"`` /
    ``"rubric_provenance_unknown"`` and parks the version at phase
    ``stabilize``: the attempt measured nothing under the final rubric, so a
    FRESH grade against the CURRENT rubric is always the recovery path.  But
    nothing scheduled that relaunch — the strategist reads a standing rubric
    hold as settled, and the auto-trigger executor is knob-gated (``observe``
    in production) — so a version stranded silently until an operator manually
    re-requested.  This re-driver is the missing scheduled relaunch.

    It re-enters ``run_qualification`` (with ``auto_trigger=True``, the sweep's
    sanctioned path for attempt-scoped recovery — NOT by claiming
    ``explicit_request``) for each open, stabilize-phase version whose
    ``last_qualification.status`` is a rubric hold, subject to a bounded
    re-drive cap (``_MAX_RUBRIC_REDRIVE_ATTEMPTS``).  The relaunch is a FRESH
    attempt (no attempt id is carried forward): the superseded attempt's rubric
    contract is invalid, so a new snapshot under the current rubric is required.

    The cap counts rubric-change re-drives PERFORMED, tracked in
    ``last_qualification["rubric_redrive_count"]`` (a per-rubric-episode
    counter): ``run_qualification`` carries it forward into the pending record
    and ``_record_rubric_provenance_outcome`` carries it into the next rubric
    hold, so the cap advances across the spawn→finalize→relaunch cycle.  The
    counter resets to 0 naturally when a NON-rubric verdict lands (that record
    carries no counter).  A rubric that keeps changing cannot spin attempts
    forever: when the cap is hit the re-driver raises an operator Alert (once,
    deduped on ``rubric_redrive_loop_escalated``) and halts — the explicit
    operator-actionable state instead of silence.

    A rubric_changed attempt is NEVER scored, ledgered, or counted as a failed
    attempt — it did not measure anything (``_record_rubric_provenance_outcome``
    records no category verdicts, no ``QualificationVerdict`` row, and
    ``compute_promotion_readiness`` excludes rubric holds from its aggregation).

    Commit behavior: COMMIT on every outcome, same as the error/infra re-drivers.
    ``run_qualification`` writes a new ``last_qualification`` (and bumps
    ``qualification_attempts``) on every path it takes; rolling back would
    discard those writes.  The re-drive counter is incremented POST-commit so a
    failed spawn is NOT counted as a performed re-drive.

    Returns True only when the relaunch actually STARTED a new qualification
    (``status == "spawned"``).  Any guard no-op or cap hit returns False.
    """
    from app import release_qualifier

    factory = session_factory or AsyncSessionLocal
    async with factory() as redrive_session:
        # Row-lock the version (same as the infra re-driver) so this autonomous
        # re-drive serializes against a concurrently-committing explicit
        # qualification instead of reading a stale rubric marker mid-flight.
        version = (
            await redrive_session.execute(
                select(release_version.ReleaseVersion)
                .where(release_version.ReleaseVersion.id == version_id)
                .with_for_update()
            )
        ).scalar_one_or_none()
        if version is None:
            return False
        qual = version.last_qualification
        if (
            not isinstance(qual, dict)
            or qual.get("status") not in _RUBRIC_REDRIVE_STATUSES
            or version.phase != "stabilize"
        ):
            return False
        # Bounded relaunches: a rubric that keeps changing must not spin
        # attempts indefinitely.  Use the per-rubric-episode counter carried on
        # last_qualification — NOT the lifetime qualification_attempts column,
        # which accrues across ALL episodes and would silently block re-drives
        # for long-lived versions.
        rubric_redrive_count = int(qual.get("rubric_redrive_count") or 0)
        if rubric_redrive_count >= _MAX_RUBRIC_REDRIVE_ATTEMPTS:
            # Raise an operator Alert (once) so the cap is visible.  Guard
            # against duplicate alerts with a flag on last_qualification.
            if not qual.get("rubric_redrive_loop_escalated"):
                alert = Alert(
                    project_id=version.project_id,
                    severity=AlertSeverity.ERROR,
                    title=(
                        f"Release v{version.version_number} rubric redrive "
                        f"loop halted"
                    ),
                    message=(
                        f"Release v{version.version_number} has had "
                        f"{rubric_redrive_count} consecutive rubric-change "
                        f"re-drive(s) while the acceptance rubric kept "
                        f"changing — autonomous re-qualification halted. "
                        f"Stabilize the rubric, then request a fresh "
                        f"qualification."
                    ),
                    event_type="release.qualification_crash_loop",
                )
                redrive_session.add(alert)
                version.last_qualification = {
                    **qual,
                    "rubric_redrive_loop_escalated": True,
                }
                flag_modified(version, "last_qualification")
                redrive_session.add(version)
                await redrive_session.commit()
                logger.warning(
                    "[RELEASE_QUALIFIER] sweep: v%d rubric_redrive_count=%d "
                    "reached cap=%d — raised operator Alert and halted "
                    "auto-relaunch",
                    version.version_number,
                    rubric_redrive_count,
                    _MAX_RUBRIC_REDRIVE_ATTEMPTS,
                )
            return False
        # The CI-compliance qualify gate must not be bypassed by a re-drive.  If
        # the integration HEAD currently breaches the guardrails (per the latest
        # stored evaluation), defer until a repair lands — the sweep's
        # ``_recheck_ci_compliance_blocked_version`` keeps re-evaluating.  Uses
        # the stored record (no fresh GitHub scan under the sweep session).
        from app.strategic_spawning import ci_compliance_qualify_gate

        project = await redrive_session.get(Project, version.project_id)
        if project is not None and ci_compliance_qualify_gate(project).get("blocked"):
            logger.info(
                "[RELEASE_QUALIFIER] sweep: deferring rubric_changed re-drive "
                "for v%d — integration HEAD breaches CI-compliance guardrails",
                version.version_number,
            )
            return False
        # A fresh attempt against the CURRENT rubric.  Deliberately NO
        # qualification_attempt_id: the superseded attempt is invalid and must
        # not be continued; auto_trigger=True is the sweep's sanctioned path for
        # attempt-scoped recovery (mirrors the auto-trigger executor's fire).
        result = await release_qualifier.run_qualification(
            redrive_session,
            version.project_id,
            version,
            retry_episode=int(qual.get("retry_episode") or 0) + 1,
            retry_episode_cap=_MAX_RUBRIC_REDRIVE_ATTEMPTS,
            requested_probes=qual.get("requested_probes"),
            auto_trigger=True,
        )
        # COMMIT on every outcome — do NOT roll back the non-spawned paths.
        # run_qualification writes a new last_qualification (and bumps
        # qualification_attempts) on every path it takes; rolling back would
        # discard those writes and leave the version in the old rubric hold.
        await redrive_session.commit()
        # If the relaunch actually STARTED, persist the per-episode re-drive
        # counter so the cap advances across sweeps.  A non-spawned result is
        # NOT counted as a performed re-drive.  The pending record already
        # carries the prior counter (run_qualification stamps it from the prior
        # record), so the increment survives even if this post-commit write is
        # lost to a crash.
        if result and result.get("status") == "spawned":
            await redrive_session.refresh(version)
            new_qual = version.last_qualification
            if isinstance(new_qual, dict):
                version.last_qualification = {
                    **new_qual,
                    "rubric_redrive_count": rubric_redrive_count + 1,
                }
                flag_modified(version, "last_qualification")
                await redrive_session.commit()
        return bool(result and result.get("status") == "spawned")


async def _redrive_walkthrough_contract_qualification(
    version_id,
    *,
    session_factory=None,
) -> bool:
    """Re-enter the qualifier for a ``not_promoted`` version blocked on a
    walkthrough evidence-contract failure.

    ``_handle_walkthrough_contract_failure`` records
    ``last_qualification.status == "not_promoted"`` with
    ``blocker == "walkthrough_e2e_contract_failed"`` — a DURABLE verdict
    (unlike the transient ``infra_failure`` / ``error`` holds) whose blocker is
    EXECUTION_SITE_DEPLOYED: the report-key validation ran in the serving
    process against the serving rubric.  Until now nothing scheduled a
    re-entry: the sweep re-drives only criterion-coverage ``not_promoted``
    blockers, reconciliation covers only ``ci_unresolved``, and the auto-trigger
    executor explicitly excludes ``not_promoted`` blockers other than the
    acceptance grade — so a version whose walkthrough report violated the
    deployed contract stranded until a human or the strategist manually
    re-requested qualification.  This re-driver is the missing scheduled path.

    It re-enters ``run_qualification`` (with ``auto_trigger=True``, the sweep's
    sanctioned path for attempt-scoped recovery) for each open, stabilize-phase
    version whose ``last_qualification.status == "not_promoted"`` AND
    ``blocker == "walkthrough_e2e_contract_failed"``, subject to a bounded
    re-drive cap (``_MAX_WALKTHROUGH_CONTRACT_REDRIVE_ATTEMPTS``).

    The re-drive does NOT spawn on an unchanged candidate: a contract-failure
    record is deterministic (the deployed validator would reject the identical
    report again), so ``_qualification_probe_contract_is_reusable`` treats it
    as reusable and ``run_qualification``'s cached-verdict guard returns
    ``{"status": "cached_verdict"}`` cheaply — no new qualification spawns, no
    VMs burn, and the cap does not advance.  A fresh qualification spawns only
    when the qualification inputs actually changed — the integration HEAD
    advanced (a conforming fix landing on automain) or the goal/rubric drifted
    (``contract_fingerprint`` mismatch) — which is precisely the recovery the
    deployed-side blocker needs.

    The cap counts re-drives PERFORMED, tracked in
    ``last_qualification["walkthrough_contract_redrive_count"]`` (a
    per-contract-episode counter): ``run_qualification`` carries it forward
    into the pending record and ``_handle_walkthrough_contract_failure``
    carries it into the next contract-failure record, so the cap advances
    across the spawn→finalize→re-drive cycle.  The counter resets to 0
    naturally when a NON-contract-failure verdict lands (that record carries no
    counter).  A deployed site that keeps rejecting reports cannot spin
    attempts forever: when the cap is hit the re-driver raises an operator
    Alert (once, deduped on ``walkthrough_contract_redrive_loop_escalated``)
    and halts — the explicit operator-actionable state instead of silence.

    Commit behavior: COMMIT on every outcome, same as the error/infra/rubric
    re-drivers.  ``run_qualification`` writes a new ``last_qualification`` (and
    bumps ``qualification_attempts``) on every path it takes; rolling back
    would discard those writes.  The re-drive counter is incremented POST-commit
    so a failed spawn is NOT counted as a performed re-drive.

    Returns True only when the re-entry actually STARTED a new qualification
    (``status == "spawned"``).  Any guard no-op, cap hit, or cached-verdict
    deferral returns False.
    """
    from app import release_qualifier

    factory = session_factory or AsyncSessionLocal
    async with factory() as redrive_session:
        # Row-lock the version (same as the infra/rubric re-drivers) so this
        # autonomous re-drive serializes against a concurrently-committing
        # explicit qualification instead of reading a stale contract-failure
        # marker mid-flight.
        version = (
            await redrive_session.execute(
                select(release_version.ReleaseVersion)
                .where(release_version.ReleaseVersion.id == version_id)
                .with_for_update()
            )
        ).scalar_one_or_none()
        if version is None:
            return False
        qual = version.last_qualification
        if (
            not isinstance(qual, dict)
            or qual.get("status") != "not_promoted"
            or qual.get("blocker") != "walkthrough_e2e_contract_failed"
            or version.phase != "stabilize"
        ):
            return False
        # Bounded attempts: a deployed site that keeps rejecting reports must
        # not spin fresh qualification attempts (and their heavy VMs) forever.
        # Use the per-contract-episode counter carried on last_qualification —
        # NOT the lifetime qualification_attempts column, which accrues across
        # ALL episodes and would silently block re-drives for long-lived
        # versions.
        redrive_count = int(qual.get("walkthrough_contract_redrive_count") or 0)
        if redrive_count >= _MAX_WALKTHROUGH_CONTRACT_REDRIVE_ATTEMPTS:
            # Raise an operator Alert (once) so the cap is visible.  Guard
            # against duplicate alerts with a flag on last_qualification.
            if not qual.get("walkthrough_contract_redrive_loop_escalated"):
                _error = str(qual.get("error") or "unavailable")
                alert = Alert(
                    project_id=version.project_id,
                    severity=AlertSeverity.ERROR,
                    title=(
                        f"Release v{version.version_number} walkthrough "
                        f"contract redrive loop halted"
                    ),
                    message=(
                        f"Release v{version.version_number} has had "
                        f"{redrive_count} walkthrough evidence-contract "
                        f"re-drive(s) while the DEPLOYED qualifier kept "
                        f"rejecting the candidate's walkthrough report "
                        f"(last error: {_error}) — autonomous re-qualification "
                        f"halted. A conforming fix on automain (or a rubric/"
                        f"goal change) is required before a fresh "
                        f"qualification can pass the evidence contract."
                    ),
                    event_type="release.qualification_crash_loop",
                )
                redrive_session.add(alert)
                version.last_qualification = {
                    **qual,
                    "walkthrough_contract_redrive_loop_escalated": True,
                }
                flag_modified(version, "last_qualification")
                redrive_session.add(version)
                await redrive_session.commit()
                logger.warning(
                    "[RELEASE_QUALIFIER] sweep: v%d "
                    "walkthrough_contract_redrive_count=%d reached cap=%d — "
                    "raised operator Alert and halted auto re-drive",
                    version.version_number,
                    redrive_count,
                    _MAX_WALKTHROUGH_CONTRACT_REDRIVE_ATTEMPTS,
                )
            return False
        # The CI-compliance qualify gate must not be bypassed by a re-drive.  If
        # the integration HEAD currently breaches the guardrails (per the latest
        # stored evaluation), defer until a repair lands — the sweep's
        # ``_recheck_ci_compliance_blocked_version`` keeps re-evaluating.  Uses
        # the stored record (no fresh GitHub scan under the sweep session).
        from app.strategic_spawning import ci_compliance_qualify_gate

        project = await redrive_session.get(Project, version.project_id)
        if project is not None and ci_compliance_qualify_gate(project).get("blocked"):
            logger.info(
                "[RELEASE_QUALIFIER] sweep: deferring walkthrough contract "
                "re-drive for v%d — integration HEAD breaches CI-compliance "
                "guardrails",
                version.version_number,
            )
            return False
        # Re-enter the qualifier with auto_trigger=True (the sweep's sanctioned
        # attempt-scoped recovery path — NEVER a claim of explicit_request).
        # run_qualification's cached-verdict guard makes an unchanged-candidate
        # call a cheap no-op (no spawn, no cap advance), so this re-drive costs
        # real qualification work only when the inputs changed.
        result = await release_qualifier.run_qualification(
            redrive_session,
            version.project_id,
            version,
            retry_episode=int(qual.get("retry_episode") or 0) + 1,
            retry_episode_cap=_MAX_WALKTHROUGH_CONTRACT_REDRIVE_ATTEMPTS,
            requested_probes=qual.get("requested_probes"),
            auto_trigger=True,
        )
        # COMMIT on every outcome — do NOT roll back the non-spawned paths.
        # run_qualification writes a new last_qualification (and bumps
        # qualification_attempts) on every path it takes; rolling back would
        # discard those writes and leave the version in the old contract-failure
        # state.
        await redrive_session.commit()
        # If the re-entry actually STARTED a fresh qualification, persist the
        # per-episode re-drive counter so the cap advances across sweeps.  A
        # non-spawned result (cached_verdict / no_credentials / sha_error / ...)
        # is NOT counted as a performed re-drive.  The pending record already
        # carries the prior counter (run_qualification stamps it from the prior
        # record), so the increment survives even if this post-commit write is
        # lost to a crash.
        if result and result.get("status") == "spawned":
            await redrive_session.refresh(version)
            new_qual = version.last_qualification
            if isinstance(new_qual, dict):
                version.last_qualification = {
                    **new_qual,
                    "walkthrough_contract_redrive_count": redrive_count + 1,
                }
                flag_modified(version, "last_qualification")
                await redrive_session.commit()
        return bool(result and result.get("status") == "spawned")


# ---------------------------------------------------------------------------
# promotion_failed / deferred_unsynced re-drivers
# ---------------------------------------------------------------------------
#
# Both statuses had a resolver rule (``qualification_triggers`` T5 and T5b) and
# NO executor: ``_fire_auto_triggered_qualification`` filters the resolver's
# decision down to the three triggers the sweep executor owns and discards the
# rest as "owned by its dedicated re-driver" -- but the promotion_failed and
# deferred_unsynced re-drivers that claim named were deleted (commit a9ed8d6e,
# 2026-08-03) and never replaced.  A version whose promotion PR went red
# therefore stayed parked at ``promotion_failed`` forever: the reconcile leg
# closed the PR, deleted the release branch, recorded the failure, and spawned a
# CI-failure remediation fix -- and once that fix merged, nothing re-fired
# qualification.  These two functions are the missing executors.


# Promotion failures a re-attempt on the SAME candidate can clear.  These are
# promotion MECHANICS failures -- credentials, release-branch/PR plumbing, a
# transient merge refusal, a claim that expired waiting for CI to conclude.
# Nothing about the candidate's CODE is implicated, so re-cutting the release
# branch for the identical sha is the correct (and only) recovery.
#
# Every OTHER promotion failure -- ``required_check_failed``,
# ``migration_guard_blocked``, ``protection_blocked_review``,
# ``merge_refused_405_terminal``, and any code this list does not know -- is a
# genuine verdict ON THE CANDIDATE: re-promoting the identical sha reproduces it
# exactly, at the cost of a fresh release-branch cut and a fresh full-matrix CI
# run.  Those wait for the integration HEAD to ADVANCE, i.e. for the harvested
# remediation fix to actually land.  Unknown codes fail CLOSED into that branch
# on purpose: a new failure mode must not inherit permission to spin.
_PROMOTION_MECHANICS_FAILURES: frozenset[str] = frozenset(
    {
        "no_credentials",
        "project_not_found",
        "lock_release_failed",
        "release_branch_failed",
        "pr_creation_failed",
        "pr_missing_number",
        "pr_closed_without_merge",
        "promotion_pending_malformed",
        "promotion_pr_identity_mismatch",
        "promotion_pending_timeout",
        "promotion_claim_abandoned",
        "merge_error",
    }
)

# Genuine (non-mechanics) promotion failures whose verdict can be NON-DETERMINISTIC
# for an unchanged candidate, and which may therefore be re-attempted on the same
# sha given positive evidence that the sha itself passes the full matrix.
#
# Only ``required_check_failed`` qualifies.  A required check is red because some
# shard reported failure, and a shard reports failure for two very different
# reasons: the code is wrong, or the harness flaked (a timeout, an OOM, a runner
# eviction, a budget inversion).  The first is a property of the sha and will
# reproduce; the second is not and will not.  ``promotion_failed`` records both
# under one code, so the code alone cannot tell them apart -- which is why the
# gate below demands separate evidence rather than trusting the code.
#
# Deliberately NOT included: ``migration_guard_blocked`` and every other
# unrecognised code.  A migration guard evaluates the same tree deterministically
# and will block the same sha every time; re-promoting it burns a release-branch
# cut and a full-matrix run to reach the identical verdict.  Unrecognised codes
# stay out for the same fail-closed reason ``_promotion_failure_needs_new_candidate``
# gives: an unknown failure mode is treated as a real verdict on the candidate.
_PROMOTION_FLAKE_RETRYABLE_FAILURES: frozenset[str] = frozenset(
    {
        "required_check_failed",
    }
)

# Minimum wait between two deferred_unsynced sync re-checks.  This is NOT a
# "retry after N minutes" firing rule -- the DECISION is made by the qualify-on-
# synced gate inside ``run_qualification``, which re-reads the promotion-target
# HEAD live on every re-entry.  This only bounds how often that live re-check
# runs, because the reaper sweeps roughly once a minute and each re-entry costs
# several GitHub round-trips.  Well under the sync gate's own 30-minute
# fall-through, so the fall-through still fires on schedule.
_DEFERRED_UNSYNCED_REDRIVE_MIN_INTERVAL_SECONDS = 300

# Hard wall-clock ceiling on how long a version may sit at deferred_unsynced
# before the re-driver escalates instead of re-checking.  The sync gate's
# bounded fall-through (``qualify_unsynced_fall_through_minutes``, default 30)
# normally releases the deferral long before this; reaching it means the
# fall-through is disabled or broken, which is an operator condition, not
# something to keep polling GitHub about forever.
_MAX_DEFERRED_UNSYNCED_WAIT_SECONDS = 6 * 60 * 60

# Keys under which each re-driver stamps its decision on ``last_qualification``.
_PROMOTION_REDRIVE_DECISION_KEY = "promotion_redrive_decision"
_DEFERRED_UNSYNCED_DECISION_KEY = "deferred_unsynced_redrive_decision"


def _parse_iso_timestamp(raw) -> datetime | None:
    """Best-effort naive-UTC parse of a stored ISO timestamp (None on garbage)."""
    if not raw:
        return None
    try:
        parsed = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    return parsed.replace(tzinfo=None) if parsed.tzinfo else parsed


async def _record_redrive_decision(
    session,
    version,
    key: str,
    decision: str,
    reason: str,
    **fields,
) -> bool:
    """Persist WHY a re-driver fired, held, capped, or escalated.

    A sweep leg that silently returns ``False`` is how the promotion_failed
    dead-end stayed invisible: the resolver armed the version every cycle, the
    executor discarded the decision, and nothing anywhere recorded that a
    decision had been discarded.  Every exit path of the two re-drivers below
    stamps this record onto ``last_qualification`` so an operator reading the
    version back sees the decision AND the inputs that produced it, without
    reconstructing them from logs that have long since rotated.

    Idempotent by CONTENT: when the decision and its fields match what is
    already stored the write is skipped entirely (only the timestamp would
    differ), so a steady-state hold does not rewrite the row on every ~60s
    sweep.  Callers therefore must not pass volatile values (elapsed seconds,
    a sha that changes every cycle) as fields.

    Returns True when a write was actually made.
    """
    qual = version.last_qualification
    if not isinstance(qual, dict):
        return False
    record: dict = {"decision": decision, "reason": reason}
    record.update({k: v for k, v in fields.items() if v is not None})
    prior = qual.get(key)
    if isinstance(prior, dict) and {
        k: v for k, v in prior.items() if k != "at"
    } == record:
        return False
    version.last_qualification = {
        **qual,
        key: {**record, "at": _utcnow().isoformat()},
    }
    flag_modified(version, "last_qualification")
    session.add(version)
    await session.commit()
    return True


def _promotion_failure_needs_new_candidate(error: str) -> bool:
    """True when only NEW code on the integration branch can clear this failure.

    Pure helper.  ``error`` is ``last_qualification["error"]`` from a
    ``promotion_failed`` record.  Fails CLOSED for unrecognised codes -- an
    unknown failure mode is treated as a genuine verdict on the candidate, so
    the re-driver waits for the integration HEAD to advance rather than
    re-cutting a release branch against unchanged evidence.
    """
    return error not in _PROMOTION_MECHANICS_FAILURES


async def _redrive_promotion_failed_qualification(
    version_id,
    *,
    session_factory=None,
    pre_sweep_status: str | None = None,
) -> bool:
    """Re-enter the qualifier for a version parked at ``promotion_failed``.

    THE DEFECT THIS CLOSES.  When a promotion PR's CI goes red,
    ``_reconcile_if_managed_check_wait`` closes the PR, deletes the
    ``release-v{N}`` branch, calls ``_record_promotion_failure`` (which returns
    the version to ``phase=stabilize`` / ``status=open`` and increments
    ``last_qualification["promotion_failure_count"]``) and harvests the CI
    failures into a remediation fix task.  That fix merges, the integration
    branch advances -- and before this re-driver existed nothing re-fired
    qualification.  ``qualification_triggers`` T5 DID arm the version
    (``trigger="blockers_cleared"``), but the sweep's auto-trigger executor
    discards every trigger outside its own three, on the stated grounds that a
    dedicated re-driver owns them.  For ``promotion_failed`` no such re-driver
    existed.  The version stayed stuck until a human re-fired qualification.

    CONDITION-DRIVEN, NEVER TIMER-DRIVEN.  There is no "retry after N minutes"
    path here; a blind retry against still-red CI would recreate and re-destroy
    a release branch every cycle.  Four independent conditions must ALL hold:

    1. ``compute_requalification_readiness`` reports ``requalification_ready``
       -- every gating carried audit finding is ledger ``resolved``/``stale`` AND
       the projected score clears the live promotion threshold.  Stale-marking
       runs first against the live integration audited surface, which is how a
       landed fix flips a finding to ``stale`` in the first place.  Readiness
       also carries the in-flight ``promotion_ci_remediation`` deferral, so the
       re-drive waits while the harvested fix is still being worked.
    2. The promotion-CI gate for the integration HEAD is not ``red``.  NOT-RED,
       never REQUIRE-GREEN, and that asymmetry is deliberate.  An ordinary
       integration HEAD has no ``ci.yml`` run at all (ci.yml's ``pull_request``
       trigger is ``branches: [main]``, so integration-branch PRs get zero
       check-runs by design), and the run the gate CAN obtain for that HEAD is a
       ``workflow_dispatch`` rehearsal, which does not share a checkout shape
       with the ``pull_request`` run a promotion PR gets — different ref
       (``refs/heads/<branch>`` vs a detached ``refs/pull/N/merge``), different
       tree whenever the base has moved, different ref inventory.  The gate now
       says so out loud: a dispatch-derived success is reported as
       ``green_rehearsal`` with ``promotion_equivalent=False``, never as
       ``green``.  So gate green was already treated here as WEAK evidence and
       still is; what bounds this loop is the circuit breaker below, not the
       gate.  A ``red`` still holds, because a full matrix that broke found
       something real whichever shape produced it.
    3. The integration HEAD resolves at all (fail closed when GitHub is
       unavailable -- an unresolved HEAD cannot prove the fix landed).
    4. CANDIDATE IDENTITY.  For a genuine failure (``required_check_failed``,
       ``migration_guard_blocked``, anything not in
       ``_PROMOTION_MECHANICS_FAILURES``) the integration HEAD must have moved
       PAST the failed candidate sha.  Re-promoting the identical sha against
       unchanged evidence is the infinite-loop failure mode and it costs a full
       release-branch cut plus a full-matrix CI run every single time.  A
       promotion MECHANICS failure -- where the candidate was never the problem
       -- may re-attempt on the unchanged sha.

       ONE FURTHER EXCEPTION, added because the rule above had a hole that
       latched PERMANENTLY and SILENTLY.  When a promotion PR reds on a FLAKY
       shard there is nothing to remediate, so no fix lands, so the integration
       HEAD never advances, so ``candidate_unchanged`` holds -- every cycle,
       forever, with no Alert and a status field that reads like an ordinary
       stabilize.  The only exit was a human noticing and force-promoting by
       hand.  So for ``_PROMOTION_FLAKE_RETRYABLE_FAILURES`` (today just
       ``required_check_failed``) the unchanged sha MAY be re-attempted, but
       only on positive evidence: ``gate_state`` in ``PROMOTION_CI_GREEN_STATES``
       (``green`` or ``green_rehearsal``), i.e. a COMPLETED ``ci.yml`` run for
       exactly that HEAD sha concluded success, whichever event shape ran it.

       WHAT THAT EVIDENCE IS AND IS NOT.  It is not proof the promotion PR will
       pass.  The gate's run is a ``workflow_dispatch`` on the integration
       branch; the promotion PR's run is a ``pull_request`` event, and the two
       do not share a checkout shape -- the same sha on the same shard and the
       same runner has been observed green under one and red under the other 31
       minutes apart, which is how a git ``safe.directory`` asymmetry stayed
       hidden.  It IS evidence that the tree at this sha builds and its tests
       pass somewhere, which is strictly more than the zero evidence the
       permanent hold rested on.  The retry is bounded by the SAME circuit
       breaker as every other path here, so the honest worst case is at most two
       additional promotion attempts followed by an operator Alert -- a loud
       stop replacing a silent one.

    BOUNDED.  The cap is the EXISTING circuit breaker,
    ``_MAX_PROMOTION_FAILURE_REQUALIFY_ATTEMPTS`` against the existing
    ``promotion_failure_count`` -- not a parallel counter.  At the cap this calls
    ``_escalate_promotion_failure_loop`` (operator Alert, the
    ``promotion_loop_escalated`` latch) and halts.  ``run_qualification``
    enforces the same cap independently at its own pre-lock gate, so the
    ceiling holds even for callers that never come through here.

    NO ROW LOCK -- deliberately unlike the rubric/walkthrough re-drivers.  This
    is the ONLY caller permitted to pass ``allow_synchronous_reuse_promote=True``
    (see ``run_qualification``'s reuse gate), and that path re-attempts the
    promotion in a DEDICATED session which must itself ``SELECT .. FOR UPDATE``
    this same version row.  Holding the lock here would produce a two-connection
    wait cycle Postgres cannot see, so its deadlock detector would never fire.
    ``run_qualification`` is self-defending (it takes its own lock after the
    reuse gate), so the unlocked read is safe.

    COMMIT DISCIPLINE: commit on progress, ROLL BACK on non-progress --
    deliberately unlike the error/infra/rubric/walkthrough re-drivers, which
    commit unconditionally.  ``promotion_failure_count`` lives ONLY on
    ``last_qualification``, and a non-progress outcome (``sha_error``,
    ``no_credentials``, a fresh ``deferred_unsynced`` note) REPLACES that dict
    wholesale -- committing one would silently reset the circuit breaker to zero
    and hand the loop unlimited attempts.  Rolling back keeps the version at
    ``promotion_failed`` with its count intact, to retry next cycle.

    Returns True only when the re-entry actually progressed: a fresh
    qualification spawned, or the passing-audit reuse-promote re-attempted the
    promotion.  Every other path returns False with a recorded reason.
    """
    from app import release_qualifier
    from app.release_readiness import (
        PROMOTION_CI_GREEN_STATES,
        auto_mark_stale_audit_findings,
        compute_promotion_ci_green_gate,
        compute_requalification_readiness,
    )

    factory = session_factory or AsyncSessionLocal
    async with factory() as redrive_session:
        version = await redrive_session.get(
            release_version.ReleaseVersion, version_id
        )
        if version is None:
            return False
        qual = version.last_qualification
        if (
            not isinstance(qual, dict)
            or qual.get("status") != "promotion_failed"
            or version.status != "open"
            or version.phase != "stabilize"
        ):
            return False
        # Do not re-drive a promotion failure an EARLIER LEG of this same sweep
        # cycle recorded: those reconcile transitions spawn the CI-failure
        # harvest and document an explicit "re-arm next cycle" intent.
        if pre_sweep_status is not None and pre_sweep_status != "promotion_failed":
            logger.info(
                "[RELEASE_QUALIFIER] sweep: promotion_failed re-drive deferring "
                "v%d -- status moved %r -> 'promotion_failed' during this cycle",
                version.version_number,
                pre_sweep_status,
            )
            return False

        failure_count = int(qual.get("promotion_failure_count") or 0)
        error = str(qual.get("error") or "")
        candidate_sha = str(qual.get("candidate_sha") or "")

        # (1) Circuit breaker -- escalate once, then halt.  Never spin.
        if failure_count >= _MAX_PROMOTION_FAILURE_REQUALIFY_ATTEMPTS:
            await _escalate_promotion_failure_loop(redrive_session, version)
            await _record_redrive_decision(
                redrive_session,
                version,
                _PROMOTION_REDRIVE_DECISION_KEY,
                "capped",
                "promotion_failure_cap",
                promotion_failure_count=failure_count,
                cap=_MAX_PROMOTION_FAILURE_REQUALIFY_ATTEMPTS,
                promotion_error=error or "unknown",
            )
            return False

        # (2) Readiness -- the remediation must actually be RESOLVED, not merely
        # attempted.  Stale-marking against the live integration surface runs
        # first: that is what turns "a fix landed on the audited path" into a
        # ledger state the readiness computation can see.
        project = await redrive_session.get(Project, version.project_id)
        if project is None:
            return False
        try:
            from app.release_qualifier_sweep import (
                _current_integration_audited_surface,
            )

            surface = await _current_integration_audited_surface(
                redrive_session, project
            )
            if surface:
                await auto_mark_stale_audit_findings(
                    redrive_session, version.project_id, version.id, surface
                )
            readiness = await compute_requalification_readiness(
                redrive_session,
                version.project_id,
                candidate_surface_paths=surface,
            )
        except Exception:  # best-effort leg; retry next cycle
            logger.exception(
                "[RELEASE_QUALIFIER] sweep: promotion_failed re-drive readiness "
                "resolution failed for version %s; deferring",
                version_id,
            )
            await redrive_session.rollback()
            return False
        if not readiness.get("requalification_ready"):
            await _record_redrive_decision(
                redrive_session,
                version,
                _PROMOTION_REDRIVE_DECISION_KEY,
                "held",
                "findings_unresolved",
                detail=str(readiness.get("reason") or "")[:400],
                gating_total=readiness.get("gating_total"),
                gating_resolved=readiness.get("gating_resolved"),
            )
            return False

        # (3) Live integration HEAD + CI state.  ``dispatch=False``: this is a
        # READ of the gate, never a trigger for a full-matrix run.
        try:
            gate = await compute_promotion_ci_green_gate(
                redrive_session, version.project_id, dispatch=False
            )
        except Exception:  # noqa: BLE001 -- degrade to unavailable, fail closed
            logger.warning(
                "[RELEASE_QUALIFIER] sweep: promotion_failed re-drive could not "
                "resolve the integration HEAD for version %s; holding",
                version_id,
                exc_info=True,
            )
            gate = {}
        gate_state = str(gate.get("gate_state") or "unavailable")
        head_sha = str(gate.get("head_sha") or "")
        if gate_state == "red":
            await _record_redrive_decision(
                redrive_session,
                version,
                _PROMOTION_REDRIVE_DECISION_KEY,
                "held",
                "integration_ci_red",
                gate_state=gate_state,
            )
            return False
        if not head_sha:
            await _record_redrive_decision(
                redrive_session,
                version,
                _PROMOTION_REDRIVE_DECISION_KEY,
                "held",
                "integration_head_unresolved",
                gate_state=gate_state,
            )
            return False

        # (4) Candidate identity -- the anti-infinite-loop guard.
        if not candidate_sha:
            await _record_redrive_decision(
                redrive_session,
                version,
                _PROMOTION_REDRIVE_DECISION_KEY,
                "held",
                "candidate_identity_unknown",
            )
            return False
        needs_new_candidate = _promotion_failure_needs_new_candidate(error)
        # A same-candidate retry for a flake-retryable failure, permitted ONLY on
        # positive evidence that this exact sha passes the full matrix.  Anything
        # short of a completed-green run for this HEAD -- pending, no_signal,
        # dispatched, unavailable -- is not evidence and does not unlock it.
        #
        # BOTH green states count, and they must.  The question here is "does the
        # full matrix pass at this sha", not "will the promotion PR agree" -- the
        # docstring above is explicit that this evidence is NOT the latter.  The
        # run the gate can obtain for an integration HEAD is a
        # ``workflow_dispatch``, which the gate now reports honestly as
        # ``green_rehearsal``; matching only the literal ``"green"`` would make
        # this escape hatch unreachable in exactly the situation it was added
        # for, restoring the permanent silent latch it exists to break.
        same_candidate_flake_retry = bool(
            needs_new_candidate
            and head_sha == candidate_sha
            and error in _PROMOTION_FLAKE_RETRYABLE_FAILURES
            and gate_state in PROMOTION_CI_GREEN_STATES
        )
        if needs_new_candidate and head_sha == candidate_sha:
            if not same_candidate_flake_retry:
                await _record_redrive_decision(
                    redrive_session,
                    version,
                    _PROMOTION_REDRIVE_DECISION_KEY,
                    "held",
                    "candidate_unchanged",
                    promotion_error=error or "unknown",
                    candidate_sha=candidate_sha[:12],
                    # WHY the hold stands, so an operator reading this back does
                    # not have to re-derive which of the two conditions failed.
                    flake_retryable=error in _PROMOTION_FLAKE_RETRYABLE_FAILURES,
                    gate_state=gate_state,
                )
                return False

        logger.info(
            "[RELEASE_QUALIFIER] sweep: re-driving promotion_failed v%d "
            "(error=%s attempt=%d/%d candidate=%s head=%s gate=%s) -- %s",
            version.version_number,
            error or "unknown",
            failure_count,
            _MAX_PROMOTION_FAILURE_REQUALIFY_ATTEMPTS,
            candidate_sha[:8],
            head_sha[:8],
            gate_state,
            (
                "the candidate sha is unchanged but its own full-matrix run is "
                "green -- re-attempting a flake-retryable failure"
                if same_candidate_flake_retry
                else (
                    "remediation resolved and the integration HEAD advanced "
                    "past the failed candidate"
                    if needs_new_candidate
                    else "the failure was promotion mechanics, not the candidate"
                )
            ),
        )
        # End this session's read transaction BEFORE re-entering the qualifier:
        # the reuse-promote path opens a dedicated session that locks this row.
        await redrive_session.commit()
        result = await release_qualifier.run_qualification(
            redrive_session,
            version.project_id,
            version,
            session_factory=session_factory,
            allow_synchronous_reuse_promote=True,
            requested_probes=qual.get("requested_probes"),
            auto_trigger=True,
        )
        result = result or {}
        result_status = str(result.get("status") or "none")
        progressed = bool(result_status == "spawned" or result.get("reused_audit"))
        if progressed:
            await redrive_session.commit()
            try:
                await redrive_session.refresh(version)
                await _record_redrive_decision(
                    redrive_session,
                    version,
                    _PROMOTION_REDRIVE_DECISION_KEY,
                    "fired",
                    "blockers_cleared",
                    result_status=result_status,
                    promotion_failure_count=failure_count,
                    candidate_sha=candidate_sha[:12],
                    # Record WHICH evidence this re-drive proceeded on.  Without
                    # it, a re-drive that fired on a dispatch rehearsal and one
                    # that fired on a real pull_request run are indistinguishable
                    # afterwards — and they are not the same claim.
                    gate_state=gate_state,
                    gate_promotion_equivalent=bool(gate.get("promotion_equivalent")),
                    # Which of the three doors this went through.  A
                    # same-candidate retry is the one an operator most needs to
                    # recognise on sight: it is re-attempting evidence that
                    # already failed once, and its budget is the failure cap.
                    retry_class=(
                        "same_candidate_flake_retry"
                        if same_candidate_flake_retry
                        else (
                            "head_advanced"
                            if needs_new_candidate
                            else "promotion_mechanics"
                        )
                    ),
                )
            except Exception:  # noqa: BLE001 -- provenance, never the verdict
                logger.warning(
                    "[RELEASE_QUALIFIER] sweep: promotion_failed re-drive fired "
                    "for v%d but its provenance stamp failed",
                    version.version_number,
                    exc_info=True,
                )
            return True
        # Non-progress: discard whatever run_qualification wrote.  See the
        # COMMIT DISCIPLINE note above -- committing here would reset
        # promotion_failure_count and uncap the loop.
        await redrive_session.rollback()
        version = await redrive_session.get(
            release_version.ReleaseVersion, version_id
        )
        if version is not None:
            await _record_redrive_decision(
                redrive_session,
                version,
                _PROMOTION_REDRIVE_DECISION_KEY,
                "held",
                "run_qualification_no_progress",
                result_status=result_status,
            )
        return False


async def _redrive_deferred_unsynced_qualification(
    version_id,
    *,
    session_factory=None,
    pre_sweep_status: str | None = None,
) -> bool:
    """Re-enter the qualifier for a version parked at ``deferred_unsynced``.

    The qualify-on-synced gate inside ``run_qualification`` DEFERS a candidate
    whose integration branch has not yet absorbed the promotion target (a
    ``main -> automain`` back-merge is still pending), writing
    ``last_qualification = {"status": "deferred_unsynced", ...}``.  That record
    is retryable by design -- but, exactly like ``promotion_failed``, nothing
    retried it: ``qualification_triggers`` T5b armed the version with
    ``trigger="deferred_unsynced_redrive"`` and the sweep's auto-trigger
    executor discarded the decision as re-driver-owned while no such re-driver
    existed.  This is that re-driver.

    CONDITION-DRIVEN.  The re-drive does not itself decide whether the branches
    are synced -- it re-enters ``run_qualification``, whose sync gate re-reads
    the promotion-target HEAD LIVE and either proceeds (synced, indeterminate,
    or its own bounded fall-through fired) or re-defers.  The only thing this
    function times is how OFTEN that live re-check runs
    (``_DEFERRED_UNSYNCED_REDRIVE_MIN_INTERVAL_SECONDS``), because the sweep
    cycles roughly once a minute and each re-entry costs several GitHub
    round-trips.  The interval sits well under the sync gate's own 30-minute
    fall-through, so the fall-through still releases the deferral on schedule.

    BOUNDED.  A per-attempt counter is impossible here -- the sync gate REBUILDS
    ``last_qualification`` from scratch on every re-defer and carries forward
    only ``first_deferred_at`` -- so the bound is wall-clock against that
    preserved timestamp: past ``_MAX_DEFERRED_UNSYNCED_WAIT_SECONDS`` the
    re-driver raises an operator Alert (once) and halts instead of polling
    GitHub forever.  Reaching it means the sync gate's fall-through is disabled
    or broken, which is an operator condition.

    COMMIT DISCIPLINE: commit the two outcomes that are progress -- a spawn, and
    a re-defer -- and roll back everything else.  A re-defer must persist: it is
    what advances ``deferred_at`` (the re-check interval) while carrying
    ``first_deferred_at`` (the wait cap) forward.  Every OTHER outcome is a
    record this re-driver cannot bound and must not keep.  ``run_qualification``
    REBUILDS ``last_qualification`` from scratch on each of its early returns --
    ``sha_error`` on a transient GitHub 503, ``external_blocker`` when promotion
    is disabled, ``no_credentials`` -- and none of those rebuilds carries
    ``first_deferred_at``.  Committing one would do two irreversible things at
    once: destroy the wall-clock bound described above (the ONLY bound this
    re-driver has), and move the version out of ``deferred_unsynced``, which is
    the status this re-driver selects on -- so it would never look at the
    version again.  A GitHub blip would thereby convert a candidate that is
    merely waiting on a back-merge into one that is waiting on nobody.  There is
    no counter to protect here, as the promotion_failed re-driver has, but there
    is a clock and a status, and they need protecting for the same reason.

    Returns True only when the re-entry actually started a fresh qualification.
    """
    from app import release_qualifier

    factory = session_factory or AsyncSessionLocal
    async with factory() as redrive_session:
        version = (
            await redrive_session.execute(
                select(release_version.ReleaseVersion)
                .where(release_version.ReleaseVersion.id == version_id)
                .with_for_update()
            )
        ).scalar_one_or_none()
        if version is None:
            return False
        qual = version.last_qualification
        if (
            not isinstance(qual, dict)
            or qual.get("status") != "deferred_unsynced"
            or version.status != "open"
            or version.phase != "stabilize"
        ):
            return False
        if pre_sweep_status is not None and pre_sweep_status != "deferred_unsynced":
            return False

        now = _utcnow()
        first_deferred = _parse_iso_timestamp(qual.get("first_deferred_at"))
        # Fail toward re-checking when the record is missing/garbage: an
        # unparseable timestamp must not become a permanent hold.
        waited = (now - first_deferred).total_seconds() if first_deferred else 0.0
        target = str(qual.get("promotion_target") or "the promotion target")
        if waited >= _MAX_DEFERRED_UNSYNCED_WAIT_SECONDS:
            prior = qual.get(_DEFERRED_UNSYNCED_DECISION_KEY)
            already = isinstance(prior, dict) and prior.get("decision") == "capped"
            if not already:
                redrive_session.add(
                    Alert(
                        project_id=version.project_id,
                        severity=AlertSeverity.ERROR,
                        title=(
                            f"Release v{version.version_number} deploy-sync "
                            f"deferral never released"
                        ),
                        message=(
                            f"Release v{version.version_number} has been held at "
                            f"deferred_unsynced for {waited / 3600:.1f}h waiting "
                            f"for {target} to be back-merged into the integration "
                            f"branch. The qualify-on-synced gate's bounded "
                            f"fall-through should have released it long before "
                            f"now; autonomous sync re-checks are halted pending "
                            f"operator review of the back-merge."
                        ),
                        event_type="release.qualification_crash_loop",
                    )
                )
                logger.warning(
                    "[RELEASE_QUALIFIER] sweep: v%d deferred_unsynced for %.1fh "
                    "(cap %.1fh) -- raised operator Alert and halted sync "
                    "re-checks",
                    version.version_number,
                    waited / 3600,
                    _MAX_DEFERRED_UNSYNCED_WAIT_SECONDS / 3600,
                )
            await _record_redrive_decision(
                redrive_session,
                version,
                _DEFERRED_UNSYNCED_DECISION_KEY,
                "capped",
                "sync_wait_cap",
                cap_hours=round(_MAX_DEFERRED_UNSYNCED_WAIT_SECONDS / 3600, 1),
                promotion_target=qual.get("promotion_target"),
            )
            return False

        last_checked = _parse_iso_timestamp(qual.get("deferred_at"))
        if (
            last_checked is not None
            and (now - last_checked).total_seconds()
            < _DEFERRED_UNSYNCED_REDRIVE_MIN_INTERVAL_SECONDS
        ):
            await _record_redrive_decision(
                redrive_session,
                version,
                _DEFERRED_UNSYNCED_DECISION_KEY,
                "held",
                "sync_recheck_interval",
                min_interval_seconds=_DEFERRED_UNSYNCED_REDRIVE_MIN_INTERVAL_SECONDS,
            )
            return False

        logger.info(
            "[RELEASE_QUALIFIER] sweep: re-checking the deploy-sync gate for v%d "
            "(deferred %.0fmin, target=%s)",
            version.version_number,
            waited / 60,
            target,
        )
        result = await release_qualifier.run_qualification(
            redrive_session,
            version.project_id,
            version,
            session_factory=session_factory,
            requested_probes=qual.get("requested_probes"),
            auto_trigger=True,
        )
        result = result or {}
        result_status = str(result.get("status") or "none")
        spawned = result_status == "spawned"
        if spawned or result_status == "deferred_unsynced":
            # PROGRESS -- persist it.  A re-defer is progress: it advances
            # deferred_at and carries first_deferred_at forward, so the next
            # sweep re-checks on schedule and the wait cap keeps accruing.
            await redrive_session.commit()
            try:
                await redrive_session.refresh(version)
                await _record_redrive_decision(
                    redrive_session,
                    version,
                    _DEFERRED_UNSYNCED_DECISION_KEY,
                    "fired" if spawned else "held",
                    "sync_gate_cleared" if spawned else "still_unsynced",
                    result_status=result_status,
                )
            except Exception:  # noqa: BLE001 -- provenance, never the verdict
                logger.warning(
                    "[RELEASE_QUALIFIER] sweep: deferred_unsynced re-drive for "
                    "v%d could not stamp its provenance",
                    version.version_number,
                    exc_info=True,
                )
            return spawned

        # NON-PROGRESS -- discard whatever run_qualification wrote.  See the
        # COMMIT DISCIPLINE note above: keeping it would drop the version out of
        # deferred_unsynced and take the wall-clock bound with it.
        logger.info(
            "[RELEASE_QUALIFIER] sweep: deferred_unsynced re-drive for v%d made "
            "no progress (%s) -- discarding, the deferral stands",
            version.version_number,
            result_status,
        )
        await redrive_session.rollback()
        version = await redrive_session.get(
            release_version.ReleaseVersion, version_id
        )
        if version is not None:
            # NOT "still_unsynced".  That is a claim about the world -- the
            # back-merge has not landed -- and this branch establishes no such
            # thing; it establishes only that re-entry did not get far enough to
            # say.  Naming it still_unsynced would send an operator to inspect a
            # back-merge that may already have landed.  The sibling
            # promotion_failed re-driver records the same neutral reason for the
            # same case, and result_status carries the specifics either way.
            await _record_redrive_decision(
                redrive_session,
                version,
                _DEFERRED_UNSYNCED_DECISION_KEY,
                "held",
                "run_qualification_no_progress",
                result_status=result_status,
            )
        return False


async def run_redrive_legs(
    version_ids,
    *,
    session_factory=None,
    pre_sweep_statuses=None,
) -> None:
    """Dispatch the sweep's two re-driver-owned legs over every open version.

    ``promotion_failed`` (T5, trigger ``blockers_cleared``) and
    ``deferred_unsynced`` (T5b) are the two statuses whose resolver decisions
    the sweep's auto-trigger executor discards as re-driver-owned.  Until the
    re-drivers above existed both dead-ended: a version whose promotion PR went
    red stayed parked even after the harvested remediation fix landed on the
    integration branch, because nothing ever re-entered the qualifier for it.

    Each re-driver is condition-gated (promotion readiness plus candidate
    identity, or the live sync gate), bounded by its own cap with an operator
    Alert at the ceiling, and records a structured reason for every outcome on
    ``last_qualification``.  ``pre_sweep_status`` defers a state that an earlier
    leg of THIS cycle produced to the next cycle, so two legs never chase one
    version around a single sweep.

    Lives here rather than inline in the sweep so the dispatch sits beside the
    re-drivers it dispatches, and so ``release_qualifier_sweep.py`` stays under
    the size cap its governance record pins.  A re-driver that raises is logged
    and skipped: one version's failure must not stop the rest of the fleet's.
    """
    statuses = pre_sweep_statuses or {}
    for redrive, label in (
        (_redrive_promotion_failed_qualification, "promotion_failed"),
        (_redrive_deferred_unsynced_qualification, "deferred_unsynced"),
    ):
        for version_id in version_ids:
            try:
                redriven = await redrive(
                    version_id,
                    session_factory=session_factory,
                    pre_sweep_status=statuses.get(version_id),
                )
            except Exception:
                logger.exception(
                    "[RELEASE_QUALIFIER] sweep: %s re-drive failed for version %s",
                    label,
                    version_id,
                )
                continue
            if redriven:
                logger.info(
                    "[RELEASE_QUALIFIER] sweep re-drove %s version %s "
                    "(blocking condition cleared — qualifier re-entered)",
                    label,
                    version_id,
                )

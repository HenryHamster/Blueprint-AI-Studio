"""Scheduled re-drivers for stranded release qualifications.

Extracted from ``release_qualifier_sweep`` (the sweep module's size cap left no
headroom for routine edits).  These are the per-status re-entry helpers the
sweep (``sweep_stuck_qualifications``) invokes once per cycle to re-drive a
version stranded in a non-conclusive qualification state — ``deferred_unsynced``,
transient ``error``, ``infra_failure``, ``not_promoted``, ``promotion_failed``,
and standing ``dry_run`` — back into ``run_qualification`` when its blocking
condition has cleared.  The force-finalize / reconcile machinery and the sweep
orchestrator itself stay in ``release_qualifier_sweep``.

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

logger = logging.getLogger(__name__)

# Cooldown between full-suite CI preflight dispatches for the SAME integration
# HEAD.  The promotion_failed re-driver runs every sweep; without this bound a red
# candidate whose HEAD is unchanged would re-dispatch ci.yml on every cycle. A
# fresh run is still dispatched immediately whenever HEAD advances past the marker.
_PROMOTION_CI_PREFLIGHT_COOLDOWN_SECONDS = 30 * 60


def _should_dispatch_ci_preflight(
    marker: dict | None,
    head_sha: str | None,
    now: datetime,
) -> bool:
    """Decide whether to dispatch a fresh CI preflight run (pure; no I/O).

    Dispatch when there is no prior marker, when the integration HEAD advanced
    past the marker's recorded sha, or when the cooldown has elapsed since the
    last dispatch.  An unparseable / missing timestamp errs toward dispatching
    (the safe direction — a missing green signal keeps re-qualification deferred).
    """
    if not isinstance(marker, dict):
        return True
    if marker.get("sha") != head_sha:
        return True
    dispatched_at = marker.get("dispatched_at")
    if not dispatched_at:
        return True
    try:
        prev = datetime.fromisoformat(dispatched_at)
    except (ValueError, TypeError):
        return True
    return (now - prev).total_seconds() >= _PROMOTION_CI_PREFLIGHT_COOLDOWN_SECONDS


async def _maybe_dispatch_ci_preflight(session, version, head_sha: str | None) -> None:
    """Dispatch a fresh full-suite ci.yml run for the integration HEAD, if due.

    Bounded by the per-SHA marker + cooldown in
    ``last_qualification.promotion_ci_preflight`` so frequent sweeps cannot storm
    ``workflow_dispatch``.  Persists (but does NOT commit — the caller owns the
    commit) an updated marker whenever a dispatch is attempted.  Best-effort: a
    dispatch failure only means the next sweep re-tries.
    """
    from app.release_readiness import compute_promotion_ci_green_gate

    qual = version.last_qualification if isinstance(version.last_qualification, dict) else {}
    marker = qual.get("promotion_ci_preflight") or {}
    now = _utcnow()
    if not _should_dispatch_ci_preflight(marker, head_sha, now):
        return
    gate = await compute_promotion_ci_green_gate(
        session, version.project_id, dispatch=True
    )
    version.last_qualification = {
        **qual,
        "promotion_ci_preflight": {
            "sha": head_sha,
            "run_id": gate.get("run_id"),
            "dispatched_at": now.isoformat(),
        },
    }
    flag_modified(version, "last_qualification")
    session.add(version)


# Maximum number of sweep-driven re-entries for a ``status=error`` version.
# A single transient GitHub outage (sha_error, no_credentials) should clear on
# the next sweep; a persistent failure is an operator issue.  Cap at 5 so a
# permanently broken credential path does not retry forever.
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


_RUBRIC_REDRIVE_STATUSES: frozenset[str] = frozenset(
    {"rubric_changed", "rubric_provenance_unknown"}
)

# Transient error codes the sweep is allowed to re-drive.  Permanent errors
# (e.g. project misconfiguration) should not be blindly retried.
_TRANSIENT_ERROR_CODES: frozenset[str] = frozenset({"sha_error", "no_credentials"})


_MAX_PROMOTION_FAILURE_REQUALIFY_ATTEMPTS = 3
"""Circuit-breaker cap on autonomous re-qualify attempts after a promotion_failed.

When a version's ``last_qualification.promotion_failure_count`` reaches this cap
the automatic re-driver stops and raises an operator-visible Alert instead of
looping forever.  Each _record_promotion_failure call increments the counter,
so this caps the TOTAL number of autonomous promotion attempts (fix→qualify→
promote→CI-red) before human review is required.
"""


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


async def _redrive_error_qualification(
    version_id,
    *,
    session_factory=None,
) -> bool:
    """Re-enter the qualifier for a ``status=error`` version caused by a transient failure.

    A single transient GitHub outage (``sha_error``, ``no_credentials``) records
    ``last_qualification.status == "error"`` and parks the version indefinitely
    because nothing schedules a retry — neither the strategist (which reads
    a standing ``error`` state as settled) nor any other sweep loop.

    This re-driver fires for open, stabilize-phase versions whose
    ``last_qualification.status == "error"`` AND whose ``error`` code is in
    ``_TRANSIENT_ERROR_CODES`` AND whose ``last_qualification["error_redrive_count"]``
    is below ``_MAX_ERROR_REDRIVE_ATTEMPTS``.  The attempt cap prevents a
    permanently broken credential path from retrying forever.

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
            return False
        result = await release_qualifier.run_qualification(
            redrive_session,
            version.project_id,
            version,
            qualification_attempt_id=qual.get("qualification_attempt_id")
            or version.active_qualification_attempt_id,
            retry_episode=int(qual.get("retry_episode") or 0) + 1,
            retry_episode_cap=_MAX_ERROR_REDRIVE_ATTEMPTS,
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
        version = await redrive_session.get(release_version.ReleaseVersion, version_id)
        if version is None:
            return False
        qual = version.last_qualification
        if (
            not isinstance(qual, dict)
            or qual.get("status") != "infra_failure"
            or version.phase != "stabilize"
        ):
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
        if effective_streak >= _MAX_INFRA_FAILURE_REDRIVE_ATTEMPTS:
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
        _cur_sig = qual.get("infra_failure_signature")
        _cur_eng = qual.get("engine_sha")
        _attempt_history = [
            row
            for row in (qual.get("infra_attempt_history") or [])
            if isinstance(row, dict)
        ]
        _same_sig_streak = 0
        if not _legacy_smoke_failure:
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
        result = await release_qualifier.run_qualification(
            redrive_session,
            version.project_id,
            version,
            qualification_attempt_id=qual.get("qualification_attempt_id")
            or version.active_qualification_attempt_id,
            retry_episode=int(qual.get("retry_episode") or 0) + 1,
            retry_episode_cap=_MAX_INFRA_FAILURE_REDRIVE_ATTEMPTS,
        )
        # COMMIT on every outcome — do NOT roll back the non-spawned paths.
        # run_qualification writes a new last_qualification (and bumps
        # qualification_attempts) on every path it takes; rolling back would
        # discard those writes and leave the version in the old infra_failure
        # state.
        await redrive_session.commit()
        return bool(result and result.get("status") == "spawned")


async def _redrive_rubric_qualification(
    version_id,
    *,
    session_factory=None,
) -> bool:
    """Launch a fresh grade after a frozen-rubric attempt could not conclude.

    ``rubric_changed`` and ``rubric_provenance_unknown`` deliberately do not
    prove the candidate.  Re-entering ``run_qualification`` snapshots today's
    active contract and starts a new blind merge-grade; the coordinator's row
    lock makes repeated sweeps harmless once the replacement is pending.
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
            or qual.get("status") not in _RUBRIC_REDRIVE_STATUSES
            or version.phase != "stabilize"
        ):
            return False
        result = await release_qualifier.run_qualification(
            redrive_session, version.project_id, version
        )
        await redrive_session.commit()
        return bool(result and result.get("status") == "spawned")


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
         SHA), ``run_qualification`` normally returns ``cached_verdict``.  The
         bounded exception is a stale keyless finding: it has no code anchor, so
         the sweep marks the old verdict non-conclusive once and lets a fresh
         qualification re-confirm or refute it on the current HEAD.  If
         ``automain`` HAS advanced, the cached-verdict guard is a miss (new SHA)
         and a fresh qualification spawns.

    Returns True only when the re-entry actually STARTED a new qualification
    (``status == "spawned"``).  Any guard no-op or readiness miss returns False.

    ``run_qualification`` is fully self-defending (phase check, in-flight check,
    cached-verdict check, all under a ``FOR UPDATE`` lock), so a version that
    changed state between the scan and the re-drive is a safe no-op.
    """
    # Both ``run_qualification`` and ``compute_requalification_readiness`` live
    # in modules that import this one (directly or transitively), so import
    # them lazily to avoid load-time import cycles.
    from app import github, release_qualifier
    from app.release_readiness import (
        _KEYLESS_FINDING_SENTINEL,
        compute_requalification_readiness,
    )

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
        keyless_stale = any(
            f.get("key") == _KEYLESS_FINDING_SENTINEL and f.get("state") == "stale"
            for f in readiness.get("findings", [])
            if isinstance(f, dict)
        )
        if keyless_stale and qual.get("keyless_requalification_consumed_at"):
            project = await redrive_session.get(Project, version.project_id)
            current_head_sha = None
            if project is not None:
                resolved = await release_qualifier._resolve_github_with_session(
                    redrive_session, project
                )
                if resolved is not None:
                    token, owner, repo = resolved
                    integration_branch, _promotion_target = (
                        release_qualifier._lts_branch_names(project)
                    )
                    try:
                        current_head_sha = await github.get_branch_sha(
                            token, owner, repo, integration_branch
                        )
                    except Exception:
                        logger.warning(
                            "[RELEASE_QUALIFIER] sweep: failed to resolve %s HEAD "
                            "for version %s while checking consumed keyless marker",
                            integration_branch,
                            version_id,
                        )
            if current_head_sha is None or current_head_sha == qual.get(
                "candidate_sha"
            ):
                logger.info(
                    "[RELEASE_QUALIFIER] sweep: version v%d keyless stale blocker "
                    "already consumed for this candidate — skipping re-drive",
                    version.version_number,
                )
                return False
        if keyless_stale:
            version.last_qualification = {
                **qual,
                "status": "requalification_requested",
                "previous_status": "not_promoted",
                "keyless_requalification_consumed_at": _utcnow().isoformat(),
            }
            flag_modified(version, "last_qualification")
            redrive_session.add(version)
        # The real backstop against redundant re-runs is run_qualification's cached-
        # verdict guard: under its FOR UPDATE lock, it re-resolves the candidate SHA.
        # Stale keyless findings are the bounded same-SHA exception because they
        # have no code anchor; rewriting the old status to non-conclusive lets
        # exactly one fresh pending qualification replace it.
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

async def _redrive_promotion_failed_qualification(
    version_id,
    *,
    session_factory=None,
) -> bool:
    """Re-enter the qualifier for a ``promotion_failed`` version after automain advances.

    When a promotion PR's CI goes red the version is recorded as
    ``promotion_failed`` and returned to the stabilize phase.  Neither the
    strategist nor the admin endpoint drives a re-entry automatically: the
    strategist reads the standing ``promotion_failed`` status as "awaiting fix"
    and the next ``qualify_request`` only fires once a strategic cycle emits one.

    This is the missing scheduled re-driver.  For each sweep it:

      1. Checks ``compute_requalification_readiness``.  That function already
         defers while a ``promotion_ci_remediation``-tagged fix task is in
         flight (``release_readiness._has_active_promotion_ci_remediation``),
         so the re-driver naturally waits for any harvested fix to land on the
         integration branch before triggering a fresh promotion.

      2. Circuit-breaker: when ``promotion_failure_count`` in
         ``last_qualification`` exceeds ``_MAX_PROMOTION_FAILURE_REQUALIFY_ATTEMPTS``
         the loop stops and an operator Alert is raised — preventing a
         persistently-red candidate from cycling forever.

      3. Calls ``run_qualification``.  Its cached-verdict guard re-resolves the
         candidate SHA under a FOR UPDATE lock.  If automain has NOT advanced
         the same-SHA cached verdict fires and this returns False (no-op).  If
         automain HAS advanced, a fresh qualification spawns.

    Returns True only when the re-entry actually STARTED a new qualification
    (``status == "spawned"``).  Any guard no-op, circuit-breaker, or readiness
    miss returns False.
    """
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
            or qual.get("status") != "promotion_failed"
            or version.phase != "stabilize"
        ):
            return False

        # Circuit breaker: stop re-qualifying if the failure count is at the cap.
        failure_count = int(qual.get("promotion_failure_count") or 0)
        if failure_count >= _MAX_PROMOTION_FAILURE_REQUALIFY_ATTEMPTS:
            # Raise an operator Alert (once) so the cap is visible.  Guard
            # against duplicate alerts with a flag on last_qualification.
            if not qual.get("promotion_loop_escalated"):
                # The Alert model has no JSON ``extra`` column (a prior
                # ``extra={...}`` kwarg was silently dropped) — carry the
                # structured evidence in the persisted ``message`` field so it
                # survives readback.
                _last_error = qual.get("error") or "unavailable"
                alert = Alert(
                    project_id=version.project_id,
                    severity=AlertSeverity.ERROR,
                    title=(
                        f"Release v{version.version_number} promotion loop halted"
                    ),
                    message=(
                        f"Release v{version.version_number} has failed promotion "
                        f"{failure_count} time(s) (last error: {_last_error}) — "
                        f"autonomous re-qualification halted. Human review "
                        f"required before next promote attempt."
                    ),
                    event_type="release.promotion_deploy_failed",
                )
                redrive_session.add(alert)
                version.last_qualification = {
                    **qual,
                    "promotion_loop_escalated": True,
                }
                flag_modified(version, "last_qualification")
                redrive_session.add(version)
                await redrive_session.commit()
                logger.warning(
                    "[RELEASE_QUALIFIER] sweep: v%d promotion_failure_count=%d "
                    "reached cap=%d — raised operator Alert and halted auto-requalify",
                    version.version_number,
                    failure_count,
                    _MAX_PROMOTION_FAILURE_REQUALIFY_ATTEMPTS,
                )
            return False

        # Readiness guard: defers while a promotion-CI remediation fix is in flight.
        readiness = await compute_requalification_readiness(
            redrive_session, version.project_id
        )
        if not readiness.get("requalification_ready"):
            return False

        # Full-suite green gate: even once the remediation window clears, only
        # re-enter qualification when the integration-branch HEAD has a COMPLETED,
        # green ci.yml run (the full 8-shard backend + frontend + agent matrix).
        # Deferring on any non-green verdict is the SAFE direction — it burns no
        # audit and consumes no circuit-breaker attempt, and it cannot wedge
        # permanently because HEAD advancing or the dispatched run finishing green
        # clears it on a later sweep.
        from app.release_readiness import compute_promotion_ci_green_gate

        gate = await compute_promotion_ci_green_gate(
            redrive_session, version.project_id, dispatch=False
        )
        gate_state = gate.get("gate_state")
        if gate_state != "green":
            logger.info(
                "[RELEASE_QUALIFIER] sweep: v%d promotion_failed re-drive deferred "
                "— CI green gate is %s (%s)",
                version.version_number,
                gate_state,
                gate.get("reason"),
            )
            if gate_state == "no_signal":
                # No ci.yml run exists for the current integration HEAD — kick off a
                # fresh full-matrix run (bounded by a per-SHA marker + cooldown), then
                # defer.  A later sweep re-enters once it concludes green.
                await _maybe_dispatch_ci_preflight(
                    redrive_session, version, gate.get("head_sha")
                )
                await redrive_session.commit()
            elif gate_state == "red":
                # A completed ci.yml run concluded failure on the integration HEAD.
                # Raise a dedup-keyed Alert and spawn harvest remediation so the
                # failure gets a fix task WITHOUT consuming a requalify attempt.
                # A repeated sweep at the same red HEAD is a no-op (dedup guard).
                head_sha = gate.get("head_sha") or ""
                dedupe_key = f"preflight_ci:red:{version.project_id}:{head_sha}"
                existing = (
                    (
                        await redrive_session.execute(
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
                if existing is not None:
                    await redrive_session.rollback()
                else:
                    project = await redrive_session.get(Project, version.project_id)
                    run_url = gate.get("html_url") or str(gate.get("run_id") or "")
                    redrive_session.add(
                        Alert(
                            project_id=version.project_id,
                            severity=AlertSeverity.ERROR,
                            title=(
                                f"Preflight CI red on integration branch "
                                f"({head_sha[:8] if head_sha else '?'})"
                            ),
                            message=(
                                f"Promotion-failed preflight: ci.yml concluded failure "
                                f"for integration HEAD "
                                f"{head_sha[:12] if head_sha else '?'}."
                                + (f" Run: {run_url}." if run_url else "")
                                + " Harvest remediation spawning."
                            ),
                            dedupe_key=dedupe_key,
                            event_type="release.ci_unresolved",
                        )
                    )
                    # Snapshot plain scalars before commit to avoid MissingGreenlet
                    # errors when ORM attributes are accessed post-flush.
                    _proj_id = version.project_id
                    _ver_id = version.id
                    _ver_num = version.version_number
                    _harvest_kw: dict | None = None
                    if project is not None:
                        from app.release_reconciliation import (
                            _harvest_promotion_ci_failures,
                            promotion_ci_selfheal_enabled,
                        )
                        _intbranch, _ = release_qualifier._lts_branch_names(project)
                        from app.project_telemetry import (
                            read_telemetry_or_doc,
                        )

                        _redrive_ar_config = (
                            await read_telemetry_or_doc(
                                redrive_session,
                                _proj_id,
                                project.design_doc,
                                "_auto_remediation",
                                {},
                            )
                            or {}
                        )
                        _harvest_kw = dict(
                            project_id=_proj_id,
                            version_id=_ver_id,
                            version_number=_ver_num,
                            owner=project.github_owner or "",
                            repo=project.github_repo or "",
                            ci_source_sha=head_sha,
                            integration_branch=_intbranch,
                            auto_remediation_config=_redrive_ar_config,
                            selfheal_enabled=promotion_ci_selfheal_enabled(project),
                        )
                    await redrive_session.commit()
                    logger.warning(
                        "[RELEASE_QUALIFIER] sweep: v%d preflight CI red HEAD %s "
                        "— alert raised, harvest spawning",
                        _ver_num,
                        head_sha[:8] if head_sha else "?",
                    )
                    if _harvest_kw is not None:
                        await _harvest_promotion_ci_failures(**_harvest_kw)
            else:
                # pending / dispatched / unavailable — nothing to persist.
                await redrive_session.rollback()
            return False

        # Thread session_factory so run_qualification's passing-audit reuse
        # (promotion_failed + unchanged audited surface → re-promote WITHOUT
        # re-auditing) runs its DEDICATED promote session on the same factory.
        # In production this is None → AsyncSessionLocal (a genuine second
        # connection); tests inject a shared factory.
        #
        # allow_synchronous_reuse_promote=True: this sweep re-driver is the ONLY
        # safe caller for the synchronous reuse-promote — it runs OUTSIDE any
        # lock-holding request (redrive_session has no uncommitted version-row
        # write on this path), so promote()'s nested staged-direction activation
        # cannot hang on a caller-held Direction lock (review finding 1).  Every
        # other caller (strategic / admin) defers to this path.
        result = await release_qualifier.run_qualification(
            redrive_session,
            version.project_id,
            version,
            session_factory=session_factory,
            allow_synchronous_reuse_promote=True,
        )
        # "spawned" = a fresh audit ran; "reused_audit" = the passing-audit
        # reuse-promote fired (it committed on its OWN dedicated session).  Both
        # are forward progress; every other result is a no-op → roll back the
        # read-only redrive_session.
        if result and (
            result.get("status") == "spawned" or result.get("reused_audit")
        ):
            await redrive_session.commit()
            return True
        await redrive_session.rollback()
        return False


async def _redrive_dry_run_candidate(
    version_id,
    *,
    session_factory=None,
) -> bool:
    """Re-enter the qualifier for a ``dry_run`` candidate once promotion is enabled.

    When a stabilize-phase version qualifies while ``promotion_enabled=False``, the
    verdict is recorded as ``dry_run`` with ``would_promote=True``.  That verdict is
    conclusive: ``run_qualification`` caches it by candidate SHA and will not re-run.
    Stall alerts intentionally treat ``dry_run`` as healthy (the operator disabled
    promotion deliberately), and nothing re-drives the version when the operator later
    enables promotion.

    This is the missing trigger.  For each sweep it checks an open, stabilize-phase
    version whose ``last_qualification.status == "dry_run"`` and
    ``last_qualification.would_promote == True``:

      1. Calls ``resolve_project_promotion_enabled``.  If promotion is STILL
         disabled, returns False immediately — the condition has not changed.
      2. Rewrites ``last_qualification`` to a non-conclusive placeholder
         (``status="requalification_requested", previous_status="dry_run"``) so the
         cached-verdict guard in ``run_qualification`` is cleared for this one
         re-entry, then re-invokes ``run_qualification`` under its own ``FOR UPDATE``
         lock with ``allow_synchronous_reuse_promote=True``.
      3. Commits on ``spawned`` or ``reused_audit``; rolls back otherwise so
         ``last_qualification`` is not left in a corrupt placeholder state.

    Returns True only when the re-entry actually started a new qualification
    or a passing-audit reuse-promote fired.  Any guard no-op returns False.
    """
    from app import release_qualifier
    from app.shared_state import resolve_project_promotion_enabled

    factory = session_factory or AsyncSessionLocal
    async with factory() as redrive_session:
        version = await redrive_session.get(release_version.ReleaseVersion, version_id)
        if version is None:
            return False
        qual = version.last_qualification
        if (
            not isinstance(qual, dict)
            or qual.get("status") != "dry_run"
            or not qual.get("would_promote")
            or version.phase != "stabilize"
        ):
            return False
        # Guard: only fire when the operator has since enabled promotion.
        if not resolve_project_promotion_enabled(version.project_id):
            return False
        # Clear the cached-verdict guard by writing a non-conclusive placeholder.
        # run_qualification's FOR UPDATE lock serializes the actual re-entry; a
        # concurrent caller that wins the lock will either spawn or return a
        # self-defending guard status, both of which are safe.
        version.last_qualification = {
            **qual,
            "status": "requalification_requested",
            "previous_status": "dry_run",
        }
        flag_modified(version, "last_qualification")
        redrive_session.add(version)
        result = await release_qualifier.run_qualification(
            redrive_session,
            version.project_id,
            version,
            session_factory=session_factory,
            allow_synchronous_reuse_promote=True,
        )
        if result and (
            result.get("status") == "spawned" or result.get("reused_audit")
        ):
            await redrive_session.commit()
            return True
        await redrive_session.rollback()
        return False

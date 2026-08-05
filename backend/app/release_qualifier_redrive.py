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
from app.models import Alert, AlertSeverity
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
    from app.deployed_sha import get_serving_sha  # noqa: PLC0415

    current = get_serving_sha()
    return bool(current) and last_engine_sha != current


_RUBRIC_REDRIVE_STATUSES: frozenset[str] = frozenset(
    {"rubric_changed", "rubric_provenance_unknown"}
)

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

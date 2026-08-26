"""Standing full-suite CI drift sentinel for the release qualifier sweep.

Extracted from ``release_qualifier_sweep`` -- a chronic size offender whose cap
is decomposed, never bumped (see
``test_release_qualifier_sweep_decomposition_resolves_size_breach``).

This is the sweep leg that watches ``ci.yml`` on the INTEGRATION branch, not a
qualification leg: it dispatches a full-suite run when no authoritative one
exists for the current HEAD, re-dispatches once per cooldown after the ci.yml
concurrency policy cancels one, raises and retires the standing drift
incidents, and hands a red conclusion to harvest remediation.  It shares no
state with the finalize / re-drive / reconcile legs -- only the sweep's
per-version loop calls it -- which is what makes it a clean seam.

The public surface is UNCHANGED: ``release_qualifier_sweep`` re-exports every
name defined here, so
``app.release_qualifier_sweep._check_full_suite_drift_sentinel`` (and the
``_DRIFT_SENTINEL_*`` telemetry keys and tunables) remain importable and
test-patchable at their original paths.
"""

from __future__ import annotations

import logging
from datetime import datetime

from app import release_drift_sentinel_alerts as drift_alerts
from app import release_version
from app.database import AsyncSessionLocal
from app.models import AlertSeverity, Project
from app.time_utils import _utcnow

logger = logging.getLogger(__name__)

# One sweep leg -- the full-suite drift sentinel -- and nothing else.  The cap is
# policy headroom over the extracted size; growth past it means a second concern
# moved in and belongs in its own module rather than recreating the sweep's
# chronic-growth problem one file over.
_MODULE_SIZE_GOVERNANCE_CAP: int = 950


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
_DRIFT_SENTINEL_TERMINAL_DISPATCH_KEY = "_drift_sentinel_terminal_dispatch"


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


async def _retire_sentinel_incidents(
    session,
    project_id,
    *,
    states: tuple[str, ...],
    reason: str,
) -> int:
    """Retire sentinel incidents whose condition the current gate falsified.

    ``release.ci_unresolved`` declares ``AlertRetirementMode.SOURCE_CONDITION``:
    this producer owns retirement.  Best-effort and non-raising — a retirement
    failure must never abort the sweep or, worse, suppress a live alert.
    """
    try:
        retired = await drift_alerts.resolve_drift_sentinel_alerts(
            session, project_id, states=states, reason=reason
        )
        if retired:
            await session.commit()
        return retired
    except Exception:
        logger.exception(
            "[DRIFT_SENTINEL] failed to retire incidents for project %s", project_id
        )
        try:
            await session.rollback()
        except Exception:
            logger.exception(
                "[DRIFT_SENTINEL] rollback failed for project %s", project_id
            )
        return 0


async def _check_full_suite_drift_sentinel(
    version_id,
    *,
    session_factory=None,
) -> bool:
    """Evaluate full-suite CI on automain and dispatch only when no run exists.

    Terminal dispatch receipts suppress the exact authority tuple after the
    read-only lookup. Red runs alert and enter harvest remediation. Canonical
    unless explicitly disabled; best-effort and non-raising.
    """
    from app import release_qualifier
    from app.project_telemetry import read_telemetry, upsert_telemetry
    from app.promotion_ci_dispatch import (
        terminal_promotion_ci_dispatch_receipt,
        terminal_promotion_ci_dispatch_receipt_active,
    )
    from app.release_readiness import PROMOTION_CI_GREEN_STATES, compute_promotion_ci_green_gate
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

            # Read-only first, even with a prior terminal dispatch receipt.
            gate = await compute_promotion_ci_green_gate(
                session,
                version.project_id,
                dispatch=False,
                # A cancelled run must surface as its own state, not collapse into
                # an immediate re-dispatch — otherwise the ci.yml concurrency
                # policy's cancel-in-progress on the integration branch drives a
                # fresh full 8-shard dispatch on every sweep with no signal.
                dispatch_on_cancelled=False,
            )
            gate_state = gate.get("gate_state")
            head_sha = gate.get("head_sha") or ""

            async def _persist_terminal_dispatch_receipt(
                dispatched_gate: dict,
            ) -> bool:
                """Persist every terminal dispatch outcome through one seam."""

                receipt = terminal_promotion_ci_dispatch_receipt(dispatched_gate)
                if receipt is None:
                    return False
                await upsert_telemetry(
                    session,
                    version.project_id,
                    _DRIFT_SENTINEL_TERMINAL_DISPATCH_KEY,
                    receipt,
                )
                await session.commit()
                return True

            integration_branch, _ = release_qualifier._lts_branch_names(project)
            expected_suppression_key = (
                f"{project.github_owner or ''}/{project.github_repo or ''}:"
                f"ci.yml:{integration_branch}:{head_sha}"
            )
            if gate_state in {"no_signal", "cancelled"}:
                prior_dispatch = (
                    await read_telemetry(
                        session,
                        version.project_id,
                        _DRIFT_SENTINEL_TERMINAL_DISPATCH_KEY,
                    )
                    or {}
                )
                if (
                    terminal_promotion_ci_dispatch_receipt_active(prior_dispatch)
                    and prior_dispatch.get("kind") == "promotion_ci_dispatch"
                    and prior_dispatch.get("retryable") is False
                    and prior_dispatch.get("workflow_file") == "ci.yml"
                    and prior_dispatch.get("ref") == integration_branch
                    and prior_dispatch.get("head_sha") == head_sha
                    and prior_dispatch.get("suppression_key")
                    == expected_suppression_key
                ):
                    gate = {
                        **gate,
                        "gate_state": "dispatch_rejected",
                        "dispatch_outcome": prior_dispatch.get("outcome"),
                        "dispatch_retryable": False,
                        "dispatch_suppression_key": expected_suppression_key,
                        "reason": (
                            "ci.yml dispatch was already terminally rejected for "
                            "this repository/ref/HEAD authority tuple"
                        ),
                    }
                    gate_state = "dispatch_rejected"

            if gate_state == "no_signal":
                if gate.get("gate_state") == "no_signal":
                    # No authoritative run and no exact same-head terminal
                    # receipt: the current candidate may attempt one dispatch.
                    gate = await compute_promotion_ci_green_gate(
                        session,
                        version.project_id,
                        dispatch=True,
                        dispatch_on_cancelled=False,
                    )
                    await _persist_terminal_dispatch_receipt(gate)
                gate_state = gate.get("gate_state")
                head_sha = gate.get("head_sha") or head_sha

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
                    await _persist_terminal_dispatch_receipt(gate)
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
                    if gate_state in PROMOTION_CI_GREEN_STATES or gate_state == "pending":
                        # A full matrix passed for the HEAD, or one is in flight —
                        # nothing left to do; the sentinel reads it next sweep.
                        if gate_state in PROMOTION_CI_GREEN_STATES:
                            await _retire_sentinel_incidents(
                                session,
                                version.project_id,
                                states=drift_alerts.ALL_STATES,
                                reason=(
                                    "ci.yml concluded success for integration "
                                    f"HEAD {head_sha[:12] if head_sha else '?'}"
                                ),
                            )
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
                    run_url = gate.get("html_url") or str(gate.get("run_id") or "")
                    await drift_alerts.raise_drift_sentinel_alert(
                        session,
                        project_id=version.project_id,
                        state=drift_alerts.STATE_CANCELLED,
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
                    )
                    await session.commit()
                    logger.warning(
                        "[DRIFT_SENTINEL] full-suite CI run cancelled for "
                        "project %s HEAD %s — standing incident raised, "
                        "re-dispatch cooldown in effect",
                        version.project_id,
                        head_sha[:8] if head_sha else "?",
                    )
                    return True

            if gate_state in ("unavailable", "dispatch_rejected"):
                # The gate could not resolve creds / HEAD / API.  This is
                # self-healing on the next sweep, but must not remain invisible
                # while full-suite CI drift accumulates.  Keep one active alert
                # per project so every new automain merge does not create a new
                # operator incident for the same unresolved integration signal.
                reason = str(gate.get("reason") or "unknown reason")
                # Per-PROJECT keys for BOTH states, never per-HEAD. The
                # per-HEAD dispatch_suppression_key bounds API churn (stop
                # re-POSTing a rejected authority tuple); it is not a
                # notification key. A 403/404 on ci.yml dispatch is a standing
                # repository-permission condition, identical on every HEAD, so
                # keying per-HEAD minted one incident per automain merge for a
                # single operator action. dispatch_outcome stays in the key:
                # 403 and 404 are different diagnoses with different fixes.
                outcome_label = str(gate.get("dispatch_outcome") or "unknown")
                head_label = head_sha[:8] if head_sha else "?"
                head_detail = head_sha[:12] if head_sha else "?"
                await drift_alerts.raise_drift_sentinel_alert(
                    session,
                    project_id=version.project_id,
                    state=(
                        drift_alerts.STATE_DISPATCH_REJECTED
                        if gate_state == "dispatch_rejected"
                        else drift_alerts.STATE_UNAVAILABLE
                    ),
                    # 403 and 404 are different diagnoses with different fixes,
                    # so each keeps its own standing incident.
                    diagnosis=(
                        outcome_label if gate_state == "dispatch_rejected" else None
                    ),
                    severity=AlertSeverity.WARNING,
                    title=(
                        f"Full-suite CI unavailable on integration branch "
                        f"({head_label})"
                    ),
                    # Refresh keeps the standing incident naming the CURRENT
                    # HEAD; occurrence_count carries how long the condition has
                    # persisted — the signal the per-HEAD alert stream destroyed.
                    message=(
                        f"Standing drift sentinel could not evaluate ci.yml "
                        f"for automain HEAD {head_detail}: {reason}. "
                        "Full-suite CI drift is currently unverified; "
                        "inspect GitHub connectivity and retry."
                    ),
                )
                await session.commit()
                logger.warning(
                    "[DRIFT_SENTINEL] full-suite CI gate unavailable for project %s "
                    "HEAD %s — standing incident raised: %s",
                    version.project_id,
                    head_label,
                    reason,
                )
                return True

            if gate_state != "red":
                if gate_state in PROMOTION_CI_GREEN_STATES:
                    # Branch HEALTH, so ``green_rehearsal`` retires here too and
                    # must: every incident this sentinel raises comes from a
                    # dispatch run itself, making red-cleared-by-green symmetric.
                    # Refusing would strand the red incident forever.  Cutting a
                    # promotion PR is the OTHER question — that one needs
                    # promotion_ci_gate_proves_promotion, not this flag.
                    await _retire_sentinel_incidents(
                        session,
                        version.project_id,
                        states=drift_alerts.ALL_STATES,
                        reason=(
                            "ci.yml concluded success for integration HEAD "
                            f"{head_sha[:12] if head_sha else '?'}"
                        ),
                    )
                return False

            # Red: raise the standing red incident + spawn harvest remediation.
            run_url = gate.get("html_url") or str(gate.get("run_id") or "")
            await drift_alerts.raise_drift_sentinel_alert(
                session,
                project_id=version.project_id,
                state=drift_alerts.STATE_RED,
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
            )
            # An authoritative failure conclusion proves the gate CAN now be
            # evaluated, so the "could not evaluate" incidents are over.  The red
            # incident itself stays open — only green retires that.
            await _retire_sentinel_incidents(
                session,
                version.project_id,
                states=drift_alerts.EVALUABILITY_STATES,
                reason=(
                    "ci.yml returned an authoritative conclusion for integration "
                    f"HEAD {head_sha[:12] if head_sha else '?'}; full-suite CI "
                    "drift is no longer unverified"
                ),
            )
            await session.commit()
            logger.warning(
                "[DRIFT_SENTINEL] full-suite CI red for project %s HEAD %s — "
                "standing incident raised, harvest spawning",
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

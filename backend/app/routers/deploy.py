"""Deployment state and health management endpoints."""

import asyncio
import logging
import os
from collections import deque

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_session
from app.models import DeploymentState, DeploymentStatus
from app.auth import get_current_user, require_admin
from app.admin import build_deploy_state_payload
from app.task_locking import deployment_state_lock
from app.time_utils import _utcnow

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/api/admin", tags=["deploy"], dependencies=[Depends(get_current_user)]
)


async def _raise_for_unavailable_state(
    session: AsyncSession, *, missing_404: bool = True
) -> None:
    """Resolve a ``deployment_state_lock`` that yielded ``None``.

    ``deployment_state_lock(skip_locked=True)`` yields ``None`` for two distinct
    reasons that callers must disambiguate: the singleton row is held by a
    concurrent writer (the automated monitor or another operator click), or no
    row exists at all. Probe with a non-locking read:

    * row held → raise ``429`` with ``Retry-After`` (mirrors
      ``lock_project_or_429``); a 5xx would let Cloud Run quarantine the
      instance, so the retry-friendly 4xx is deliberate.
    * row missing → raise ``404`` when ``missing_404`` (resume / reset-health /
      mark-stable / rollback all require an existing row), or return so the
      caller can preserve its own missing-row behavior (pause still zeroes
      in-memory capacity even with no persisted row).
    """
    exists = (
        await session.execute(
            select(DeploymentState.id).where(DeploymentState.id == 1)
        )
    ).first()
    if exists is None:
        if missing_404:
            raise HTTPException(status_code=404, detail="No deployment state found")
        return
    raise HTTPException(
        status_code=429,
        detail="Deployment state locked by a concurrent writer, retry",
        headers={"Retry-After": "2"},
    )


@router.get("/deploy-state")
async def get_deploy_state(
    session: AsyncSession = Depends(get_session),
    current_user: dict = Depends(get_current_user),
):
    """Get current deployment state including monitoring status and health."""
    return await build_deploy_state_payload(session)


@router.post("/deploy-state/pause")
async def pause_pipeline(
    session: AsyncSession = Depends(get_session),
    current_user: dict = Depends(require_admin),
):
    """Manually pause the pipeline (set max_concurrent_vms=0). Used for manual intervention."""
    from app.shared_state import runtime_config

    # Read AND mutate the singleton under the same row lock the automated monitor
    # uses (deployment_state_lock == SELECT FOR UPDATE on id=1), so a concurrent
    # auto-transition or a duplicate operator click serializes instead of being
    # silently clobbered (lost update).
    from app import pause_clock  # local import to avoid cycle at module load

    async with deployment_state_lock(session, skip_locked=True) as state:
        if state is None:
            # Held → 429; genuinely missing → fall through (pause still zeroes
            # in-memory capacity even with no persisted row, as before).
            await _raise_for_unavailable_state(session, missing_404=False)
        else:
            state.status = DeploymentStatus.PAUSED
            state.pause_reason = "Manual pause via admin"
            pause_clock.mark_paused(state, pause_clock.PAUSED_KIND_MANUAL)
            # Idempotent: only snapshot pre-pause capacity if not already paused
            if state.pre_pause_max_vms is None:
                state.pre_pause_max_vms = runtime_config["max_concurrent_vms"]
            state.runtime_max_concurrent_vms = runtime_config["max_concurrent_vms"]
            session.add(state)
    await session.commit()

    old_vms = runtime_config["max_concurrent_vms"]
    runtime_config["max_concurrent_vms"] = 0
    logger.warning(
        f"[ADMIN] Pipeline PAUSED manually (was max_concurrent_vms={old_vms})"
    )
    from app.deploy_health import broadcast_deploy_state

    await broadcast_deploy_state()
    return {"status": "paused", "previous_max_vms": old_vms}


@router.post("/deploy-state/resume")
async def resume_pipeline(
    session: AsyncSession = Depends(get_session),
    current_user: dict = Depends(require_admin),
):
    """Resume the pipeline after a pause. Restores max_concurrent_vms to pre-pause value."""
    from app.shared_state import runtime_config

    from app import pause_clock  # local import to avoid cycle at module load

    # Re-check the resume precondition against the LOCKED, freshly-loaded row so a
    # concurrent auto-pause/auto-resume can't slip between a stale read and the
    # write. The whole read-check-mutate window collapses into one committed
    # transaction (the legacy double-commit is gone).
    async with deployment_state_lock(session, skip_locked=True) as state:
        if state is None:
            await _raise_for_unavailable_state(session)

        # Validate current state — only resume from PAUSED / ROLLED_BACK
        if state.status == DeploymentStatus.HALTED:
            raise HTTPException(
                status_code=400,
                detail="Pipeline is HALTED (3+ consecutive rollbacks). Use mark-stable to acknowledge before resuming.",
            )
        if state.status not in (DeploymentStatus.PAUSED, DeploymentStatus.ROLLED_BACK):
            raise HTTPException(
                status_code=400,
                detail=f"Pipeline is {state.status}, not paused — nothing to resume",
            )

        restore_vms = state.pre_pause_max_vms or state.runtime_max_concurrent_vms or 5
        state.status = DeploymentStatus.STABLE
        state.pause_reason = None
        pause_clock.accumulate_pause_window(state)
        state.pre_pause_max_vms = None
        runtime_config["max_concurrent_vms"] = restore_vms
        state.runtime_max_concurrent_vms = restore_vms
        session.add(state)
    await session.commit()

    logger.warning(f"[ADMIN] Pipeline RESUMED (max_concurrent_vms={restore_vms})")
    from app.deploy_health import broadcast_deploy_state

    await broadcast_deploy_state()
    return {"status": "resumed", "max_concurrent_vms": restore_vms}


@router.post("/deploy-state/reset-health")
async def reset_deploy_health(
    session: AsyncSession = Depends(get_session),
    current_user: dict = Depends(require_admin),
):
    """Force-reset health state and resume the pipeline. Emergency operator escape hatch."""
    from app.shared_state import runtime_config, external_health

    from app import pause_clock  # local import to avoid cycle at module load

    # Re-check the PAUSED-only precondition against the LOCKED row and perform the
    # DB read-mutate-commit atomically; the in-memory health/streak resets below
    # are not row-state and stay where they were.
    async with deployment_state_lock(session, skip_locked=True) as state:
        if state is None:
            await _raise_for_unavailable_state(session)

        if state.status != DeploymentStatus.PAUSED:
            raise HTTPException(
                status_code=400,
                detail=f"Pipeline is {state.status}, not PAUSED — reset-health only applies to PAUSED state",
            )

        restore_vms = state.pre_pause_max_vms or state.runtime_max_concurrent_vms or 5

        # Clear external health failures
        external_health["failed_deps"] = []
        for key in external_health:
            if key != "failed_deps" and isinstance(external_health[key], bool):
                external_health[key] = True

        # Reset in-memory streak counters in deploy_health.py
        try:
            import app.deploy_health as _dh

            _dh._pause_failure_streak = 0
            _dh._reset_health_recovery_history()
            _dh._recent_auto_resumes.clear()
            _dh._last_resume_was_forced = False
            _dh._consecutive_force_resumes = 0
        except Exception:
            logger.exception("[DEPLOY] Failed to reset deploy_health streak counters")

        # Reset zone health — clear cooldowns so all zones are available
        try:
            from app.compute import _zone_failures, _zone_health

            _zone_failures.clear()
            _zone_health.clear()
        except Exception:
            logger.exception("[DEPLOY] Failed to reset zone health counters")

        # Clear failure history so failure_rate resets to 0
        from app.shared_state import deploy_monitoring

        deploy_monitoring["task_results"] = deque(maxlen=100)

        # Restore VM count and clear pause fields
        runtime_config["max_concurrent_vms"] = restore_vms
        state.runtime_max_concurrent_vms = restore_vms
        state.status = DeploymentStatus.STABLE
        state.pause_reason = None
        pause_clock.accumulate_pause_window(state)
        state.pre_pause_max_vms = None
        session.add(state)
    await session.commit()
    try:
        from app.state_persistence import _persist_zone_health

        await _persist_zone_health()
    except Exception:
        logger.exception("[DEPLOY] Failed to persist cleared deploy health state")

    logger.warning(
        f"[ADMIN] Health force-reset — pipeline RESUMED (max_concurrent_vms={restore_vms})"
    )
    from app.deploy_health import broadcast_deploy_state

    await broadcast_deploy_state()
    return await build_deploy_state_payload(session)


@router.post("/deploy-state/mark-stable")
async def mark_deploy_stable(
    session: AsyncSession = Depends(get_session),
    current_user: dict = Depends(require_admin),
):
    """Manually mark the current deploy as stable (skip monitoring window)."""
    from app.shared_state import deploy_monitoring

    # Read-mutate-commit the singleton atomically under the row lock so a manual
    # mark-stable can't silently erase a concurrent auto-pause (lost update).
    async with deployment_state_lock(session, skip_locked=True) as state:
        if state is None:
            await _raise_for_unavailable_state(session)

        state.status = DeploymentStatus.STABLE
        state.last_known_good_revision = state.current_revision
        state.last_known_good_sha = state.current_sha
        state.rollback_count = 0
        state.rollback_reason = None
        session.add(state)
    await session.commit()

    deploy_monitoring["window_start"] = None
    deploy_monitoring["task_results"] = deque(maxlen=100)
    logger.warning(
        f"[ADMIN] Deploy manually marked STABLE — failure history cleared (sha={state.current_sha[:8]})"
    )
    from app.deploy_health import broadcast_deploy_state

    await broadcast_deploy_state()
    return {"status": "stable", "sha": state.current_sha}


@router.post("/deploy-state/rollback")
async def trigger_rollback(
    session: AsyncSession = Depends(get_session),
    current_user: dict = Depends(require_admin),
):
    """One-click rollback: shift Cloud Run traffic to last_known_good_revision.
    Pauses the pipeline and records the rollback in deployment state."""
    import subprocess
    from app.shared_state import runtime_config, deploy_monitoring

    from app import pause_clock  # local import to avoid cycle at module load
    from app.deploy_health import should_rollback

    # Hold the row lock across the entire rollback: precondition re-check (no good
    # revision / double-rollback guard), the gcloud traffic shift, and the state
    # mutation all happen against the same locked snapshot. This makes the
    # rollback_count increment and pre_pause snapshot exactly-once even under a
    # concurrent auto-transition or a duplicate operator click — the second writer
    # blocks on the lock (skip_locked monitor backs off) and re-evaluates the
    # ROLLED_BACK guard against the committed state, so it is rejected with 400.
    async with deployment_state_lock(session, skip_locked=True) as state:
        if state is None:
            await _raise_for_unavailable_state(session)

        if (
            not state.last_known_good_revision
            or state.last_known_good_revision == state.current_revision
        ):
            raise HTTPException(
                status_code=400, detail="No previous good revision to rollback to"
            )

        # Guard against double-rollback
        if state.status == DeploymentStatus.ROLLED_BACK:
            raise HTTPException(
                status_code=400,
                detail="Already rolled back — use resume or mark-stable first",
            )

        # Get causation analysis for the rollback reason
        _, analysis_reason, _ = should_rollback()

        # Attempt Cloud Run traffic shift via gcloud
        project_id = os.getenv("GCP_PROJECT_ID", "")
        region = os.getenv("GCP_REGION", "us-central1")
        service = os.getenv("ORCHESTRATOR_SERVICE_NAME", "blueprint-orchestrator")

        rollback_revision = state.last_known_good_revision
        rollback_error = None

        # Run the traffic shift FIRST, before mutating any deployment state. A
        # manual rollback is only complete once Cloud Run actually moves traffic
        # to the last-known-good revision; persisting ROLLED_BACK / zeroing
        # capacity before the shift succeeds would record a completed rollback
        # while traffic is still serving the bad revision (and the double-rollback
        # guard would then block a retry).
        try:

            def _run_gcloud_rollback():
                return subprocess.run(
                    [
                        "gcloud",
                        "run",
                        "services",
                        "update-traffic",
                        service,
                        "--to-revisions",
                        f"{rollback_revision}=100",
                        "--region",
                        region,
                        "--project",
                        project_id,
                    ],
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    timeout=30,
                )

            result = await asyncio.to_thread(_run_gcloud_rollback)
            if result.returncode != 0:
                rollback_error = result.stderr[:500]
                logger.error(f"[ADMIN] Cloud Run rollback failed: {rollback_error}")
        except Exception as e:
            rollback_error = str(e)[:500]
            logger.error(f"[ADMIN] Rollback command failed: {e}", exc_info=True)

        if rollback_error is not None:
            # Traffic shift failed → leave ALL deployment state untouched. Do not
            # persist ROLLED_BACK, do not increment the counter, do not snapshot
            # capacity, do not mark the pause clock, do not pause the pipeline.
            # The operator-facing state stays distinguishable from a completed
            # rollback and the double-rollback guard does not block a retry. The
            # row lock is released without a status-changing commit.
            logger.warning(
                f"[ADMIN] ROLLBACK NOT APPLIED — traffic shift failed for "
                f"{state.current_sha[:8]} → {state.last_known_good_sha[:8]}; "
                f"state left unchanged ({state.status})"
            )
            # Return a 200 with a distinct, truthful status rather than a 5xx:
            # Cloud Run quarantines instances on 5xx (see _raise_for_unavailable_state),
            # and the failure here is an operator-actionable retry, not a server fault.
            return {
                "status": "rollback_failed",
                "from_sha": state.current_sha,
                "to_revision": rollback_revision,
                "to_sha": state.last_known_good_sha,
                "reason": analysis_reason,
                "gcloud_error": rollback_error,
                "pipeline_paused": False,
                "serving_sha": state.current_sha,
            }

        # Traffic shift SUCCEEDED — only now record the completed rollback.
        # The ROLLED_BACK guard above makes this a real status transition every time
        # we reach here, so the rollback_count increment and capacity snapshot are
        # taken exactly once per rollback (a re-entrant call is rejected by the guard).
        #
        # Capture the bad (rolled-back-FROM) SHA into a local BEFORE we advance
        # current_sha onto the good revision below: the warning log and the JSON
        # response's `from_sha` must still report the revision we rolled back FROM,
        # while `to_revision`/`to_sha` report the good target now serving traffic.
        bad_sha = state.current_sha
        good_sha = state.last_known_good_sha
        state.status = DeploymentStatus.ROLLED_BACK
        state.rolled_back_from_sha = bad_sha
        state.rollback_reason = f"Manual rollback: {analysis_reason}"
        state.rollback_count += 1
        # Idempotent capacity snapshot: only capture the genuine pre-pause capacity
        # if no prior pause already saved it. Rolling back an already-paused pipeline
        # (manual or auto) leaves live capacity at zero, so an unconditional snapshot
        # would clobber the real value and a later resume would restore the wrong
        # concurrency. Mirror the manual-pause guard in pause_pipeline.
        if state.pre_pause_max_vms is None:
            state.pre_pause_max_vms = (
                runtime_config["max_concurrent_vms"]
                or state.runtime_max_concurrent_vms
            )
        # Mark the pause window on the pause clock so pause-aware task timers
        # (CI backstop, queued-age, pre-claim reaper) subtract the rollback-induced
        # downtime. mark_paused is idempotent: if the pipeline was already paused it
        # preserves the existing paused_at/paused_kind rather than restarting the window.
        pause_clock.mark_paused(state, pause_clock.PAUSED_KIND_AUTO)
        # Advance the live revision/SHA onto the rolled-back target so every
        # downstream reader (activity feed, strategist deployment_state_block,
        # admin live-SHA display, rollback-button disable comparison) immediately
        # sees the good revision as current instead of the stale bad one. Provenance
        # of the bad revision is preserved in rolled_back_from_sha (set above).
        state.current_revision = rollback_revision
        state.current_sha = good_sha
        state.deployed_at = _utcnow()
        session.add(state)
    await session.commit()

    # Pause pipeline
    runtime_config["max_concurrent_vms"] = 0
    deploy_monitoring["window_start"] = None

    logger.warning(
        f"[ADMIN] ROLLBACK triggered: {bad_sha[:8]} → {good_sha[:8]}"
    )
    from app.deploy_health import broadcast_deploy_state

    await broadcast_deploy_state()

    return {
        "status": "rolled_back",
        "from_sha": bad_sha,
        "to_revision": rollback_revision,
        "to_sha": good_sha,
        "reason": analysis_reason,
        "gcloud_error": None,
        "pipeline_paused": True,
    }


@router.get("/zone-health")
async def get_zone_health_endpoint(
    current_user: dict = Depends(get_current_user),
):
    """Get GCP zone health status for VM provisioning."""
    from app.compute import get_zone_health

    return await asyncio.to_thread(get_zone_health)

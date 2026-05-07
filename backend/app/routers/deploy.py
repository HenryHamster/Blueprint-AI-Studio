"""Deployment state and health management endpoints."""

import asyncio
import logging
import os
from collections import deque

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_session
from app.models import DeploymentState, DeploymentStatus
from app.auth import get_current_user, require_admin
from app.admin import build_deploy_state_payload

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/api/admin", tags=["deploy"], dependencies=[Depends(get_current_user)]
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

    state = await session.get(DeploymentState, 1)
    if state:
        from app import pause_clock  # noqa: PLC0415 — local import to avoid cycle at module load

        state.status = DeploymentStatus.PAUSED
        state.pause_reason = "Manual pause via admin"
        pause_clock.mark_paused(state, pause_clock.PAUSED_KIND_MANUAL)
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

    state = await session.get(DeploymentState, 1)
    if not state:
        raise HTTPException(status_code=404, detail="No deployment state found")

    # Validate current state — only resume from PAUSED
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

    from app import pause_clock  # noqa: PLC0415 — local import to avoid cycle at module load

    restore_vms = state.pre_pause_max_vms or state.runtime_max_concurrent_vms or 5
    state.status = DeploymentStatus.STABLE
    state.pause_reason = None
    pause_clock.accumulate_pause_window(state)
    state.pre_pause_max_vms = None
    session.add(state)
    await session.commit()

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

    state = await session.get(DeploymentState, 1)
    if not state:
        raise HTTPException(status_code=404, detail="No deployment state found")

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
    from app import pause_clock  # noqa: PLC0415 — local import to avoid cycle at module load

    runtime_config["max_concurrent_vms"] = restore_vms
    state.runtime_max_concurrent_vms = restore_vms
    state.status = DeploymentStatus.STABLE
    state.pause_reason = None
    pause_clock.accumulate_pause_window(state)
    state.pre_pause_max_vms = None
    session.add(state)
    await session.commit()

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

    state = await session.get(DeploymentState, 1)
    if not state:
        raise HTTPException(status_code=404, detail="No deployment state found")

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

    state = await session.get(DeploymentState, 1)
    if not state:
        raise HTTPException(status_code=404, detail="No deployment state found")

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
    from app.deploy_health import should_rollback

    _, analysis_reason, _ = should_rollback()

    # Attempt Cloud Run traffic shift via gcloud
    project_id = os.getenv("GCP_PROJECT_ID", "")
    region = os.getenv("GCP_REGION", "us-central1")
    service = "blueprint-orchestrator"

    rollback_revision = state.last_known_good_revision
    rollback_error = None

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
                timeout=30,
            )

        result = await asyncio.to_thread(_run_gcloud_rollback)
        if result.returncode != 0:
            rollback_error = result.stderr[:500]
            logger.error(f"[ADMIN] Cloud Run rollback failed: {rollback_error}")
    except Exception as e:
        rollback_error = str(e)[:500]
        logger.error(f"[ADMIN] Rollback command failed: {e}", exc_info=True)

    # Update deployment state regardless of gcloud result
    state.status = DeploymentStatus.ROLLED_BACK
    state.rolled_back_from_sha = state.current_sha
    state.rollback_reason = f"Manual rollback: {analysis_reason}"
    state.rollback_count += 1
    state.pre_pause_max_vms = runtime_config["max_concurrent_vms"]
    session.add(state)
    await session.commit()

    # Pause pipeline
    runtime_config["max_concurrent_vms"] = 0
    deploy_monitoring["window_start"] = None

    logger.warning(
        f"[ADMIN] ROLLBACK triggered: {state.current_sha[:8]} → {state.last_known_good_sha[:8]}"
    )
    from app.deploy_health import broadcast_deploy_state

    await broadcast_deploy_state()

    return {
        "status": "rolled_back" if not rollback_error else "rollback_requested",
        "from_sha": state.current_sha,
        "to_revision": rollback_revision,
        "to_sha": state.last_known_good_sha,
        "reason": analysis_reason,
        "gcloud_error": rollback_error,
        "pipeline_paused": True,
    }


@router.get("/zone-health")
async def get_zone_health_endpoint(
    current_user: dict = Depends(get_current_user),
):
    """Get GCP zone health status for VM provisioning."""
    from app.compute import get_zone_health

    return get_zone_health()

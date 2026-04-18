"""Direction lifecycle management for autonomous research.

Handles dead direction pruning, auto-recovery from auto-pause, and active
direction filtering (cooldown enforcement, auto-pause, completion detection).
Extracted from autonomous_research.py to create headroom in that module.
"""
import logging
from datetime import datetime, timezone

from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm.attributes import flag_modified

from app.models import Project
from app.shared_state import runtime_config as _runtime_config
from app.time_utils import _utcnow

logger = logging.getLogger(__name__)


async def _process_direction_lifecycle(
    project: Project,
    session: AsyncSession,
    cooldown_minutes: float,
    busy_direction_ids: set,
    now: datetime,
) -> list:
    """Prune dead directions, auto-recover paused ones, and return eligible active directions.

    Mutates project.directions in place for auto-pause and recovery state changes.
    Commits the session if any direction state was modified.

    Returns a list of active, non-cooling directions eligible for research.
    """
    from app.strategic_completion import _check_direction_health, _check_direction_completion  # noqa: PLC0415

    directions_modified = False
    # Prune dead directions (expired/completed/rejected) to prevent JSON bloat
    live_dirs = [d for d in (project.directions or []) if d.get("status") in ("active", "proposed", "paused")]
    if len(live_dirs) < len(project.directions or []):
        logger.info(f"[AUTONOMOUS] Pruned {len(project.directions or []) - len(live_dirs)} dead directions from {project.id}")
        project.directions = live_dirs
        flag_modified(project, "directions")
        directions_modified = True
    # Auto-recovery: reactivate auto-paused directions past cooldown
    recovery_hours = _runtime_config.get("direction_auto_recovery_hours", 48)
    for d in (project.directions or []):
        if d.get("status") != "paused" or not d.get("auto_paused") or not d.get("paused_at"):
            continue
        hours = (_utcnow() - datetime.fromisoformat(d["paused_at"])).total_seconds() / 3600
        if hours < recovery_hours:
            continue
        if _check_direction_health(d) is None:
            d["status"] = "active"
            for k in ("auto_paused", "paused_reason", "paused_at"):
                d.pop(k, None)
            logger.info(f"[AUTONOMOUS] Auto-recovered direction {d.get('id')} after {hours:.0f}h cooldown")
            directions_modified = True
        else:
            d["paused_at"] = _utcnow().isoformat()
            logger.info(f"[AUTONOMOUS] Direction {d.get('id')} still unhealthy, extending pause")
            directions_modified = True
    active_directions = []
    auto_paused_any = False
    for d in (project.directions or []):
        if d.get("status") != "active":
            continue
        did = d.get("id")
        # Skip directions with an unrolled research cycle still in progress
        if did and did in busy_direction_ids:
            logger.debug(
                f"[RESEARCH] Skipping direction {did}: research cycle already active"
            )
            continue
        last_researched = d.get("last_researched_at")
        if last_researched:
            minutes_ago = (now - datetime.fromisoformat(last_researched)).total_seconds() / 60
            effective_cooldown = cooldown_minutes
            if d.get("possibly_saturated"):
                effective_cooldown = cooldown_minutes * 2.0
                logger.debug(
                    f"[RESEARCH] Direction {did} saturated — applying 2x cooldown ({cooldown_minutes*2}m)"
                )
            if minutes_ago < effective_cooldown:
                logger.debug(
                    f"[RESEARCH] Skipping direction {did}: last researched "
                    f"{minutes_ago:.1f}m ago (cooldown: {effective_cooldown}m)"
                )
                continue
        # Auto-pause check — must come after cooldown to only evaluate directions eligible for research
        pause_reason = _check_direction_health(d)
        if pause_reason:
            d["status"] = "paused"
            d["paused_reason"] = pause_reason
            d["paused_at"] = _utcnow().isoformat()
            d["auto_paused"] = True
            d["auto_pause_count"] = d.get("auto_pause_count", 0) + 1
            logger.info(f"[AUTONOMOUS] Auto-paused direction {did}: {pause_reason}")
            auto_paused_any = True
            try:
                from app.notification_dispatch import post_direction_milestone_message  # noqa: PLC0415
                await post_direction_milestone_message(
                    session, project.id, d.get("text", did), "auto-paused", pause_reason
                )
            except Exception as _e:
                logger.warning(f"[NOTIFY] Failed to post auto-paused message: {_e}")
            MAX_AUTO_PAUSE_CYCLES = 3
            if d.get("auto_pause_count", 0) >= MAX_AUTO_PAUSE_CYCLES:
                d["status"] = "completed"
                d["auto_completed"] = True
                d["auto_completed_reason"] = (
                    f"Auto-retired: direction was auto-paused {d['auto_pause_count']} times "
                    f"without producing results. Slot freed for new directions."
                )
                d["completed_at"] = _utcnow().isoformat()
                logger.info(
                    f"[AUTONOMOUS] Auto-retired direction {did} after "
                    f"{d['auto_pause_count']} auto-pause cycles: {d.get('text', '')[:60]}"
                )
                from app.models import Alert, AlertSeverity  # noqa: PLC0415
                alert = Alert(
                    project_id=project.id,
                    severity=AlertSeverity.INFO,
                    title="Direction auto-retired",
                    message=(
                        f"Direction '{d.get('text', '')[:80]}' was auto-retired after "
                        f"{d['auto_pause_count']} consecutive auto-pause cycles. "
                        f"Slot freed for strategic agent to create a replacement."
                    ),
                )
                session.add(alert)
                try:
                    from app.notification_dispatch import post_direction_milestone_message as _post_milestone  # noqa: PLC0415
                    _retire_reason = (
                        f"Auto-retired after {d['auto_pause_count']} consecutive auto-pause cycles. "
                        f"Slot freed for strategic agent to create a replacement."
                    )
                    await _post_milestone(
                        session, project.id, d.get("text", did), "auto-retired", _retire_reason
                    )
                except Exception as _e:
                    logger.warning(f"[NOTIFY] Failed to post auto-retired message: {_e}")
                directions_modified = True
            else:
                # Spawn diagnosis research so the agent can investigate the failure
                # pattern before the 48h cooldown elapses — skipped on auto-retire
                # since a retired direction is terminal and has no future cycles.
                try:
                    from app.autonomous_research import _spawn_direction_diagnosis  # noqa: PLC0415
                    if await _spawn_direction_diagnosis(project, d, pause_reason, session):
                        d["diagnosis_spawned_at"] = _utcnow().isoformat()
                        directions_modified = True
                except Exception as _e:
                    logger.warning(
                        f"[DIAGNOSIS] Failed to spawn diagnosis research for {did}: {_e}"
                    )
            continue
        # Positive completion detection — mark for user review but don't pause
        completion_suggestion = _check_direction_completion(d)
        if completion_suggestion:
            if not d.get("possibly_complete"):
                d["possibly_complete"] = True
                d["completion_suggestion"] = completion_suggestion
                logger.info(f"[AUTONOMOUS] Direction {did} possibly complete: {completion_suggestion}")
                directions_modified = True
        else:
            if d.get("possibly_complete"):
                d.pop("possibly_complete", None)
                d.pop("completion_suggestion", None)
                directions_modified = True
        active_directions.append(d)
    if auto_paused_any or directions_modified:
        flag_modified(project, "directions")
        session.add(project)
        await session.commit()
    return active_directions

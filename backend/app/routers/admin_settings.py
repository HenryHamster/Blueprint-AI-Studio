from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select
from typing import Optional, Dict, Any, List, TypedDict
import os
import re
import uuid
import json
import httpx
import logging
import asyncio
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from sqlalchemy import func
from sqlalchemy.orm.attributes import flag_modified

from app.database import get_session
from app.shared_state import limiter, circuit_breakers as _circuit_breakers
from app.models import Task, TaskStatus, Project, UserIntegration, MaxPlanAccount, DeploymentState, DeploymentStatus, _utcnow
from app.auth import get_current_user, require_admin
from app.github import generate_installation_access_token
from app.broadcast import broadcast_to_project
from app.admin import _get_user_project, _get_user_project_with_role, _user_project_ids

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/api/admin",
    tags=["admin-settings"],
    dependencies=[Depends(get_current_user)]
)


# ---------------------------------------------------------------------------
# Diagnostics TypedDicts and helpers
# ---------------------------------------------------------------------------

class StuckTaskInfo(TypedDict):
    task_id: str
    title: str
    status: TaskStatus
    stuck_reason: str
    stuck_for_seconds: int
    attempt_count: int
    failure_class: Optional[str]
    unresolvable_deps: List[str]
    remediation_hint: str


class CircuitBreakerInfo(TypedDict):
    state: str
    failure_count: int
    cooldown_seconds: int
    cooldown_remaining_seconds: Optional[int]
    opened_at: Optional[str]


class ProjectSummary(TypedDict):
    project_id: str
    stuck_count: int
    queued_count: int
    in_progress_count: int
    circuit_breaker_state: str


def _classify_stuck_failure(task: Task) -> Optional[str]:
    """Classify stuck tasks using the canonical classifier, returning None for CODE_ERROR.

    Stuck tasks have not actually failed yet, so CODE_ERROR (the default fallback) is
    misleading. We suppress it and return None so the UI shows 'unclassified' instead.
    """
    from app.failure_classification import classify_failure  # noqa: PLC0415
    result = classify_failure(task)
    return result if result != "CODE_ERROR" else None


def _fmt_duration(seconds: float) -> str:
    """Format seconds into a compact human-readable string."""
    s = int(seconds)
    if s < 60:
        return f"{s}s"
    if s < 3600:
        return f"{s // 60}m{s % 60:02d}s" if s % 60 else f"{s // 60}m"
    h, rem = divmod(s, 3600)
    m = rem // 60
    return f"{h}h{m:02d}m" if m else f"{h}h"


async def _build_stuck_tasks(
    tasks: List[Task],
    now: datetime,
    session: AsyncSession,
) -> List[StuckTaskInfo]:
    """Detect stuck tasks and build the diagnostics list."""
    from app.compute import get_zone_health  # noqa: PLC0415
    stuck: List[StuckTaskInfo] = []
    provision_timeout_secs = 5 * 60
    no_heartbeat_secs = 5 * 60
    long_queued_secs = 30 * 60
    stuck_deps_secs = 10 * 60
    evaluating_timeout_secs = 2 * 3600  # 2 hours

    # Pre-fetch all dependency statuses and titles in a single batch query to avoid N+1
    all_dep_uuids: set[uuid.UUID] = set()
    for task in tasks:
        if task.status == TaskStatus.QUEUED:
            created_age = (now - task.created_at).total_seconds()
            if (task.depends_on or []) and created_age > stuck_deps_secs:
                for d in task.depends_on:
                    try:
                        all_dep_uuids.add(uuid.UUID(d))
                    except (ValueError, AttributeError):
                        pass

    dep_status_map: Dict[str, TaskStatus] = {}
    dep_title_map: Dict[str, str] = {}
    if all_dep_uuids:
        dep_result = await session.execute(
            select(Task.id, Task.status, Task.title).where(Task.id.in_(all_dep_uuids))
        )
        for row in dep_result.all():
            dep_status_map[str(row.id)] = row.status
            dep_title_map[str(row.id)] = row.title or str(row.id)[:8]

    # Count running/queued tasks for capacity hints
    running_count = sum(1 for t in tasks if t.status == TaskStatus.IN_PROGRESS)
    queued_count = sum(1 for t in tasks if t.status == TaskStatus.QUEUED)

    # Fetch zone health once for provision_timeout hints
    try:
        zone_health = get_zone_health()
    except Exception:
        zone_health = {}

    for task in tasks:
        stuck_reason = None
        stuck_for_seconds = 0
        unresolvable_deps: List[str] = []
        remediation_hint = ""

        if task.status == TaskStatus.PROVISIONING and task.last_heartbeat is None and task.started_at is not None:
            age = (now - task.started_at).total_seconds()
            if age > provision_timeout_secs:
                stuck_reason = "provision_timeout"
                stuck_for_seconds = int(age - provision_timeout_secs)
                ctx = task.context or {}
                zone = ctx.get("provision_zone")
                if zone:
                    zh = zone_health.get(zone, {})
                    if zh.get("cooldown_active"):
                        remediation_hint = f"VM timed out in {zone} — zone in cooldown, reaper will retry"
                    else:
                        remediation_hint = f"VM timed out in {zone} — zone available, reaper will retry"
                else:
                    remediation_hint = "VM provision timed out — reaper will detect and retry"

        elif task.status == TaskStatus.IN_PROGRESS and task.last_heartbeat is not None:
            heartbeat_age = (now - task.last_heartbeat).total_seconds()
            if heartbeat_age > no_heartbeat_secs:
                stuck_reason = "no_heartbeat"
                stuck_for_seconds = int(heartbeat_age - no_heartbeat_secs)
                remediation_hint = f"No heartbeat for {_fmt_duration(heartbeat_age)} — VM may have crashed, reaper will clean up"

        elif task.status == TaskStatus.QUEUED:
            created_age = (now - task.created_at).total_seconds()
            dep_ids = task.depends_on or []

            if dep_ids and created_age > stuck_deps_secs:
                # Check whether all deps are terminal-failure using pre-fetched map
                valid_dep_uuids = []
                for d in dep_ids:
                    try:
                        valid_dep_uuids.append(uuid.UUID(d))
                    except (ValueError, AttributeError):
                        pass
                if valid_dep_uuids:
                    known_rows = [
                        (str(uid), dep_status_map[str(uid)])
                        for uid in valid_dep_uuids
                        if str(uid) in dep_status_map
                    ]
                    failed_dep_ids = [
                        dep_id
                        for dep_id, status in known_rows
                        if status in (TaskStatus.FAILED, TaskStatus.CANCELLED)
                    ]
                    if failed_dep_ids and len(failed_dep_ids) == len(known_rows):
                        stuck_reason = "stuck_deps"
                        stuck_for_seconds = int(created_age - stuck_deps_secs)
                        unresolvable_deps = failed_dep_ids
                        dep_titles = [dep_title_map.get(d, d[:8]) for d in failed_dep_ids[:2]]
                        suffix = f" +{len(failed_dep_ids) - 2} more" if len(failed_dep_ids) > 2 else ""
                        remediation_hint = f"Blocked by {len(failed_dep_ids)} failed task(s): {', '.join(dep_titles)}{suffix}"

            elif not dep_ids and created_age > long_queued_secs:
                stuck_reason = "long_queued"
                stuck_for_seconds = int(created_age - long_queued_secs)
                exhausted = [z for z, d in zone_health.items() if d.get("cooldown_active")]
                if exhausted:
                    remediation_hint = f"All zones exhausted — {running_count} running, {queued_count}Q waiting"
                else:
                    remediation_hint = f"No available slots — {running_count} running, {queued_count}Q waiting"

        elif task.status == TaskStatus.EVALUATING:
            # Skip CI-waiting tasks — they have their own timeout
            ctx = task.context or {}
            if not ctx.get("ci_waiting") and not ctx.get("review_loop", {}).get("ci_waiting"):
                # Find when the task entered EVALUATING from status_log
                evaluating_since = None
                for entry in reversed(task.status_log or []):
                    msg = entry.get("msg", "")
                    if "→ EVALUATING" in msg:
                        try:
                            evaluating_since = datetime.fromisoformat(entry["t"])
                        except (KeyError, ValueError, TypeError):
                            pass
                        break
                if evaluating_since is None:
                    evaluating_since = task.started_at or task.created_at
                eval_age = (now - evaluating_since).total_seconds()
                if eval_age > evaluating_timeout_secs:
                    stuck_reason = "evaluation_timeout"
                    stuck_for_seconds = int(eval_age - evaluating_timeout_secs)
                    remediation_hint = f"Evaluating for {_fmt_duration(eval_age)} — may need manual review"

        if stuck_reason:
            stuck.append({
                "task_id": str(task.id),
                "title": task.title,
                "status": task.status,
                "stuck_reason": stuck_reason,
                "stuck_for_seconds": stuck_for_seconds,
                "attempt_count": task.attempt_count,
                "failure_class": _classify_stuck_failure(task),
                "unresolvable_deps": unresolvable_deps,
                "remediation_hint": remediation_hint,
            })

    return stuck


def _format_circuit_breaker(cb: dict) -> CircuitBreakerInfo:
    opened_at = cb.get("opened_at")
    cooldown_remaining = None
    if opened_at and cb.get("state") == "OPEN":
        elapsed = (_utcnow() - opened_at).total_seconds()
        cooldown_remaining = max(0, int(cb.get("cooldown_seconds", 1800) - elapsed))
    return {
        "state": cb.get("state", "CLOSED"),
        "failure_count": cb.get("failure_count", 0),
        "cooldown_seconds": cb.get("cooldown_seconds", 1800),
        "cooldown_remaining_seconds": cooldown_remaining,
        "opened_at": opened_at.isoformat() if opened_at else None,
    }


def _build_notification_config_response(config: dict, *, is_admin: bool) -> dict:
    """Return notification config, masking webhook_url for non-admin callers.

    Webhook URLs (Slack, PagerDuty, Discord) embed auth tokens, so they must
    not be returned to non-admin users. Non-admins receive a boolean flag
    indicating whether a URL is configured instead.
    """
    if is_admin:
        return config
    return {
        "webhook_url": "",
        "webhook_url_configured": bool(config.get("webhook_url")),
        "enabled_events": config.get("enabled_events", ["TASK_FAILED", "APPROVAL_NEEDED", "DEPLOY_HEALTH_ALERT"]),
        "throttle_seconds": config.get("throttle_seconds", 30),
    }


# ---------------------------------------------------------------------------
# Settings endpoints
# ---------------------------------------------------------------------------

@router.get("/settings")
async def get_settings(
    session: AsyncSession = Depends(get_session),
    current_user: dict = Depends(get_current_user)
):
    from app.shared_state import runtime_config as _runtime_config
    from app.max_plan_pool import _max_plan_pools
    from app.models import UserIntegration

    # Check if the user has a GitHub integration
    github_account = None
    stmt = select(UserIntegration).where(UserIntegration.user_id == current_user["user_id"])
    integration = (await session.execute(stmt)).scalars().first()
    if integration and integration.github_account_name:
        github_account = integration.github_account_name

    user_pool = _max_plan_pools.get(current_user["user_id"], [])
    return {
        "max_concurrent_vms": _runtime_config["max_concurrent_vms"],
        "max_retries": _runtime_config["max_retries"],
        "max_autonomous_tasks_per_hour": _runtime_config["max_autonomous_tasks_per_hour"],
        "max_global_autonomous_tasks_per_hour": _runtime_config.get("max_global_autonomous_tasks_per_hour", 50),
        "autonomous_research_interval_minutes": _runtime_config.get("autonomous_research_interval_minutes", 30),
        "task_type_models": _runtime_config.get("task_type_models", {}),
        "task_type_efforts": _runtime_config.get("task_type_efforts", {}),
        "health_check_urls": _runtime_config.get("health_check_urls", []),
        "agent_prompt_suffixes": _runtime_config.get("agent_prompt_suffixes", {}),
        "autonomous_periodic_tasks": _runtime_config.get("autonomous_periodic_tasks", {}),
        "autonomous_direction_cooldown_minutes": _runtime_config.get("autonomous_direction_cooldown_minutes", 240),
        "autonomous_research_wave_size": _runtime_config.get("autonomous_research_wave_size", 3),
        "strategic_research_frequency": _runtime_config.get("strategic_research_frequency", 6),
        "github_account": github_account,
        "require_approval_gates": _runtime_config.get("require_approval_gates", True),
        "daily_cost_ceiling_usd": _runtime_config.get("daily_cost_ceiling_usd", 0),
        "webhook_url_configured": bool(os.getenv("PIPELINE_WEBHOOK_URL")),
        "notification_config": _build_notification_config_response(
            _runtime_config.get("notification_config", {
                "webhook_url": "",
                "enabled_events": ["TASK_FAILED", "APPROVAL_NEEDED", "DEPLOY_HEALTH_ALERT"],
                "throttle_seconds": 30,
            }),
            is_admin=current_user.get("role") == "admin",
        ),
        "using_max_plan": len([a for a in user_pool if a.get("enabled", True)]) > 0,
        "max_plan_accounts": [
            {"id": a["id"], "name": a["name"], "enabled": a.get("enabled", True),
             "active_tasks": a["active_tasks"], "total_tasks": a["total_tasks"],
             "token_created_at": a.get("token_created_at"),
             "rl_hit_count": a.get("rl_hit_count", 0),
             "disabled_models": {
                 model: {"hits": info.get("hits", 0), "at": info["at"].isoformat() if info.get("at") else None}
                 for model, info in a.get("disabled_models", {}).items()
             }}
            for a in user_pool
        ],
    }


@router.patch("/settings")
@limiter.limit("30/minute")
async def update_settings(
    request: Request,
    payload: Dict[str, Any],
    session: AsyncSession = Depends(get_session),
    current_user: dict = Depends(require_admin)
):
    from app.shared_state import runtime_config as _runtime_config, runtime_config_lock
    async with runtime_config_lock:
        return await _apply_settings(payload, session, _runtime_config)


async def _apply_settings(payload: Dict[str, Any], session: AsyncSession, _runtime_config: Dict):
    # Snapshot config so we can restore on DB commit failure
    _config_snapshot = dict(_runtime_config)

    if "max_concurrent_vms" in payload:
        try:
            val = int(payload["max_concurrent_vms"])
        except (ValueError, TypeError):
            raise HTTPException(status_code=400, detail="max_concurrent_vms must be a number")
        if not (1 <= val <= 100):
            raise HTTPException(status_code=400, detail="max_concurrent_vms must be between 1 and 100")
        _runtime_config["max_concurrent_vms"] = val
        # Clear PAUSED/HALTED state if user explicitly sets VMs > 0
        # Otherwise the next cold start will override back to 0
        if val > 0:
            state = await session.get(DeploymentState, 1)
            if state and state.status in (DeploymentStatus.PAUSED, DeploymentStatus.HALTED):
                old_status = state.status
                state.status = DeploymentStatus.STABLE
                state.pause_reason = None
                state.paused_at = None
                state.pre_pause_max_vms = None
                session.add(state)
                logger.warning(f"[SETTINGS] Cleared {old_status} state — user set max_vms={val}")
    if "max_retries" in payload:
        try:
            val = int(payload["max_retries"])
        except (ValueError, TypeError):
            raise HTTPException(status_code=400, detail="max_retries must be a number")
        if not (0 <= val <= 10):
            raise HTTPException(status_code=400, detail="max_retries must be between 0 and 10")
        _runtime_config["max_retries"] = val
    if "max_autonomous_tasks_per_hour" in payload:
        try:
            val = int(payload["max_autonomous_tasks_per_hour"])
        except (ValueError, TypeError):
            raise HTTPException(status_code=400, detail="max_autonomous_tasks_per_hour must be a number")
        if not (1 <= val <= 50):
            raise HTTPException(status_code=400, detail="max_autonomous_tasks_per_hour must be between 1 and 50")
        _runtime_config["max_autonomous_tasks_per_hour"] = val
    if "max_global_autonomous_tasks_per_hour" in payload:
        try:
            val = int(payload["max_global_autonomous_tasks_per_hour"])
        except (ValueError, TypeError):
            raise HTTPException(status_code=400, detail="max_global_autonomous_tasks_per_hour must be a number")
        if not (1 <= val <= 500):
            raise HTTPException(status_code=400, detail="max_global_autonomous_tasks_per_hour must be between 1 and 500")
        _runtime_config["max_global_autonomous_tasks_per_hour"] = val
    if "autonomous_research_interval_minutes" in payload:
        try:
            val = int(payload["autonomous_research_interval_minutes"])
        except (ValueError, TypeError):
            raise HTTPException(status_code=400, detail="autonomous_research_interval_minutes must be a number")
        if not (1 <= val <= 1440):
            raise HTTPException(status_code=400, detail="autonomous_research_interval_minutes must be between 1 and 1440")
        _runtime_config["autonomous_research_interval_minutes"] = val
    VALID_MODELS = {"claude-opus-4-6", "claude-sonnet-4-6", "claude-haiku-4-5-20251001", ""}
    VALID_EFFORTS = {"low", "medium", "high", "max", ""}
    if "task_type_models" in payload:
        val = payload["task_type_models"]
        if not isinstance(val, dict):
            raise HTTPException(status_code=400, detail="task_type_models must be a dict")
        if any(v not in VALID_MODELS for v in val.values()):
            raise HTTPException(status_code=400, detail="Invalid model in task_type_models")
        _runtime_config["task_type_models"] = val
    if "task_type_efforts" in payload:
        val = payload["task_type_efforts"]
        if not isinstance(val, dict):
            raise HTTPException(status_code=400, detail="task_type_efforts must be a dict")
        if any(v not in VALID_EFFORTS for v in val.values()):
            raise HTTPException(status_code=400, detail="Invalid effort in task_type_efforts")
        _runtime_config["task_type_efforts"] = val
    if "agent_prompt_suffixes" in payload:
        val = payload["agent_prompt_suffixes"]
        if not isinstance(val, dict):
            raise HTTPException(status_code=400, detail="agent_prompt_suffixes must be a dict")
        if any(not isinstance(v, str) for v in val.values()):
            raise HTTPException(status_code=400, detail="agent_prompt_suffixes values must be strings")
        _runtime_config["agent_prompt_suffixes"] = val
    if "autonomous_periodic_tasks" in payload:
        val = payload["autonomous_periodic_tasks"]
        if not isinstance(val, dict):
            raise HTTPException(status_code=400, detail="autonomous_periodic_tasks must be a dict")
        VALID_TYPES = {"security_audit", "qa", "deps"}
        for type_key, type_cfg in val.items():
            if type_key not in VALID_TYPES:
                raise HTTPException(status_code=400, detail=f"Invalid task type: {type_key}")
            if not isinstance(type_cfg, dict):
                raise HTTPException(status_code=400, detail=f"Config for {type_key} must be a dict")
            if "enabled" in type_cfg and not isinstance(type_cfg["enabled"], bool):
                raise HTTPException(status_code=400, detail=f"enabled for {type_key} must be a boolean")
            if "interval_hours" in type_cfg:
                try:
                    ih = int(type_cfg["interval_hours"])
                except (ValueError, TypeError):
                    raise HTTPException(status_code=400, detail=f"interval_hours for {type_key} must be a number")
                if not (1 <= ih <= 8760):
                    raise HTTPException(status_code=400, detail=f"interval_hours for {type_key} must be between 1 and 8760")
        # Merge into existing config (don't replace keys not in the payload)
        existing = _runtime_config.get("autonomous_periodic_tasks", {})
        for type_key, type_cfg in val.items():
            existing.setdefault(type_key, {}).update(type_cfg)
        _runtime_config["autonomous_periodic_tasks"] = existing
    # Accept both new (minutes) and old (hours) keys for backward compat
    if "autonomous_direction_cooldown_minutes" in payload:
        try:
            val = int(payload["autonomous_direction_cooldown_minutes"])
        except (ValueError, TypeError):
            raise HTTPException(status_code=400, detail="autonomous_direction_cooldown_minutes must be a number")
        if not (1 <= val <= 525600):
            raise HTTPException(status_code=400, detail="autonomous_direction_cooldown_minutes must be between 1 and 525600")
        _runtime_config["autonomous_direction_cooldown_minutes"] = val
    elif "autonomous_direction_cooldown_hours" in payload:
        try:
            val = int(payload["autonomous_direction_cooldown_hours"])
        except (ValueError, TypeError):
            raise HTTPException(status_code=400, detail="autonomous_direction_cooldown_hours must be a number")
        _runtime_config["autonomous_direction_cooldown_minutes"] = val * 60
    if "autonomous_research_wave_size" in payload:
        try:
            val = int(payload["autonomous_research_wave_size"])
        except (ValueError, TypeError):
            raise HTTPException(status_code=400, detail="autonomous_research_wave_size must be a number")
        if not (1 <= val <= 50):
            raise HTTPException(status_code=400, detail="autonomous_research_wave_size must be between 1 and 50")
        _runtime_config["autonomous_research_wave_size"] = val
    if "strategic_research_frequency" in payload:
        try:
            val = int(payload["strategic_research_frequency"])
        except (ValueError, TypeError):
            raise HTTPException(status_code=400, detail="strategic_research_frequency must be a number")
        if not (0 <= val <= 100):
            raise HTTPException(status_code=400, detail="strategic_research_frequency must be between 0 and 100 (0 = disabled)")
        _runtime_config["strategic_research_frequency"] = val
    if "require_approval_gates" in payload:
        val = payload["require_approval_gates"]
        if not isinstance(val, bool):
            raise HTTPException(status_code=400, detail="require_approval_gates must be a boolean")
        _runtime_config["require_approval_gates"] = val
    if "daily_cost_ceiling_usd" in payload:
        try:
            val = float(payload["daily_cost_ceiling_usd"])
        except (ValueError, TypeError):
            raise HTTPException(status_code=400, detail="daily_cost_ceiling_usd must be a number")
        if not (0 <= val <= 100000):
            raise HTTPException(status_code=400, detail="daily_cost_ceiling_usd must be between 0 and 100000 (0 = disabled)")
        _runtime_config["daily_cost_ceiling_usd"] = val
    if "health_check_urls" in payload:
        val = payload["health_check_urls"]
        if not isinstance(val, list):
            raise HTTPException(status_code=400, detail="health_check_urls must be a list")
        if any(not isinstance(u, str) for u in val):
            raise HTTPException(status_code=400, detail="health_check_urls entries must be strings")
        if any(not u.startswith(("http://", "https://")) for u in val if u):
            raise HTTPException(status_code=400, detail="health_check_urls entries must start with http:// or https://")
        _runtime_config["health_check_urls"] = [u for u in val if u]
    if "notification_config" in payload:
        val = payload["notification_config"]
        if not isinstance(val, dict):
            raise HTTPException(status_code=400, detail="notification_config must be an object")
        webhook_url = val.get("webhook_url", "")
        if not isinstance(webhook_url, str):
            raise HTTPException(status_code=400, detail="notification_config.webhook_url must be a string")
        if webhook_url and not webhook_url.startswith(("http://", "https://")):
            raise HTTPException(status_code=400, detail="notification_config.webhook_url must start with http:// or https://")
        _VALID_NOTIFICATION_EVENTS = frozenset([
            "TASK_FAILED", "TASK_COMPLETED", "APPROVAL_NEEDED", "DEPLOY_HEALTH_ALERT",
            "PR_MERGED", "BUDGET_EXCEEDED",
        ])
        enabled_events = val.get("enabled_events", ["TASK_FAILED", "APPROVAL_NEEDED", "DEPLOY_HEALTH_ALERT"])
        if not isinstance(enabled_events, list):
            raise HTTPException(status_code=400, detail="notification_config.enabled_events must be a list")
        invalid_events = [e for e in enabled_events if e not in _VALID_NOTIFICATION_EVENTS]
        if invalid_events:
            raise HTTPException(
                status_code=400,
                detail=f"notification_config.enabled_events contains unknown events: {invalid_events}. "
                       f"Valid values: {sorted(_VALID_NOTIFICATION_EVENTS)}",
            )
        throttle_seconds = val.get("throttle_seconds", 30)
        try:
            throttle_seconds = int(throttle_seconds)
        except (ValueError, TypeError):
            raise HTTPException(status_code=400, detail="notification_config.throttle_seconds must be a number")
        if not (0 <= throttle_seconds <= 3600):
            raise HTTPException(status_code=400, detail="notification_config.throttle_seconds must be between 0 and 3600")
        _runtime_config["notification_config"] = {
            "webhook_url": webhook_url,
            "enabled_events": enabled_events,
            "throttle_seconds": throttle_seconds,
        }
    if "test_notification" in payload and payload["test_notification"] is True:
        nc = _runtime_config.get("notification_config", {})
        webhook_url = nc.get("webhook_url", "")
        if not webhook_url:
            raise HTTPException(status_code=400, detail="No webhook URL configured")
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.post(webhook_url, json={"type": "TEST", "message": "Blueprint test notification"})
            if resp.status_code >= 400:
                raise HTTPException(status_code=502, detail=f"Webhook returned {resp.status_code}")
        except httpx.RequestError as e:
            raise HTTPException(status_code=502, detail=f"Webhook request failed: {e}")
        return {"ok": True, "test_notification_sent": True}

    # Persist to DB so values survive Cloud Run restarts
    # Always persist all runtime config values (not just changed ones) to ensure consistency
    try:
        # FOR UPDATE prevents concurrent settings writes from racing (multi-instance)
        state = (await session.execute(
            select(DeploymentState).where(DeploymentState.id == 1).with_for_update()
        )).scalars().first()
        if state:
            state.runtime_max_concurrent_vms = _runtime_config["max_concurrent_vms"]
            state.runtime_max_retries = _runtime_config["max_retries"]
            state.runtime_max_autonomous_tasks_per_hour = _runtime_config["max_autonomous_tasks_per_hour"]
            state.runtime_max_global_autonomous_tasks_per_hour = _runtime_config.get("max_global_autonomous_tasks_per_hour")
            state.runtime_autonomous_research_interval_minutes = _runtime_config.get("autonomous_research_interval_minutes", 30)
            state.runtime_autonomous_direction_cooldown_minutes = _runtime_config.get("autonomous_direction_cooldown_minutes", 240)
            # Persist dict/list config fields to JSONB column
            try:
                state.runtime_config_json = {
                    "task_type_models": _runtime_config.get("task_type_models", {}),
                    "task_type_efforts": _runtime_config.get("task_type_efforts", {}),
                    "agent_prompt_suffixes": _runtime_config.get("agent_prompt_suffixes", {}),
                    "autonomous_periodic_tasks": _runtime_config.get("autonomous_periodic_tasks", {}),
                    "health_check_urls": _runtime_config.get("health_check_urls", []),
                    "autonomous_research_wave_size": _runtime_config.get("autonomous_research_wave_size", 3),
                    "strategic_research_frequency": _runtime_config.get("strategic_research_frequency", 6),
                    "require_approval_gates": _runtime_config.get("require_approval_gates", True),
                    "daily_cost_ceiling_usd": _runtime_config.get("daily_cost_ceiling_usd", 0),
                    "notification_config": _runtime_config.get("notification_config", {
                        "webhook_url": "",
                        "enabled_events": ["TASK_FAILED", "APPROVAL_NEEDED", "DEPLOY_HEALTH_ALERT"],
                        "throttle_seconds": 30,
                    }),
                }
            except AttributeError:
                pass  # Column not yet added (rolling deploy, old schema)
            session.add(state)
            await session.commit()
            logger.info(f"[SETTINGS] Persisted runtime config: max_vms={state.runtime_max_concurrent_vms}")
    except Exception as e:
        # Restore in-memory config to pre-change state on DB failure
        _runtime_config.update(_config_snapshot)
        logger.error(f"[SETTINGS] DB persist failed, rolling back in-memory config: {e}")
        await session.rollback()
        raise HTTPException(status_code=500, detail="Failed to persist settings")

    return _runtime_config


# --- Max Plan Account Pool ---

@router.post("/max-plan-accounts")
@limiter.limit("10/minute")
async def add_max_plan_account(
    request: Request,
    payload: Dict[str, Any],
    session: AsyncSession = Depends(get_session),
    current_user: dict = Depends(get_current_user)
):
    """Add a Max plan account. Provide 'name' and 'oauth_token' (sk-ant-oat01-... from `claude setup-token`).
    Optionally provide 'claude_config_json' (content of ~/.claude.json for onboarding bypass)."""
    from app.max_plan_pool import _max_plan_pools, _account_by_id
    name = payload.get("name")
    oauth_token = payload.get("oauth_token")
    if not name or not oauth_token:
        raise HTTPException(status_code=400, detail="'name' and 'oauth_token' are required")
    if not oauth_token.startswith("sk-ant-"):
        raise HTTPException(status_code=400, detail="oauth_token should be a token starting with 'sk-ant-' from `claude setup-token`")
    import re
    if not re.fullmatch(r'sk-ant-[A-Za-z0-9_\-]+', oauth_token):
        raise HTTPException(status_code=400, detail="oauth_token contains invalid characters")

    claude_config_json = payload.get("claude_config_json")
    if claude_config_json is not None:
        import json as _json
        try:
            _json.loads(claude_config_json)
        except (ValueError, TypeError):
            raise HTTPException(status_code=400, detail="claude_config_json must be valid JSON")

    now = _utcnow()
    db_account = MaxPlanAccount(
        name=name,
        oauth_token=oauth_token,  # Raw token saved temporarily for ID generation
        claude_config_json=claude_config_json,
        token_created_at=now,
        user_id=current_user["user_id"],
    )
    session.add(db_account)
    await session.commit()
    await session.refresh(db_account)

    # Store token in Secret Manager, save resource name in DB
    try:
        from app.secrets_manager import store_user_secret
        secret_name = f"maxplan-{db_account.id}"
        resource_name = await store_user_secret("system", secret_name, oauth_token)
        db_account.oauth_token = resource_name
        session.add(db_account)
        await session.commit()
    except Exception as e:
        logger.warning(f"[POOL] Secret Manager unavailable, token stored in DB directly: {e}")
        # Fallback: raw token stays in DB (backward compat)

    # Sync to in-memory pool (always use the raw token for runtime)
    from app.max_plan_pool import _max_plan_pool_lock
    entry = {
        "id": str(db_account.id),
        "user_id": current_user["user_id"],
        "name": db_account.name,
        "oauth_token": oauth_token,  # In-memory pool always holds the resolved token
        "claude_config_json": db_account.claude_config_json,
        "enabled": db_account.enabled,
        "active_tasks": 0,
        "total_tasks": db_account.total_tasks,
        "token_created_at": now.isoformat(),
    }
    async with _max_plan_pool_lock:
        _max_plan_pools.setdefault(current_user["user_id"], []).append(entry)
        _account_by_id[str(db_account.id)] = entry
    return {"id": str(db_account.id), "name": db_account.name, "message": "Account added to pool"}


@router.delete("/max-plan-accounts/{account_id}")
@limiter.limit("10/minute")
async def remove_max_plan_account(
    request: Request,
    account_id: str,
    force: bool = Query(False),
    session: AsyncSession = Depends(get_session),
    current_user: dict = Depends(get_current_user)
):
    from app.max_plan_pool import _max_plan_pools, _account_by_id

    # Delete from database
    try:
        acct_uuid = uuid.UUID(account_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid account ID")
    db_account = await session.get(MaxPlanAccount, acct_uuid)
    if not db_account:
        raise HTTPException(status_code=404, detail="Account not found")
    if db_account.user_id != current_user["user_id"]:
        raise HTTPException(status_code=404, detail="Account not found")

    # Check for active tasks using this account (PROVISIONING and IN_PROGRESS both hold slots)
    active_using = (await session.execute(
        select(Task.id).where(
            Task.status.in_([TaskStatus.IN_PROGRESS, TaskStatus.PROVISIONING]),
            Task.context["max_plan_account_id"].as_string() == account_id
        )
    )).scalars().all()
    if active_using and not force:
        raise HTTPException(
            status_code=409,
            detail=f"Account has {len(active_using)} active task(s). "
                   f"Pass ?force=true to remove anyway (tasks will fail on next token refresh)."
        )
    if active_using and force:
        logger.warning(f"[POOL] Force-deleting account {account_id} with {len(active_using)} active tasks — slots will reconcile next cycle")

    # Clean up Secret Manager secret if stored there
    if db_account.oauth_token and db_account.oauth_token.startswith("projects/"):
        try:
            from app.secrets_manager import delete_user_secret
            await delete_user_secret(db_account.oauth_token)
        except Exception as e:
            logger.warning(f"[POOL] Failed to delete Secret Manager secret for account {account_id}: {e}")

    await session.delete(db_account)
    await session.commit()

    # Remove from in-memory pool
    from app.max_plan_pool import _max_plan_pool_lock
    async with _max_plan_pool_lock:
        user_id = current_user["user_id"]
        if user_id in _max_plan_pools:
            _max_plan_pools[user_id] = [a for a in _max_plan_pools[user_id] if a["id"] != account_id]
        _account_by_id.pop(account_id, None)
    return {"message": "Account removed"}


@router.patch("/max-plan-accounts/{account_id}")
async def update_max_plan_account(
    account_id: str,
    payload: Dict[str, Any],
    session: AsyncSession = Depends(get_session),
    current_user: dict = Depends(get_current_user)
):
    from app.max_plan_pool import _account_by_id

    # Update in database
    try:
        acct_uuid = uuid.UUID(account_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid account ID")
    db_account = await session.get(MaxPlanAccount, acct_uuid)
    if not db_account:
        raise HTTPException(status_code=404, detail="Account not found")
    if db_account.user_id != current_user["user_id"]:
        raise HTTPException(status_code=404, detail="Account not found")

    if "enabled" in payload:
        db_account.enabled = bool(payload["enabled"])
    if "name" in payload:
        db_account.name = payload["name"]
    _raw_new_token = None  # Track raw token for in-memory pool sync
    if "oauth_token" in payload:
        new_token = payload["oauth_token"]
        if not new_token.startswith("sk-ant-"):
            raise HTTPException(status_code=400, detail="oauth_token should start with 'sk-ant-'")
        import re
        if not re.fullmatch(r'sk-ant-[A-Za-z0-9_\-]+', new_token):
            raise HTTPException(status_code=400, detail="oauth_token contains invalid characters")
        _raw_new_token = new_token
        # Store in Secret Manager, save resource name in DB
        try:
            from app.secrets_manager import store_user_secret
            secret_name = f"maxplan-{account_id}"
            resource_name = await store_user_secret("system", secret_name, new_token)
            db_account.oauth_token = resource_name
        except Exception as e:
            logger.warning(f"[POOL] Secret Manager unavailable, token stored in DB directly: {e}")
            db_account.oauth_token = new_token
        db_account.token_created_at = _utcnow()
    if "claude_config_json" in payload:
        import json as _json
        try:
            _json.loads(payload["claude_config_json"])
        except (ValueError, TypeError):
            raise HTTPException(status_code=400, detail="claude_config_json must be valid JSON")
        db_account.claude_config_json = payload["claude_config_json"]
    db_account.updated_at = _utcnow()
    session.add(db_account)

    # Commit and sync to in-memory pool atomically under lock to prevent
    # dispatch seeing stale state between DB commit and pool update
    from app.max_plan_pool import _max_plan_pool_lock
    async with _max_plan_pool_lock:
        await session.commit()
        mem_account = _account_by_id.get(account_id)
        if mem_account:
            if "enabled" in payload:
                mem_account["enabled"] = db_account.enabled
            if "name" in payload:
                mem_account["name"] = db_account.name
            if "oauth_token" in payload:
                mem_account["oauth_token"] = _raw_new_token  # In-memory pool holds resolved token, not resource name
                mem_account["token_created_at"] = db_account.token_created_at.isoformat() if db_account.token_created_at else None
            if "claude_config_json" in payload:
                mem_account["claude_config_json"] = db_account.claude_config_json

    return {"id": account_id, "name": db_account.name, "enabled": db_account.enabled}


@router.post("/max-plan-accounts/{account_id}/reset-rate-limits")
async def reset_account_rate_limits(
    account_id: str,
    session: AsyncSession = Depends(get_session),
    current_user: dict = Depends(get_current_user)
):
    """Clear all rate-limit state (disabled_models) for a Max plan account."""
    from app.max_plan_pool import _account_by_id, _max_plan_pool_lock, _pending_rl_persists

    try:
        acct_uuid = uuid.UUID(account_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid account ID")
    db_account = await session.get(MaxPlanAccount, acct_uuid)
    if not db_account:
        raise HTTPException(status_code=404, detail="Account not found")
    if db_account.user_id != current_user["user_id"]:
        raise HTTPException(status_code=404, detail="Account not found")

    # Clear in-memory rate-limit state under lock
    async with _max_plan_pool_lock:
        mem_account = _account_by_id.get(account_id)
        if mem_account:
            mem_account.pop("disabled_models", None)
            mem_account["enabled"] = True
            mem_account.pop("disabled_at", None)
            mem_account.pop("rl_hit_count", None)

    # Clear DB rate-limit state
    db_account.rate_limit_state = None
    db_account.enabled = True
    db_account.updated_at = _utcnow()
    session.add(db_account)
    await session.commit()

    # Clear from pending persist retry queue
    if account_id in _pending_rl_persists:
        _pending_rl_persists.remove(account_id)

    logger.info(f"[POOL] Rate-limit state reset for account {db_account.name} ({account_id}) by user {current_user['user_id']}")
    return {"message": f"Rate-limit state cleared for account {db_account.name}"}


# --- GitHub Repository & Branch Discovery ---

async def _get_installation_token_for_user(user_id: str, session: AsyncSession) -> str:
    """
    Retrieves a GitHub installation access token for the given user.
    Raises HTTPException if no integration or installation is found.
    """
    stmt = select(UserIntegration).where(UserIntegration.user_id == user_id)
    integration = (await session.execute(stmt)).scalars().first()

    if not integration:
        raise HTTPException(
            status_code=400,
            detail="No GitHub integration found. Please connect your GitHub account first."
        )

    if integration.github_installation_id:
        try:
            token = await generate_installation_access_token(integration.github_installation_id)
            return token
        except HTTPException:
            raise HTTPException(
                status_code=502,
                detail="Failed to generate GitHub installation token. The installation may have been removed."
            )

    raise HTTPException(
        status_code=400,
        detail="No GitHub App installation found for your account. Please install the GitHub App first."
    )


@router.get("/github/repos")
async def list_github_repos(
    session: AsyncSession = Depends(get_session),
    current_user: dict = Depends(get_current_user)
):
    """
    Lists repositories accessible via the current user's GitHub App installation.
    Returns a sorted list of {full_name, default_branch} objects.
    """
    token = await _get_installation_token_for_user(current_user["user_id"], session)

    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github.v3+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }

    repos = []
    url = "https://api.github.com/installation/repositories?per_page=100"

    async with httpx.AsyncClient(timeout=15.0) as client:
        while url:
            resp = await client.get(url, headers=headers)
            if resp.status_code != 200:
                logger.error(f"GitHub API error listing repos: {resp.status_code} {resp.text}")
                raise HTTPException(
                    status_code=502,
                    detail="Failed to fetch repositories from GitHub."
                )
            data = resp.json()
            for r in data.get("repositories", []):
                repos.append({
                    "full_name": r["full_name"],
                    "default_branch": r.get("default_branch", "main"),
                })
            # Follow pagination
            url = resp.links.get("next", {}).get("url")

    repos.sort(key=lambda r: r["full_name"].lower())
    return {"repositories": repos}


@router.get("/github/branches")
async def list_github_branches(
    repo: str = Query(..., pattern=r"^[a-zA-Z0-9_.-]+/[a-zA-Z0-9_.-]+$"),
    session: AsyncSession = Depends(get_session),
    current_user: dict = Depends(get_current_user)
):
    """
    Lists branches for a specific repository accessible via the user's GitHub App installation.
    Query param: repo=owner/repo
    Returns a sorted list of branch names.
    """
    token = await _get_installation_token_for_user(current_user["user_id"], session)

    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github.v3+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }

    branches = []
    url = f"https://api.github.com/repos/{repo}/branches?per_page=100"

    async with httpx.AsyncClient(timeout=15.0) as client:
        while url:
            resp = await client.get(url, headers=headers)
            if resp.status_code == 404:
                raise HTTPException(
                    status_code=404,
                    detail=f"Repository '{repo}' not found or not accessible."
                )
            if resp.status_code != 200:
                logger.error(f"GitHub API error listing branches for {repo}: {resp.status_code} {resp.text}")
                raise HTTPException(
                    status_code=502,
                    detail="Failed to fetch branches from GitHub."
                )
            data = resp.json()
            for b in data:
                branches.append(b["name"])
            # Follow pagination
            url = resp.links.get("next", {}).get("url")

    branches.sort(key=str.lower)
    return {"branches": branches}


@router.post("/restart-orchestrator")
async def restart_orchestrator(
    current_user: dict = Depends(require_admin)
):
    """Restart the orchestrator background task without restarting the entire service."""
    import asyncio
    from app.shared_state import background_tasks
    from app.orchestrator import orchestrator_worker

    old_task = background_tasks.get("orchestrator")
    if old_task and not old_task.done():
        old_task.cancel()
        try:
            await old_task
        except asyncio.CancelledError:
            pass

    background_tasks["orchestrator"] = asyncio.create_task(orchestrator_worker())
    logger.info("[ADMIN] Orchestrator restarted via admin endpoint")
    return {"status": "restarted"}


@router.post("/repair-state")
async def repair_state(
    session: AsyncSession = Depends(get_session),
    current_user: dict = Depends(require_admin)
):
    """Post-rollback state audit and repair. Fixes stuck tasks, orphaned parents, stale evaluating tasks."""
    from app.models import TaskHistory
    repairs = []

    # 1. Tasks stuck in PROVISIONING/IN_PROGRESS with no heartbeat for 15+ min → FAILED
    fifteen_min_ago = _utcnow() - timedelta(minutes=15)
    stuck_tasks = (await session.execute(
        select(Task).where(
            Task.status.in_([TaskStatus.PROVISIONING, TaskStatus.IN_PROGRESS]),
            Task.vm_instance_id.isnot(None),
        )
    )).scalars().all()

    for t in stuck_tasks:
        cutoff = t.last_heartbeat or t.started_at or t.created_at
        if cutoff and cutoff < fifteen_min_ago:
            original_status = t.status  # Capture before overwriting
            t.status = TaskStatus.FAILED
            t.latest_status_message = f"Repaired: stuck in {original_status} with no heartbeat since {cutoff.isoformat()}"
            session.add(t)
            session.add(TaskHistory(task_id=t.id, action="REPAIR", details={"reason": "stuck_no_heartbeat", "original_status": str(original_status)}))
            repairs.append(f"Failed stuck task {t.id}")

            # Clean up VM
            if t.vm_instance_id:
                from app.main import _ensure_vm_deleted
                await _ensure_vm_deleted(t, reason="admin_repair")

    # 2. EVALUATING tasks where all children are terminal but parent not updated
    evaluating_tasks = (await session.execute(
        select(Task).where(Task.status == TaskStatus.EVALUATING)
    )).scalars().all()

    for parent in evaluating_tasks:
        children = (await session.execute(
            select(Task).where(Task.parent_task_id == parent.id)
        )).scalars().all()

        if not children:
            continue

        all_terminal = all(c.status in [TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.CANCELLED] for c in children)
        if all_terminal:
            # Re-trigger rollup by setting back to EVALUATING (the orchestrator will pick it up)
            parent.latest_status_message = "Repaired: all children terminal, re-triggering rollup"
            session.add(parent)
            session.add(TaskHistory(task_id=parent.id, action="REPAIR", details={"reason": "stale_evaluating"}))
            repairs.append(f"Re-triggered rollup for {parent.id}")

    # 3. Tasks with parent_task_id pointing to non-existent tasks
    all_task_ids = {str(t.id) for t in (await session.execute(select(Task.id))).scalars().all()}
    orphan_children = (await session.execute(
        select(Task).where(Task.parent_task_id.isnot(None))
    )).scalars().all()

    for t in orphan_children:
        if str(t.parent_task_id) not in all_task_ids:
            t.parent_task_id = None
            t.latest_status_message = "Repaired: cleared orphaned parent reference"
            session.add(t)
            session.add(TaskHistory(task_id=t.id, action="REPAIR", details={"reason": "orphaned_parent_ref"}))
            repairs.append(f"Cleared orphaned parent ref on {t.id}")

    # 4. Circuit breakers stuck in HALF_OPEN with a deleted/non-existent test task
    from app.main import _maybe_create_circuit_breaker_alert, _utcnow as _main_utcnow
    for proj_id, breaker in list(_circuit_breakers.items()):
        if breaker.get("state") != "HALF_OPEN":
            continue
        test_id = breaker.get("half_open_task_id")
        if not test_id:
            continue
        try:
            test_task = await session.get(Task, uuid.UUID(test_id))
        except Exception:
            test_task = None
        if test_task is None:
            breaker["state"] = "OPEN"
            breaker["opened_at"] = _main_utcnow()
            breaker["cooldown_seconds"] = min(breaker["cooldown_seconds"] * 2, 7200)
            breaker["half_open_task_id"] = None
            logger.warning(f"[ADMIN REPAIR] Circuit breaker for {proj_id} stuck in HALF_OPEN — test task {test_id} not found, resetting to OPEN")
            try:
                project_uuid = uuid.UUID(proj_id)
                await _maybe_create_circuit_breaker_alert(
                    project_uuid, session,
                    f"HALF_OPEN test task deleted/reaped (admin repair) — circuit breaker re-opened (cooldown: {breaker['cooldown_seconds']}s)"
                )
            except Exception:
                pass
            repairs.append(f"Reset HALF_OPEN circuit breaker for project {proj_id} (test task {test_id} not found)")

    await session.commit()
    logger.warning(f"[ADMIN] State repair completed: {len(repairs)} repairs")
    return {"repairs": repairs, "count": len(repairs)}


# ── Autonomy Debug ────────────────────────────────────────────────────────────

@router.get("/projects/{project_id}/autonomy-debug")
async def get_autonomy_debug(
    project_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    current_user: dict = Depends(get_current_user),
):
    """Return a detailed snapshot of why autonomy is or isn't spawning for a project."""
    from app.shared_state import runtime_config as _runtime_config, get_circuit_breaker
    from app.max_plan_pool import _max_plan_pools, _account_available_for_model
    import app.shared_state as _shared_state_mod

    project = await _get_user_project(project_id, current_user["user_id"], session)

    now = _utcnow()
    blocking_guards = []

    # Guard 0: no available Max Plan accounts
    user_pool = _max_plan_pools.get(project.user_id, [])
    research_model = _runtime_config.get("task_type_models", {}).get("research")
    has_available = any(_account_available_for_model(a, research_model) for a in user_pool) or bool(os.getenv("ANTHROPIC_API_KEY"))
    if not has_available:
        blocking_guards.append({
            "guard": "no_available_accounts",
            "reason": f"No Max Plan accounts available for research model ({research_model})",
            "pool_size": len(user_pool),
        })

    # Guard 1: autonomous_disabled
    if not project.autonomous_enabled:
        blocking_guards.append({
            "guard": "autonomous_disabled",
            "reason": "project.autonomous_enabled is False",
        })

    # Guard 2: circuit_breaker_open
    raw_breaker = get_circuit_breaker(str(project_id))
    breaker_state = raw_breaker["state"]
    cooldown_ends_at = None
    if raw_breaker.get("opened_at"):
        cooldown_end = raw_breaker["opened_at"] + timedelta(seconds=raw_breaker["cooldown_seconds"])
        cooldown_ends_at = cooldown_end.isoformat()
        if breaker_state == "OPEN" and now < cooldown_end:
            blocking_guards.append({
                "guard": "circuit_breaker_open",
                "reason": "Circuit breaker is OPEN and cooldown has not elapsed",
                "opened_at": raw_breaker["opened_at"].isoformat(),
                "cooldown_ends_at": cooldown_ends_at,
                "failure_count": raw_breaker["failure_count"],
            })

    circuit_breaker_info = {
        "state": breaker_state,
        "failure_count": raw_breaker["failure_count"],
        "success_count": raw_breaker["success_count"],
        "opened_at": raw_breaker["opened_at"].isoformat() if raw_breaker.get("opened_at") else None,
        "cooldown_seconds": raw_breaker["cooldown_seconds"],
        "cooldown_ends_at": cooldown_ends_at,
    }

    # Guard 3: no_eligible_directions
    cooldown_minutes = _runtime_config.get("autonomous_direction_cooldown_minutes", 240)
    cooldown_seconds_dir = cooldown_minutes * 60
    directions_info = []
    for d in (project.directions or []):
        if d.get("status") != "active":
            continue
        last_researched_at = d.get("last_researched_at")
        if last_researched_at:
            last_dt = datetime.fromisoformat(last_researched_at)
            elapsed = (now - last_dt).total_seconds()
            remaining = cooldown_seconds_dir - elapsed
            is_eligible = remaining <= 0
            seconds_until_eligible = max(0, int(remaining)) if not is_eligible else None
        else:
            is_eligible = True
            seconds_until_eligible = None
        directions_info.append({
            "id": d.get("id"),
            "text": d.get("text", ""),
            "status": d.get("status", "active"),
            "last_researched_at": last_researched_at,
            "cooldown_minutes": cooldown_minutes,
            "is_eligible": is_eligible,
            "seconds_until_eligible": seconds_until_eligible,
        })

    eligible_directions = [d for d in directions_info if d["is_eligible"]]
    if not eligible_directions:
        # Either no active directions at all, or all are in cooldown
        min_wait = min(
            (d["seconds_until_eligible"] for d in directions_info if d["seconds_until_eligible"] is not None),
            default=None,
        )
        reason = (
            "No active directions exist"
            if not directions_info
            else "All active directions are still in cooldown"
        )
        blocking_guards.append({
            "guard": "no_eligible_directions",
            "reason": reason,
            "time_until_eligible_seconds": min_wait,
        })

    # Guard 4: research_task_active — only block on actively running research,
    # not EVALUATING parents (they're just waiting on children to finish)
    active_tasks_result = await session.execute(
        select(Task).where(
            Task.project_id == project_id,
            Task.status.in_([TaskStatus.QUEUED, TaskStatus.PROVISIONING, TaskStatus.IN_PROGRESS]),
        )
    )
    active_tasks = active_tasks_result.scalars().all()
    active_research_count = 0
    busy_direction_ids: list[str] = []
    for t in active_tasks:
        payload = t.first_payload
        if payload.get("type") == "research":
            active_research_count += 1
            did = payload.get("current_direction_id")
            if did:
                busy_direction_ids.append(did)
    max_parallel = _runtime_config.get("max_parallel_research_per_project", 2)
    if active_research_count >= max_parallel:
        blocking_guards.append({
            "guard": "research_task_active",
            "reason": f"Active research ({active_research_count}) >= max_parallel ({max_parallel})",
            "active_research_count": active_research_count,
            "max_parallel": max_parallel,
        })
    if busy_direction_ids:
        # Mark busy directions in the directions info for visibility
        for d in directions_info:
            if d["id"] in busy_direction_ids:
                d["is_eligible"] = False
                d["busy_reason"] = "direction has active research task"

    # Guard 5: rate_limit_exceeded
    one_hour_ago = now - timedelta(hours=1)
    recent_tasks_result = await session.execute(
        select(Task).where(
            Task.project_id == project_id,
            Task.created_by == "ORCHESTRATOR",
            Task.created_at >= one_hour_ago,
        )
    )
    recent_tasks = recent_tasks_result.scalars().all()
    tasks_this_hour = sum(1 for t in recent_tasks if (t.first_payload).get("type") == "research")
    max_per_hour = _runtime_config.get("max_autonomous_tasks_per_hour", 5)
    if tasks_this_hour >= max_per_hour:
        blocking_guards.append({
            "guard": "rate_limit_exceeded",
            "reason": f"tasks_this_hour ({tasks_this_hour}) >= max_per_hour ({max_per_hour})",
            "tasks_this_hour": tasks_this_hour,
            "max_per_hour": max_per_hour,
        })

    # Guard 6: waiting_for_interval — interval gate hasn't elapsed since last_idle_check
    interval_minutes = _runtime_config.get("autonomous_research_interval_minutes", 30)
    last_idle_check = _shared_state_mod.last_idle_check
    if last_idle_check:
        interval_elapsed = (now - last_idle_check).total_seconds() >= interval_minutes * 60
        next_check_at = (last_idle_check + timedelta(minutes=interval_minutes)).isoformat()
    else:
        interval_elapsed = True
        next_check_at = now.isoformat()

    if not interval_elapsed:
        secs_until_check = max(0, int((last_idle_check + timedelta(minutes=interval_minutes) - now).total_seconds()))
        blocking_guards.append({
            "guard": "waiting_for_interval",
            "reason": f"Research interval ({interval_minutes}m) has not elapsed since last check",
            "next_check_at": next_check_at,
            "seconds_until_check": secs_until_check,
        })

    return {
        "spawn_blocked": len(blocking_guards) > 0,
        "blocking_guards": blocking_guards,
        "circuit_breaker": circuit_breaker_info,
        "rate_limit": {
            "tasks_this_hour": tasks_this_hour,
            "max_per_hour": max_per_hour,
        },
        "directions": directions_info,
        "next_check_at": next_check_at,
        "strategic_cycle_history": (project.vision or {}).get("_strategic_cycle_history", []),
    }


@router.post("/projects/{project_id}/circuit-breaker/reset")
async def reset_circuit_breaker(
    project_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    current_user: dict = Depends(get_current_user),
):
    """Manually reset a project's circuit breaker to CLOSED state."""
    await _get_user_project_with_role(project_id, current_user["user_id"], session, "member")
    import app.shared_state as _shared_state
    from app.orchestrator import _persist_circuit_breakers

    key = str(project_id)
    breaker = _shared_state.circuit_breakers.get(key)
    if not breaker:
        raise HTTPException(status_code=404, detail="No circuit breaker state found for this project")

    base_cooldown = breaker.get("base_cooldown", 1800)
    breaker["state"] = "CLOSED"
    breaker["failure_count"] = 0
    breaker["cooldown_seconds"] = base_cooldown
    breaker["opened_at"] = None
    breaker["half_open_task_id"] = None

    await _persist_circuit_breakers()

    # Also clear strategic cycle state so the cycle can recover
    from app.models import Project
    from sqlalchemy.orm.attributes import flag_modified
    project = await session.get(Project, project_id)
    if project and project.vision:
        vision_keys_to_clear = {"_strategic_failures", "_pending_strategic_review"}
        if vision_keys_to_clear & project.vision.keys():
            project.vision = {**project.vision}
            for key in vision_keys_to_clear:
                project.vision.pop(key, None)
            flag_modified(project, "vision")
            session.add(project)
            await session.commit()
            logger.info(f"[ADMIN] Cleared strategic cycle state (_strategic_failures, _pending_strategic_review) for project {project_id}")

    user_email = current_user.get("email", current_user.get("sub", "unknown"))
    logger.info(f"[ADMIN] Circuit breaker manually reset for project {project_id} by {user_email}")

    await broadcast_to_project(key, "autonomy_updated", {"project_id": key, "circuit_breaker_reset": True})

    return {
        "project_id": key,
        "state": breaker["state"],
        "failure_count": breaker["failure_count"],
        "cooldown_seconds": breaker["cooldown_seconds"],
        "opened_at": None,
        "half_open_task_id": None,
    }


# ---------------------------------------------------------------------------
# GET /api/admin/projects/{project_id}/diagnostics
# ---------------------------------------------------------------------------

@router.get("/projects/{project_id}/diagnostics")
async def get_project_diagnostics(
    project_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    current_user: dict = Depends(get_current_user),
):
    """Return a structured health snapshot for a single project's pipeline."""
    from app.shared_state import get_circuit_breaker
    from app.compute import get_zone_health_summary

    project = await _get_user_project(project_id, current_user["user_id"], session)

    now = _utcnow()

    active_statuses = [TaskStatus.QUEUED, TaskStatus.PROVISIONING, TaskStatus.IN_PROGRESS, TaskStatus.EVALUATING]
    active_result = await session.execute(
        select(Task).where(
            Task.project_id == project_id,
            Task.status.in_(active_statuses),
            Task.archived.is_(False),
        )
    )
    active_tasks = active_result.scalars().all()

    stuck_tasks = await _build_stuck_tasks(active_tasks, now, session)

    queued_count = sum(1 for t in active_tasks if t.status == TaskStatus.QUEUED)
    in_progress_count = sum(1 for t in active_tasks if t.status == TaskStatus.IN_PROGRESS)

    cb_raw = get_circuit_breaker(str(project_id))
    circuit_breaker = _format_circuit_breaker(cb_raw)

    try:
        zone_health = get_zone_health_summary()
    except Exception:
        zone_health = {"available": 0, "exhausted": []}

    return {
        "project_id": str(project_id),
        "stuck_tasks": stuck_tasks,
        "circuit_breaker": circuit_breaker,
        "queued_count": queued_count,
        "in_progress_count": in_progress_count,
        "zone_health": zone_health,
    }


# ---------------------------------------------------------------------------
# GET /api/admin/diagnostics  (aggregated across all projects)
# ---------------------------------------------------------------------------

@router.get("/diagnostics")
async def get_global_diagnostics(
    session: AsyncSession = Depends(get_session),
    current_user: dict = Depends(require_admin),
):
    """Aggregate pipeline diagnostics across all projects."""
    from app.shared_state import get_circuit_breaker, circuit_breakers
    from app.compute import get_zone_health_summary

    now = _utcnow()

    active_statuses = [TaskStatus.QUEUED, TaskStatus.PROVISIONING, TaskStatus.IN_PROGRESS, TaskStatus.EVALUATING]
    active_result = await session.execute(
        select(Task).where(
            Task.status.in_(active_statuses),
            Task.archived.is_(False),
        )
    )
    active_tasks = active_result.scalars().all()

    # Group tasks by project
    by_project: Dict[str, List[Task]] = {}
    for task in active_tasks:
        pid = str(task.project_id)
        by_project.setdefault(pid, []).append(task)

    all_stuck: List[StuckTaskInfo] = []
    project_summaries: List[ProjectSummary] = []
    total_queued = 0
    total_in_progress = 0

    for pid, tasks in by_project.items():
        stuck = await _build_stuck_tasks(tasks, now, session)
        queued = sum(1 for t in tasks if t.status == TaskStatus.QUEUED)
        in_prog = sum(1 for t in tasks if t.status == TaskStatus.IN_PROGRESS)
        total_queued += queued
        total_in_progress += in_prog
        all_stuck.extend(stuck)

        cb_raw = get_circuit_breaker(pid)
        project_summaries.append({
            "project_id": pid,
            "stuck_count": len(stuck),
            "queued_count": queued,
            "in_progress_count": in_prog,
            "circuit_breaker_state": cb_raw.get("state", "CLOSED"),
        })

    # All known circuit breakers (includes projects with no active tasks)
    all_circuit_breakers = {
        pid: _format_circuit_breaker(cb)
        for pid, cb in circuit_breakers.items()
    }

    try:
        zone_health = get_zone_health_summary()
    except Exception:
        zone_health = {"available": 0, "exhausted": []}

    return {
        "stuck_tasks": all_stuck,
        "total_stuck_count": len(all_stuck),
        "queued_count": total_queued,
        "in_progress_count": total_in_progress,
        "projects": project_summaries,
        "circuit_breakers": all_circuit_breakers,
        "zone_health": zone_health,
    }


@router.get('/cost-summary')
async def cost_summary(
    session: AsyncSession = Depends(get_session),
    days: int = Query(7, ge=1, le=365),
    current_user: dict = Depends(require_admin),
):
    cutoff = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=days)
    tasks = (await session.execute(
        select(Task).where(Task.created_at >= cutoff)
    )).scalars().all()

    # e2-standard-2 pricing: ~$0.067/hr on-demand, ~$0.020/hr spot
    ON_DEMAND_RATE = 0.067 / 3600  # per second
    SPOT_RATE = 0.020 / 3600

    by_type = defaultdict(lambda: {'count': 0, 'total_seconds': 0, 'estimated_cost': 0.0})
    for t in tasks:
        if t.vm_provisioned_at and t.vm_stopped_at:
            seconds = (t.vm_stopped_at - t.vm_provisioned_at).total_seconds()
            rate = SPOT_RATE if t.vm_spot else ON_DEMAND_RATE
            task_type = t.first_payload.get('type', 'unknown')
            by_type[task_type]['count'] += 1
            by_type[task_type]['total_seconds'] += seconds
            by_type[task_type]['estimated_cost'] += seconds * rate

    return {
        'period_days': days,
        'by_task_type': dict(by_type),
        'total_estimated_cost': sum(v['estimated_cost'] for v in by_type.values()),
    }

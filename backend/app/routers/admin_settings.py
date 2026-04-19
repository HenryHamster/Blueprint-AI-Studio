from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select
from typing import Dict, Any
import os
import uuid
import httpx
import logging
from datetime import timedelta
from sqlalchemy.orm import aliased

from app.database import get_session
from app.shared_state import limiter, circuit_breakers as _circuit_breakers
from app.models import Task, TaskStatus, UserIntegration, DeploymentState, DeploymentStatus, _utcnow
from app.auth import get_current_user, require_admin
from app.github import generate_installation_access_token

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/api/admin",
    tags=["admin-settings"],
    dependencies=[Depends(get_current_user)]
)


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
        "enabled_events": config.get("enabled_events", ["TASK_FAILED", "APPROVAL_NEEDED", "DEPLOY_HEALTH_ALERT", "REMEDIATION_ESCALATED"]),
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
        "productive_direction_cooldown_minutes": _runtime_config.get("productive_direction_cooldown_minutes", 45),
        "max_parallel_research_per_project": _runtime_config.get("max_parallel_research_per_project", 3),
        "reserved_slots_for_types": _runtime_config.get("reserved_slots_for_types", {"create": 1, "research": 1}),
        "strategic_research_frequency": _runtime_config.get("strategic_research_frequency", 6),
        "github_account": github_account,
        "require_approval_gates": _runtime_config.get("require_approval_gates", True),
        "daily_cost_ceiling_usd": _runtime_config.get("daily_cost_ceiling_usd", 0),
        "webhook_url_configured": bool(os.getenv("PIPELINE_WEBHOOK_URL")),
        "notification_config": _build_notification_config_response(
            _runtime_config.get("notification_config", {
                "webhook_url": "",
                "enabled_events": ["TASK_FAILED", "APPROVAL_NEEDED", "DEPLOY_HEALTH_ALERT", "REMEDIATION_ESCALATED"],
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
        if not (0 <= val <= 100):
            raise HTTPException(status_code=400, detail="max_concurrent_vms must be between 0 and 100")
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
    # Legacy 4-6 entries accepted so stored runtime_config from prior revisions
    # validates without rewrite; new tasks should use claude-opus-4-7 only.
    VALID_MODELS = {"claude-opus-4-7", "claude-opus-4-6", "claude-sonnet-4-6", "claude-haiku-4-5-20251001", ""}
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
    if "productive_direction_cooldown_minutes" in payload:
        try:
            val = int(payload["productive_direction_cooldown_minutes"])
        except (ValueError, TypeError):
            raise HTTPException(status_code=400, detail="productive_direction_cooldown_minutes must be a number")
        if not (1 <= val <= 525600):
            raise HTTPException(status_code=400, detail="productive_direction_cooldown_minutes must be between 1 and 525600")
        _runtime_config["productive_direction_cooldown_minutes"] = val
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
    if "max_provision_retries" in payload:
        try:
            val = int(payload["max_provision_retries"])
        except (ValueError, TypeError):
            raise HTTPException(status_code=400, detail="max_provision_retries must be a number")
        if not (1 <= val <= 10):
            raise HTTPException(status_code=400, detail="max_provision_retries must be between 1 and 10")
        _runtime_config["max_provision_retries"] = val
    if "max_rollup_retries" in payload:
        try:
            val = int(payload["max_rollup_retries"])
        except (ValueError, TypeError):
            raise HTTPException(status_code=400, detail="max_rollup_retries must be a number")
        if not (1 <= val <= 20):
            raise HTTPException(status_code=400, detail="max_rollup_retries must be between 1 and 20")
        _runtime_config["max_rollup_retries"] = val
    if "architecture_review_frequency" in payload:
        try:
            val = int(payload["architecture_review_frequency"])
        except (ValueError, TypeError):
            raise HTTPException(status_code=400, detail="architecture_review_frequency must be a number")
        if not (0 <= val <= 100):
            raise HTTPException(status_code=400, detail="architecture_review_frequency must be between 0 and 100 (0 = disabled)")
        _runtime_config["architecture_review_frequency"] = val
    if "max_active_directions" in payload:
        try:
            val = int(payload["max_active_directions"])
        except (ValueError, TypeError):
            raise HTTPException(status_code=400, detail="max_active_directions must be a number")
        if not (1 <= val <= 50):
            raise HTTPException(status_code=400, detail="max_active_directions must be between 1 and 50")
        _runtime_config["max_active_directions"] = val
    if "max_parallel_research_per_project" in payload:
        try:
            val = int(payload["max_parallel_research_per_project"])
        except (ValueError, TypeError):
            raise HTTPException(status_code=400, detail="max_parallel_research_per_project must be a number")
        if not (1 <= val <= 20):
            raise HTTPException(status_code=400, detail="max_parallel_research_per_project must be between 1 and 20")
        _runtime_config["max_parallel_research_per_project"] = val
    if "reserved_slots_for_types" in payload:
        val = payload["reserved_slots_for_types"]
        if not isinstance(val, dict):
            raise HTTPException(status_code=400, detail="reserved_slots_for_types must be a dict")
        VALID_RESERVED_TYPES = {"create", "research", "plan"}
        normalized: Dict[str, int] = {}
        for type_key, count in val.items():
            if type_key not in VALID_RESERVED_TYPES:
                raise HTTPException(status_code=400, detail=f"reserved_slots_for_types: invalid task type {type_key}; must be one of {sorted(VALID_RESERVED_TYPES)}")
            try:
                cnt = int(count)
            except (ValueError, TypeError):
                raise HTTPException(status_code=400, detail=f"reserved_slots_for_types[{type_key}] must be a number")
            if not (0 <= cnt <= 10):
                raise HTTPException(status_code=400, detail=f"reserved_slots_for_types[{type_key}] must be between 0 and 10")
            normalized[type_key] = cnt
        _runtime_config["reserved_slots_for_types"] = normalized
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
            "PR_MERGED", "BUDGET_EXCEEDED", "AUTO_FIX_SPAWNED", "AUTO_FIX_MERGED",
            "AUTO_FIX_ESCALATED", "REMEDIATION_ESCALATED",
        ])
        enabled_events = val.get("enabled_events", ["TASK_FAILED", "APPROVAL_NEEDED", "DEPLOY_HEALTH_ALERT", "REMEDIATION_ESCALATED"])
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
                    "strategic_research_frequency": _runtime_config.get("strategic_research_frequency", 6),
                    "require_approval_gates": _runtime_config.get("require_approval_gates", True),
                    "daily_cost_ceiling_usd": _runtime_config.get("daily_cost_ceiling_usd", 0),
                    "max_provision_retries": _runtime_config.get("max_provision_retries", 3),
                    "max_rollup_retries": _runtime_config.get("max_rollup_retries", 5),
                    "architecture_review_frequency": _runtime_config.get("architecture_review_frequency", 3),
                    "max_active_directions": _runtime_config.get("max_active_directions", 10),
                    "max_parallel_research_per_project": _runtime_config.get("max_parallel_research_per_project", 3),
                    "reserved_slots_for_types": _runtime_config.get("reserved_slots_for_types", {"create": 1, "research": 1}),
                    "notification_config": _runtime_config.get("notification_config", {
                        "webhook_url": "",
                        "enabled_events": ["TASK_FAILED", "APPROVAL_NEEDED", "DEPLOY_HEALTH_ALERT", "REMEDIATION_ESCALATED"],
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
        logger.exception("[SETTINGS] DB persist failed, rolling back in-memory config")
        await session.rollback()
        raise HTTPException(status_code=500, detail=f"Failed to persist settings: {e}")

    return _runtime_config


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


@router.post("/github-quota/refresh")
async def refresh_github_quota(
    current_user: dict = Depends(require_admin),
):
    """Force an immediate probe of GitHub's /rate_limit endpoint.

    Returns the refreshed shared_state.github_quota dict so the UI can
    update without waiting for the next 30s health-detailed poll.
    Useful after the pill shows suspicious stale values (e.g. a
    remaining=0 reading that got stuck across a bucket switch).
    """
    import app.shared_state as _ss  # noqa: PLC0415
    from app.github import probe_rate_limit  # noqa: PLC0415
    ok = await probe_rate_limit()
    gq = dict(_ss.github_quota)
    remaining = gq.get("remaining")
    if remaining is None:
        status = "unknown"
    elif remaining < 100:
        status = "critical"
    elif remaining < 500:
        status = "degraded"
    else:
        status = "ok"
    gq["status"] = status
    return {"probed": bool(ok), "github_quota": gq}


@router.post("/github-quota/reset-tracking")
async def reset_github_quota_tracking(
    current_user: dict = Depends(require_admin),
):
    """Clear the in-memory quota tracking state.

    Forces the next _github_request response to repopulate from scratch —
    useful to unstick a stale `remaining` value from a polluted bucket
    observation (e.g. App-JWT contamination before the install-token
    filter deployed).
    """
    import app.shared_state as _ss  # noqa: PLC0415
    _ss.github_quota.update({
        "remaining": None, "limit": None, "reset_at": None, "last_updated": None,
    })
    _ss.github_call_counts.clear()
    _ss.github_call_counts_window_reset_at = None
    # Immediately probe to repopulate with a fresh observation.
    from app.github import probe_rate_limit  # noqa: PLC0415
    await probe_rate_limit()
    return {"status": "reset", "github_quota": dict(_ss.github_quota)}


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
            from app.failure_classification import persist_failure_category  # noqa: PLC0415
            persist_failure_category(t, "AGENT_TIMEOUT")
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
    ParentTask = aliased(Task)
    orphan_children = (await session.execute(
        select(Task).where(
            Task.parent_task_id.isnot(None),
            ~select(ParentTask.id)
            .where(ParentTask.id == Task.parent_task_id)
            .correlate(Task)
            .exists()
        )
    )).scalars().all()

    for t in orphan_children:
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
                logger.exception(f"[ADMIN REPAIR] Failed to create circuit breaker alert for {proj_id}")
            repairs.append(f"Reset HALF_OPEN circuit breaker for project {proj_id} (test task {test_id} not found)")

    await session.commit()
    logger.warning(f"[ADMIN] State repair completed: {len(repairs)} repairs")
    return {"repairs": repairs, "count": len(repairs)}


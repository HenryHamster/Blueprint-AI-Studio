"""Health check primitives for external dependency monitoring.

Extracted from deploy_health._check_external_health to keep deploy_health.py
under its function ceiling. Each function checks one external dependency,
updates the shared ext dict, and returns the dependency name if failed or
None if healthy.
"""

import logging
import os
from datetime import datetime, timedelta, timezone
from typing import Optional

import app.shared_state as _shared_state
from app.database import AsyncSessionLocal
from app.max_plan_pool import _OPUS_MODEL, _account_available_for_model, _account_by_id
from app.models import Task
from sqlmodel import select

logger = logging.getLogger("blueprint")

# Entries older than this in task_results are considered stale and ignored by
# health checks. This prevents an auto-pause/auto-resume deadlock: while paused
# (max_concurrent_vms=0) no new results are appended, so the last pre-pause
# failures would otherwise keep tripping checks forever.
_TASK_RESULT_STALENESS_MINUTES = 15


async def check_db_health(ext: dict) -> Optional[str]:
    """Check database connectivity. Returns 'db' if failed, None if healthy."""
    try:
        async with AsyncSessionLocal() as session:
            await session.execute(select(Task).limit(1))
        ext["db"] = True
        ext["errors"].pop("db", None)
        return None
    except Exception as e:
        logger.warning(f"[HEALTH] DB health check failed: {e}", exc_info=True)
        ext["db"] = False
        ext["errors"]["db"] = f"{type(e).__name__}: {str(e)[:200]}"
        return "db"


async def check_github_health(ext: dict, now: datetime) -> Optional[str]:
    """Check GitHub API health using passive detection with active fallback.

    Returns 'github_api' if failed, None if healthy.
    """
    github_stale_threshold = 60  # 1 minute — detect outages faster during idle
    github_age = (
        (now - _shared_state.last_github_success).total_seconds()
        if _shared_state.last_github_success
        else None
    )

    if github_age is not None and github_age < github_stale_threshold:
        # Recent success — GitHub is healthy, no active check needed
        ext["github_api"] = True
        ext["errors"].pop("github_api", None)
        return None

    # No recent success — either idle or actually down. Make a dedicated active check
    # using a separate short-lived client to avoid connection pool contention.
    check_method = "passive" if github_age is not None else "active (cold start)"
    try:
        import httpx
        from app.github import generate_github_app_jwt
        jwt_token = generate_github_app_jwt()
        async with httpx.AsyncClient(timeout=10.0) as health_client:
            resp = await health_client.get(
                "https://api.github.com/app",
                headers={"Authorization": f"Bearer {jwt_token}"},
            )
        ext["github_api"] = resp.status_code == 200
        if ext["github_api"]:
            _shared_state.last_github_success = now
            ext["errors"].pop("github_api", None)
            return None
        else:
            logger.warning(f"[HEALTH] GitHub API returned {resp.status_code}")
            ext["errors"]["github_api"] = f"HTTP {resp.status_code} (check: {check_method})"
            return "github_api"
    except Exception as e:
        logger.warning(f"[HEALTH] GitHub API health check failed: {type(e).__name__}: {e!r}")
        ext["github_api"] = False
        ext["errors"]["github_api"] = f"{type(e).__name__}: {str(e)[:200]} (check: {check_method})"
        return "github_api"


def check_claude_api_health(ext: dict) -> Optional[str]:
    """Check Claude API credential and account availability.

    Returns 'claude_api' if failed, None if healthy.
    """
    has_credentials = bool(_account_by_id) or bool(os.getenv("ANTHROPIC_API_KEY"))
    if not has_credentials:
        ext["claude_api"] = False
        return "claude_api"

    all_accounts = list(_account_by_id.values())
    if all_accounts:
        # Check if accounts have a token and are enabled. Don't use model=None
        # (conservative path treats any per-model disable as fully unavailable,
        # which causes one rate-limited model to poison the entire health check).
        # Per-model availability is enforced at dispatch time.
        available_accounts = [a for a in all_accounts if a.get("oauth_token") and a.get("enabled", True)]
        if not available_accounts:
            ext["claude_api"] = False
            disabled_names = [a["name"] for a in all_accounts]
            logger.warning(f"[HEALTH] All Claude API accounts unavailable: {', '.join(disabled_names)}")
            return "claude_api"
        # Also verify at least one account can dispatch on the primary model (Opus).
        # Without this, all-accounts-Opus-rate-limited reports healthy even though
        # no task can actually dispatch, defeating auto-pause.
        opus_available = [a for a in available_accounts if _account_available_for_model(a, _OPUS_MODEL)]
        if not opus_available:
            ext["claude_api"] = False
            logger.warning(
                f"[HEALTH] All Claude API accounts rate-limited for {_OPUS_MODEL} — "
                f"0/{len(available_accounts)} can dispatch primary model"
            )
            return "claude_api"
        ext["claude_api"] = True
        if len(opus_available) < len(available_accounts):
            logger.info(
                f"[HEALTH] Claude API degraded — {len(available_accounts) - len(opus_available)}/"
                f"{len(available_accounts)} accounts rate-limited for {_OPUS_MODEL}, "
                f"{len(opus_available)} still available"
            )
        return None
    else:
        # No account pool — using raw ANTHROPIC_API_KEY. Fall back to burst detection
        # since there's no per-account cooldown mechanism for a single key.
        recent_results = list(_shared_state.deploy_monitoring["task_results"])[-5:]
        cutoff = (datetime.now(timezone.utc) - timedelta(minutes=_TASK_RESULT_STALENESS_MINUTES)).isoformat()
        fresh_results = [r for r in recent_results if r[0] >= cutoff]
        rate_limit_count = sum(1 for r in fresh_results if r[2] == "RATE_LIMIT")
        if rate_limit_count >= 3:
            ext["claude_api"] = False
            logger.warning(
                f"[HEALTH] Claude API rate limited (single key) — {rate_limit_count}/5 recent tasks hit limits"
            )
            return "claude_api"
        else:
            ext["claude_api"] = True
            return None


def check_gcp_health(ext: dict) -> Optional[str]:
    """Check GCP health via recent provisioning success/failure tracking.

    Returns 'gcp' if failed, None if healthy.
    """
    cutoff = (datetime.now(timezone.utc) - timedelta(minutes=_TASK_RESULT_STALENESS_MINUTES)).isoformat()
    fresh_all = [r for r in _shared_state.deploy_monitoring["task_results"] if r[0] >= cutoff]
    recent_provisions = [r for r in fresh_all if r[2] == "PROVISION_FAILURE"]
    if len(recent_provisions) >= 3:
        # Check if ALL recent failures are provision failures (guard against empty filter)
        recent_all = fresh_all[-5:]
        recent_failures = [r for r in recent_all if r[1] == "failure"]
        if recent_failures and all(r[2] == "PROVISION_FAILURE" for r in recent_failures):
            ext["gcp"] = False
            return "gcp"
    ext["gcp"] = True
    return None


def log_zone_health_transitions(
    current_exhausted: set,
    previous_exhausted: set,
    zone_summary: dict,
) -> None:
    """Log zone_health_exhausted and zone_health_recovered events for zone transitions."""
    available_zones = zone_summary.get("available", 0)
    total_zones = zone_summary.get("total_zones", 0)
    for zone in current_exhausted - previous_exhausted:
        logger.warning(
            "zone_health_exhausted",
            extra={"zone": zone, "available_zones": available_zones, "total_zones": total_zones},
        )
    for zone in previous_exhausted - current_exhausted:
        logger.info(
            "zone_health_recovered",
            extra={"zone": zone, "available_zones": available_zones, "total_zones": total_zones},
        )

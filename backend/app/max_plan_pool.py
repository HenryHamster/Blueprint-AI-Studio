"""
Max Plan account pool — in-memory cache of DB-persisted Claude Code OAuth accounts.

Extracted from main.py. This module owns:
  - Global pool state (_max_plan_pools, _account_by_id, _max_plan_pool_lock, _pending_rl_persists)
  - RATE_LIMIT_KEYWORDS constant (shared with classify_failure in main.py and reaper.py)
  - Pool lifecycle: load_max_plan_pool, select_max_plan_account, release_max_plan_account,
    recheck_disabled_accounts
  - Helpers: _all_accounts, _account_available_for_model, _extract_task_model
"""

import asyncio
import logging
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from app.database import AsyncSessionLocal

logger = logging.getLogger(__name__)


def _utcnow() -> datetime:
    """Naive UTC timestamp, compatible with TIMESTAMP WITHOUT TIME ZONE columns."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


# --- Max Plan Account Pool ---
# In-memory cache of DB-persisted accounts. Keeps active_tasks count (runtime only).
# Per-user pools keyed by user_id. Flat lookup by account_id for O(1) release.
_max_plan_pools: Dict[str, List[Dict[str, Any]]] = {}  # user_id → accounts
_account_by_id: Dict[str, Dict[str, Any]] = {}  # account_id → account dict
_max_plan_pool_lock = asyncio.Lock()
_pending_rl_persists: List[str] = []  # Account IDs whose rate-limit state failed to persist

# Rate-limit keyword patterns — shared between classify_failure() and reaper
RATE_LIMIT_KEYWORDS = ["rate limit", "rate_limit", "rate-limit", "429", "credit", "billing",
                       "usage limit", "usage exhausted", "too many requests",
                       "resource_exhausted", "daily limit", "tokens per minute",
                       "quota exceeded", "exceeded your", "plan limit",
                       "requests per minute", "overloaded"]


# Backward compat: flat view for code that needs all accounts
def _all_accounts() -> List[Dict[str, Any]]:
    return list(_account_by_id.values())


async def load_max_plan_pool():
    """Load Max plan accounts from database into per-user pools on startup.
    Reconciles active_tasks from DB by counting tasks currently PROVISIONING/IN_PROGRESS."""
    from sqlmodel import select as sql_select
    from app.models import MaxPlanAccount, Task, TaskStatus

    async with AsyncSessionLocal() as session:
        result = await session.execute(sql_select(MaxPlanAccount))
        accounts = result.scalars().all()

        # Count actually active tasks per account to reconcile after restarts
        active_tasks = (await session.execute(
            sql_select(Task).where(Task.status.in_([TaskStatus.PROVISIONING, TaskStatus.IN_PROGRESS]))
        )).scalars().all()
        active_per_account: Dict[str, int] = {}
        for t in active_tasks:
            acct_id = (t.context or {}).get("max_plan_account_id")
            if acct_id:
                active_per_account[acct_id] = active_per_account.get(acct_id, 0) + 1

        _max_plan_pools.clear()
        _account_by_id.clear()
        for acct in accounts:
            acct_id = str(acct.id)
            # Resolve Secret Manager resource names to actual tokens
            token = acct.oauth_token
            if token and token.startswith("projects/"):
                try:
                    from app.secrets_manager import get_user_secret
                    token = await get_user_secret(token)
                except Exception as e:
                    logger.error(f"[POOL] Failed to fetch token from Secret Manager for account {acct.name}: {e}")
                    token = None
            entry = {
                "id": acct_id,
                "user_id": acct.user_id,
                "name": acct.name,
                "oauth_token": token,
                "claude_config_json": acct.claude_config_json,
                "enabled": acct.enabled,
                "active_tasks": active_per_account.get(acct_id, 0),
                "total_tasks": acct.total_tasks,
                "token_created_at": acct.token_created_at.isoformat() if acct.token_created_at else None,
            }
            # Restore rate-limit state from DB (skip expired cooldowns)
            rl_state = acct.rate_limit_state or {}
            db_disabled_models = rl_state.get("disabled_models", {})
            if db_disabled_models:
                now = _utcnow()
                restored_dm = {}
                for model_name, info in db_disabled_models.items():
                    at_str = info.get("at")
                    if at_str:
                        at_dt = datetime.fromisoformat(at_str)
                        hits = info.get("hits", 1)
                        cooldown_minutes = min(30 * (2 ** (hits - 1)), 240)
                        if (now - at_dt).total_seconds() < cooldown_minutes * 60:
                            restored_dm[model_name] = {"hits": hits, "at": at_dt}
                if restored_dm:
                    entry["disabled_models"] = restored_dm
                    logger.info(f"[STARTUP] Restored rate-limit state for {acct.name}: {list(restored_dm.keys())}")
            _max_plan_pools.setdefault(acct.user_id, []).append(entry)
            _account_by_id[acct_id] = entry
        total_active = sum(active_per_account.values())
        logger.info(f"[STARTUP] Loaded {len(_account_by_id)} Max plan accounts for {len(_max_plan_pools)} users ({total_active} active tasks reconciled)")


def _account_available_for_model(acct: dict, model: Optional[str]) -> bool:
    """Check if an account is available for the given model."""
    if not acct.get("oauth_token"):
        return False  # Token missing (Secret Manager failure at startup)
    if not acct.get("enabled", True):
        return False  # Entirely disabled (legacy path)
    dm = acct.get("disabled_models", {})
    if not model:
        # No model specified — allow the account. Per-model disables only block
        # tasks that explicitly request that model. Previously this blocked the
        # account entirely when ANY model was disabled, causing false rejections
        # (e.g., Opus rate-limit blocking all Sonnet/unknown-model tasks).
        return True
    return model not in dm


def _extract_task_model(task: Any) -> Optional[str]:
    """Extract which model a task uses from its payload."""
    payloads = task.payloads or []
    if payloads:
        return payloads[0].get("model")
    return None


async def select_max_plan_account(user_id: str, model: Optional[str] = None, blacklist: Optional[List[str]] = None) -> Optional[Dict[str, Any]]:
    """Select the Max plan account with the lowest active task count for a specific user.
    Filters by model availability — accounts with a specific model disabled are skipped.
    Pre-increments active_tasks atomically under lock to prevent races."""
    async with _max_plan_pool_lock:
        user_pool = _max_plan_pools.get(user_id, [])
        available = [
            a for a in user_pool
            if _account_available_for_model(a, model) and (not blacklist or a["id"] not in blacklist)
        ]
        if not available:
            return None
        account = min(available, key=lambda a: a["active_tasks"])
        account["active_tasks"] = account.get("active_tasks", 0) + 1
        return account


async def release_max_plan_account(account_id: str):
    """Decrement active tasks for an account when a task finishes."""
    async with _max_plan_pool_lock:
        account = _account_by_id.get(account_id)
        if account:
            if account["active_tasks"] <= 0:
                logger.warning(f"[POOL] Account {account['name']} ({account_id}) release underflow — active_tasks was {account['active_tasks']}")
            account["active_tasks"] = max(0, account["active_tasks"] - 1)


async def recheck_disabled_accounts():
    """Re-enable accounts/models that were disabled by rate limiting after cooldown.
    Checks both per-model disables and legacy full-account disables."""
    from app.models import MaxPlanAccount

    changed_accounts = []
    async with _max_plan_pool_lock:
        now = _utcnow()

        for acct in _account_by_id.values():
            # Per-model cooldown checks
            dm = acct.get("disabled_models", {})
            for model_name, info in list(dm.items()):
                disabled_at = info.get("at")
                if not disabled_at:
                    continue
                hits = info.get("hits", 1)
                cooldown_minutes = min(30 * (2 ** (hits - 1)), 240)
                if (now - disabled_at).total_seconds() >= cooldown_minutes * 60:
                    del dm[model_name]
                    changed_accounts.append(acct)
                    logger.warning(f"[POOL] Re-enabled account {acct['name']} model {model_name} after {cooldown_minutes:.0f}min cooldown (hit #{hits})")

            # Legacy full-account disable (backward compat)
            if not acct.get("enabled", True):
                disabled_at = acct.get("disabled_at")
                if not disabled_at:
                    continue
                rl_hit_count = acct.get("rl_hit_count", 1)
                cooldown_minutes = min(30 * (2 ** (rl_hit_count - 1)), 240)
                if (now - disabled_at).total_seconds() >= cooldown_minutes * 60:
                    acct["enabled"] = True
                    acct["disabled_at"] = None
                    changed_accounts.append(acct)
                    logger.warning(f"[POOL] Re-enabled account {acct['name']} after {cooldown_minutes:.0f}min cooldown (rl_hit #{rl_hit_count})")

    # Retry any previously failed rate-limit persists
    if _pending_rl_persists:
        retry_ids = list(_pending_rl_persists)
        for acct_id in retry_ids:
            acct = _account_by_id.get(acct_id)
            if acct and acct not in changed_accounts:
                changed_accounts.append(acct)

    # Persist changes to DB (outside lock to avoid holding it during IO)
    if changed_accounts:
        try:
            async with AsyncSessionLocal() as rl_session:
                for acct in changed_accounts:
                    db_acct = await rl_session.get(MaxPlanAccount, uuid.UUID(acct["id"]))
                    if db_acct:
                        dm = acct.get("disabled_models", {})
                        db_acct.rate_limit_state = {
                            "disabled_models": {
                                m: {"hits": i["hits"], "at": i["at"].isoformat()}
                                for m, i in dm.items()
                            }
                        } if dm else None
                        db_acct.enabled = acct.get("enabled", True)
                        rl_session.add(db_acct)
                await rl_session.commit()
            # Clear any retried accounts on success
            for acct in changed_accounts:
                if acct["id"] in _pending_rl_persists:
                    _pending_rl_persists.remove(acct["id"])
        except Exception as e:
            logger.warning(f"[POOL] Failed to persist rate-limit state changes: {e}")

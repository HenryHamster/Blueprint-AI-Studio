"""
Max Plan account pool — in-memory cache of DB-persisted Claude Code OAuth accounts.

Extracted from main.py. This module owns:
  - Global pool state (_max_plan_pools, _account_by_id, _max_plan_pool_lock, _pending_rl_persists)
  - RATE_LIMIT_PATTERNS constant (shared with classify_failure in main.py and reaper.py)
  - Pool lifecycle: load_max_plan_pool, select_max_plan_account, release_max_plan_account,
    recheck_disabled_accounts
  - Helpers: _all_accounts, _account_available_for_model, _extract_task_model
"""

import asyncio
import logging
import random
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List

from sqlalchemy.exc import SQLAlchemyError

from app.database import AsyncSessionLocal
from app.time_utils import _utcnow

logger = logging.getLogger(__name__)


# --- Max Plan Account Pool ---
# In-memory cache of DB-persisted accounts. Keeps active_tasks count (runtime only).
# Per-user pools keyed by user_id. Flat lookup by account_id for O(1) release.
_max_plan_pools: Dict[str, List[Dict[str, Any]]] = {}  # user_id → accounts
_account_by_id: Dict[str, Dict[str, Any]] = {}  # account_id → account dict
_max_plan_pool_lock = asyncio.Lock()
_pending_rl_persists: List[str] = []  # Account IDs whose rate-limit state failed to persist

# Exact substrings from Claude Code CLI / Anthropic API rate-limit messages.
# Shared between classify_failure() and any caller that needs to detect a real
# Claude rate-limit (vs GitHub 429, GCP quota, Anthropic 529 overload, etc).
#
# Sourced from verbatim error strings reported in anthropics/claude-code issues.
# Do NOT add loose keywords like "credit", "billing", "429", "quota exceeded",
# or "overloaded" — those caused false positives that disabled accounts for
# unrelated failures (GCP quota, credit-card setup errors, Anthropic 529s).
RATE_LIMIT_PATTERNS = [
    # Subscription / Max-plan quota exhausted (CLI messages)
    "you've hit your limit",                                 # "...· resets 5am (Asia/Seoul)"
    "you've hit your usage limit",
    "claude ai usage limit reached",
    "you're out of extra usage",
    "5-hour limit reached",
    # Anthropic API 429
    "api error: rate limit reached",                         # Claude Code's wrapper around 429
    "this request would exceed your account's rate limit",   # raw 429 JSON body
]

# Claude Max plan shares session quota across model tiers. Empirically:
#   - Opus 429 implies Sonnet is also rate-limited (shared session bucket);
#     not symmetric, since Opus has a stricter sub-bucket of its own.
#   - Haiku shares Opus's meter exactly — it does NOT have its own bucket,
#     so haiku tracking normalizes to opus via _normalize_rl_model().
_MODEL_TIER_CASCADE: Dict[str, tuple] = {
    "claude-opus-4-6": ("claude-opus-4-6", "claude-sonnet-4-6"),
    "claude-sonnet-4-6": ("claude-sonnet-4-6",),
}

_HAIKU_MODEL = "claude-haiku-4-5-20251001"
_OPUS_MODEL = "claude-opus-4-6"


def _normalize_rl_model(model: str | None) -> str | None:
    """Map Haiku to Opus for rate-limit tracking — they share the same budget.
    Used at every rate-limit code path that keys into disabled_models so Haiku
    tasks bookkeep against Opus's meter and Opus disable/clear automatically
    covers Haiku."""
    if model == _HAIKU_MODEL:
        return _OPUS_MODEL
    return model


def cascade_disabled_models(model: str) -> tuple:
    """Return every model that should be marked disabled when `model` hits a 429.
    Normalizes Haiku to Opus first (shared meter). Falls back to the single
    model for unknown names so new variants don't silently cascade to the
    whole account."""
    normalized = _normalize_rl_model(model)
    return _MODEL_TIER_CASCADE.get(normalized, (normalized,) if normalized else (model,))


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
                except Exception:
                    logger.exception(f"[POOL] Failed to fetch token from Secret Manager for account {acct.name}")
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


def _account_state_for_model(acct: dict, model: str | None, now: datetime | None = None) -> str:
    """Derive the circuit-breaker state for an (account, model) pair.

    Returns "healthy", "probing", or "unavailable":
    - "healthy"     — usable with full concurrency
    - "probing"     — cooldown elapsed but quota not yet verified via a live task;
                      must be serialized to one task at a time by the caller
    - "unavailable" — missing token, legacy-disabled, within cooldown, or
                      (conservative) model=None on an account with any disables

    Check is a direct lookup: only disabled_models[model] matters for model's
    state. The mark path (agent_status.py) writes all cascade entries up-front
    (Opus 429 → opus, sonnet, haiku entries); the clear path (on success)
    removes the cascade scope. Each entry is independent.
    """
    if not acct.get("oauth_token"):
        return "unavailable"
    if not acct.get("enabled", True):
        return "unavailable"
    dm = acct.get("disabled_models") or {}
    if not dm:
        return "healthy"
    if model is None:
        # Conservative: unknown model with any disable is unavailable. Preserves
        # the existing behavior for health checks and the autonomous-research gate.
        return "unavailable"
    # Haiku shares Opus's meter — look up Opus's entry.
    model = _normalize_rl_model(model)
    info = dm.get(model)
    if info is None:
        return "healthy"
    at_dt = info.get("at")
    if at_dt is None:
        return "unavailable"
    hits = info.get("hits", 1)
    cooldown_seconds = min(30 * (2 ** (hits - 1)), 240) * 60
    now = now or _utcnow()
    if (now - at_dt).total_seconds() < cooldown_seconds:
        return "unavailable"
    return "probing"


def _account_available_for_model(acct: dict, model: str | None) -> bool:
    """True if the account is either healthy OR probing for the given model.
    Probing accounts are serialized inside select_max_plan_account; outside
    callers (blacklist pruning, gates, health checks) treat them as usable."""
    return _account_state_for_model(acct, model) != "unavailable"


def _extract_task_model(task: Any) -> str | None:
    """Extract which model a task uses from its payload."""
    payloads = task.payloads or []
    if payloads:
        return payloads[0].get("model")
    return None


async def _persist_rate_limit_state(acct_id: str) -> None:
    """Persist a single account's in-memory disabled_models to DB.
    Caller MUST hold _max_plan_pool_lock. Raises on DB failure — caller is
    responsible for queuing retries via _pending_rl_persists."""
    from app.models import MaxPlanAccount
    acct = _account_by_id.get(acct_id)
    if acct is None:
        return
    async with AsyncSessionLocal() as rl_session:
        db_acct = await rl_session.get(MaxPlanAccount, uuid.UUID(acct_id))
        if db_acct is None:
            return
        dm = acct.get("disabled_models") or {}
        db_acct.rate_limit_state = {
            "disabled_models": {
                m: {"hits": i["hits"], "at": i["at"].isoformat()}
                for m, i in dm.items()
            }
        } if dm else None
        db_acct.enabled = acct.get("enabled", True)
        rl_session.add(db_acct)
        await rl_session.commit()


async def select_max_plan_account(user_id: str, model: str | None = None, blacklist: List[str] | None = None) -> Dict[str, Any] | None:
    """Select a Max plan account for a task. Two-tier circuit-breaker selection:

    1. Healthy accounts first, picked by min(active_tasks) for load balancing.
    2. Probing accounts (post-cooldown, unverified) second, but only if
       active_tasks == 0 — this serializes the first task after cooldown as a
       de-facto probe, preventing a thundering herd on a still-rate-limited
       account. If the probe succeeds, the COMPLETED path clears disabled_models
       (restoring full concurrency); if it 429s, the mark path extends cooldown.

    Pre-increments active_tasks atomically under lock."""
    async with _max_plan_pool_lock:
        user_pool = _max_plan_pools.get(user_id, [])
        healthy: List[Dict[str, Any]] = []
        probing: List[Dict[str, Any]] = []
        for a in user_pool:
            if blacklist and a["id"] in blacklist:
                continue
            state = _account_state_for_model(a, model)
            if state == "healthy":
                healthy.append(a)
            elif state == "probing" and a.get("active_tasks", 0) == 0:
                probing.append(a)

        if healthy:
            account = min(healthy, key=lambda a: a["active_tasks"])
        elif probing:
            # Deterministic by id so concurrent selects under the same lock get
            # distinct probes when multiple accounts are eligible.
            account = min(probing, key=lambda a: a["id"])
            logger.info(f"[POOL] Probing account {account['name']} for model {model} (cooldown elapsed, serializing as probe)")
        else:
            return None

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


async def disable_account_model(acct_id: str, model: str) -> None:
    """Mark an account's cascade of models as rate-limited and persist.

    Called from both agent_status.py (when the agent POSTs a RATE_LIMIT failure)
    and reaper.py (when the agent's pre-flight dies without POSTing). Both paths
    must update disabled_models so the orchestrator's blacklist pruning
    preserves the entry on the next dispatch cycle.

    Acquires _max_plan_pool_lock. On DB persist failure, appends to
    _pending_rl_persists for retry (same contract as clear_rate_limit_state_on_success).
    """
    if not acct_id or not model:
        return
    # Normalize Haiku → Opus so the meter is keyed correctly.
    model = _normalize_rl_model(model)
    cascade = cascade_disabled_models(model)
    async with _max_plan_pool_lock:
        acct = _account_by_id.get(acct_id)
        if acct is None:
            return
        dm = acct.setdefault("disabled_models", {})
        now = _utcnow()
        for m in cascade:
            info = dm.setdefault(m, {"hits": 0})
            info["hits"] += 1
            info["at"] = now
        primary_hits = dm[model]["hits"]
        cooldown = min(30 * (2 ** (primary_hits - 1)), 240) * random.uniform(0.85, 1.15)
        logger.warning(
            f"[POOL] Account {acct['name']} disabled models {list(cascade)} "
            f"(primary {model} hit #{primary_hits}, cooldown {cooldown:.0f}min)"
        )
        try:
            await _persist_rate_limit_state(acct_id)
        except SQLAlchemyError as e:
            logger.warning(f"[POOL] Failed to persist rate-limit state for {acct_id}: {e} — queued for retry")
            if acct_id not in _pending_rl_persists:
                _pending_rl_persists.append(acct_id)


async def clear_rate_limit_state_on_success(acct_id: str, model: str) -> bool:
    """Clear cascade-scoped disabled_models entries after a successful task.

    Cascade: Opus success clears (opus, sonnet, haiku); Sonnet success clears
    (sonnet, haiku); Haiku success clears (haiku,). A successful task proves
    the shared quota is fine for that model and everything it cascades into,
    but does NOT prove the higher tiers are fine (Sonnet success says nothing
    about Opus's stricter sub-bucket).

    Returns True if any entry was removed. Acquires _max_plan_pool_lock.
    On DB persist failure, appends to _pending_rl_persists for retry."""
    if not acct_id or not model:
        return False
    # Normalize Haiku → Opus so clearing Haiku's bucket clears the shared meter.
    model = _normalize_rl_model(model)
    cascade = cascade_disabled_models(model)
    async with _max_plan_pool_lock:
        acct = _account_by_id.get(acct_id)
        if acct is None:
            return False
        dm = acct.get("disabled_models") or {}
        cleared = []
        for m in cascade:
            if m in dm:
                cleared.append(m)
                del dm[m]
        if not cleared:
            return False
        if not dm:
            acct.pop("disabled_models", None)
        logger.info(f"[POOL] Account {acct['name']} cleared disabled models {cleared} after successful {model} task")
        try:
            await _persist_rate_limit_state(acct_id)
        except SQLAlchemyError as e:
            logger.warning(f"[POOL] Failed to persist rate-limit clear for {acct_id}: {e} — queued for retry")
            if acct_id not in _pending_rl_persists:
                _pending_rl_persists.append(acct_id)
        return True


async def recheck_disabled_accounts():
    """Re-enable legacy full-account disables after cooldown and drain the
    persist-retry queue. Per-model disables are NOT auto-deleted here — the
    state machine in _account_state_for_model transitions them to "probing"
    lazily once cooldown elapses, and clear_rate_limit_state_on_success drops
    entries after a successful probe task."""
    from app.models import MaxPlanAccount

    changed_accounts = []
    async with _max_plan_pool_lock:
        now = _utcnow()

        for acct in _account_by_id.values():
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

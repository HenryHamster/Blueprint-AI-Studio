"""Max plan account CRUD endpoints.

Manages Max plan account pool entries: DB persistence of MaxPlanAccount rows
alongside the in-memory pool in app.max_plan_pool.
"""
import uuid
import logging
from typing import Dict, Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from app.database import get_session
from app.shared_state import limiter
from app.models import Task, TaskStatus, MaxPlanAccount, _utcnow
from app.auth import get_current_user

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/api/admin",
    tags=["admin-pool-accounts"],
    dependencies=[Depends(get_current_user)]
)


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

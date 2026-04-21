"""Max plan account CRUD endpoints.

Manages DB persistence of MaxPlanAccount rows alongside the in-memory pool in
app.max_plan_pool. v1 accepts a provider-aware payload while keeping a thin
legacy Anthropic OAuth path for existing clients.
"""

import json
import logging
import uuid
from typing import Any, Dict

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from app.auth import get_current_user
from app.database import get_session
from app.models import LLMAuthMode, LLMProviderName, MaxPlanAccount, Task, TaskStatus, _utcnow
from app.providers.registry import get_provider
from app.shared_state import limiter

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/api/admin",
    tags=["admin-pool-accounts"],
    dependencies=[Depends(get_current_user)],
)


def _bad_request(detail: str) -> HTTPException:
    return HTTPException(status_code=400, detail=detail)


def _parse_provider(raw: Any) -> LLMProviderName:
    try:
        return raw if isinstance(raw, LLMProviderName) else LLMProviderName(str(raw))
    except ValueError as exc:
        allowed = ", ".join(p.value for p in LLMProviderName)
        raise _bad_request(f"provider must be one of: {allowed}") from exc


def _parse_auth_mode(raw: Any) -> LLMAuthMode:
    try:
        return raw if isinstance(raw, LLMAuthMode) else LLMAuthMode(str(raw))
    except ValueError as exc:
        allowed = ", ".join(m.value for m in LLMAuthMode)
        raise _bad_request(f"auth_mode must be one of: {allowed}") from exc


def _validate_claude_config_json(raw: Any) -> str | None:
    if raw is None:
        return None
    if not isinstance(raw, str):
        raise _bad_request("claude_config_json must be a JSON string")
    try:
        json.loads(raw)
    except (ValueError, TypeError) as exc:
        raise _bad_request("claude_config_json must be valid JSON") from exc
    return raw


def _provider_metadata_from_payload(payload: Dict[str, Any], current: Dict[str, Any] | None = None) -> Dict[str, Any]:
    if "provider_metadata" not in payload:
        return dict(current or {})
    metadata = payload["provider_metadata"]
    if metadata is None:
        return {}
    if not isinstance(metadata, dict):
        raise _bad_request("provider_metadata must be a dict")
    return dict(metadata)


def _canonical_secret_from_payload(payload: Dict[str, Any]) -> str | None:
    raw = payload.get("secret")
    if raw is None:
        return None
    if not isinstance(raw, str) or not raw.strip():
        raise _bad_request("secret must be a non-empty string")
    return raw.strip()


def _legacy_anthropic_secret_from_payload(payload: Dict[str, Any]) -> str | None:
    raw = payload.get("oauth_token")
    if raw is None:
        return None
    if not isinstance(raw, str) or not raw.strip():
        raise _bad_request("oauth_token must be a non-empty string")
    return raw.strip()


def _create_request_shape(payload: Dict[str, Any]) -> tuple[LLMProviderName, LLMAuthMode, str]:
    canonical_secret = _canonical_secret_from_payload(payload)
    if canonical_secret is not None:
        if "provider" not in payload or "auth_mode" not in payload:
            raise _bad_request("'provider', 'auth_mode', and 'secret' are required for provider-aware account creation")
        return _parse_provider(payload["provider"]), _parse_auth_mode(payload["auth_mode"]), canonical_secret

    legacy_secret = _legacy_anthropic_secret_from_payload(payload)
    if legacy_secret is not None:
        return LLMProviderName.ANTHROPIC, LLMAuthMode.OAUTH, legacy_secret

    raise _bad_request("'name' and either canonical 'secret' or legacy 'oauth_token' are required")


def _update_target_identity(
    payload: Dict[str, Any],
    db_account: MaxPlanAccount,
) -> tuple[LLMProviderName, LLMAuthMode, str | None]:
    provider = _parse_provider(payload["provider"]) if "provider" in payload else db_account.provider
    auth_mode = _parse_auth_mode(payload["auth_mode"]) if "auth_mode" in payload else db_account.auth_mode

    canonical_secret = _canonical_secret_from_payload(payload)
    legacy_secret = _legacy_anthropic_secret_from_payload(payload)
    new_secret = canonical_secret if canonical_secret is not None else legacy_secret

    if (provider != db_account.provider or auth_mode != db_account.auth_mode) and new_secret is None:
        raise _bad_request("Changing provider or auth_mode requires a new secret")

    if legacy_secret is not None and (provider != LLMProviderName.ANTHROPIC or auth_mode != LLMAuthMode.OAUTH):
        raise _bad_request("legacy oauth_token updates are only valid for anthropic/oauth accounts")

    return provider, auth_mode, new_secret


def _validate_provider_inputs(
    provider: LLMProviderName,
    auth_mode: LLMAuthMode,
    secret: str,
    provider_metadata: Dict[str, Any],
) -> tuple[str, Dict[str, Any]]:
    provider_impl = get_provider(provider)
    try:
        result = provider_impl.validate_secret(secret, auth_mode, provider_metadata)
    except ValueError as exc:
        raise _bad_request(str(exc)) from exc
    return result.secret, result.provider_metadata


def _runtime_pool_secret(provider: LLMProviderName, auth_mode: LLMAuthMode, raw_secret: str | None) -> str | None:
    if provider == LLMProviderName.ANTHROPIC and auth_mode == LLMAuthMode.OAUTH:
        return raw_secret
    return None


def _stored_oauth_token_ref(provider: LLMProviderName, auth_mode: LLMAuthMode, stored_secret_ref: str | None) -> str | None:
    if provider == LLMProviderName.ANTHROPIC and auth_mode == LLMAuthMode.OAUTH:
        return stored_secret_ref
    return None


def _account_secret_ref(db_account: MaxPlanAccount) -> str | None:
    return db_account.secret_blob_ref or db_account.oauth_token


@router.post("/max-plan-accounts")
@limiter.limit("10/minute")
async def add_max_plan_account(
    request: Request,
    payload: Dict[str, Any],
    session: AsyncSession = Depends(get_session),
    current_user: dict = Depends(get_current_user),
):
    """Add an LLM account record.

    Canonical payload:
      - name
      - provider
      - auth_mode
      - secret
      - provider_metadata (optional)

    Legacy compatibility path:
      - name
      - oauth_token  (maps to anthropic/oauth)
    """
    from app.max_plan_pool import _account_by_id, _max_plan_pool_lock, _max_plan_pools

    name = payload.get("name")
    if not isinstance(name, str) or not name.strip():
        raise _bad_request("'name' is required")

    provider, auth_mode, raw_secret = _create_request_shape(payload)
    provider_metadata = _provider_metadata_from_payload(payload)
    normalized_secret, normalized_metadata = _validate_provider_inputs(provider, auth_mode, raw_secret, provider_metadata)

    claude_config_json = _validate_claude_config_json(payload.get("claude_config_json"))
    if claude_config_json is not None and (provider != LLMProviderName.ANTHROPIC or auth_mode != LLMAuthMode.OAUTH):
        raise _bad_request("claude_config_json is only valid for anthropic/oauth accounts")

    now = _utcnow()
    db_account = MaxPlanAccount(
        name=name.strip(),
        provider=provider,
        auth_mode=auth_mode,
        oauth_token=None,
        claude_config_json=claude_config_json,
        secret_blob_ref=None,
        provider_metadata=normalized_metadata or None,
        token_created_at=now,
        user_id=current_user["user_id"],
    )
    session.add(db_account)
    await session.commit()
    await session.refresh(db_account)

    stored_secret_ref: str | None = None
    try:
        from app.secrets_manager import store_user_secret

        secret_name = f"llm-account-{db_account.id}"
        stored_secret_ref = await store_user_secret("system", secret_name, normalized_secret)
        db_account.secret_blob_ref = stored_secret_ref
        db_account.oauth_token = _stored_oauth_token_ref(provider, auth_mode, stored_secret_ref)
        session.add(db_account)
        await session.commit()
    except Exception as exc:
        logger.warning(f"[POOL] Secret Manager unavailable, secret stored inline for account {db_account.id}: {exc}")
        db_account.secret_blob_ref = normalized_secret
        db_account.oauth_token = _stored_oauth_token_ref(provider, auth_mode, normalized_secret)
        session.add(db_account)
        await session.commit()

    entry = {
        "id": str(db_account.id),
        "user_id": current_user["user_id"],
        "name": db_account.name,
        "provider": db_account.provider.value,
        "auth_mode": db_account.auth_mode.value,
        "secret_blob_ref": db_account.secret_blob_ref,
        "provider_metadata": db_account.provider_metadata or {},
        "runtime_secret": normalized_secret,
        "runtime_ready": bool(normalized_secret),
        "oauth_token": _runtime_pool_secret(provider, auth_mode, normalized_secret),
        "claude_config_json": db_account.claude_config_json,
        "enabled": db_account.enabled,
        "active_tasks": 0,
        "total_tasks": db_account.total_tasks,
        "token_created_at": now.isoformat(),
    }
    async with _max_plan_pool_lock:
        _max_plan_pools.setdefault(current_user["user_id"], []).append(entry)
        _account_by_id[str(db_account.id)] = entry
    return {
        "id": str(db_account.id),
        "name": db_account.name,
        "provider": db_account.provider.value,
        "auth_mode": db_account.auth_mode.value,
        "message": "Account added to pool",
    }


@router.delete("/max-plan-accounts/{account_id}")
@limiter.limit("10/minute")
async def remove_max_plan_account(
    request: Request,
    account_id: str,
    force: bool = Query(False),
    session: AsyncSession = Depends(get_session),
    current_user: dict = Depends(get_current_user),
):
    from app.max_plan_pool import _account_by_id, _max_plan_pools

    try:
        acct_uuid = uuid.UUID(account_id)
    except ValueError as exc:
        raise _bad_request("Invalid account ID") from exc

    db_account = await session.get(MaxPlanAccount, acct_uuid)
    if not db_account or db_account.user_id != current_user["user_id"]:
        raise HTTPException(status_code=404, detail="Account not found")

    active_using = (
        await session.execute(
            select(Task.id).where(
                Task.status.in_([TaskStatus.IN_PROGRESS, TaskStatus.PROVISIONING]),
                Task.context["max_plan_account_id"].as_string() == account_id,
            )
        )
    ).scalars().all()
    if active_using and not force:
        raise HTTPException(
            status_code=409,
            detail=(
                f"Account has {len(active_using)} active task(s). "
                "Pass ?force=true to remove anyway (tasks will fail on next token refresh)."
            ),
        )
    if active_using and force:
        logger.warning(f"[POOL] Force-deleting account {account_id} with {len(active_using)} active tasks — slots will reconcile next cycle")

    secret_ref = _account_secret_ref(db_account)
    if secret_ref and secret_ref.startswith("projects/"):
        try:
            from app.secrets_manager import delete_user_secret

            await delete_user_secret(secret_ref)
        except Exception as exc:
            logger.warning(f"[POOL] Failed to delete Secret Manager secret for account {account_id}: {exc}")

    await session.delete(db_account)
    await session.commit()

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
    current_user: dict = Depends(get_current_user),
):
    from app.max_plan_pool import _account_by_id, _max_plan_pool_lock

    try:
        acct_uuid = uuid.UUID(account_id)
    except ValueError as exc:
        raise _bad_request("Invalid account ID") from exc

    db_account = await session.get(MaxPlanAccount, acct_uuid)
    if not db_account or db_account.user_id != current_user["user_id"]:
        raise HTTPException(status_code=404, detail="Account not found")

    if "enabled" in payload:
        db_account.enabled = bool(payload["enabled"])
    if "name" in payload:
        if not isinstance(payload["name"], str) or not payload["name"].strip():
            raise _bad_request("name must be a non-empty string")
        db_account.name = payload["name"].strip()

    target_provider, target_auth_mode, new_secret = _update_target_identity(payload, db_account)
    target_metadata = _provider_metadata_from_payload(payload, current=db_account.provider_metadata)

    if new_secret is not None:
        normalized_secret, normalized_metadata = _validate_provider_inputs(
            target_provider, target_auth_mode, new_secret, target_metadata
        )
    else:
        normalized_secret = None
        normalized_metadata = target_metadata
        try:
            get_provider(target_provider)
            required_fields = get_provider(target_provider).required_metadata_fields(target_auth_mode)
        except ValueError as exc:
            raise _bad_request(str(exc)) from exc
        missing_fields = [field for field in required_fields if not normalized_metadata.get(field)]
        if ("provider_metadata" in payload or "provider" in payload or "auth_mode" in payload) and missing_fields:
            raise _bad_request(f"Missing provider metadata fields: {', '.join(missing_fields)}")

    if "provider" in payload:
        db_account.provider = target_provider
    if "auth_mode" in payload:
        db_account.auth_mode = target_auth_mode
    if "provider_metadata" in payload:
        db_account.provider_metadata = normalized_metadata or None

    if "claude_config_json" in payload:
        db_account.claude_config_json = _validate_claude_config_json(payload.get("claude_config_json"))
    if (
        target_provider != LLMProviderName.ANTHROPIC
        or target_auth_mode != LLMAuthMode.OAUTH
    ) and "claude_config_json" not in payload:
        db_account.claude_config_json = None
    if db_account.claude_config_json is not None and (
        db_account.provider != LLMProviderName.ANTHROPIC or db_account.auth_mode != LLMAuthMode.OAUTH
    ):
        raise _bad_request("claude_config_json is only valid for anthropic/oauth accounts")

    if (
        target_provider != LLMProviderName.ANTHROPIC
        or target_auth_mode != LLMAuthMode.OAUTH
    ) and ("provider" in payload or "auth_mode" in payload):
        db_account.claude_config_json = None

    if normalized_secret is not None:
        try:
            from app.secrets_manager import store_user_secret

            secret_name = f"llm-account-{account_id}"
            stored_secret_ref = await store_user_secret("system", secret_name, normalized_secret)
        except Exception as exc:
            logger.warning(f"[POOL] Secret Manager unavailable, secret stored inline for account {account_id}: {exc}")
            stored_secret_ref = normalized_secret
        db_account.secret_blob_ref = stored_secret_ref
        db_account.oauth_token = _stored_oauth_token_ref(target_provider, target_auth_mode, stored_secret_ref)
        db_account.token_created_at = _utcnow()

    if "provider" in payload or "auth_mode" in payload:
        db_account.oauth_token = _stored_oauth_token_ref(
            db_account.provider,
            db_account.auth_mode,
            db_account.secret_blob_ref,
        )

    db_account.updated_at = _utcnow()
    session.add(db_account)

    async with _max_plan_pool_lock:
        await session.commit()
        mem_account = _account_by_id.get(account_id)
        if mem_account:
            mem_account["enabled"] = db_account.enabled
            mem_account["name"] = db_account.name
            mem_account["provider"] = db_account.provider.value
            mem_account["auth_mode"] = db_account.auth_mode.value
            mem_account["secret_blob_ref"] = db_account.secret_blob_ref
            mem_account["provider_metadata"] = db_account.provider_metadata or {}
            mem_account["claude_config_json"] = db_account.claude_config_json
            if normalized_secret is not None or "provider" in payload or "auth_mode" in payload:
                mem_account["runtime_secret"] = normalized_secret
                mem_account["runtime_ready"] = bool(normalized_secret)
                mem_account["oauth_token"] = _runtime_pool_secret(
                    db_account.provider,
                    db_account.auth_mode,
                    normalized_secret,
                )
                mem_account["token_created_at"] = (
                    db_account.token_created_at.isoformat() if db_account.token_created_at else None
                )

    return {
        "id": account_id,
        "name": db_account.name,
        "provider": db_account.provider.value,
        "auth_mode": db_account.auth_mode.value,
        "enabled": db_account.enabled,
    }


@router.post("/max-plan-accounts/{account_id}/reset-rate-limits")
async def reset_account_rate_limits(
    account_id: str,
    session: AsyncSession = Depends(get_session),
    current_user: dict = Depends(get_current_user),
):
    """Clear all rate-limit state (disabled_models) for a Max plan account."""
    from app.max_plan_pool import _account_by_id, _max_plan_pool_lock, _pending_rl_persists

    try:
        acct_uuid = uuid.UUID(account_id)
    except ValueError as exc:
        raise _bad_request("Invalid account ID") from exc

    db_account = await session.get(MaxPlanAccount, acct_uuid)
    if not db_account or db_account.user_id != current_user["user_id"]:
        raise HTTPException(status_code=404, detail="Account not found")

    async with _max_plan_pool_lock:
        mem_account = _account_by_id.get(account_id)
        if mem_account:
            mem_account.pop("disabled_models", None)
            mem_account["enabled"] = True
            mem_account.pop("disabled_at", None)
            mem_account.pop("rl_hit_count", None)

    db_account.rate_limit_state = None
    db_account.enabled = True
    db_account.updated_at = _utcnow()
    session.add(db_account)
    await session.commit()

    if account_id in _pending_rl_persists:
        _pending_rl_persists.remove(account_id)

    logger.info(f"[POOL] Rate-limit state reset for account {db_account.name} ({account_id}) by user {current_user['user_id']}")
    return {"message": f"Rate-limit state cleared for account {db_account.name}"}

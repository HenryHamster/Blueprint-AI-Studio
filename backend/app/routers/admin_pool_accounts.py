"""Max plan account CRUD endpoints.

Manages DB persistence of MaxPlanAccount rows alongside the in-memory pool in
app.max_plan_pool. v1 accepts a provider-aware payload while keeping a thin
legacy Anthropic OAuth path for existing clients.
"""

import json
import logging
import uuid
from datetime import datetime
from typing import Any, Dict

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy import cast, func, or_
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import load_only
from sqlmodel import select

from app.admin_helpers import _get_user_project
from app.auth import get_current_user, require_admin
from app.database import get_session, resolve_dialect_name
from app.digest_cache import DigestCache, get_digest_cache
from app.digest_precompute import annotate_precompute_metadata
from app.models import (
    Alert,
    AlertSeverity,
    LLMAuthMode,
    LLMProviderName,
    MaxPlanAccount,
    PoolVm,
    PoolVmState,
    Task,
    TaskStatus,
    _utcnow,
)
from app.providers.registry import get_provider
from app.shared_state import limiter

logger = logging.getLogger(__name__)

_pool_account_cache_generations: dict[str, int] = {}

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


def _provider_metadata_from_payload(
    payload: Dict[str, Any], current: Dict[str, Any] | None = None
) -> Dict[str, Any]:
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


def _create_request_shape(
    payload: Dict[str, Any],
) -> tuple[LLMProviderName, LLMAuthMode, str]:
    canonical_secret = _canonical_secret_from_payload(payload)
    if canonical_secret is not None:
        if "provider" not in payload or "auth_mode" not in payload:
            raise _bad_request(
                "'provider', 'auth_mode', and 'secret' are required for provider-aware account creation"
            )
        return (
            _parse_provider(payload["provider"]),
            _parse_auth_mode(payload["auth_mode"]),
            canonical_secret,
        )

    legacy_secret = _legacy_anthropic_secret_from_payload(payload)
    if legacy_secret is not None:
        return LLMProviderName.ANTHROPIC, LLMAuthMode.OAUTH, legacy_secret

    raise _bad_request(
        "'name' and either canonical 'secret' or legacy 'oauth_token' are required"
    )


def _update_target_identity(
    payload: Dict[str, Any],
    db_account: MaxPlanAccount,
) -> tuple[LLMProviderName, LLMAuthMode, str | None]:
    provider = (
        _parse_provider(payload["provider"])
        if "provider" in payload
        else db_account.provider
    )
    auth_mode = (
        _parse_auth_mode(payload["auth_mode"])
        if "auth_mode" in payload
        else db_account.auth_mode
    )

    canonical_secret = _canonical_secret_from_payload(payload)
    legacy_secret = _legacy_anthropic_secret_from_payload(payload)
    new_secret = canonical_secret if canonical_secret is not None else legacy_secret

    if (
        provider != db_account.provider or auth_mode != db_account.auth_mode
    ) and new_secret is None:
        raise _bad_request("Changing provider or auth_mode requires a new secret")

    if legacy_secret is not None and (
        provider != LLMProviderName.ANTHROPIC or auth_mode != LLMAuthMode.OAUTH
    ):
        raise _bad_request(
            "legacy oauth_token updates are only valid for anthropic/oauth accounts"
        )

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


def _runtime_pool_secret(
    provider: LLMProviderName, auth_mode: LLMAuthMode, raw_secret: str | None
) -> str | None:
    if provider == LLMProviderName.ANTHROPIC and auth_mode == LLMAuthMode.OAUTH:
        return raw_secret
    return None


def _stored_oauth_token_ref(
    provider: LLMProviderName, auth_mode: LLMAuthMode, stored_secret_ref: str | None
) -> str | None:
    if provider == LLMProviderName.ANTHROPIC and auth_mode == LLMAuthMode.OAUTH:
        return stored_secret_ref
    return None


def _account_secret_ref(db_account: MaxPlanAccount) -> str | None:
    return db_account.secret_blob_ref or db_account.oauth_token


_CAP_FIELDS = (
    "daily_task_cap",
    "max_concurrent_tasks",
    "auto_disable_after_rate_limits",
)


def _parse_cap(payload: Dict[str, Any], field: str) -> int | None | object:
    """Return the parsed cap value, the sentinel `_UNSET` if the key is absent,
    or None to clear. Raises _bad_request on invalid values."""
    if field not in payload:
        return _UNSET
    raw = payload[field]
    if raw is None or raw == "":
        return None
    try:
        value = int(raw)
    except (TypeError, ValueError) as exc:
        raise _bad_request(f"{field} must be a positive integer or null") from exc
    if value < 1:
        raise _bad_request(f"{field} must be a positive integer or null")
    return value


_UNSET = object()


def _annotate_response_freshness(
    result: Dict[str, Any], served_stale: bool
) -> Dict[str, Any]:
    """Stamp response-level freshness onto a cached pool-account payload.

    Mirrors the digest / health-overview contract (see admin_health.py): the
    age is derived from the payload's OWN ``generated_at`` (not the cache's
    wall age) so a served-stale fallback that is hours old reports a large
    ``precompute_age_seconds`` and ``served_stale=True`` — an operator can
    never mistake a frozen cache reading for a live one. All added keys are
    additive; the existing ``accounts`` / ``rows`` payload is untouched.
    """
    generated_at = result.get("generated_at") if isinstance(result, dict) else None
    age_seconds: float | None = None
    if generated_at:
        try:
            age_seconds = max(
                0.0,
                (_utcnow() - datetime.fromisoformat(generated_at)).total_seconds(),
            )
        except (ValueError, TypeError):
            age_seconds = None
    annotated = annotate_precompute_metadata(
        result,
        precomputed=False,
        precomputed_at=generated_at,
        precompute_age_seconds=age_seconds,
    )
    annotated["served_stale"] = served_stale
    return annotated


def _pool_account_cache_generation(user_id: str) -> int:
    return _pool_account_cache_generations.get(user_id, 0)


def _invalidate_pool_account_digests(cache: DigestCache, user_id: str) -> None:
    _pool_account_cache_generations[user_id] = (
        _pool_account_cache_generation(user_id) + 1
    )
    cache.invalidate_matching(
        lambda key: (
            isinstance(key, tuple)
            and len(key) >= 2
            and key[0] in {"max-plan-accounts-health", "provider-model-breakdown"}
            and key[1] == user_id
        )
    )


def _iso_or_none(value: Any) -> str | None:
    return value.isoformat() if hasattr(value, "isoformat") else None


def _serialize_disabled_models(raw: Any) -> dict[str, dict[str, Any]]:
    if not isinstance(raw, dict):
        return {}
    serialized: dict[str, dict[str, Any]] = {}
    for model, info in raw.items():
        if not isinstance(info, dict):
            continue
        entry: dict[str, Any] = {
            "hits": int(info.get("hits") or 0),
            "at": _iso_or_none(info.get("at")),
        }
        for field in (
            "kind",
            "provider",
            "retry_after_seconds",
            "cooldown_seconds",
            "regression_stats",
        ):
            if field in info:
                entry[field] = info[field]
        serialized[str(model)] = entry
    return serialized


def _disabled_models_from_db(account: MaxPlanAccount) -> dict[str, dict[str, Any]]:
    raw = (account.rate_limit_state or {}).get("disabled_models")
    if not isinstance(raw, dict):
        return {}
    serialized: dict[str, dict[str, Any]] = {}
    for model, info in raw.items():
        if not isinstance(info, dict):
            continue
        entry: dict[str, Any] = {
            "hits": int(info.get("hits") or 0),
            "at": info.get("at"),
        }
        for field in (
            "kind",
            "provider",
            "retry_after_seconds",
            "cooldown_seconds",
            "regression_stats",
        ):
            if field in info:
                entry[field] = info[field]
        serialized[str(model)] = entry
    return serialized


def _cap_remaining(cap: int | None, used: int) -> int | None:
    if cap is None:
        return None
    return max(0, int(cap) - int(used))


def _cap_usage_fraction(cap: int | None, used: int) -> float | None:
    if cap is None or int(cap) <= 0:
        return None
    return round(min(1.0, max(0, int(used)) / int(cap)), 4)


def _account_usage_summary(
    *,
    active_tasks: int,
    total_tasks: int,
    daily_task_count: int,
    daily_task_cap: int | None,
    max_concurrent_tasks: int | None,
) -> dict[str, Any]:
    daily_remaining = _cap_remaining(daily_task_cap, daily_task_count)
    concurrent_remaining = _cap_remaining(max_concurrent_tasks, active_tasks)
    at_daily_cap = daily_remaining == 0 if daily_remaining is not None else False
    at_concurrent_cap = (
        concurrent_remaining == 0 if concurrent_remaining is not None else False
    )
    return {
        "active_tasks": active_tasks,
        "total_tasks": total_tasks,
        "daily_task_count": daily_task_count,
        "daily_task_cap": daily_task_cap,
        "daily_task_remaining": daily_remaining,
        "daily_task_usage_fraction": _cap_usage_fraction(
            daily_task_cap, daily_task_count
        ),
        "max_concurrent_tasks": max_concurrent_tasks,
        "concurrent_task_remaining": concurrent_remaining,
        "concurrent_task_usage_fraction": _cap_usage_fraction(
            max_concurrent_tasks, active_tasks
        ),
        "at_daily_cap": at_daily_cap,
        "at_concurrent_cap": at_concurrent_cap,
        "at_any_cap": at_daily_cap or at_concurrent_cap,
    }


def _account_usage_snapshot(mem_account: dict[str, Any] | None) -> dict[str, Any]:
    if mem_account is None:
        return {
            "known": False,
            "five_hour_utilization": None,
            "seven_day_utilization": None,
            "resets_at": {},
            "probed_at": None,
            "stale": True,
        }
    from app.max_plan_pool import _usage_snapshot_summary

    return _usage_snapshot_summary(mem_account, _utcnow())


def _account_status_summary(
    *,
    enabled: bool,
    credential_configured: bool,
    runtime_ready: bool,
    disabled_reason: str | None,
    disabled_models: dict[str, dict[str, Any]],
    usage: dict[str, Any],
) -> tuple[str, list[str]]:
    reasons: list[str] = []
    if not enabled:
        reasons.append("disabled")
    if not credential_configured:
        reasons.append("missing_credentials")
    if credential_configured and not runtime_ready:
        reasons.append("runtime_not_loaded")
    if usage["at_daily_cap"]:
        reasons.append("daily_cap_reached")
    if usage["at_concurrent_cap"]:
        reasons.append("concurrent_cap_reached")
    if disabled_reason:
        reasons.append("disabled_reason")
    if disabled_models:
        reasons.append("rate_limited_models")

    if not enabled:
        return "disabled", reasons
    if not credential_configured:
        return "unconfigured", reasons
    if not runtime_ready:
        return "unavailable", reasons
    if usage["at_any_cap"]:
        return "at_cap", reasons
    if disabled_reason or disabled_models:
        return "degraded", reasons
    return "ready", reasons


def _serialize_max_plan_account_row(
    db_account: MaxPlanAccount, mem_account: dict[str, Any] | None
) -> dict[str, Any]:
    provider = db_account.provider or LLMProviderName.ANTHROPIC
    auth_mode = db_account.auth_mode or LLMAuthMode.OAUTH
    provider_value = (
        provider.value if isinstance(provider, LLMProviderName) else str(provider)
    )
    auth_mode_value = (
        auth_mode.value if isinstance(auth_mode, LLMAuthMode) else str(auth_mode)
    )
    metadata = (
        dict(mem_account.get("provider_metadata") or {})
        if mem_account is not None
        else dict(db_account.provider_metadata or {})
    )
    daily_reset_at = (
        mem_account.get("daily_task_count_reset_at")
        if mem_account is not None
        else db_account.daily_task_count_reset_at
    )
    disabled_models = (
        _serialize_disabled_models(mem_account.get("disabled_models"))
        if mem_account is not None
        else _disabled_models_from_db(db_account)
    )
    active_tasks = int((mem_account or {}).get("active_tasks") or 0)
    total_tasks = int(db_account.total_tasks or 0)
    daily_task_count = int(
        (mem_account or {}).get("daily_task_count", db_account.daily_task_count)
        or 0
    )
    runtime_ready = bool((mem_account or {}).get("runtime_ready"))
    credential_configured = bool(_account_secret_ref(db_account))
    disabled_reason = (mem_account or {}).get(
        "disabled_reason", db_account.disabled_reason
    )
    usage = _account_usage_summary(
        active_tasks=active_tasks,
        total_tasks=total_tasks,
        daily_task_count=daily_task_count,
        daily_task_cap=db_account.daily_task_cap,
        max_concurrent_tasks=db_account.max_concurrent_tasks,
    )
    usage_snapshot = _account_usage_snapshot(mem_account)
    account_status, status_reasons = _account_status_summary(
        enabled=bool(db_account.enabled),
        credential_configured=credential_configured,
        runtime_ready=runtime_ready,
        disabled_reason=disabled_reason,
        disabled_models=disabled_models,
        usage=usage,
    )
    return {
        "id": str(db_account.id),
        "name": db_account.name,
        "enabled": bool(db_account.enabled),
        "provider": provider_value,
        "auth_mode": auth_mode_value,
        "provider_metadata": metadata,
        "active_tasks": active_tasks,
        "total_tasks": total_tasks,
        "token_created_at": _iso_or_none(db_account.token_created_at),
        "rl_hit_count": int((mem_account or {}).get("rl_hit_count") or 0),
        "daily_task_cap": db_account.daily_task_cap,
        "max_concurrent_tasks": db_account.max_concurrent_tasks,
        "auto_disable_after_rate_limits": db_account.auto_disable_after_rate_limits,
        "daily_task_count": daily_task_count,
        "daily_task_count_reset_at": _iso_or_none(daily_reset_at),
        "disabled_reason": disabled_reason,
        "disabled_at": _iso_or_none(
            (mem_account or {}).get("disabled_at", db_account.disabled_at)
        ),
        "disabled_models": disabled_models,
        "credential_configured": credential_configured,
        "runtime_ready": runtime_ready,
        "account_status": account_status,
        "status_reasons": status_reasons,
        "usage": usage,
        "usage_snapshot": usage_snapshot,
        # Persisted rate-limit/usage headroom snapshot (survives restarts).
        # In-memory usage_snapshot above is the live probe; this is the last
        # known-good value from the DB, available even when the pool is cold.
        "rate_limit_usage": db_account.rate_limit_usage,
    }


async def list_authoritative_max_plan_accounts(
    session: AsyncSession,
    user_id: str,
    limit: int = 100,
    offset: int = 0,
) -> list[dict[str, Any]]:
    """Authoritative persisted account roster overlaid with live pool fields."""
    from app.max_plan_pool import _account_by_id, _max_plan_pool_lock

    rows = (
        (
            await session.execute(
                select(MaxPlanAccount)
                .where(MaxPlanAccount.user_id == user_id)
                .order_by(MaxPlanAccount.created_at, MaxPlanAccount.id)
                .limit(limit)
                .offset(offset)
            )
        )
        .scalars()
        .all()
    )
    async with _max_plan_pool_lock:
        mem_by_id = {
            str(account_id): dict(account)
            for account_id, account in _account_by_id.items()
            if account.get("user_id") == user_id
        }
    return [
        _serialize_max_plan_account_row(row, mem_by_id.get(str(row.id)))
        for row in rows
    ]


_HEALTH_FAILURE_CLASSES = (
    "CODE_ERROR",
    "RATE_LIMIT",
    "EXTERNAL_API",
    "PROVISION_FAILURE",
    "MIGRATION_ERROR",
    "AGENT_TIMEOUT",
    "SCOPE_OVERFLOW",
)

_FAILURE_CLASS_BUCKET_GROUPS: dict[str, list[str]] = {
    "provider": ["RATE_LIMIT", "EXTERNAL_API"],
    "infrastructure": ["PROVISION_FAILURE", "AGENT_TIMEOUT", "SCOPE_OVERFLOW"],
    "code": ["CODE_ERROR", "MIGRATION_ERROR"],
}


@router.get("/max-plan-accounts")
async def get_max_plan_accounts(
    session: AsyncSession = Depends(get_session),
    current_user: dict = Depends(require_admin),
):
    accounts = await list_authoritative_max_plan_accounts(
        session, current_user["user_id"]
    )
    return {
        "accounts": accounts,
        "generated_at": _utcnow().isoformat(),
    }


@router.get("/max-plan-accounts/health")
async def get_account_health_summary(
    session: AsyncSession = Depends(get_session),
    current_user: dict = Depends(get_current_user),
    cache: DigestCache = Depends(get_digest_cache),
):
    """Per-account health summary: state, 24h completions/RL events, health score,
    7d success rate, rate-limited models with cooldown, and per-failure-class
    task counts.

    Wraps the compute path in a TTL cache with in-flight dedup so that
    concurrent admin UI polls don't each re-execute the per-account failed-task
    scan. Cache key is the user_id — the response varies per user because
    summaries are filtered to the user's accounts. Default TTL 15s.
    """
    user_id = current_user["user_id"]
    cache_key = (
        "max-plan-accounts-health",
        user_id,
        _pool_account_cache_generation(user_id),
    )
    result, served_stale = await cache.get_or_compute_meta(
        cache_key,
        lambda: _compute_account_health_summary(
            user_id=user_id,
            session=session,
        ),
    )
    return _annotate_response_freshness(result, served_stale)


async def _compute_account_health_summary(
    *,
    user_id: str,
    session: AsyncSession,
) -> Dict[str, Any]:
    """Compute the per-account health summary payload (cached by caller)."""
    from datetime import timedelta

    from app.failure_classification import classify_failure
    from app.max_plan_pool import get_all_account_health_summaries

    now = _utcnow()
    cutoff = now - timedelta(hours=24)

    summaries = get_all_account_health_summaries(user_id=user_id, now=now)

    account_ids = {s["id"] for s in summaries}
    failure_counts: dict[str, dict[str, int]] = {}
    if account_ids:
        dialect_name = resolve_dialect_name(session)
        # Phase B4 (H-P3): drop status_log/payloads/plan_output JSONB. The
        # downstream consumer reads context, account_id, latest_status_message
        # via classify_failure(); payloads/status_log are not consumed.
        _account_id_list = list(account_ids)
        if dialect_name == "sqlite":
            _llm_predicates = [
                func.json_extract(Task.context, "$.llm_account_id").in_(
                    _account_id_list
                )
            ]
            _mp_predicates = [
                func.json_extract(Task.context, "$.max_plan_account_id").in_(
                    _account_id_list
                )
            ]
        else:
            # Postgres keeps the GIN-friendly containment operator so
            # ``ix_task_context_gin`` can satisfy the context-match predicates.
            _llm_predicates = [
                Task.context.op("@>")(cast({"llm_account_id": aid}, JSONB))
                for aid in _account_id_list
            ]
            _mp_predicates = [
                Task.context.op("@>")(cast({"max_plan_account_id": aid}, JSONB))
                for aid in _account_id_list
            ]
        failed_tasks = (
            (
                await session.execute(
                    select(Task)
                    .options(
                        load_only(
                            Task.id,
                            Task.status,
                            Task.account_id,
                            Task.context,
                            Task.latest_status_message,
                            Task.updated_at,
                            Task.project_id,  # classify_failure → record_residual_fingerprint
                            Task.payloads,  # classify_failure reads first_payload (L-P2),
                            raiseload=True
                        )
                    )
                    .where(
                        Task.status.in_([TaskStatus.FAILED, TaskStatus.CANCELLED]),
                        Task.updated_at >= cutoff,
                        Task.latest_status_message.isnot(None),
                        or_(
                            Task.account_id.in_(_account_id_list),
                            *_llm_predicates,
                            *_mp_predicates,
                        ),
                    )
                    .order_by(Task.updated_at.desc())
                    .limit(1000)
                )
            )
            .scalars()
            .all()
        )
        for t in failed_tasks:
            acct_id = t.account_id
            if not acct_id:
                ctx = t.context or {}
                acct_id = ctx.get("llm_account_id") or ctx.get("max_plan_account_id")
            if acct_id not in account_ids:
                continue
            fc = classify_failure(t)
            bucket = failure_counts.setdefault(
                acct_id, {c: 0 for c in _HEALTH_FAILURE_CLASSES}
            )
            if fc in bucket:
                bucket[fc] += 1

    accounts = []
    for s in summaries:
        acct_id = s["id"]
        state = "disabled" if s["state"] == "fully_disabled" else s["state"]
        rate_limited_models = []
        for m in s["rate_limited_models"]:
            entry: dict = {
                "model": m["model"],
                "cooldown_type": m["kind"],
                "cooldown_seconds_remaining": m["cooldown_remaining_seconds"],
            }
            if m.get("regression_stats"):
                entry["regression_stats"] = m["regression_stats"]
            rate_limited_models.append(entry)
        accounts.append(
            {
                "account_id": acct_id,
                "label": s.get("name", acct_id),
                "provider": s.get("provider"),
                "state": state,
                "disabled_reason": s.get("disabled_reason"),
                "health_score": s["health_score"],
                "success_rate_7d": s.get("success_rate_7d"),
                "avg_duration_seconds": s.get("avg_duration_seconds"),
                "completions_24h": s["completions_24h"],
                "rl_events_24h": s["rl_events_24h"],
                "rate_limited_models": rate_limited_models,
                "usage_snapshot": s.get(
                    "usage_snapshot", _account_usage_snapshot(None)
                ),
                "failure_class_counts": failure_counts.get(
                    acct_id, {c: 0 for c in _HEALTH_FAILURE_CLASSES}
                ),
                "failure_class_buckets": {
                    bucket: sum(
                        failure_counts.get(acct_id, {}).get(cls, 0) for cls in classes
                    )
                    for bucket, classes in _FAILURE_CLASS_BUCKET_GROUPS.items()
                },
            }
        )

    return {"accounts": accounts, "generated_at": now.isoformat()}


@router.get("/max-plan-accounts/provider-model-breakdown")
async def get_provider_model_breakdown(
    session: AsyncSession = Depends(get_session),
    current_user: dict = Depends(get_current_user),
    cache: DigestCache = Depends(get_digest_cache),
    expand_accounts: bool = Query(False),
    project_id: uuid.UUID | None = Query(None),
):
    """Rolling 7-day per-(provider, model) success-rate breakdown with optional per-account drill-in.

    When project_id is supplied the caller must be a member of that project and only
    tasks belonging to that project are counted. When omitted, all tasks for the
    user's accounts are included.
    """
    user_id = current_user["user_id"]

    if project_id is not None:
        await _get_user_project(project_id, user_id, session)

    user_acct_ids = (
        (
            await session.execute(
                select(MaxPlanAccount.id).where(MaxPlanAccount.user_id == user_id)
            )
        )
        .scalars()
        .all()
    )
    user_acct_id_strs = [str(aid) for aid in user_acct_ids]
    if not user_acct_id_strs:
        return _annotate_response_freshness(
            {"rows": [], "generated_at": _utcnow().isoformat()}, served_stale=False
        )

    cache_key = (
        "provider-model-breakdown",
        user_id,
        _pool_account_cache_generation(user_id),
        str(project_id) if project_id is not None else None,
        expand_accounts,
    )
    result, served_stale = await cache.get_or_compute_meta(
        cache_key,
        lambda: _compute_provider_model_breakdown(
            session=session,
            account_ids=user_acct_id_strs,
            expand_accounts=expand_accounts,
            project_id=project_id,
        ),
    )
    return _annotate_response_freshness(result, served_stale)


async def _compute_provider_model_breakdown(
    *,
    session: AsyncSession,
    account_ids: list[str],
    expand_accounts: bool,
    project_id: uuid.UUID | None,
) -> dict:
    from app.account_model_regression import compute_provider_model_breakdown

    rows = await compute_provider_model_breakdown(
        session, account_ids, expand_accounts=expand_accounts, project_id=project_id
    )
    return {"rows": rows, "generated_at": _utcnow().isoformat()}


@router.post("/max-plan-accounts")
@limiter.limit("10/minute")
async def add_max_plan_account(
    request: Request,
    payload: Dict[str, Any],
    session: AsyncSession = Depends(get_session),
    current_user: dict = Depends(require_admin),
    cache: DigestCache = Depends(get_digest_cache),
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
    normalized_secret, normalized_metadata = _validate_provider_inputs(
        provider, auth_mode, raw_secret, provider_metadata
    )

    claude_config_json = _validate_claude_config_json(payload.get("claude_config_json"))
    if claude_config_json is not None and (
        provider != LLMProviderName.ANTHROPIC or auth_mode != LLMAuthMode.OAUTH
    ):
        raise _bad_request(
            "claude_config_json is only valid for anthropic/oauth accounts"
        )

    now = _utcnow()
    cap_values: Dict[str, int | None] = {}
    for field in _CAP_FIELDS:
        parsed = _parse_cap(payload, field)
        if parsed is not _UNSET:
            cap_values[field] = parsed  # type: ignore[assignment]

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
        **cap_values,
    )
    session.add(db_account)
    await session.commit()
    await session.refresh(db_account)

    stored_secret_ref: str | None = None
    try:
        from app.secrets_manager import store_user_secret

        secret_name = f"llm-account-{db_account.id}"
        stored_secret_ref = await store_user_secret(
            "system", secret_name, normalized_secret
        )
        db_account.secret_blob_ref = stored_secret_ref
        db_account.oauth_token = _stored_oauth_token_ref(
            provider, auth_mode, stored_secret_ref
        )
        session.add(db_account)
        await session.commit()
    except Exception as exc:
        logger.exception(
            f"[POOL] Secret Manager unavailable, aborting account creation for {db_account.id}: {exc}"
        )
        await session.delete(db_account)
        await session.commit()
        raise HTTPException(
            status_code=503,
            detail="Secret storage temporarily unavailable, please retry",
        ) from exc

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
        "daily_task_cap": db_account.daily_task_cap,
        "max_concurrent_tasks": db_account.max_concurrent_tasks,
        "auto_disable_after_rate_limits": db_account.auto_disable_after_rate_limits,
        "daily_task_count": 0,
        "daily_task_count_reset_at": None,
        "disabled_reason": None,
    }
    async with _max_plan_pool_lock:
        _max_plan_pools.setdefault(current_user["user_id"], []).append(entry)
        _account_by_id[str(db_account.id)] = entry

    # Kick off an availability probe in the background so the operator gets
    # the visible-models list on this key populated automatically — no need
    # to remember to click "refresh" after each add. Best-effort: any failure
    # just stashes an error string; the admin UI can retry via the explicit
    # refresh endpoint.
    await _best_effort_refresh_availability(entry, db_account.id, normalized_secret)
    _invalidate_pool_account_digests(cache, current_user["user_id"])

    return {
        "id": str(db_account.id),
        "name": db_account.name,
        "provider": db_account.provider.value,
        "auth_mode": db_account.auth_mode.value,
        "available_models": (entry.get("provider_metadata") or {}).get(
            "available_models"
        ),
        "message": "Account added to pool",
    }


async def _best_effort_refresh_availability(
    mem_account: Dict[str, Any],
    account_uuid: uuid.UUID,
    secret: str | None,
) -> None:
    """Refresh `available_models` on a freshly created/updated account.

    Runs inline (not a background task) so the admin sees the result in the
    add-account response — tempting to fire-and-forget but a 5s network call
    is acceptable here and avoids task-lifetime / connection-pool weirdness.
    Failures are swallowed: the stored error string and timestamp on
    provider_metadata are the operator-facing signal.
    """
    from app.account_model_availability import refresh_account_available_models
    from app.database import AsyncSessionLocal

    try:
        await refresh_account_available_models(mem_account, secret=secret)
    except Exception:
        logger.exception("[POOL] availability refresh raised unexpectedly; continuing")
        return

    # Persist whatever landed on mem_account back to the DB row.
    metadata = mem_account.get("provider_metadata") or {}
    try:
        async with AsyncSessionLocal() as persist_session:
            row = await persist_session.get(MaxPlanAccount, account_uuid)
            if row is not None:
                row.provider_metadata = dict(metadata) or None
                row.updated_at = _utcnow()
                persist_session.add(row)
                await persist_session.commit()
    except Exception:
        logger.exception(
            "[POOL] persisting availability metadata failed for account %s; "
            "keeping in-memory copy only",
            account_uuid,
        )


@router.delete("/max-plan-accounts/{account_id}")
@limiter.limit("10/minute")
async def remove_max_plan_account(
    request: Request,
    account_id: str,
    force: bool = Query(False),
    session: AsyncSession = Depends(get_session),
    current_user: dict = Depends(require_admin),
    cache: DigestCache = Depends(get_digest_cache),
):
    from app.max_plan_pool import _account_by_id, _max_plan_pool_lock, _max_plan_pools

    try:
        acct_uuid = uuid.UUID(account_id)
    except ValueError as exc:
        raise _bad_request("Invalid account ID") from exc

    db_account = await session.get(MaxPlanAccount, acct_uuid)
    if not db_account or db_account.user_id != current_user["user_id"]:
        raise HTTPException(status_code=404, detail="Account not found")

    active_using_rows = (
        await session.execute(
            select(Task.id, Task.project_id).where(
                Task.status.in_([TaskStatus.IN_PROGRESS, TaskStatus.PROVISIONING]),
                or_(
                    Task.account_id == account_id,
                    Task.llm_account_id == account_id,
                    Task.context["max_plan_account_id"].as_string() == account_id,
                ),
            )
        )
    ).all()
    active_using = [r[0] for r in active_using_rows]
    warm_refs = (
        (
            await session.execute(
                select(PoolVm.id, PoolVm.state, PoolVm.vm_instance_id).where(
                    PoolVm.account_id == account_id,
                    PoolVm.state.in_(
                        [
                            PoolVmState.PROVISIONING,
                            PoolVmState.IDLE,
                            PoolVmState.LEASED,
                        ]
                    ),
                )
            )
        )
        .all()
    )
    async with _max_plan_pool_lock:
        mem_account = _account_by_id.get(account_id)
        live_active_count = int((mem_account or {}).get("active_tasks") or 0)

    if (active_using or warm_refs or live_active_count > 0) and not force:
        blockers: list[str] = []
        if active_using:
            blockers.append(f"{len(active_using)} active task(s)")
        if live_active_count > 0:
            blockers.append(f"{live_active_count} live in-memory reservation(s)")
        if warm_refs:
            blockers.append(f"{len(warm_refs)} warm-pool VM reference(s)")
        raise HTTPException(
            status_code=409,
            detail=(
                f"Account cannot be removed safely: {', '.join(blockers)}. "
                "Pass ?force=true to remove anyway; active tasks or warm VMs may fail "
                "when they next refresh credentials."
            ),
        )
    if (active_using or warm_refs or live_active_count > 0) and force:
        affected_ids_short = ", ".join(str(t)[:8] for t in active_using[:10])
        if len(active_using) > 10:
            affected_ids_short += f" (+{len(active_using) - 10} more)"
        logger.warning(
            "[POOL] Force-deleting account %s with %s active tasks, %s live "
            "in-memory reservations, and %s warm-pool references; affected: %s",
            account_id,
            len(active_using),
            live_active_count,
            len(warm_refs),
            affected_ids_short or "none",
        )
        # E5: emit a per-project Alert so operators see which tasks are at risk.
        # Without this, force-delete was a silent secret-revocation that surfaced
        # only when individual agents started 401-ing on token refresh.
        affected_by_project: dict[uuid.UUID, list[uuid.UUID]] = {}
        for tid, pid in active_using_rows:
            affected_by_project.setdefault(pid, []).append(tid)
        for project_id, task_ids in affected_by_project.items():
            short_list = ", ".join(str(t)[:8] for t in task_ids[:10])
            suffix = f" (+{len(task_ids) - 10} more)" if len(task_ids) > 10 else ""
            session.add(
                Alert(
                    project_id=project_id,
                    severity=AlertSeverity.WARNING,
                    title=f"Pool account force-deleted with {len(task_ids)} active task(s)",
                    message=(
                        f"Account {db_account.name} ({account_id[:8]}) was force-removed; "
                        f"these tasks may fail on next token refresh: {short_list}{suffix}"
                    ),
                )
            )

    secret_ref = _account_secret_ref(db_account)
    if secret_ref and secret_ref.startswith("projects/"):
        try:
            from app.secrets_manager import delete_user_secret

            await delete_user_secret(secret_ref)
        except Exception as exc:
            logger.warning(
                f"[POOL] Failed to delete Secret Manager secret for account {account_id}: {exc}"
            )

    await session.delete(db_account)
    await session.commit()

    async with _max_plan_pool_lock:
        user_id = current_user["user_id"]
        if user_id in _max_plan_pools:
            _max_plan_pools[user_id] = [
                a for a in _max_plan_pools[user_id] if a["id"] != account_id
            ]
        _account_by_id.pop(account_id, None)
    _invalidate_pool_account_digests(cache, current_user["user_id"])
    return {"message": "Account removed"}


@router.patch("/max-plan-accounts/{account_id}")
async def update_max_plan_account(
    account_id: str,
    payload: Dict[str, Any],
    session: AsyncSession = Depends(get_session),
    current_user: dict = Depends(require_admin),
    cache: DigestCache = Depends(get_digest_cache),
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
        was_enabled = db_account.enabled
        db_account.enabled = bool(payload["enabled"])
        # Manual re-enable clears any auto-disable reason so the UI stops showing stale text.
        if db_account.enabled and not was_enabled:
            db_account.disabled_reason = None
    if "name" in payload:
        if not isinstance(payload["name"], str) or not payload["name"].strip():
            raise _bad_request("name must be a non-empty string")
        db_account.name = payload["name"].strip()
    for field in _CAP_FIELDS:
        parsed = _parse_cap(payload, field)
        if parsed is not _UNSET:
            setattr(db_account, field, parsed)

    target_provider, target_auth_mode, new_secret = _update_target_identity(
        payload, db_account
    )
    target_metadata = _provider_metadata_from_payload(
        payload, current=db_account.provider_metadata
    )

    if new_secret is not None:
        normalized_secret, normalized_metadata = _validate_provider_inputs(
            target_provider, target_auth_mode, new_secret, target_metadata
        )
    else:
        normalized_secret = None
        normalized_metadata = target_metadata
        try:
            get_provider(target_provider)
            required_fields = get_provider(target_provider).required_metadata_fields(
                target_auth_mode
            )
        except ValueError as exc:
            raise _bad_request(str(exc)) from exc
        missing_fields = [
            field for field in required_fields if not normalized_metadata.get(field)
        ]
        if (
            "provider_metadata" in payload
            or "provider" in payload
            or "auth_mode" in payload
        ) and missing_fields:
            raise _bad_request(
                f"Missing provider metadata fields: {', '.join(missing_fields)}"
            )

    if "provider" in payload:
        db_account.provider = target_provider
    if "auth_mode" in payload:
        db_account.auth_mode = target_auth_mode
    if "provider_metadata" in payload:
        db_account.provider_metadata = normalized_metadata or None

    if "claude_config_json" in payload:
        db_account.claude_config_json = _validate_claude_config_json(
            payload.get("claude_config_json")
        )
    if (
        target_provider != LLMProviderName.ANTHROPIC
        or target_auth_mode != LLMAuthMode.OAUTH
    ) and "claude_config_json" not in payload:
        db_account.claude_config_json = None
    if db_account.claude_config_json is not None and (
        db_account.provider != LLMProviderName.ANTHROPIC
        or db_account.auth_mode != LLMAuthMode.OAUTH
    ):
        raise _bad_request(
            "claude_config_json is only valid for anthropic/oauth accounts"
        )

    if (
        target_provider != LLMProviderName.ANTHROPIC
        or target_auth_mode != LLMAuthMode.OAUTH
    ) and ("provider" in payload or "auth_mode" in payload):
        db_account.claude_config_json = None

    if normalized_secret is not None:
        try:
            from app.secrets_manager import store_user_secret

            secret_name = f"llm-account-{account_id}"
            stored_secret_ref = await store_user_secret(
                "system", secret_name, normalized_secret
            )
        except Exception as exc:
            logger.exception(
                f"[POOL] Secret Manager unavailable, aborting secret update for account {account_id}: {exc}"
            )
            raise HTTPException(
                status_code=503,
                detail="Secret storage temporarily unavailable, please retry",
            ) from exc
        db_account.secret_blob_ref = stored_secret_ref
        db_account.oauth_token = _stored_oauth_token_ref(
            target_provider, target_auth_mode, stored_secret_ref
        )
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
            mem_account["daily_task_cap"] = db_account.daily_task_cap
            mem_account["max_concurrent_tasks"] = db_account.max_concurrent_tasks
            mem_account["auto_disable_after_rate_limits"] = (
                db_account.auto_disable_after_rate_limits
            )
            mem_account["disabled_reason"] = db_account.disabled_reason
            if (
                normalized_secret is not None
                or "provider" in payload
                or "auth_mode" in payload
            ):
                mem_account["runtime_secret"] = normalized_secret
                mem_account["runtime_ready"] = bool(normalized_secret)
                mem_account["oauth_token"] = _runtime_pool_secret(
                    db_account.provider,
                    db_account.auth_mode,
                    normalized_secret,
                )
                mem_account["token_created_at"] = (
                    db_account.token_created_at.isoformat()
                    if db_account.token_created_at
                    else None
                )

    # When the secret/provider/auth-mode change, the prior availability snapshot
    # is no longer valid — re-probe so the dispatcher's filter stays accurate.
    secret_or_identity_changed = (
        normalized_secret is not None or "provider" in payload or "auth_mode" in payload
    )
    if secret_or_identity_changed and mem_account is not None:
        await _best_effort_refresh_availability(
            mem_account,
            db_account.id,
            normalized_secret or mem_account.get("runtime_secret"),
        )
    _invalidate_pool_account_digests(cache, current_user["user_id"])

    return {
        "id": account_id,
        "name": db_account.name,
        "provider": db_account.provider.value,
        "auth_mode": db_account.auth_mode.value,
        "enabled": db_account.enabled,
        "daily_task_cap": db_account.daily_task_cap,
        "max_concurrent_tasks": db_account.max_concurrent_tasks,
        "auto_disable_after_rate_limits": db_account.auto_disable_after_rate_limits,
        "available_models": ((mem_account or {}).get("provider_metadata") or {}).get(
            "available_models"
        )
        if mem_account
        else None,
    }


@router.post("/max-plan-accounts/{account_id}/reset-rate-limits")
async def reset_account_rate_limits(
    account_id: str,
    session: AsyncSession = Depends(get_session),
    current_user: dict = Depends(require_admin),
    cache: DigestCache = Depends(get_digest_cache),
):
    """Clear all rate-limit state (disabled_models) for a Max plan account."""
    from app.max_plan_pool import (
        _account_by_id,
        _max_plan_pool_lock,
        _pending_rl_persists,
    )

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
            mem_account.pop("startup_failure_cooldown_at", None)
            mem_account.pop("startup_failure_events", None)
            mem_account["enabled"] = True
            mem_account.pop("disabled_at", None)
            mem_account.pop("rl_hit_count", None)
            mem_account["disabled_reason"] = None

    db_account.rate_limit_state = None
    db_account.enabled = True
    db_account.disabled_reason = None
    db_account.disabled_at = None
    db_account.updated_at = _utcnow()
    session.add(db_account)
    await session.commit()

    if account_id in _pending_rl_persists:
        _pending_rl_persists.remove(account_id)

    logger.info(
        f"[POOL] Rate-limit state reset for account {db_account.name} ({account_id}) by user {current_user['user_id']}"
    )
    _invalidate_pool_account_digests(cache, current_user["user_id"])
    return {"message": f"Rate-limit state cleared for account {db_account.name}"}


@router.post("/max-plan-accounts/{account_id}/regression-quarantine/clear")
async def clear_account_model_regression_quarantine(
    account_id: str,
    provider: str = Query(..., description="provider name (e.g. anthropic)"),
    model: str = Query(..., description="exact model name (e.g. claude-opus-4-8)"),
    session: AsyncSession = Depends(get_session),
    current_user: dict = Depends(require_admin),
    cache: DigestCache = Depends(get_digest_cache),
):
    """Manual override: clear a kind='regression' disabled_models entry for
    (account, model). Only regression entries are touched — rate_limit /
    quota_exhausted / quarantine entries have their own recovery semantics
    (cooldown timers + probe-on-success) and must be reset via dedicated
    paths.

    The 6h time-based reprobe is the automatic recovery; this endpoint is
    for operators who want to re-enable a (provider, model) before the
    cooldown elapses.
    """
    from app.max_plan_pool import clear_account_model_regression

    try:
        acct_uuid = uuid.UUID(account_id)
    except ValueError as exc:
        raise _bad_request("Invalid account ID") from exc

    db_account = await session.get(MaxPlanAccount, acct_uuid)
    if not db_account or db_account.user_id != current_user["user_id"]:
        raise HTTPException(status_code=404, detail="Account not found")

    if not provider.strip() or not model.strip():
        raise _bad_request("provider and model are required")

    cleared = await clear_account_model_regression(
        account_id, model.strip(), provider.strip()
    )
    if not cleared:
        # No-op when (a) the entry doesn't exist or (b) it exists but isn't
        # regression-kind — surface a 404 so callers know the override was
        # rejected rather than silently succeed.
        raise HTTPException(
            status_code=404,
            detail=(
                f"No regression-quarantine entry found for {provider}/{model} on this account "
                "(either none exists or the disable kind is not 'regression')"
            ),
        )

    logger.info(
        f"[POOL] Regression-quarantine cleared for account {db_account.name} "
        f"({account_id}) {provider}/{model} by user {current_user['user_id']}"
    )
    _invalidate_pool_account_digests(cache, current_user["user_id"])
    return {
        "id": account_id,
        "provider": provider,
        "model": model,
        "cleared": True,
    }


@router.post("/max-plan-accounts/{account_id}/refresh-models")
@limiter.limit("10/minute")
async def refresh_account_model_availability(
    request: Request,
    account_id: str,
    session: AsyncSession = Depends(get_session),
    current_user: dict = Depends(require_admin),
    cache: DigestCache = Depends(get_digest_cache),
):
    """Probe the provider's list-models endpoint for this account and cache
    the visible-models list on ``provider_metadata``.

    Operator-facing: the refresh is rate-limited and bypasses the background
    sweep, so admins can trigger a fresh check right after adding a key.
    Returns the matrix: available_models, refreshed_at, error (if any),
    plus ``known_preview`` (models the provider family recognizes as preview
    — useful for "which preview models does this key cover?" UI).
    """
    from app.account_model_availability import refresh_account_available_models
    from app.max_plan_pool import _account_by_id, _max_plan_pool_lock
    from app.providers.gemini import _GEMINI_PREVIEW_MODELS

    try:
        acct_uuid = uuid.UUID(account_id)
    except ValueError as exc:
        raise _bad_request("Invalid account ID") from exc

    db_account = await session.get(MaxPlanAccount, acct_uuid)
    if not db_account or db_account.user_id != current_user["user_id"]:
        raise HTTPException(status_code=404, detail="Account not found")

    mem_account = _account_by_id.get(account_id)
    if not mem_account:
        raise HTTPException(
            status_code=409, detail="Account not loaded in pool; restart or re-add"
        )

    # The in-memory account carries `runtime_secret` — the raw secret or a
    # Secret-Manager-resolved string, populated at pool load. When that's None
    # (Secret Manager fetch failed at load), refresh_account_available_models
    # stashes an error and returns None rather than blindly trying to use the
    # secret_blob_ref which would typically be a `projects/.../secrets/...`
    # path, not the key itself.
    runtime_secret = mem_account.get("runtime_secret")
    models = await refresh_account_available_models(mem_account, secret=runtime_secret)

    # Persist whatever landed on the in-memory dict (could be a fresh list,
    # could be just an error stash) back to the DB row. We do this even on
    # None result so the `available_models_error` string reaches operators.
    updated_metadata = mem_account.get("provider_metadata") or {}
    async with _max_plan_pool_lock:
        db_account.provider_metadata = dict(updated_metadata) or None
        db_account.updated_at = _utcnow()
        session.add(db_account)
        await session.commit()

    known_preview = (
        sorted(m for m in (models or []) if m in _GEMINI_PREVIEW_MODELS)
        if db_account.provider == LLMProviderName.GEMINI
        else []
    )

    logger.info(
        "[POOL] refresh-models for account %s: %s",
        account_id,
        f"{len(models)} models"
        if models is not None
        else f"failed ({updated_metadata.get('available_models_error')})",
    )
    _invalidate_pool_account_digests(cache, current_user["user_id"])

    return {
        "id": account_id,
        "available_models": updated_metadata.get("available_models"),
        "refreshed_at": updated_metadata.get("available_models_refreshed_at"),
        "error": updated_metadata.get("available_models_error"),
        "known_preview_models_visible": known_preview,
    }

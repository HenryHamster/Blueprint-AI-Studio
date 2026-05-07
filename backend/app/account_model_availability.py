"""Per-account model-availability helpers.

Some LLM providers gate models per API key: Gemini's 3.x preview models
require preview access on the project; OpenAI keys may have org-level flags
restricting which preview families are visible. Without a pre-check, the
dispatcher will pick a key that 404s on the requested model and burn a slot
figuring it out.

This module caches the list of model IDs each account can actually call,
so pool selection can skip accounts that don't have the requested model.
Results live on ``MaxPlanAccount.provider_metadata`` as:

    {
        "available_models": ["gemini-2.5-flash", "gemini-2.5-pro", ...],
        "available_models_refreshed_at": "2026-04-22T14:53:00+00:00",
        "available_models_error": None | "<short error string>",
    }

Refresh is best-effort: failures stash an error string and leave stale
entries in place, so a transient network blip doesn't wipe the cache.
Callers treat missing / unknown metadata as "we don't know, try anyway" —
we never stall dispatch on a failed refresh.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Mapping

from app.models import LLMAuthMode, LLMProviderName
from app.providers.registry import get_provider

logger = logging.getLogger(__name__)


# How long a cached availability snapshot is trusted before we suggest a
# refresh. Not a hard expiry — stale snapshots are still used by the
# dispatcher (better than "unknown"); this just drives "refresh needed"
# indicators in the admin UI and the background sweeper.
AVAILABILITY_STALE_AFTER = timedelta(hours=6)

# Rolling window and minimum sample count for 7-day success-rate computation.
# Must match _OUTCOME_WINDOW_SECONDS in max_plan_pool.py (7 × 24 × 3600 s).
SUCCESS_RATE_WINDOW = timedelta(days=7)
SUCCESS_RATE_MIN_SAMPLES = 3


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _parse_iso(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def account_available_models(account: Mapping[str, Any]) -> list[str] | None:
    """Return the cached model list for ``account``, or None if not yet cached.

    None means "don't filter this account" — treat as may-have-access.
    An empty list means "confirmed empty" (key exists but has no models
    visible, e.g. revoked); downstream filters should skip it.
    """
    metadata = account.get("provider_metadata") or {}
    if not isinstance(metadata, Mapping):
        return None
    models = metadata.get("available_models")
    if isinstance(models, list):
        return [m for m in models if isinstance(m, str)]
    return None


def account_has_model(account: Mapping[str, Any], model: str) -> bool | None:
    """Return True/False if the account is known to have/lack ``model``.

    Returns None when the availability hasn't been probed yet or the last
    probe failed — callers must NOT treat None as False. Fail-open: if we
    don't know, try the account; a runtime 404 will reclassify as
    EXTERNAL_API and the dispatcher can try another account.
    """
    models = account_available_models(account)
    if models is None:
        return None
    return model in models


def account_availability_is_stale(
    account: Mapping[str, Any], now: datetime | None = None
) -> bool:
    metadata = account.get("provider_metadata") or {}
    if not isinstance(metadata, Mapping):
        return True
    refreshed = _parse_iso(metadata.get("available_models_refreshed_at"))
    if refreshed is None:
        return True
    now = now or _utcnow()
    return (now - refreshed) > AVAILABILITY_STALE_AFTER


def account_success_rate_7d(
    account: Mapping[str, Any],
    now: datetime | None = None,
) -> float | None:
    """Return the 7-day task success rate for ``account``, or None when data is sparse.

    Reads from ``outcome_events`` on the in-memory account dict (each entry:
    ``{"at": datetime, "success": bool}``). Returns a float in [0.0, 1.0] when
    at least ``SUCCESS_RATE_MIN_SAMPLES`` outcomes exist in the window, or None
    for new/low-traffic accounts so a single unlucky failure doesn't unfairly
    penalise them.

    Uses naive UTC timestamps to match how max_plan_pool records outcome_events
    (via time_utils._utcnow, which strips tzinfo). Callers that track their own
    ``now`` should pass it in for consistency.
    """
    # Naive UTC — must match the storage format used by max_plan_pool.record_*.
    # Strip tzinfo from caller-supplied `now` too; the default strips it and
    # stored events are always naive, so a mixed comparison would TypeError.
    _now = (now if now is not None else datetime.now(timezone.utc)).replace(tzinfo=None)
    cutoff = _now - SUCCESS_RATE_WINDOW
    events = [
        e
        for e in account.get("outcome_events", [])
        if e.get("at") is not None and e["at"] > cutoff
    ]
    if len(events) < SUCCESS_RATE_MIN_SAMPLES:
        return None
    return sum(1 for e in events if e.get("success")) / len(events)


def account_success_rate_7d_for_type(
    account: Mapping[str, Any],
    task_type: str,
    now: datetime | None = None,
) -> float | None:
    """Return the 7-day task success rate for ``account`` scoped to ``task_type``.

    Returns None when fewer than SUCCESS_RATE_MIN_SAMPLES typed outcomes exist —
    callers should fall back to ``account_success_rate_7d()`` for the aggregate.
    Uses the same naive-UTC timestamp convention as ``account_success_rate_7d``.
    """
    _now = (now if now is not None else datetime.now(timezone.utc)).replace(tzinfo=None)
    cutoff = _now - SUCCESS_RATE_WINDOW
    events = [
        e
        for e in account.get("outcome_events", [])
        if e.get("at") is not None
        and e["at"] > cutoff
        and e.get("task_type") == task_type
    ]
    if len(events) < SUCCESS_RATE_MIN_SAMPLES:
        return None
    return sum(1 for e in events if e.get("success")) / len(events)


async def refresh_account_available_models(
    account: dict[str, Any],
    *,
    secret: str | None = None,
) -> list[str] | None:
    """Probe the provider for ``account``'s visible models and cache the result.

    Mutates ``account["provider_metadata"]`` in place with the new list and
    an ISO timestamp. If the provider can't list (OAuth-only, no endpoint,
    network error), records an error string and leaves any existing
    `available_models` entry untouched so stale data keeps helping the
    dispatcher until the next successful refresh.

    Returns the fresh list on success, or None on failure / unsupported
    combinations. Never raises.
    """
    provider_name = account.get("provider") or LLMProviderName.ANTHROPIC.value
    auth_mode_raw = account.get("auth_mode") or LLMAuthMode.OAUTH.value
    try:
        provider_enum = LLMProviderName(provider_name)
        auth_mode_enum = LLMAuthMode(auth_mode_raw)
    except ValueError:
        logger.info(
            "[AVAILABILITY] unrecognized provider=%r / auth_mode=%r on account %s",
            provider_name,
            auth_mode_raw,
            account.get("id"),
        )
        return None

    effective_secret = secret if secret is not None else account.get("runtime_secret")
    if not effective_secret:
        logger.info(
            "[AVAILABILITY] account %s has no runtime secret available; skipping refresh",
            account.get("id"),
        )
        return None

    provider_impl = get_provider(provider_enum)
    provider_metadata = account.get("provider_metadata") or {}
    try:
        models = await provider_impl.list_available_models(
            effective_secret,
            auth_mode_enum,
            provider_metadata=provider_metadata,
        )
    except Exception as exc:
        logger.exception(
            "[AVAILABILITY] list_available_models raised for account %s; stashing error",
            account.get("id"),
        )
        _stash_error(account, str(exc))
        return None

    if models is None:
        # Provider returned None without raising — either unsupported auth
        # mode or a soft failure. Stash a note so the UI can show "couldn't
        # check" but keep any existing cached list untouched.
        _stash_error(account, "provider returned no result (unsupported or transient)")
        return None

    _stash_result(account, models)
    return models


def _stash_result(account: dict[str, Any], models: list[str]) -> None:
    metadata = dict(account.get("provider_metadata") or {})
    metadata["available_models"] = list(models)
    metadata["available_models_refreshed_at"] = _utcnow().isoformat()
    metadata["available_models_error"] = None
    account["provider_metadata"] = metadata


def _stash_error(account: dict[str, Any], error: str) -> None:
    metadata = dict(account.get("provider_metadata") or {})
    metadata["available_models_error"] = error[:500]
    # Don't touch available_models / refreshed_at — last successful snapshot
    # stays in place until the next successful refresh.
    account["provider_metadata"] = metadata

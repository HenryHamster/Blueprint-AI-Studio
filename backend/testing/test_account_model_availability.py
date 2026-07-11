"""Per-account model-availability tests.

Covers:
- provider.list_available_models for Gemini (API key) and OpenAI, with
  httpx mocks
- account_has_model / availability caching round-trip
- refresh_account_available_models mutates provider_metadata correctly
- select_max_plan_account filter skips accounts that don't have the
  requested model
- Preview→GA fallback in select_llm_runtime_for_task
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock

import pytest

from app.account_model_availability import (
    account_availability_is_stale,
    account_has_model,
    refresh_account_available_models,
)
from app.max_plan_pool import (
    _account_by_id,
    _max_plan_pool_lock,
    _max_plan_pools,
    select_max_plan_account,
    select_max_plan_account_balanced,
)
from app.models import LLMAuthMode, LLMProviderName
from app.providers.registry import get_provider

OPUS = "claude-opus-4-7"


# ---------------------------------------------------------------------------
# Gemini list_available_models — happy path and soft-failure path.
# ---------------------------------------------------------------------------


class TestGeminiListAvailableModels:
    @pytest.mark.asyncio
    async def test_api_key_parses_models_prefix(self, mocker):
        """v1beta response has {"models":[{"name":"models/gemini-2.5-pro", ...}]}.
        Strip the `models/` prefix so output is plain IDs."""
        provider = get_provider(LLMProviderName.GEMINI)
        mock_resp = mocker.MagicMock(status_code=200)
        mock_resp.json = mocker.MagicMock(
            return_value={
                "models": [
                    {"name": "models/gemini-2.5-pro"},
                    {"name": "models/gemini-2.5-flash"},
                    {"name": "models/gemini-3.1-pro-preview"},
                    # Duplicates + malformed entries must not crash the parser.
                    {"name": "models/gemini-2.5-pro"},
                    {"not_a_name": "bogus"},
                    None,
                ]
            }
        )
        mock_resp.raise_for_status = mocker.MagicMock()
        mock_client = mocker.MagicMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)
        mock_client.get = AsyncMock(return_value=mock_resp)
        mocker.patch("httpx.AsyncClient", return_value=mock_client)

        models = await provider.list_available_models(
            "AIza" + "x" * 30, LLMAuthMode.API_KEY, {}
        )
        assert models == [
            "gemini-2.5-flash",
            "gemini-2.5-pro",
            "gemini-3.1-pro-preview",
        ]

    @pytest.mark.asyncio
    async def test_service_account_returns_none(self):
        """SA mode not wired yet (needs OAuth2 token minting); contract says
        None means 'don't filter'. Regression: callers must not 500 here."""
        provider = get_provider(LLMProviderName.GEMINI)
        result = await provider.list_available_models(
            '{"type":"service_account","client_email":"x","private_key":"y"}',
            LLMAuthMode.SERVICE_ACCOUNT,
            {"project": "demo", "location": "us-central1"},
        )
        assert result is None

    @pytest.mark.asyncio
    async def test_network_error_returns_none(self, mocker):
        """Any exception from the HTTP call → None (not raise). Keeps a
        flaky network from starving a working account."""
        provider = get_provider(LLMProviderName.GEMINI)
        mock_client = mocker.MagicMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)
        mock_client.get = AsyncMock(side_effect=RuntimeError("connection refused"))
        mocker.patch("httpx.AsyncClient", return_value=mock_client)

        result = await provider.list_available_models(
            "AIza" + "x" * 30, LLMAuthMode.API_KEY, {}
        )
        assert result is None

    @pytest.mark.asyncio
    async def test_empty_secret_returns_none(self):
        provider = get_provider(LLMProviderName.GEMINI)
        assert await provider.list_available_models("", LLMAuthMode.API_KEY, {}) is None
        assert (
            await provider.list_available_models(None, LLMAuthMode.API_KEY, {}) is None
        )


# ---------------------------------------------------------------------------
# OpenAI list_available_models.
# ---------------------------------------------------------------------------


class TestOpenAIListAvailableModels:
    @pytest.mark.asyncio
    async def test_api_key_parses_data_ids(self, mocker):
        """v1/models response: {"data":[{"id":"gpt-5.4", ...}, ...]}."""
        provider = get_provider(LLMProviderName.OPENAI)
        mock_resp = mocker.MagicMock(status_code=200)
        mock_resp.json = mocker.MagicMock(
            return_value={
                "data": [
                    {"id": "gpt-5.4", "object": "model"},
                    {"id": "gpt-5.4-mini"},
                    {"id": "gpt-5.3-codex"},
                    {"no_id": True},  # malformed; skip
                ]
            }
        )
        mock_resp.raise_for_status = mocker.MagicMock()
        mock_client = mocker.MagicMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)
        mock_client.get = AsyncMock(return_value=mock_resp)
        mocker.patch("httpx.AsyncClient", return_value=mock_client)

        models = await provider.list_available_models(
            "sk-proj-abc", LLMAuthMode.API_KEY, {}
        )
        assert models == ["gpt-5.3-codex", "gpt-5.4", "gpt-5.4-mini"]

    @pytest.mark.asyncio
    async def test_oauth_mode_returns_none(self):
        """Codex OAuth access tokens aren't accepted by /v1/models — skip."""
        provider = get_provider(LLMProviderName.OPENAI)
        raw = '{"tokens":{"access_token":"at"}}'
        result = await provider.list_available_models(raw, LLMAuthMode.OAUTH, {})
        assert result is None


# ---------------------------------------------------------------------------
# DeepSeek list_available_models — GET /models at the API root (not under the
# /anthropic compat prefix), OpenAI-style {"data":[{"id": ...}]} response.
# ---------------------------------------------------------------------------


class TestDeepSeekListAvailableModels:
    @pytest.mark.asyncio
    async def test_api_key_parses_data_ids_from_api_root(self, mocker):
        provider = get_provider(LLMProviderName.DEEPSEEK)
        mock_resp = mocker.MagicMock(status_code=200)
        mock_resp.json = mocker.MagicMock(
            return_value={
                "object": "list",
                "data": [
                    {"id": "deepseek-v4-pro", "object": "model", "owned_by": "deepseek"},
                    {"id": "deepseek-v4-flash", "object": "model", "owned_by": "deepseek"},
                    {"no_id": True},  # malformed; skip
                ],
            }
        )
        mock_resp.raise_for_status = mocker.MagicMock()
        mock_client = mocker.MagicMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)
        mock_client.get = AsyncMock(return_value=mock_resp)
        mocker.patch("httpx.AsyncClient", return_value=mock_client)

        models = await provider.list_available_models(
            "ds-test-key", LLMAuthMode.API_KEY, {}
        )
        assert models == ["deepseek-v4-flash", "deepseek-v4-pro"]
        # The listing endpoint lives at the API root — the /anthropic compat
        # prefix must be stripped from the default base URL.
        url = mock_client.get.await_args.args[0]
        assert url == "https://api.deepseek.com/models"
        headers = mock_client.get.await_args.kwargs["headers"]
        assert headers["Authorization"] == "Bearer ds-test-key"

    @pytest.mark.asyncio
    async def test_metadata_base_url_override_maps_to_root(self, mocker):
        provider = get_provider(LLMProviderName.DEEPSEEK)
        mock_resp = mocker.MagicMock(status_code=200)
        mock_resp.json = mocker.MagicMock(return_value={"data": []})
        mock_resp.raise_for_status = mocker.MagicMock()
        mock_client = mocker.MagicMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)
        mock_client.get = AsyncMock(return_value=mock_resp)
        mocker.patch("httpx.AsyncClient", return_value=mock_client)

        models = await provider.list_available_models(
            "ds-test-key",
            LLMAuthMode.API_KEY,
            {"anthropic_base_url": "https://proxy.example/deepseek/anthropic"},
        )
        assert models == []
        url = mock_client.get.await_args.args[0]
        assert url == "https://proxy.example/deepseek/models"

    @pytest.mark.asyncio
    async def test_network_error_returns_none(self, mocker):
        """Contract: never raise; None = unknown, don't filter against it."""
        provider = get_provider(LLMProviderName.DEEPSEEK)
        mock_client = mocker.MagicMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)
        mock_client.get = AsyncMock(side_effect=RuntimeError("connection refused"))
        mocker.patch("httpx.AsyncClient", return_value=mock_client)

        result = await provider.list_available_models(
            "ds-test-key", LLMAuthMode.API_KEY, {}
        )
        assert result is None

    @pytest.mark.asyncio
    async def test_empty_secret_returns_none(self):
        provider = get_provider(LLMProviderName.DEEPSEEK)
        assert await provider.list_available_models("", LLMAuthMode.API_KEY, {}) is None
        assert (
            await provider.list_available_models(None, LLMAuthMode.API_KEY, {}) is None
        )

    @pytest.mark.asyncio
    async def test_unsupported_auth_mode_returns_none_not_raise(self):
        """A corrupt/miskeyed account row (oauth on deepseek) must resolve to
        unknown per the never-raises contract, not blow up the sweeper."""
        provider = get_provider(LLMProviderName.DEEPSEEK)
        result = await provider.list_available_models(
            "ds-test-key", LLMAuthMode.OAUTH, {}
        )
        assert result is None

    @pytest.mark.asyncio
    async def test_non_object_json_body_returns_none(self, mocker):
        """A syntactically valid but non-object body (bare array) is treated
        as unknown — never raises out of the contract."""
        provider = get_provider(LLMProviderName.DEEPSEEK)
        mock_resp = mocker.MagicMock(status_code=200)
        mock_resp.json = mocker.MagicMock(return_value=[{"id": "deepseek-v4-pro"}])
        mock_resp.raise_for_status = mocker.MagicMock()
        mock_client = mocker.MagicMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)
        mock_client.get = AsyncMock(return_value=mock_resp)
        mocker.patch("httpx.AsyncClient", return_value=mock_client)

        result = await provider.list_available_models(
            "ds-test-key", LLMAuthMode.API_KEY, {}
        )
        assert result is None

    @pytest.mark.asyncio
    async def test_alien_vocabulary_fails_open_to_none(self, mocker):
        """Fleet-starvation guard: the sweeper refreshes every account with
        this result and the pool hard-filters accounts whose list lacks the
        requested tier-map id. A successful listing containing NONE of the
        documented ids is far more likely a catalog rename than every key
        losing access, so it must resolve to unknown (None) — while an EMPTY
        list is preserved as the legitimate revoked-key signal."""
        provider = get_provider(LLMProviderName.DEEPSEEK)

        def client_returning(payload):
            mock_resp = mocker.MagicMock(status_code=200)
            mock_resp.json = mocker.MagicMock(return_value=payload)
            mock_resp.raise_for_status = mocker.MagicMock()
            mock_client = mocker.MagicMock()
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=None)
            mock_client.get = AsyncMock(return_value=mock_resp)
            return mock_client

        mocker.patch(
            "httpx.AsyncClient",
            return_value=client_returning(
                {"data": [{"id": "deepseek-v5-ultra"}, {"id": "deepseek-v5-mini"}]}
            ),
        )
        assert (
            await provider.list_available_models("ds-test-key", LLMAuthMode.API_KEY, {})
            is None
        )

        mocker.patch(
            "httpx.AsyncClient", return_value=client_returning({"data": []})
        )
        assert (
            await provider.list_available_models("ds-test-key", LLMAuthMode.API_KEY, {})
            == []
        )


# ---------------------------------------------------------------------------
# Availability cache helpers — account_has_model / refresh.
# ---------------------------------------------------------------------------


class TestAccountHasModel:
    def test_returns_true_when_in_list(self):
        acct = {"provider_metadata": {"available_models": ["gemini-2.5-pro"]}}
        assert account_has_model(acct, "gemini-2.5-pro") is True

    def test_returns_false_when_confirmed_missing(self):
        acct = {"provider_metadata": {"available_models": ["gemini-2.5-pro"]}}
        assert account_has_model(acct, "gemini-3.1-pro-preview") is False

    def test_returns_none_when_never_probed(self):
        """None means 'unknown' — callers must NOT skip the account."""
        acct = {"provider_metadata": {}}
        assert account_has_model(acct, "gemini-2.5-pro") is None

    def test_returns_none_when_metadata_missing(self):
        acct = {}
        assert account_has_model(acct, "gemini-2.5-pro") is None

    def test_returns_false_on_empty_probed_list(self):
        """Confirmed-empty list (key exists but has no models) → False.
        Distinguishes 'revoked key' from 'never probed'."""
        acct = {"provider_metadata": {"available_models": []}}
        assert account_has_model(acct, "gemini-2.5-pro") is False


class TestRefreshAccountAvailableModels:
    @pytest.mark.asyncio
    async def test_stashes_result_and_timestamp(self, mocker):
        mock_resp = mocker.MagicMock(status_code=200)
        mock_resp.json = mocker.MagicMock(
            return_value={"models": [{"name": "models/gemini-2.5-pro"}]}
        )
        mock_resp.raise_for_status = mocker.MagicMock()
        mock_client = mocker.MagicMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)
        mock_client.get = AsyncMock(return_value=mock_resp)
        mocker.patch("httpx.AsyncClient", return_value=mock_client)

        account = {
            "id": "abc",
            "provider": LLMProviderName.GEMINI.value,
            "auth_mode": LLMAuthMode.API_KEY.value,
            "runtime_secret": "AIza" + "x" * 30,
            "provider_metadata": {},
        }
        result = await refresh_account_available_models(account)
        assert result == ["gemini-2.5-pro"]
        md = account["provider_metadata"]
        assert md["available_models"] == ["gemini-2.5-pro"]
        assert md["available_models_refreshed_at"]  # ISO timestamp set
        assert md["available_models_error"] is None

    @pytest.mark.asyncio
    async def test_preserves_stale_list_on_failure(self, mocker):
        """Failed refresh stashes error but leaves the last good snapshot
        untouched — a flaky network shouldn't starve the dispatcher."""
        mock_client = mocker.MagicMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)
        mock_client.get = AsyncMock(side_effect=RuntimeError("boom"))
        mocker.patch("httpx.AsyncClient", return_value=mock_client)

        account = {
            "id": "abc",
            "provider": LLMProviderName.GEMINI.value,
            "auth_mode": LLMAuthMode.API_KEY.value,
            "runtime_secret": "AIza" + "x" * 30,
            "provider_metadata": {
                "available_models": ["gemini-2.5-pro"],
                "available_models_refreshed_at": "2026-04-20T00:00:00+00:00",
            },
        }
        result = await refresh_account_available_models(account)
        assert result is None
        md = account["provider_metadata"]
        # Prior list + timestamp untouched; error string recorded.
        assert md["available_models"] == ["gemini-2.5-pro"]
        assert md["available_models_refreshed_at"] == "2026-04-20T00:00:00+00:00"
        assert md["available_models_error"]

    @pytest.mark.asyncio
    async def test_no_secret_returns_none(self):
        account = {
            "provider": LLMProviderName.GEMINI.value,
            "auth_mode": LLMAuthMode.API_KEY.value,
            "runtime_secret": None,
        }
        assert await refresh_account_available_models(account) is None


class TestStalenessDetection:
    def test_missing_timestamp_is_stale(self):
        assert account_availability_is_stale({"provider_metadata": {}}) is True
        assert account_availability_is_stale({}) is True

    def test_recent_is_fresh(self):
        from datetime import datetime, timezone

        now = datetime.now(timezone.utc)
        acct = {
            "provider_metadata": {
                "available_models_refreshed_at": (
                    now - timedelta(minutes=5)
                ).isoformat(),
            }
        }
        assert account_availability_is_stale(acct, now=now) is False

    def test_old_timestamp_is_stale(self):
        from datetime import datetime, timezone

        now = datetime.now(timezone.utc)
        acct = {
            "provider_metadata": {
                "available_models_refreshed_at": (
                    now - timedelta(hours=12)
                ).isoformat(),
            }
        }
        assert account_availability_is_stale(acct, now=now) is True


class TestSuccessRateSelection:
    @pytest.mark.asyncio
    async def test_account_lacking_model_is_skipped_even_with_lower_active_tasks(self):
        """Account confirmed to lack the requested model is skipped even when it
        has fewer active_tasks than the account that does have the model."""
        user_id = f"u-{uuid.uuid4().hex}"
        no_model_id = f"acct-no-model-{uuid.uuid4().hex}"
        has_model_id = f"acct-has-model-{uuid.uuid4().hex}"
        no_model = {
            "id": no_model_id,
            "name": "no-model",
            "enabled": True,
            "oauth_token": "tok-no-model",
            "active_tasks": 0,  # Would have won without availability filtering.
            # Confirmed-empty list: account_has_model returns False → skipped.
            "provider_metadata": {"available_models": []},
        }
        has_model = {
            "id": has_model_id,
            "name": "has-model",
            "enabled": True,
            "oauth_token": "tok-has-model",
            "active_tasks": 1,
            "provider_metadata": {"available_models": ["claude-opus-4-7"]},
        }

        async with _max_plan_pool_lock:
            _max_plan_pools[user_id] = [no_model, has_model]
            _account_by_id[no_model_id] = no_model
            _account_by_id[has_model_id] = has_model

        try:
            selected = await select_max_plan_account(
                user_id=user_id, model="claude-opus-4-7"
            )
            assert selected is not None
            assert selected["id"] == has_model_id
        finally:
            async with _max_plan_pool_lock:
                _max_plan_pools.pop(user_id, None)
                _account_by_id.pop(no_model_id, None)
                _account_by_id.pop(has_model_id, None)

    @pytest.mark.asyncio
    async def test_success_rate_tiebreaks_within_available_accounts(self):
        """Among accounts that pass the availability filter, 7d success rate wins
        over active_tasks as the tiebreaker."""
        user_id = f"u-{uuid.uuid4().hex}"
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        low_success_id = f"acct-low-{uuid.uuid4().hex}"
        high_success_id = f"acct-high-{uuid.uuid4().hex}"
        low_success = {
            "id": low_success_id,
            "name": "low-success",
            "enabled": True,
            "oauth_token": "tok-low",
            "active_tasks": 0,  # Would have won on active_tasks alone.
            "provider_metadata": {"available_models": ["claude-opus-4-7"]},
            "outcome_events": [
                {"at": now - timedelta(hours=i + 1), "success": i == 0}
                for i in range(5)
            ],
        }
        high_success = {
            "id": high_success_id,
            "name": "high-success",
            "enabled": True,
            "oauth_token": "tok-high",
            "active_tasks": 1,
            "provider_metadata": {"available_models": ["claude-opus-4-7"]},
            "outcome_events": [
                {"at": now - timedelta(hours=i + 1), "success": i < 4} for i in range(5)
            ],
        }

        async with _max_plan_pool_lock:
            _max_plan_pools[user_id] = [low_success, high_success]
            _account_by_id[low_success_id] = low_success
            _account_by_id[high_success_id] = high_success

        try:
            selected = await select_max_plan_account(
                user_id=user_id, model="claude-opus-4-7"
            )
            assert selected is not None
            assert selected["id"] == high_success_id
        finally:
            async with _max_plan_pool_lock:
                _max_plan_pools.pop(user_id, None)
                _account_by_id.pop(low_success_id, None)
                _account_by_id.pop(high_success_id, None)


# ---------------------------------------------------------------------------
# select_max_plan_account_balanced — success-rate biasing
# ---------------------------------------------------------------------------


class TestBalancedSuccessRateSelection:
    """Covers four scenarios for balanced-mode dispatch with 7d success rate.

    Tests 1, 3, 4 exercise current (stable) behaviour — load balancing and
    probing semantics — and must pass both before and after the sibling
    cluster-1/2 change.  Test 2 is marked xfail(strict=False): it checks the
    new success-rate tiebreak that cluster 1/2 adds to
    select_max_plan_account_balanced; it will auto-promote to XPASS once that
    change merges.
    """

    @pytest.mark.asyncio
    async def test_low_sample_account_wins_on_lower_load(self):
        """A low-sample account (< 3 outcomes, no rate computed) is not filtered
        out.  When its active_tasks count is lower it still wins over a
        high-success-rate account that is busier."""
        user_id = f"u-{uuid.uuid4().hex}"
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        low_sample_id = f"acct-ls-{uuid.uuid4().hex}"
        high_success_id = f"acct-hs-{uuid.uuid4().hex}"
        low_sample = {
            "id": low_sample_id,
            "name": "low-sample",
            "enabled": True,
            "oauth_token": "tok",
            "active_tasks": 0,
            "outcome_events": [  # 2 events — below MIN_SAMPLES threshold
                {"at": now - timedelta(hours=1), "success": False},
                {"at": now - timedelta(hours=2), "success": True},
            ],
        }
        high_success = {
            "id": high_success_id,
            "name": "high-success",
            "enabled": True,
            "oauth_token": "tok",
            "active_tasks": 3,  # More busy than low_sample
            "outcome_events": [
                {"at": now - timedelta(hours=i + 1), "success": True} for i in range(5)
            ],
        }
        async with _max_plan_pool_lock:
            _max_plan_pools[user_id] = [low_sample, high_success]
            _account_by_id[low_sample_id] = low_sample
            _account_by_id[high_success_id] = high_success
        try:
            result = await select_max_plan_account_balanced(
                user_id=user_id,
                provider_models=[("anthropic", None)],
            )
            assert result is not None
            assert result[0]["id"] == low_sample_id
        finally:
            async with _max_plan_pool_lock:
                _max_plan_pools.pop(user_id, None)
                _account_by_id.pop(low_sample_id, None)
                _account_by_id.pop(high_success_id, None)

    @pytest.mark.asyncio
    @pytest.mark.xfail(
        strict=False, reason="Balanced success-rate tiebreak added by cluster 1/2"
    )
    async def test_equal_load_high_sample_high_success_wins(self):
        """Among healthy candidates with equal active_tasks, the account with a
        materially higher 7d success rate should be selected over one with a low
        rate.  Ids are assigned so that the low-success account would win the
        current alphabetic tiebreak — the xfail verifies the new behaviour."""
        user_id = f"u-{uuid.uuid4().hex}"
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        # "aaa-..." sorts before "zzz-..." so current code picks low_success.
        low_success_id = "aaa-lo-" + uuid.uuid4().hex
        high_success_id = "zzz-hi-" + uuid.uuid4().hex
        low_success = {
            "id": low_success_id,
            "name": "low-success",
            "enabled": True,
            "oauth_token": "tok",
            "active_tasks": 0,
            "outcome_events": [
                {"at": now - timedelta(hours=i + 1), "success": i == 0}
                for i in range(5)  # 1/5 = 20 %
            ],
        }
        high_success = {
            "id": high_success_id,
            "name": "high-success",
            "enabled": True,
            "oauth_token": "tok",
            "active_tasks": 0,
            "outcome_events": [
                {"at": now - timedelta(hours=i + 1), "success": i < 4}
                for i in range(5)  # 4/5 = 80 %
            ],
        }
        async with _max_plan_pool_lock:
            _max_plan_pools[user_id] = [low_success, high_success]
            _account_by_id[low_success_id] = low_success
            _account_by_id[high_success_id] = high_success
        try:
            result = await select_max_plan_account_balanced(
                user_id=user_id,
                provider_models=[("anthropic", None)],
            )
            assert result is not None
            assert result[0]["id"] == high_success_id
        finally:
            async with _max_plan_pool_lock:
                _max_plan_pools.pop(user_id, None)
                _account_by_id.pop(low_success_id, None)
                _account_by_id.pop(high_success_id, None)

    @pytest.mark.asyncio
    async def test_equal_success_tiebreaks_by_lower_load(self):
        """When both accounts have identical success rates, the less-loaded one wins."""
        user_id = f"u-{uuid.uuid4().hex}"
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        busy_id = f"acct-busy-{uuid.uuid4().hex}"
        idle_id = f"acct-idle-{uuid.uuid4().hex}"
        busy = {
            "id": busy_id,
            "name": "busy",
            "enabled": True,
            "oauth_token": "tok",
            "active_tasks": 3,
            "outcome_events": [
                {"at": now - timedelta(hours=i + 1), "success": True} for i in range(5)
            ],
        }
        idle = {
            "id": idle_id,
            "name": "idle",
            "enabled": True,
            "oauth_token": "tok",
            "active_tasks": 0,
            "outcome_events": [
                {"at": now - timedelta(hours=i + 1), "success": True} for i in range(5)
            ],
        }
        async with _max_plan_pool_lock:
            _max_plan_pools[user_id] = [busy, idle]
            _account_by_id[busy_id] = busy
            _account_by_id[idle_id] = idle
        try:
            result = await select_max_plan_account_balanced(
                user_id=user_id,
                provider_models=[("anthropic", None)],
            )
            assert result is not None
            assert result[0]["id"] == idle_id
        finally:
            async with _max_plan_pool_lock:
                _max_plan_pools.pop(user_id, None)
                _account_by_id.pop(busy_id, None)
                _account_by_id.pop(idle_id, None)

    @pytest.mark.asyncio
    async def test_task_type_scoped_bias_in_balanced_selection(self):
        """When task_type is passed, balanced selection uses task-type-scoped
        success rate instead of (or as fallback from) the aggregate."""
        user_id = f"u-{uuid.uuid4().hex}"
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        good_id = f"acct-good-{uuid.uuid4().hex}"
        bad_id = f"acct-bad-{uuid.uuid4().hex}"
        # good: 3/3 "create" successes; bad: 0/3 "create" successes.
        good = {
            "id": good_id,
            "name": "good",
            "enabled": True,
            "oauth_token": "tok",
            "active_tasks": 0,
            "outcome_events": [
                {
                    "at": now - timedelta(hours=i + 1),
                    "success": True,
                    "task_type": "create",
                }
                for i in range(3)
            ],
        }
        bad = {
            "id": bad_id,
            "name": "bad",
            "enabled": True,
            "oauth_token": "tok",
            "active_tasks": 0,
            "outcome_events": [
                {
                    "at": now - timedelta(hours=i + 1),
                    "success": False,
                    "task_type": "create",
                }
                for i in range(5)
            ],
        }
        async with _max_plan_pool_lock:
            _max_plan_pools[user_id] = [good, bad]
            _account_by_id[good_id] = good
            _account_by_id[bad_id] = bad
        try:
            result = await select_max_plan_account_balanced(
                user_id=user_id,
                provider_models=[("anthropic", None)],
                task_type="create",
            )
            assert result is not None
            assert result[0]["id"] == good_id
        finally:
            async with _max_plan_pool_lock:
                _max_plan_pools.pop(user_id, None)
                _account_by_id.pop(good_id, None)
                _account_by_id.pop(bad_id, None)

    @pytest.mark.asyncio
    async def test_all_low_success_rates_fall_back_to_load_balance(self):
        """When all accounts have uniformly low success rates the soft-bias
        filter keeps the full pool — no account is starved — and the
        load-based tiebreak picks the least-busy one."""
        user_id = f"u-{uuid.uuid4().hex}"
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        busy_id = f"acct-busy-{uuid.uuid4().hex}"
        idle_id = f"acct-idle-{uuid.uuid4().hex}"
        busy = {
            "id": busy_id,
            "name": "busy-low",
            "enabled": True,
            "oauth_token": "tok",
            "active_tasks": 2,
            # 0% success — lowest possible
            "outcome_events": [
                {"at": now - timedelta(hours=i + 1), "success": False} for i in range(5)
            ],
        }
        idle = {
            "id": idle_id,
            "name": "idle-low",
            "enabled": True,
            "oauth_token": "tok",
            "active_tasks": 0,
            # 20% success — also low, but this account is idle
            "outcome_events": [
                {"at": now - timedelta(hours=i + 1), "success": i == 0}
                for i in range(5)
            ],
        }
        async with _max_plan_pool_lock:
            _max_plan_pools[user_id] = [busy, idle]
            _account_by_id[busy_id] = busy
            _account_by_id[idle_id] = idle
        try:
            result = await select_max_plan_account_balanced(
                user_id=user_id,
                provider_models=[("anthropic", None)],
            )
            # Must not starve: selection succeeds even when all rates are low.
            assert result is not None
            # Pool median = 0.20 (upper element of sorted [0.0, 0.2]);
            # threshold = 0.20 - 0.20 = 0.0 → busy (0.0 >= 0.0) survives too,
            # so both accounts pass and load is the tiebreak.
            assert result[0]["id"] == idle_id
        finally:
            async with _max_plan_pool_lock:
                _max_plan_pools.pop(user_id, None)
                _account_by_id.pop(busy_id, None)
                _account_by_id.pop(idle_id, None)

    @pytest.mark.asyncio
    async def test_probing_account_eligible_when_idle(self):
        """A probing-state account (cooldown elapsed, active_tasks == 0) is
        selected in balanced mode when no healthy account is available."""
        user_id = f"u-{uuid.uuid4().hex}"
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        probing_id = f"acct-probe-{uuid.uuid4().hex}"
        probing = {
            "id": probing_id,
            "name": "probing",
            "enabled": True,
            "oauth_token": "tok",
            "active_tasks": 0,
            # hits=1 → 30 min cooldown; at=2h ago → cooldown elapsed → probing
            "disabled_models": {
                OPUS: {"hits": 1, "at": now - timedelta(hours=2)},
            },
        }
        async with _max_plan_pool_lock:
            _max_plan_pools[user_id] = [probing]
            _account_by_id[probing_id] = probing
        try:
            result = await select_max_plan_account_balanced(
                user_id=user_id,
                provider_models=[("anthropic", OPUS)],
            )
            assert result is not None
            assert result[0]["id"] == probing_id
        finally:
            async with _max_plan_pool_lock:
                _max_plan_pools.pop(user_id, None)
                _account_by_id.pop(probing_id, None)


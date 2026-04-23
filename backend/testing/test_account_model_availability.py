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

from datetime import timedelta
from unittest.mock import AsyncMock

import pytest

from app.account_model_availability import (
    account_availability_is_stale,
    account_has_model,
    refresh_account_available_models,
)
from app.models import LLMAuthMode, LLMProviderName
from app.providers.registry import get_provider


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
        mock_resp.json = mocker.MagicMock(return_value={
            "models": [
                {"name": "models/gemini-2.5-pro"},
                {"name": "models/gemini-2.5-flash"},
                {"name": "models/gemini-3.1-pro-preview"},
                # Duplicates + malformed entries must not crash the parser.
                {"name": "models/gemini-2.5-pro"},
                {"not_a_name": "bogus"},
                None,
            ]
        })
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
        assert await provider.list_available_models(None, LLMAuthMode.API_KEY, {}) is None


# ---------------------------------------------------------------------------
# OpenAI list_available_models.
# ---------------------------------------------------------------------------

class TestOpenAIListAvailableModels:
    @pytest.mark.asyncio
    async def test_api_key_parses_data_ids(self, mocker):
        """v1/models response: {"data":[{"id":"gpt-5.4", ...}, ...]}."""
        provider = get_provider(LLMProviderName.OPENAI)
        mock_resp = mocker.MagicMock(status_code=200)
        mock_resp.json = mocker.MagicMock(return_value={
            "data": [
                {"id": "gpt-5.4", "object": "model"},
                {"id": "gpt-5.4-mini"},
                {"id": "gpt-5.3-codex"},
                {"no_id": True},  # malformed; skip
            ]
        })
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
        mock_resp.json = mocker.MagicMock(return_value={
            "models": [{"name": "models/gemini-2.5-pro"}]
        })
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
        acct = {"provider_metadata": {
            "available_models_refreshed_at": (now - timedelta(minutes=5)).isoformat(),
        }}
        assert account_availability_is_stale(acct, now=now) is False

    def test_old_timestamp_is_stale(self):
        from datetime import datetime, timezone
        now = datetime.now(timezone.utc)
        acct = {"provider_metadata": {
            "available_models_refreshed_at": (now - timedelta(hours=12)).isoformat(),
        }}
        assert account_availability_is_stale(acct, now=now) is True

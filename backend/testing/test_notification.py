"""Tests for notification.py — _send_webhook_notification SSRF protection."""

import os
from unittest.mock import AsyncMock, patch

import pytest

from app.notification import _send_webhook_notification


# ---------------------------------------------------------------------------
# _send_webhook_notification — SSRF guard
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_skips_when_url_not_set():
    """No PIPELINE_WEBHOOK_URL → early return, no HTTP call."""
    with patch.dict(os.environ, {}, clear=True):
        # Should not raise and no HTTP call should be attempted
        await _send_webhook_notification("test", "title", "details")


@pytest.mark.asyncio
async def test_blocks_ssrf_unsafe_url(caplog):
    """Unsafe URL (e.g. private IP) → warning logged, no HTTP call."""
    unsafe_url = "http://169.254.169.254/metadata"
    with (
        patch.dict(os.environ, {"PIPELINE_WEBHOOK_URL": unsafe_url}),
        patch(
            "app.notification._resolve_safe_external_url",
            return_value=(False, "link-local/metadata address blocked", None),
        ),
        patch("httpx.AsyncClient") as mock_client,
    ):
        await _send_webhook_notification("test", "title", "details")

    mock_client.assert_not_called()
    assert "SSRF check blocked" in caplog.text
    assert unsafe_url in caplog.text


@pytest.mark.asyncio
async def test_safe_url_makes_pinned_post():
    """Safe URL → POST to resolved-IP pinned URL with Host header + SNI."""
    safe_url = "https://hooks.example.com/path"
    with (
        patch.dict(os.environ, {"PIPELINE_WEBHOOK_URL": safe_url}),
        patch(
            "app.notification._resolve_safe_external_url",
            return_value=(True, "", "93.184.216.34"),
        ),
        patch("httpx.AsyncClient") as mock_client_cls,
    ):
        # Simulate the context manager: AsyncClient() → async with client → client.post
        mock_client = AsyncMock()
        mock_client_cls.return_value.__aenter__.return_value = mock_client
        mock_client.post = AsyncMock()

        await _send_webhook_notification("test", "title", "details")

    # Verify the POST was made to the pinned IP, not the original hostname
    mock_client.post.assert_called_once()
    call_kwargs = mock_client.post.call_args.kwargs
    call_args = mock_client.post.call_args.args

    # The URL should use the resolved IP, not the original hostname
    pinned_url = call_args[0] if call_args else call_kwargs.get("url", "")
    assert "93.184.216.34" in pinned_url
    assert "hooks.example.com" not in pinned_url.replace(
        "93.184.216.34", ""
    ), "Original hostname should be replaced by IP"

    # Host header must preserve the original hostname (virtual hosting)
    headers = call_kwargs.get("headers", {})
    assert headers.get("Host") == "hooks.example.com"

    # SNI must preserve the original hostname (TLS cert validation)
    extensions = call_kwargs.get("extensions", {})
    assert extensions.get("sni_hostname") == "hooks.example.com"

    # The JSON payload should contain our event data
    json_payload = call_kwargs.get("json", {})
    assert json_payload["event_type"] == "test"
    assert json_payload["title"] == "title"
    assert json_payload["details"] == "details"
    assert "timestamp" in json_payload


@pytest.mark.asyncio
async def test_safe_url_with_port():
    """Safe URL with explicit port → pinned URL preserves port, Host header includes it."""
    safe_url = "https://hooks.example.com:8443/path"
    with (
        patch.dict(os.environ, {"PIPELINE_WEBHOOK_URL": safe_url}),
        patch(
            "app.notification._resolve_safe_external_url",
            return_value=(True, "", "93.184.216.34"),
        ),
        patch("httpx.AsyncClient") as mock_client_cls,
    ):
        mock_client = AsyncMock()
        mock_client_cls.return_value.__aenter__.return_value = mock_client
        mock_client.post = AsyncMock()

        await _send_webhook_notification("test", "title", "details")

    mock_client.post.assert_called_once()
    call_kwargs = mock_client.post.call_args.kwargs
    call_args = mock_client.post.call_args.args

    pinned_url = call_args[0] if call_args else call_kwargs.get("url", "")
    assert "93.184.216.34:8443" in pinned_url

    headers = call_kwargs.get("headers", {})
    assert headers.get("Host") == "hooks.example.com:8443"


@pytest.mark.asyncio
async def test_blocked_url_no_http_call(caplog):
    """Blocked URL → no httpx call, warning logged."""
    blocked_url = "http://10.0.0.1/hook"
    with (
        patch.dict(os.environ, {"PIPELINE_WEBHOOK_URL": blocked_url}),
        patch(
            "app.notification._resolve_safe_external_url",
            return_value=(False, "private RFC1918/RFC4193 address blocked", None),
        ),
        patch("httpx.AsyncClient") as mock_client,
    ):
        await _send_webhook_notification("test", "title", "details")

    mock_client.assert_not_called()
    assert "SSRF check blocked" in caplog.text

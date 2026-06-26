"""Tests for CSRF middleware (double-submit cookie protection)."""

import os

import pytest
from httpx import AsyncClient, ASGITransport

os.environ.setdefault("AGENT_JWT_SECRET", "test-secret-key-for-csrf-tests-xx")
os.environ.setdefault("WS_TICKET_SECRET", "test-ws-ticket-for-csrf-tests-xxx")

from app.main import app  # noqa: E402
from app.csrf import CSRF_COOKIE_NAME, CSRF_HEADER_NAME  # noqa: E402


# ── Safe methods ───────────────────────────────────────────────────────────────


class TestCSRFSafeMethods:
    """GET/HEAD/OPTIONS pass through without CSRF validation."""

    @pytest.mark.asyncio
    async def test_get_request_passes_through(self):
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.get("/")
        assert resp.status_code == 200

    @pytest.mark.asyncio
    async def test_get_sets_csrf_cookie_when_absent(self):
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.get("/")
        assert CSRF_COOKIE_NAME in resp.cookies

    @pytest.mark.asyncio
    async def test_get_does_not_overwrite_existing_csrf_cookie(self):
        """A csrf_token cookie that is already present should not be reset."""
        existing = "already-set-csrf-token"
        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://test",
            cookies={CSRF_COOKIE_NAME: existing},
        ) as client:
            resp = await client.get("/")
        assert CSRF_COOKIE_NAME not in resp.cookies

    @pytest.mark.asyncio
    async def test_options_passes_through(self):
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.options("/")
        # OPTIONS may return 200 or 405 depending on routing; not 403 from CSRF
        assert resp.status_code != 403


# ── Bearer-token bypass ────────────────────────────────────────────────────────


class TestCSRFBearerBypass:
    """Requests with Authorization: Bearer are exempt from CSRF validation."""

    @pytest.mark.asyncio
    async def test_post_with_bearer_token_bypasses_csrf(self):
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.post(
                "/nonexistent-endpoint",
                headers={"Authorization": "Bearer fake-jwt"},
                json={},
            )
        assert resp.status_code != 403

    @pytest.mark.asyncio
    async def test_put_with_bearer_token_bypasses_csrf(self):
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.put(
                "/nonexistent-endpoint",
                headers={"Authorization": "Bearer fake-jwt"},
                json={},
            )
        assert resp.status_code != 403

    @pytest.mark.asyncio
    async def test_patch_with_bearer_token_bypasses_csrf(self):
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.patch(
                "/nonexistent-endpoint",
                headers={"Authorization": "Bearer fake-jwt"},
                json={},
            )
        assert resp.status_code != 403

    @pytest.mark.asyncio
    async def test_delete_with_bearer_token_bypasses_csrf(self):
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.delete(
                "/nonexistent-endpoint",
                headers={"Authorization": "Bearer fake-jwt"},
            )
        assert resp.status_code != 403

    @pytest.mark.asyncio
    async def test_bearer_case_insensitive(self):
        """Authorization header matching is case-insensitive on the scheme."""
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.post(
                "/nonexistent-endpoint",
                headers={"Authorization": "BEARER fake-jwt"},
                json={},
            )
        assert resp.status_code != 403


# ── CSRF token validation ──────────────────────────────────────────────────────


class TestCSRFTokenValidation:
    """Non-Bearer state-changing requests require a matching CSRF token."""

    @pytest.mark.asyncio
    async def test_post_without_csrf_token_returns_403(self):
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.post("/nonexistent-endpoint", json={})
        assert resp.status_code == 403
        assert "CSRF" in resp.json()["error"]["message"]

    @pytest.mark.asyncio
    async def test_post_with_cookie_but_no_header_returns_403(self):
        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://test",
            cookies={CSRF_COOKIE_NAME: "my-csrf-token"},
        ) as client:
            resp = await client.post(
                "/nonexistent-endpoint",
                json={},
            )
        assert resp.status_code == 403
        assert "missing" in resp.json()["error"]["message"]

    @pytest.mark.asyncio
    async def test_post_with_header_but_no_cookie_returns_403(self):
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.post(
                "/nonexistent-endpoint",
                headers={CSRF_HEADER_NAME: "some-token"},
                json={},
            )
        assert resp.status_code == 403
        assert "missing" in resp.json()["error"]["message"]

    @pytest.mark.asyncio
    async def test_post_with_mismatched_token_returns_403(self):
        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://test",
            cookies={CSRF_COOKIE_NAME: "real-token"},
        ) as client:
            resp = await client.post(
                "/nonexistent-endpoint",
                headers={CSRF_HEADER_NAME: "wrong-token"},
                json={},
            )
        assert resp.status_code == 403
        assert "mismatch" in resp.json()["error"]["message"]

    @pytest.mark.asyncio
    async def test_post_with_matching_csrf_token_passes_middleware(self):
        """Matching cookie + header bypasses CSRF; routing takes over (404 not 403)."""
        token = "valid-csrf-token-value"
        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://test",
            cookies={CSRF_COOKIE_NAME: token},
        ) as client:
            resp = await client.post(
                "/nonexistent-endpoint",
                headers={CSRF_HEADER_NAME: token},
                json={},
            )
        assert resp.status_code != 403

    @pytest.mark.asyncio
    async def test_patch_without_csrf_returns_403(self):
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.patch("/nonexistent-endpoint", json={})
        assert resp.status_code == 403

    @pytest.mark.asyncio
    async def test_delete_without_csrf_returns_403(self):
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.delete("/nonexistent-endpoint")
        assert resp.status_code == 403

    @pytest.mark.asyncio
    async def test_put_without_csrf_returns_403(self):
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.put("/nonexistent-endpoint", json={})
        assert resp.status_code == 403

    @pytest.mark.asyncio
    async def test_error_response_has_correct_shape(self):
        """CSRF 403 uses the standard error envelope format."""
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.post("/nonexistent-endpoint", json={})
        body = resp.json()
        assert "error" in body
        assert body["error"]["code"] == 403


# ── X-Agent-Token bypass ───────────────────────────────────────────────────────


class TestCSRFAgentTokenBypass:
    """Requests with X-Agent-Token are exempt from CSRF (agent fleet auth is not cookie-based)."""

    @pytest.mark.asyncio
    async def test_post_with_x_agent_token_bypasses_csrf(self):
        """Agent POST with X-Agent-Token and no Bearer/CSRF tokens must not be 403."""
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.post(
                "/nonexistent-endpoint",
                headers={"X-Agent-Token": "fake-agent-jwt"},
                json={},
            )
        assert resp.status_code != 403

    @pytest.mark.asyncio
    async def test_patch_with_x_agent_token_bypasses_csrf(self):
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.patch(
                "/nonexistent-endpoint",
                headers={"X-Agent-Token": "fake-agent-jwt"},
                json={},
            )
        assert resp.status_code != 403

    @pytest.mark.asyncio
    async def test_agent_token_without_bearer_is_sufficient(self):
        """X-Agent-Token alone (no Bearer, no CSRF cookie/header) must bypass CSRF."""
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.post(
                "/nonexistent-endpoint",
                headers={"X-Agent-Token": "fake-agent-jwt"},
                # deliberately no Authorization: Bearer, no csrf_token cookie, no X-CSRF-Token
                json={},
            )
        assert resp.status_code != 403


# ── Path-based exemptions ──────────────────────────────────────────────────────


class TestCSRFPathExemptions:
    """Paths in _EXEMPT_PATHS bypass CSRF (HMAC-signed server-to-server endpoints)."""

    @pytest.mark.asyncio
    async def test_github_webhook_post_bypasses_csrf(self):
        """GitHub webhook POSTs carry HMAC signatures, not CSRF tokens; must not 403."""
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.post(
                "/api/github/webhook",
                headers={"X-Hub-Signature-256": "sha256=fake"},
                json={"action": "opened"},
            )
        # The route handler may reject for other reasons (401, 422, 500) but CSRF must not block it.
        assert resp.status_code != 403

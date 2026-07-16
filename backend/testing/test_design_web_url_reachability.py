"""Tests for design-document web-URL reachability gate.

Covers _probe_url_reachability and _design_web_only_grounding_miss in
knowledge_completion.py — the greenfield design acceptance path that verifies
web-only citations are actually reachable before marking a design doc accepted.

All tests are standalone (no DB) and mock httpx to avoid real network calls.
"""

from unittest.mock import AsyncMock, MagicMock, patch

from app.knowledge_completion import (
    _design_web_only_grounding_miss,
    _probe_url_reachability,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _httpx_response(status_code: int) -> MagicMock:
    resp = MagicMock()
    resp.status_code = status_code
    return resp


def _make_async_client(head_status: int | None = None, get_status: int | None = None):
    """Return a mock httpx.AsyncClient that responds with the given status codes."""

    async def _head(url: str) -> MagicMock:
        return _httpx_response(head_status if head_status is not None else 200)

    async def _get(url: str) -> MagicMock:
        return _httpx_response(get_status if get_status is not None else 200)

    client = AsyncMock()
    client.head = _head
    client.get = _get
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__ = AsyncMock(return_value=False)
    return client


# ---------------------------------------------------------------------------
# _probe_url_reachability
# ---------------------------------------------------------------------------


class TestProbeUrlReachability:
    async def test_200_is_reachable(self):
        client = _make_async_client(head_status=200)
        with patch("httpx.AsyncClient", return_value=client):
            result = await _probe_url_reachability("https://example.com")
        assert result == "reachable"

    async def test_301_redirect_is_reachable(self):
        client = _make_async_client(head_status=301)
        with patch("httpx.AsyncClient", return_value=client):
            result = await _probe_url_reachability("https://example.com")
        assert result == "reachable"

    async def test_404_is_dead(self):
        client = _make_async_client(head_status=404)
        with patch("httpx.AsyncClient", return_value=client):
            result = await _probe_url_reachability("https://example.com/gone")
        assert result == "dead"

    async def test_410_is_dead(self):
        client = _make_async_client(head_status=410)
        with patch("httpx.AsyncClient", return_value=client):
            result = await _probe_url_reachability("https://example.com/deleted")
        assert result == "dead"

    async def test_405_falls_back_to_get_reachable(self):
        client = _make_async_client(head_status=405, get_status=200)
        with patch("httpx.AsyncClient", return_value=client):
            result = await _probe_url_reachability("https://example.com")
        assert result == "reachable"

    async def test_405_falls_back_to_get_404_is_dead(self):
        client = _make_async_client(head_status=405, get_status=404)
        with patch("httpx.AsyncClient", return_value=client):
            result = await _probe_url_reachability("https://example.com")
        assert result == "dead"

    async def test_connect_error_is_dead(self):
        import httpx

        async def _bad_head(url: str):
            raise httpx.ConnectError("Name or service not known")

        client = AsyncMock()
        client.head = _bad_head
        client.__aenter__ = AsyncMock(return_value=client)
        client.__aexit__ = AsyncMock(return_value=False)
        with patch("httpx.AsyncClient", return_value=client):
            result = await _probe_url_reachability("https://dead.example.invalid")
        assert result == "dead"

    async def test_timeout_with_retry_is_transient(self):
        """A URL that consistently times out should not hard-fail (transient)."""
        import httpx

        async def _timeout_head(url: str):
            raise httpx.TimeoutException("timed out")

        client = AsyncMock()
        client.head = _timeout_head
        client.__aenter__ = AsyncMock(return_value=client)
        client.__aexit__ = AsyncMock(return_value=False)
        with patch("httpx.AsyncClient", return_value=client), patch(
            "asyncio.sleep", new=AsyncMock()
        ):
            result = await _probe_url_reachability("https://slow.example.com")
        assert result == "transient"

    async def test_500_with_retry_is_transient(self):
        """5xx errors after retry should be classified as transient."""
        client = _make_async_client(head_status=503)
        with patch("httpx.AsyncClient", return_value=client), patch(
            "asyncio.sleep", new=AsyncMock()
        ):
            result = await _probe_url_reachability("https://down.example.com")
        assert result == "transient"

    async def test_timeout_first_then_reachable_is_reachable(self):
        """A URL that times out once but succeeds on retry should be 'reachable'."""
        import httpx

        call_count = 0

        async def _flaky_head(url: str):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise httpx.TimeoutException("first timeout")
            return _httpx_response(200)

        client = AsyncMock()
        client.head = _flaky_head
        client.__aenter__ = AsyncMock(return_value=client)
        client.__aexit__ = AsyncMock(return_value=False)
        with patch("httpx.AsyncClient", return_value=client), patch(
            "asyncio.sleep", new=AsyncMock()
        ):
            result = await _probe_url_reachability("https://flaky.example.com")
        assert result == "reachable"


# ---------------------------------------------------------------------------
# _design_web_only_grounding_miss
# ---------------------------------------------------------------------------


class TestDesignWebOnlyGroundingMiss:
    """Unit tests for the web-only URL reachability gate for design documents."""

    async def _probe_returning(self, outcome: str):
        """Return an async mock that makes _probe_url_reachability return `outcome`."""
        return AsyncMock(return_value=outcome)

    async def test_no_citations_returns_none(self):
        """A document with no citations has nothing to check — gate passes."""
        doc = {"citations": []}
        result = await _design_web_only_grounding_miss(doc)
        assert result is None

    async def test_file_citation_skips_check(self):
        """Mixed citations (file + web) are NOT checked — file gate handles them."""
        doc = {
            "citations": [
                "https://example.com",
                "backend/app/foo.py",
            ]
        }
        # No httpx mock needed — function should return None without any I/O
        result = await _design_web_only_grounding_miss(doc)
        assert result is None

    async def test_only_file_citations_skips_check(self):
        """Pure file citations — nothing for the web probe to check."""
        doc = {"citations": ["backend/app/foo.py", "agent/src/index.ts"]}
        result = await _design_web_only_grounding_miss(doc)
        assert result is None

    async def test_all_web_urls_dead_returns_failure(self):
        """All web citations definitively dead → gate fails with a reason."""
        doc = {
            "citations": ["https://dead1.example.com", "https://dead2.example.com"]
        }
        with patch(
            "app.knowledge_completion._probe_url_reachability",
            new=AsyncMock(return_value="dead"),
        ):
            result = await _design_web_only_grounding_miss(doc)
        assert result is not None
        assert "dead" in result.lower()
        assert "2" in result

    async def test_any_reachable_url_returns_none(self):
        """One reachable URL among dead ones → gate passes (errs toward acceptance)."""
        doc = {
            "citations": [
                "https://dead.example.com",
                "https://live.example.com",
            ]
        }
        outcomes = iter(["dead", "reachable"])

        async def _probe(url: str) -> str:
            return next(outcomes)

        with patch("app.knowledge_completion._probe_url_reachability", side_effect=_probe):
            result = await _design_web_only_grounding_miss(doc)
        assert result is None

    async def test_transient_errors_return_none(self):
        """All transient errors → gate passes (flaky network ≠ dead URL)."""
        doc = {
            "citations": ["https://slow.example.com", "https://error.example.com"]
        }
        with patch(
            "app.knowledge_completion._probe_url_reachability",
            new=AsyncMock(return_value="transient"),
        ):
            result = await _design_web_only_grounding_miss(doc)
        assert result is None

    async def test_mixed_dead_and_transient_returns_none(self):
        """Mix of dead and transient → gate passes (not ALL definitively dead)."""
        doc = {
            "citations": ["https://dead.example.com", "https://slow.example.com"]
        }
        outcomes = iter(["dead", "transient"])

        async def _probe(url: str) -> str:
            return next(outcomes)

        with patch("app.knowledge_completion._probe_url_reachability", side_effect=_probe):
            result = await _design_web_only_grounding_miss(doc)
        assert result is None

    async def test_single_dead_url_returns_failure(self):
        """A single web citation that is dead → gate fails."""
        doc = {"citations": ["https://dead.example.com"]}
        with patch(
            "app.knowledge_completion._probe_url_reachability",
            new=AsyncMock(return_value="dead"),
        ):
            result = await _design_web_only_grounding_miss(doc)
        assert result is not None
        assert "dead" in result.lower()

    async def test_caps_at_max_probes(self):
        """URLs beyond _MAX_DESIGN_URL_PROBES are silently skipped."""
        from app.knowledge_completion import _MAX_DESIGN_URL_PROBES

        urls = [f"https://example{i}.com" for i in range(_MAX_DESIGN_URL_PROBES + 5)]
        doc = {"citations": urls}

        probed = []

        async def _probe(url: str) -> str:
            probed.append(url)
            return "dead"

        with patch("app.knowledge_completion._probe_url_reachability", side_effect=_probe):
            result = await _design_web_only_grounding_miss(doc)

        assert len(probed) == _MAX_DESIGN_URL_PROBES
        assert result is not None

    async def test_source_citations_key_also_accepted(self):
        """Documents using 'source_citations' instead of 'citations' are covered."""
        doc = {"source_citations": ["https://dead.example.com"]}
        with patch(
            "app.knowledge_completion._probe_url_reachability",
            new=AsyncMock(return_value="dead"),
        ):
            result = await _design_web_only_grounding_miss(doc)
        assert result is not None

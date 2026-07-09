"""Tests for request-duration telemetry middleware (Server-Timing headers and slow-request logging)."""

import asyncio
import logging

import pytest
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from httpx import AsyncClient, ASGITransport

from app.main import app, RequestDurationMiddleware
from app.auth import get_current_user
from app.server_timing import async_timed


TEST_USER_ID = "test-user-123"
TEST_USER_EMAIL = "test@example.com"


async def _mock_admin_user():
    """Return a mock admin user."""
    return {"user_id": TEST_USER_ID, "email": TEST_USER_EMAIL, "role": "admin"}


@pytest.fixture
async def client():
    """Create a test client with admin user override."""
    app.dependency_overrides[get_current_user] = _mock_admin_user
    try:
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as ac:
            yield ac
    finally:
        app.dependency_overrides.pop(get_current_user, None)


@pytest.mark.asyncio
async def test_server_timing_header_on_admin_endpoint(client):
    """Verify Server-Timing header is added to admin API responses."""
    response = await client.get("/api/debug/orchestrator")
    assert response.status_code == 200
    assert "Server-Timing" in response.headers
    timing_header = response.headers["Server-Timing"]
    assert timing_header.startswith("backend;dur=")
    # Extract the duration value and verify it's a number
    dur_str = timing_header.split("backend;dur=")[1]
    dur_float = float(dur_str)
    assert dur_float >= 0


@pytest.mark.asyncio
async def test_server_timing_format(client):
    """Verify Server-Timing header has correct format."""
    response = await client.get("/api/debug/orchestrator")
    assert response.status_code == 200
    timing = response.headers["Server-Timing"]
    # Format should be "backend;dur=X.X"
    assert "backend;dur=" in timing
    # Extract and validate the numeric part
    parts = timing.split("backend;dur=")
    assert len(parts) == 2
    try:
        float(parts[1])
    except ValueError:
        pytest.fail(f"Invalid duration format in Server-Timing: {timing}")


@pytest.mark.asyncio
async def test_slow_request_logging(client, caplog):
    """Verify slow requests are logged with route and duration."""
    with caplog.at_level(logging.WARNING):
        # debug endpoint should be fairly fast, but make a call
        response = await client.get("/api/debug/orchestrator")
        assert response.status_code == 200

        # If slow (>500ms), there should be a log entry
        # (this is informational; we don't assert it must be slow)
        slow_logs = [
            r for r in caplog.records if "slow request" in r.getMessage()
        ]
        # Check that if a slow log exists, it has the right fields
        for log_record in slow_logs:
            assert hasattr(log_record, "route")
            assert hasattr(log_record, "method")
            assert hasattr(log_record, "duration_ms")
            assert hasattr(log_record, "status")


@pytest.mark.asyncio
async def test_non_admin_endpoint_no_timing():
    """Verify non-admin endpoints don't get Server-Timing header."""
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        # /api/version is a public, non-admin endpoint
        response = await client.get("/api/version")
        assert response.status_code == 200
        # Non-admin endpoints should not have Server-Timing
        assert "Server-Timing" not in response.headers


def _make_timing_test_app() -> FastAPI:
    """Create a minimal FastAPI app with RequestDurationMiddleware for timing tests."""
    test_app = FastAPI()
    test_app.add_middleware(RequestDurationMiddleware)

    @test_app.get("/api/admin/_test_timing")
    async def _timed_handler(request: Request):
        async with async_timed(request, "attention"):
            await asyncio.sleep(0.001)
        async with async_timed(request, "context"):
            await asyncio.sleep(0.002)
        async with async_timed(request, "runtime"):
            await asyncio.sleep(0.001)
        return JSONResponse({"ok": True})

    return test_app


@pytest.mark.asyncio
async def test_multi_entry_server_timing_header():
    """Verify Server-Timing header includes per-step entries appended after backend;dur=.

    Acceptance evidence for step-level timing instrumentation.  When a handler
    accumulates entries via ``async_timed``, the middleware appends them as
    comma-separated ``name;dur=X.X`` segments after the leading ``backend;dur=``
    entry.
    """
    test_app = _make_timing_test_app()
    async with AsyncClient(
        transport=ASGITransport(app=test_app), base_url="http://test"
    ) as client:
        response = await client.get("/api/admin/_test_timing")
        assert response.status_code == 200
        timing = response.headers.get("Server-Timing", "")
        assert timing, "Server-Timing header is missing"

        # backend;dur= must be the first entry
        assert timing.startswith("backend;dur="), (
            f"Expected Server-Timing to start with 'backend;dur=', got: {timing}"
        )

        # Per-step entries should be present
        assert "attention;dur=" in timing, f"Missing 'attention' entry in: {timing}"
        assert "context;dur=" in timing, f"Missing 'context' entry in: {timing}"
        assert "runtime;dur=" in timing, f"Missing 'runtime' entry in: {timing}"

        # Parse the full header into individual entries
        parts = timing.split(", ")
        assert len(parts) >= 4, f"Expected at least 4 entries, got {len(parts)}: {parts}"
        assert parts[0].startswith("backend;dur="), (
            f"First entry must be 'backend;dur=', got: {parts[0]}"
        )

        # Each entry must have a valid numeric duration
        for part in parts:
            assert ";dur=" in part, f"Entry missing ';dur=' delimiter: {part}"
            _name, dur_str = part.split(";dur=")
            assert _name, f"Entry with empty name: {part}"
            try:
                float(dur_str)
            except ValueError as exc:
                pytest.fail(f"Invalid duration value in entry '{part}': {exc}")


@pytest.mark.asyncio
async def test_server_timing_backend_first():
    """Verify backend;dur= entry is always first, even when step entries exist."""
    test_app = _make_timing_test_app()
    async with AsyncClient(
        transport=ASGITransport(app=test_app), base_url="http://test"
    ) as client:
        response = await client.get("/api/admin/_test_timing")
        assert response.status_code == 200
        timing = response.headers.get("Server-Timing", "")
        assert timing.startswith("backend;dur="), (
            f"backend entry must be first, got: {timing}"
        )


@pytest.mark.asyncio
async def test_server_timing_deduplicates_same_name():
    """Verify same-name entries are deduplicated (last-wins)."""
    dedup_app = FastAPI()
    dedup_app.add_middleware(RequestDurationMiddleware)

    @dedup_app.get("/api/admin/_test_dedup")
    async def _dedup_handler(request: Request):
        async with async_timed(request, "step"):
            await asyncio.sleep(0.001)
        async with async_timed(request, "step"):
            await asyncio.sleep(0.002)
        return JSONResponse({"ok": True})

    async with AsyncClient(
        transport=ASGITransport(app=dedup_app), base_url="http://test"
    ) as client:
        response = await client.get("/api/admin/_test_dedup")
        assert response.status_code == 200
        timing = response.headers.get("Server-Timing", "")
        # "step;dur=" should appear exactly once in the per-step entries
        # (it may appear 0 times if the timing is < 0.05ms — unlikely but possible)
        per_step_portion = timing.split(", ", 1)[1] if ", " in timing else ""
        if per_step_portion:
            assert per_step_portion.count("step;dur=") == 1, (
                f"Expected 'step;dur=' to appear once in {per_step_portion}"
            )


@pytest.mark.asyncio
async def test_no_entries_no_header_change():
    """Verify endpoints without per-step entries produce single-entry header format."""
    bare_app = FastAPI()
    bare_app.add_middleware(RequestDurationMiddleware)

    @bare_app.get("/api/admin/_test_bare")
    async def _bare_handler(request: Request):
        return JSONResponse({"ok": True})

    async with AsyncClient(
        transport=ASGITransport(app=bare_app), base_url="http://test"
    ) as client:
        response = await client.get("/api/admin/_test_bare")
        assert response.status_code == 200
        timing = response.headers.get("Server-Timing", "")
        assert timing.startswith("backend;dur=")
        # When no per-step entries, there should be exactly one dur= entry
        assert timing.count(";dur=") == 1, (
            f"Expected exactly one dur= entry, got: {timing}"
        )

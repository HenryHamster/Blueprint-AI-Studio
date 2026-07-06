"""Tests for request-duration telemetry middleware (Server-Timing headers and slow-request logging)."""

import logging

import pytest
from httpx import AsyncClient, ASGITransport

from app.main import app
from app.auth import get_current_user


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

"""Tests for the operator-inbox liveness observability endpoint.

Covers:
- GET /api/admin/operator-inbox-liveness returns snapshot shape
- ?force_run=true triggers a live check and returns run_results
- Non-admin user receives 403
- Endpoint returns 200 with zero counters when no ticks have fired
"""

import pytest

from httpx import AsyncClient, ASGITransport
from unittest.mock import patch, AsyncMock

from app.main import app
from app.auth import require_admin
import app.operator_inbox_liveness as _liveness_mod


@pytest.fixture(autouse=True)
def reset_liveness():
    """Reset in-process liveness counters before each test."""
    _liveness_mod._reset_liveness_for_tests()
    yield
    _liveness_mod._reset_liveness_for_tests()


# ---------------------------------------------------------------------------
# test_liveness_observability_endpoint_returns_snapshot
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_liveness_observability_endpoint_returns_snapshot(
    client: AsyncClient,
):
    """GET /api/admin/operator-inbox-liveness returns all five expected keys
    with correct types even when no liveness ticks have fired.
    """
    response = await client.get("/api/admin/operator-inbox-liveness")
    assert response.status_code == 200

    data = response.json()
    assert "total_ticks" in data
    assert "total_forward_leaked" in data
    assert "total_badge_invisible" in data
    assert "total_info_alert_noise" in data
    assert "recent_ticks" in data

    # With no ticks, all counters are 0 and recent_ticks is empty.
    assert data["total_ticks"] == 0
    assert data["total_forward_leaked"] == 0
    assert data["total_badge_invisible"] == 0
    assert data["total_info_alert_noise"] == 0
    assert data["recent_ticks"] == []


@pytest.mark.asyncio
async def test_liveness_snapshot_reflects_accumulated_state(client: AsyncClient):
    """After injecting counters, the endpoint reflects the accumulated state."""
    # Directly mutate the module-level counters (test-internal seam).
    _liveness_mod._total_ticks = 3
    _liveness_mod._total_forward_leaked = 7
    _liveness_mod._total_badge_invisible = 0
    _liveness_mod._total_info_alert_noise = 2
    _liveness_mod._observability_ring.append(
        {"tick": 3, "projects": 2, "flagged": 1, "forward_leaked": 7,
         "badge_invisible": 0, "info_alert_noise": 2}
    )

    response = await client.get("/api/admin/operator-inbox-liveness")
    assert response.status_code == 200

    data = response.json()
    assert data["total_ticks"] == 3
    assert data["total_forward_leaked"] == 7
    assert data["total_info_alert_noise"] == 2
    assert len(data["recent_ticks"]) == 1
    assert data["recent_ticks"][0]["forward_leaked"] == 7


@pytest.mark.asyncio
async def test_liveness_force_run_returns_run_results(
    client: AsyncClient,
    db_session,
):
    """?force_run=true triggers run_operator_inbox_liveness_check and appends run_results."""
    from app.operator_inbox_liveness import LivenessResult

    fake_results = [
        LivenessResult(
            project_id="proj-abc",
            status="ok",
            forward_leaked=0,
            badge_invisible=0,
            info_alert_noise=0,
            detail="all ok",
        )
    ]

    with patch(
        "app.operator_inbox_liveness.run_operator_inbox_liveness_check",
        new=AsyncMock(return_value=fake_results),
    ):
        response = await client.get(
            "/api/admin/operator-inbox-liveness?force_run=true"
        )

    assert response.status_code == 200
    data = response.json()
    assert "run_results" in data
    assert isinstance(data["run_results"], list)
    assert len(data["run_results"]) == 1
    r = data["run_results"][0]
    assert r["project_id"] == "proj-abc"
    assert r["status"] == "ok"
    assert r["forward_leaked"] == 0


@pytest.mark.asyncio
async def test_liveness_non_admin_gets_403():
    """Non-admin user receives 403 from the liveness endpoint."""

    async def _non_admin():
        return {"user_id": "nobody", "email": "nobody@example.com", "role": "viewer"}

    app.dependency_overrides[require_admin] = _non_admin
    try:
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as anon_client:
            response = await anon_client.get("/api/admin/operator-inbox-liveness")
        # require_admin raises HTTPException(403) for non-admins; FastAPI converts
        # dependency exceptions to the appropriate status code.
        assert response.status_code in (401, 403)
    finally:
        app.dependency_overrides.pop(require_admin, None)

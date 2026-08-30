"""Tests for the in-process digest latency rolling aggregate.

Covers: record_digest_request accumulation, p50/p95 percentile correctness,
tier-count tracking, deque bounding at 1000, and empty-state snapshot safety.
Also exercises the /api/admin/digest-latency-stats endpoint via TestClient.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest
from app.digest_latency import (
    _reset_for_testing,
    record_digest_request,
    get_digest_latency_snapshot,
)


@pytest.fixture(autouse=True)
def _clean_state():
    """Reset in-process state before each test to avoid cross-test pollution."""
    _reset_for_testing()
    yield
    _reset_for_testing()


class TestRecordAndSnapshot:
    def test_empty_state_returns_none_percentiles(self):
        snap = get_digest_latency_snapshot()
        assert snap["latency_percentiles"]["p50_seconds"] is None
        assert snap["latency_percentiles"]["p95_seconds"] is None
        assert snap["latency_percentiles"]["sample_n"] == 0
        assert snap["total_served"] == 0

    def test_single_request_recorded(self):
        record_digest_request(0.5, "l1_precompute")
        snap = get_digest_latency_snapshot()
        assert snap["latency_percentiles"]["sample_n"] == 1
        assert snap["latency_percentiles"]["p50_seconds"] == 0.5
        assert snap["latency_percentiles"]["p95_seconds"] == 0.5
        assert snap["tier_counts"]["l1_precompute"] == 1
        assert snap["total_served"] == 1

    def test_tier_counts_increment_per_tier(self):
        record_digest_request(0.1, "l1_precompute")
        record_digest_request(0.2, "l2_shared_cache")
        record_digest_request(1.5, "l3_on_demand")
        record_digest_request(0.3, "l1_precompute")
        snap = get_digest_latency_snapshot()
        assert snap["tier_counts"]["l1_precompute"] == 2
        assert snap["tier_counts"]["l2_shared_cache"] == 1
        assert snap["tier_counts"]["l3_on_demand"] == 1
        assert snap["total_served"] == 4

    def test_unknown_tier_not_counted_in_tier_counts(self):
        record_digest_request(0.5, "unknown_tier")
        snap = get_digest_latency_snapshot()
        # Duration is recorded but no tier counter incremented
        assert snap["latency_percentiles"]["sample_n"] == 1
        assert snap["total_served"] == 0
        assert "unknown_tier" not in snap["tier_counts"]

    def test_p50_p95_correctness_known_values(self):
        # 10 values: 0.1 0.2 ... 1.0
        for i in range(1, 11):
            record_digest_request(i * 0.1, "l3_on_demand")
        snap = get_digest_latency_snapshot()
        # p50 index = int(10*0.50) = 5 → sorted[5] = 0.6
        assert snap["latency_percentiles"]["p50_seconds"] == pytest.approx(
            0.6, abs=0.001
        )
        # p95 index = int(10*0.95) = 9 → sorted[9] = 1.0
        assert snap["latency_percentiles"]["p95_seconds"] == pytest.approx(
            1.0, abs=0.001
        )
        assert snap["latency_percentiles"]["sample_n"] == 10

    def test_deque_bounded_at_1000(self):
        from app.digest_latency import _durations, _DEQUE_MAXLEN

        assert _durations.maxlen == _DEQUE_MAXLEN
        # Record 1100 values — only 1000 survive
        for i in range(1100):
            record_digest_request(float(i) * 0.001, "l1_precompute")
        snap = get_digest_latency_snapshot()
        assert snap["latency_percentiles"]["sample_n"] == 1000

    def test_instance_note_present(self):
        snap = get_digest_latency_snapshot()
        assert "instance_note" in snap
        assert "per-instance" in snap["instance_note"]


@pytest.fixture
def override_get_metrics_reader():
    """Override get_metrics_reader so auth is bypassed for non-JWT tests.

    The conftest client fixture overrides require_admin, but digest-latency-stats
    now depends on get_metrics_reader. This fixture adds the matching bypass.
    """
    from app.auth import get_metrics_reader
    from app.main import app

    app.dependency_overrides[get_metrics_reader] = lambda: {"role": "admin"}
    yield
    app.dependency_overrides.pop(get_metrics_reader, None)


class TestDigestLatencyEndpoint:
    """Integration test: GET /api/admin/digest-latency-stats returns 200."""

    @pytest.mark.asyncio
    async def test_endpoint_returns_snapshot(
        self, client, db_session, override_get_metrics_reader
    ):
        record_digest_request(0.25, "l1_precompute")
        record_digest_request(1.8, "l3_on_demand")

        resp = await client.get(
            "/api/admin/digest-latency-stats",
            headers={"Authorization": "Bearer admin-test-token"},
        )
        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert "latency_percentiles" in data
        assert "tier_counts" in data
        assert "total_served" in data
        assert data["tier_counts"]["l1_precompute"] >= 1
        assert data["tier_counts"]["l3_on_demand"] >= 1

    @pytest.mark.asyncio
    async def test_endpoint_accepts_agent_jwt(
        self,
        override_get_session,
        db_session,
    ):
        """GET /api/admin/digest-latency-stats returns 200 with a valid agent JWT.

        The endpoint lives on metrics_router (registered directly in main.py without
        the get_current_user router-level dep), so agent JWTs are accepted by
        get_metrics_reader without any override of get_current_user.
        """
        from app.auth import create_agent_token
        from app.main import app
        from app.models import Project, Task
        from httpx import ASGITransport, AsyncClient

        record_digest_request(0.5, "l2_shared_cache")

        project = Project(name="Digest metrics", user_id="metrics-owner")
        db_session.add(project)
        await db_session.flush()
        task = Task(
            project_id=project.id,
            vm_instance_id="metrics-probe-vm-001",
            payloads=[{"type": "research"}],
        )
        db_session.add(task)
        await db_session.commit()

        token = create_agent_token(
            task_id=str(task.id),
            vm_instance_id="metrics-probe-vm-001",
            task_type="research",
            project_id=str(project.id),
        )
        # No get_current_user override — the real dependency stack must pass.
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as raw_client:
            resp = await raw_client.get(
                "/api/admin/digest-latency-stats",
                headers={"Authorization": f"Bearer {token}"},
            )

        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert "latency_percentiles" in data
        assert "tier_counts" in data
        assert data["tier_counts"]["l2_shared_cache"] >= 1

    @pytest.mark.asyncio
    async def test_endpoint_accepts_firebase_admin_token(
        self, override_get_session, db_session
    ):
        """Browser admins use Firebase ID tokens via apiFetch/getIdToken."""
        from app.main import app
        from app.models import AllowedUser
        from httpx import ASGITransport, AsyncClient

        email = "metrics-admin@example.com"
        db_session.add(AllowedUser(email=email, role="admin"))
        await db_session.commit()
        record_digest_request(0.75, "l1_precompute")

        decoded = {
            "uid": "metrics-admin-uid",
            "email": email,
            "email_verified": True,
            "name": "Metrics Admin",
        }
        with patch("app.auth.firebase_auth.verify_id_token", return_value=decoded):
            async with AsyncClient(
                transport=ASGITransport(app=app), base_url="http://test"
            ) as raw_client:
                resp = await raw_client.get(
                    "/api/admin/digest-latency-stats",
                    headers={"Authorization": "Bearer fake-firebase-admin-token"},
                )

        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert "latency_percentiles" in data
        assert "tier_counts" in data
        assert data["tier_counts"]["l1_precompute"] >= 1

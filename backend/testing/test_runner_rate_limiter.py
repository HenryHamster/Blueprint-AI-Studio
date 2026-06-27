"""Tests for per-runner-id in-memory rate limiting on runner polling endpoints.

Covers:
  * Unit tests for the RunnerRateLimiter utility (no DB needed)
  * Integration tests via the HTTP client (429 with Retry-After header)
"""

import uuid

import pytest
from httpx import AsyncClient

from app import auth as auth_mod
from app.models import LocalRunner, Project, ProjectMember, _utcnow
from app.runner_rate_limiter import RunnerRateLimiter, reset_runner_rate_limiter


# ── Fixtures ─────────────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def reset_limiter():
    """Clear rate limiter state before and after every test to prevent bleed."""
    reset_runner_rate_limiter()
    yield
    reset_runner_rate_limiter()


async def _make_project(session, owner_id="test-user-123", name="RL Project"):
    project = Project(name=name, user_id=owner_id)
    session.add(project)
    await session.commit()
    await session.refresh(project)
    session.add(ProjectMember(project_id=project.id, user_id=owner_id, role="owner"))
    await session.commit()
    return project


async def _make_runner(session, project_id, **kwargs):
    token = auth_mod.create_runner_token(uuid.uuid4())
    runner = LocalRunner(
        project_id=project_id,
        token_hash=auth_mod.hash_runner_token(token),
        last_seen_at=kwargs.pop("last_seen_at", _utcnow()),
        capabilities=kwargs.pop(
            "capabilities",
            {"task_types": "all", "has_gpu": False, "max_concurrent": 2},
        ),
        **kwargs,
    )
    session.add(runner)
    await session.commit()
    await session.refresh(runner)
    return runner, token


# ── Unit tests for RunnerRateLimiter ────────────────────────────────────────


class TestRunnerRateLimiterUnit:
    """Tests against a fresh RunnerRateLimiter instance (no DB, no HTTP)."""

    @pytest.mark.asyncio
    async def test_requests_within_limit_are_allowed(self):
        limiter = RunnerRateLimiter()
        runner_id = str(uuid.uuid4())
        for _ in range(5):
            allowed, retry_after = await limiter.check(runner_id, "next_task")
            assert allowed is True
            assert retry_after == 0

    @pytest.mark.asyncio
    async def test_exceeding_limit_returns_denied_with_retry_after(self):
        limiter = RunnerRateLimiter()
        runner_id = str(uuid.uuid4())
        limit = 3
        # Override the limit for this test by using a custom limiter with a tiny limit.
        from app import runner_rate_limiter as rl_mod

        original = rl_mod._ENDPOINT_LIMITS.get("next_task")
        rl_mod._ENDPOINT_LIMITS["next_task"] = limit
        try:
            for _ in range(limit):
                allowed, _ = await limiter.check(runner_id, "next_task")
                assert allowed is True

            allowed, retry_after = await limiter.check(runner_id, "next_task")
            assert allowed is False
            assert retry_after >= 1
        finally:
            if original is not None:
                rl_mod._ENDPOINT_LIMITS["next_task"] = original
            else:
                del rl_mod._ENDPOINT_LIMITS["next_task"]

    @pytest.mark.asyncio
    async def test_per_runner_isolation(self):
        """Exhausting runner A's quota must not affect runner B."""
        from app import runner_rate_limiter as rl_mod

        limiter = RunnerRateLimiter()
        runner_a = str(uuid.uuid4())
        runner_b = str(uuid.uuid4())
        limit = 2
        original = rl_mod._ENDPOINT_LIMITS.get("next_task")
        rl_mod._ENDPOINT_LIMITS["next_task"] = limit
        try:
            # Exhaust runner A.
            for _ in range(limit):
                await limiter.check(runner_a, "next_task")
            allowed_a, _ = await limiter.check(runner_a, "next_task")
            assert allowed_a is False

            # Runner B should still be allowed.
            allowed_b, _ = await limiter.check(runner_b, "next_task")
            assert allowed_b is True
        finally:
            if original is not None:
                rl_mod._ENDPOINT_LIMITS["next_task"] = original
            else:
                del rl_mod._ENDPOINT_LIMITS["next_task"]

    @pytest.mark.asyncio
    async def test_per_endpoint_isolation(self):
        """Exhausting next_task quota must not affect heartbeat quota."""
        from app import runner_rate_limiter as rl_mod

        limiter = RunnerRateLimiter()
        runner_id = str(uuid.uuid4())
        limit = 2
        original_nt = rl_mod._ENDPOINT_LIMITS.get("next_task")
        rl_mod._ENDPOINT_LIMITS["next_task"] = limit
        try:
            # Exhaust next_task.
            for _ in range(limit):
                await limiter.check(runner_id, "next_task")
            allowed_nt, _ = await limiter.check(runner_id, "next_task")
            assert allowed_nt is False

            # Heartbeat for the same runner is unaffected.
            allowed_hb, _ = await limiter.check(runner_id, "heartbeat")
            assert allowed_hb is True
        finally:
            if original_nt is not None:
                rl_mod._ENDPOINT_LIMITS["next_task"] = original_nt
            else:
                del rl_mod._ENDPOINT_LIMITS["next_task"]

    @pytest.mark.asyncio
    async def test_heartbeat_has_higher_default_limit(self):
        """Heartbeat limit is 120 by default; next_task is 60."""
        from app import runner_rate_limiter as rl_mod

        assert rl_mod._HEARTBEAT_LIMIT > rl_mod._DEFAULT_LIMIT
        assert rl_mod._ENDPOINT_LIMITS["heartbeat"] == rl_mod._HEARTBEAT_LIMIT
        assert rl_mod._ENDPOINT_LIMITS["next_task"] == rl_mod._DEFAULT_LIMIT

    @pytest.mark.asyncio
    async def test_clear_resets_state(self):
        from app import runner_rate_limiter as rl_mod

        limiter = RunnerRateLimiter()
        runner_id = str(uuid.uuid4())
        limit = 2
        original = rl_mod._ENDPOINT_LIMITS.get("next_task")
        rl_mod._ENDPOINT_LIMITS["next_task"] = limit
        try:
            for _ in range(limit):
                await limiter.check(runner_id, "next_task")
            denied, _ = await limiter.check(runner_id, "next_task")
            assert denied is False

            limiter.clear()

            allowed, _ = await limiter.check(runner_id, "next_task")
            assert allowed is True
        finally:
            if original is not None:
                rl_mod._ENDPOINT_LIMITS["next_task"] = original
            else:
                del rl_mod._ENDPOINT_LIMITS["next_task"]

    @pytest.mark.asyncio
    async def test_expired_entries_prune_on_access(self):
        """After window expires, previously-counted requests are pruned."""
        from app import runner_rate_limiter as rl_mod
        import time

        limiter = RunnerRateLimiter()
        runner_id = str(uuid.uuid4())

        # Set a tiny window (1 second) and limit of 1 to force expiry quickly.
        original_window = rl_mod._WINDOW_SECONDS
        original = rl_mod._ENDPOINT_LIMITS.get("next_task")
        rl_mod._WINDOW_SECONDS = 0.05  # 50ms
        rl_mod._ENDPOINT_LIMITS["next_task"] = 1
        try:
            allowed, _ = await limiter.check(runner_id, "next_task")
            assert allowed is True

            denied, _ = await limiter.check(runner_id, "next_task")
            assert denied is False

            # Wait for the window to expire.
            time.sleep(0.1)

            # Now should be allowed again (old timestamp pruned).
            allowed_again, _ = await limiter.check(runner_id, "next_task")
            assert allowed_again is True
        finally:
            rl_mod._WINDOW_SECONDS = original_window
            if original is not None:
                rl_mod._ENDPOINT_LIMITS["next_task"] = original
            else:
                del rl_mod._ENDPOINT_LIMITS["next_task"]

    @pytest.mark.asyncio
    async def test_stale_windows_are_evicted_on_sweep(self):
        """Stale windows for runners that stopped polling are removed by sweep, bounding memory."""
        from app import runner_rate_limiter as rl_mod
        import time

        limiter = RunnerRateLimiter()

        original_window = rl_mod._WINDOW_SECONDS
        rl_mod._WINDOW_SECONDS = 0.05  # 50ms
        try:
            # Add 10 runner windows.
            runners = [str(uuid.uuid4()) for _ in range(10)]
            for runner in runners:
                allowed, _ = await limiter.check(runner, "next_task")
                assert allowed is True

            assert len(limiter._windows) == 10

            # Wait for all windows to expire (runners "stopped polling").
            time.sleep(0.1)

            # Sweep must remove ALL stale windows — dict must shrink to 0.
            await limiter.sweep()
            assert len(limiter._windows) == 0, (
                f"Expected 0 windows after sweep of stopped runners, "
                f"got {len(limiter._windows)}. Stale windows were not evicted."
            )

            # Add 5 more runner windows, let them expire, then add 1 new active runner.
            stale_runners = [str(uuid.uuid4()) for _ in range(5)]
            for runner in stale_runners:
                await limiter.check(runner, "next_task")

            time.sleep(0.1)  # stale_runners' windows now expired

            new_runner = str(uuid.uuid4())
            await limiter.check(new_runner, "next_task")

            # Before sweep: 5 stale + 1 active = 6 entries.
            assert len(limiter._windows) == 6

            # After sweep: only the 1 active runner remains.
            await limiter.sweep()
            assert len(limiter._windows) == 1, (
                f"Expected 1 window after sweep (only active runner), "
                f"got {len(limiter._windows)}. Stale windows were not evicted."
            )

            # Verify the surviving window belongs to new_runner.
            assert (new_runner, "next_task") in limiter._windows
        finally:
            rl_mod._WINDOW_SECONDS = original_window

    @pytest.mark.asyncio
    async def test_periodic_sweep_via_check_count(self):
        """Periodic sweep triggered inside check() evicts stale windows automatically."""
        from app import runner_rate_limiter as rl_mod
        import time

        limiter = RunnerRateLimiter()

        original_window = rl_mod._WINDOW_SECONDS
        original_interval = rl_mod._SWEEP_INTERVAL
        rl_mod._WINDOW_SECONDS = 0.05  # 50ms
        rl_mod._SWEEP_INTERVAL = 10  # sweep every 10 calls
        try:
            # Add 5 stale runner windows, then let them expire.
            stale_runners = [str(uuid.uuid4()) for _ in range(5)]
            for runner in stale_runners:
                await limiter.check(runner, "next_task")

            time.sleep(0.1)

            # Add one active runner.  Use a fresh UUID so it doesn't conflict.
            active_runner = str(uuid.uuid4())
            await limiter.check(active_runner, "next_task")

            # Before sweep fires: 5 stale + 1 active = 6 entries.
            assert len(limiter._windows) == 6

            # Drive check_count to the next sweep boundary (interval=10, already at 6).
            # Four more calls to any runner will trigger the sweep at call #10.
            filler = str(uuid.uuid4())
            for _ in range(4):
                await limiter.check(filler, "next_task")

            # The sweep should have fired and removed the 5 stale windows,
            # leaving active_runner + filler = 2 entries.
            assert len(limiter._windows) <= 2, (
                f"Expected ≤2 windows after periodic sweep, "
                f"got {len(limiter._windows)}. Stale windows were not evicted."
            )
        finally:
            rl_mod._WINDOW_SECONDS = original_window
            rl_mod._SWEEP_INTERVAL = original_interval


# ── Integration tests via HTTP client ────────────────────────────────────────


class TestRunnerRateLimitHTTP:
    """End-to-end: hit the real FastAPI app and assert 429 / Retry-After."""

    @pytest.mark.asyncio
    async def test_next_task_within_limit_returns_204(
        self, client: AsyncClient, db_session
    ):
        """A single next-task poll from a runner within limit returns 204 (no task)."""
        project = await _make_project(db_session)
        runner, token = await _make_runner(db_session, project.id)

        resp = await client.post(
            "/api/runner/next-task",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 204

    @pytest.mark.asyncio
    async def test_heartbeat_within_limit_returns_200(
        self, client: AsyncClient, db_session
    ):
        """A single heartbeat within limit returns 200."""
        project = await _make_project(db_session)
        runner, token = await _make_runner(db_session, project.id)

        resp = await client.post(
            "/api/runner/heartbeat",
            json={"active_task_ids": []},
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 200

    @pytest.mark.asyncio
    async def test_next_task_returns_429_when_limit_exceeded(
        self, client: AsyncClient, db_session
    ):
        """Exceeding the per-runner limit on next-task returns 429 with Retry-After."""
        from app import runner_rate_limiter as rl_mod

        project = await _make_project(db_session)
        runner, token = await _make_runner(db_session, project.id)

        original = rl_mod._ENDPOINT_LIMITS.get("next_task")
        rl_mod._ENDPOINT_LIMITS["next_task"] = 2
        try:
            # First two requests allowed.
            for _ in range(2):
                resp = await client.post(
                    "/api/runner/next-task",
                    headers={"Authorization": f"Bearer {token}"},
                )
                assert resp.status_code in (200, 204)

            # Third request must be rate-limited.
            resp = await client.post(
                "/api/runner/next-task",
                headers={"Authorization": f"Bearer {token}"},
            )
            assert resp.status_code == 429
            assert "retry-after" in resp.headers
            assert int(resp.headers["retry-after"]) >= 1
        finally:
            if original is not None:
                rl_mod._ENDPOINT_LIMITS["next_task"] = original
            else:
                del rl_mod._ENDPOINT_LIMITS["next_task"]

    @pytest.mark.asyncio
    async def test_heartbeat_returns_429_when_limit_exceeded(
        self, client: AsyncClient, db_session
    ):
        """Exceeding the per-runner limit on heartbeat returns 429 with Retry-After."""
        from app import runner_rate_limiter as rl_mod

        project = await _make_project(db_session)
        runner, token = await _make_runner(db_session, project.id)

        original = rl_mod._ENDPOINT_LIMITS.get("heartbeat")
        rl_mod._ENDPOINT_LIMITS["heartbeat"] = 2
        try:
            for _ in range(2):
                resp = await client.post(
                    "/api/runner/heartbeat",
                    json={"active_task_ids": []},
                    headers={"Authorization": f"Bearer {token}"},
                )
                assert resp.status_code == 200

            resp = await client.post(
                "/api/runner/heartbeat",
                json={"active_task_ids": []},
                headers={"Authorization": f"Bearer {token}"},
            )
            assert resp.status_code == 429
            assert "retry-after" in resp.headers
        finally:
            if original is not None:
                rl_mod._ENDPOINT_LIMITS["heartbeat"] = original
            else:
                del rl_mod._ENDPOINT_LIMITS["heartbeat"]

    @pytest.mark.asyncio
    async def test_two_runners_rate_limited_independently(
        self, client: AsyncClient, db_session
    ):
        """Runner A hitting the limit must not block runner B."""
        from app import runner_rate_limiter as rl_mod

        project = await _make_project(db_session)
        runner_a, token_a = await _make_runner(db_session, project.id, hostname="a")
        runner_b, token_b = await _make_runner(db_session, project.id, hostname="b")

        original = rl_mod._ENDPOINT_LIMITS.get("next_task")
        rl_mod._ENDPOINT_LIMITS["next_task"] = 1
        try:
            # Exhaust runner A (first call allowed, second denied).
            resp = await client.post(
                "/api/runner/next-task",
                headers={"Authorization": f"Bearer {token_a}"},
            )
            assert resp.status_code in (200, 204)

            resp = await client.post(
                "/api/runner/next-task",
                headers={"Authorization": f"Bearer {token_a}"},
            )
            assert resp.status_code == 429

            # Runner B is unaffected.
            resp = await client.post(
                "/api/runner/next-task",
                headers={"Authorization": f"Bearer {token_b}"},
            )
            assert resp.status_code in (200, 204)
        finally:
            if original is not None:
                rl_mod._ENDPOINT_LIMITS["next_task"] = original
            else:
                del rl_mod._ENDPOINT_LIMITS["next_task"]

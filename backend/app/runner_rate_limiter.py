"""Per-runner-id sliding-window rate limiter for runner polling endpoints.

Keyed on (runner_id, endpoint) — never on IP address.  All state is held
in a single asyncio.Lock-guarded dict; no Redis, no external deps.

Usage::

    from app.runner_rate_limiter import check_runner_rate_limit

    allowed, retry_after = await check_runner_rate_limit(runner_id, "next_task")
    if not allowed:
        raise HTTPException(429, headers={"Retry-After": str(retry_after)})
"""

import asyncio
import os
from collections import deque
from time import monotonic

# Window length (seconds).  Limits are expressed as max requests per window.
_WINDOW_SECONDS = 60.0

# Default limits per endpoint (requests per window).
_DEFAULT_LIMIT = int(os.environ.get("RUNNER_RATE_LIMIT_DEFAULT", "60"))
_HEARTBEAT_LIMIT = int(os.environ.get("RUNNER_RATE_LIMIT_HEARTBEAT", "120"))

_ENDPOINT_LIMITS: dict[str, int] = {
    "next_task": _DEFAULT_LIMIT,
    "heartbeat": _HEARTBEAT_LIMIT,
    "next_compute_job": _DEFAULT_LIMIT,
    "report_compute_job": _DEFAULT_LIMIT,
    "upload_compute_job_artifact": _DEFAULT_LIMIT,
}


class RunnerRateLimiter:
    """Sliding-window rate limiter keyed on (runner_id, endpoint)."""

    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        # Maps (runner_id, endpoint) -> deque of monotonic timestamps
        self._windows: dict[tuple[str, str], deque] = {}

    def _get_limit(self, endpoint: str) -> int:
        return _ENDPOINT_LIMITS.get(endpoint, _DEFAULT_LIMIT)

    async def check(self, runner_id: str, endpoint: str) -> tuple[bool, int]:
        """Return (allowed, retry_after_seconds).

        allowed=True means the request is within quota.  On False, retry_after
        is how many seconds until the oldest in-window request expires.
        """
        limit = self._get_limit(endpoint)
        now = monotonic()
        cutoff = now - _WINDOW_SECONDS
        key = (runner_id, endpoint)

        async with self._lock:
            window = self._windows.get(key)
            if window is None:
                window = deque()
                self._windows[key] = window

            # Prune expired timestamps.
            while window and window[0] <= cutoff:
                window.popleft()

            if len(window) < limit:
                window.append(now)
                return True, 0

            # Denied: compute how long until the oldest entry leaves the window.
            oldest = window[0]
            retry_after = max(1, int(_WINDOW_SECONDS - (now - oldest)) + 1)
            return False, retry_after

    def clear(self) -> None:
        """Reset all state.  Intended for tests only."""
        self._windows.clear()


# Module-level singleton — imported and called by routers/runner.py.
_limiter = RunnerRateLimiter()


async def check_runner_rate_limit(runner_id: str, endpoint: str) -> tuple[bool, int]:
    """Check whether runner_id may call endpoint.

    Returns (allowed: bool, retry_after_seconds: int).
    """
    return await _limiter.check(runner_id, endpoint)


def reset_runner_rate_limiter() -> None:
    """Clear all in-memory state.  Call from test fixtures to prevent bleed."""
    _limiter.clear()

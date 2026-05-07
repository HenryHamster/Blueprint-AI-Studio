"""In-flight dedup + short TTL cache for `/api/admin/digest`.

Rationale (2026-04-20 incident): concurrent admin UI polls (DashboardHome
every 30s, CatchUpPanel every user-configured interval, plus WS-driven
refetches) were each executing the full digest DB scan in parallel. Five
overlapping requests held ~4 GiB total and tipped the Cloud Run instance
past its memory limit. Caching responses for a short window lets concurrent
callers share the same compute; per-instance in-memory is enough because
each Cloud Run container does its own pile-up.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any, Awaitable, Callable, Dict, Hashable, Tuple


class DigestCache:
    """TTL cache with in-flight dedup.

    For a given key, at most one `compute_fn` runs at a time; concurrent
    callers await the same `asyncio.Future`. Completed results are cached
    for `ttl` seconds. Exceptions propagate to all awaiters and are NOT
    cached — the next call retries cleanly.
    """

    def __init__(self, default_ttl: float = 15.0) -> None:
        self._default_ttl = default_ttl
        # key -> (result, expires_at)
        self._results: Dict[Hashable, Tuple[Any, float]] = {}
        # key -> Future being computed
        self._in_flight: Dict[Hashable, asyncio.Future[Any]] = {}

    def clear(self) -> None:
        """Drop all cached results. In-flight futures are left alone so
        awaiters don't get stranded; they finish and skip caching."""
        self._results.clear()

    async def get_or_compute(
        self,
        key: Hashable,
        compute_fn: Callable[[], Awaitable[Any]],
        ttl: float | None = None,
    ) -> Any:
        now = time.monotonic()
        self._sweep_expired(now)

        cached = self._results.get(key)
        if cached is not None and cached[1] > now:
            return cached[0]

        in_flight = self._in_flight.get(key)
        if in_flight is not None:
            return await in_flight

        loop = asyncio.get_running_loop()
        future: asyncio.Future[Any] = loop.create_future()
        self._in_flight[key] = future
        try:
            result = await compute_fn()
        except BaseException as exc:
            self._in_flight.pop(key, None)
            if not future.done():
                future.set_exception(exc)
                # Mark as retrieved so asyncio doesn't log
                # "Future exception was never retrieved" at GC time when no
                # concurrent awaiters joined. Multiple awaiters still each
                # see the exception via their own `await future`.
                future.exception()
            raise
        else:
            effective_ttl = self._default_ttl if ttl is None else ttl
            self._results[key] = (result, time.monotonic() + effective_ttl)
            self._in_flight.pop(key, None)
            if not future.done():
                future.set_result(result)
            return result

    def _sweep_expired(self, now: float) -> None:
        expired = [k for k, (_, exp) in self._results.items() if exp <= now]
        for k in expired:
            self._results.pop(k, None)


_default_cache = DigestCache()


def get_digest_cache() -> DigestCache:
    """FastAPI dependency. Tests override via `app.dependency_overrides`."""
    return _default_cache

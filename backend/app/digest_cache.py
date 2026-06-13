"""In-flight dedup + short TTL cache for `/api/admin/digest`.

Rationale (2026-04-20 incident): concurrent admin UI polls (DashboardHome
every 30s, CatchUpPanel every user-configured interval, plus WS-driven
refetches) were each executing the full digest DB scan in parallel. Five
overlapping requests held ~4 GiB total and tipped the Cloud Run instance
past its memory limit. Caching responses for a short window lets concurrent
callers share the same compute; per-instance in-memory is enough because
each Cloud Run container does its own pile-up.

Serve-stale-on-failure (2026-05-29 incident): as the task table grew, the
digest/health-overview compute started exceeding the 120s statement timeout.
Because failures were not cached, every poll re-ran the failing query — a
recompute storm that 500-ed the dashboard AND held a connection per attempt,
starving the shared pool and stalling the orchestrator main cycle (the
"no progress heartbeat in 198s" hang). The cache now falls back to the last
successful result on failure and negative-caches it briefly, so a slow/failing
compute can neither 500 the dashboard nor stampede the DB. Pair with a tight
per-query statement_timeout in the compute path so a failing attempt releases
its pool connection quickly.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Awaitable, Callable, Dict, Hashable, Tuple

logger = logging.getLogger(__name__)


class DigestCache:
    """TTL cache with in-flight dedup and serve-stale-on-failure.

    For a given key, at most one `compute_fn` runs at a time; concurrent
    callers await the same `asyncio.Future`. Completed results are cached
    for `ttl` seconds. On a compute failure, the last successful result is
    served (if any) and negative-cached for `stale_retry_ttl` seconds so the
    failing compute is not re-run on every poll; the exception only
    propagates when there is no prior good result to fall back to.
    """

    def __init__(
        self, default_ttl: float = 15.0, stale_retry_ttl: float = 30.0
    ) -> None:
        self._default_ttl = default_ttl
        # How long a served-stale result is reused before the compute is
        # retried again — bounds recompute frequency during an outage.
        self._stale_retry_ttl = stale_retry_ttl
        # key -> (result, expires_at) — the fresh/negative TTL cache
        self._results: Dict[Hashable, Tuple[Any, float]] = {}
        # key -> last successful result — never expires (bounded by the small
        # set of distinct (project_id, lookback) keys); the stale fallback.
        self._last_good: Dict[Hashable, Any] = {}
        # key -> Future being computed
        self._in_flight: Dict[Hashable, asyncio.Future[Any]] = {}
        # key -> whether the currently-cached result was served from the
        # stale fallback (a failed compute), vs. produced by a fresh compute.
        # Set True on serve-stale-on-failure, cleared on a successful compute.
        # Exposed via ``get_or_compute_meta`` so callers can mark a response
        # as not-live even when its age alone wouldn't trip a staleness
        # threshold (the "recent cache but compute currently failing" window).
        self._served_stale: Dict[Hashable, bool] = {}

    def clear(self) -> None:
        """Drop all cached results (fresh + last-good). In-flight futures are
        left alone so awaiters don't get stranded; they finish and skip
        caching."""
        self._results.clear()
        self._last_good.clear()
        self._served_stale.clear()

    async def get_or_compute(
        self,
        key: Hashable,
        compute_fn: Callable[[], Awaitable[Any]],
        ttl: float | None = None,
    ) -> Any:
        """Result-only wrapper around :meth:`get_or_compute_meta`.

        Preserved byte-compatible for the callers that don't need the
        served-stale signal. The freshness-annotated endpoints
        (admin_digest, admin_health, admin_pool_accounts, and
        admin_release_version's version-lifecycle cache) use
        :meth:`get_or_compute_meta` directly; this wrapper remains for the
        remaining result-only callers (e.g. admin_release_version's
        agent-serving-SHAs cache).
        """
        result, _served_stale = await self.get_or_compute_meta(
            key, compute_fn, ttl=ttl
        )
        return result

    async def get_or_compute_meta(
        self,
        key: Hashable,
        compute_fn: Callable[[], Awaitable[Any]],
        ttl: float | None = None,
    ) -> Tuple[Any, bool]:
        """Like :meth:`get_or_compute` but also returns whether the result was
        served from the stale fallback.

        Returns ``(result, served_stale)``. ``served_stale`` is True when the
        returned payload came from the last-good fallback because the live
        compute failed (and for reads served from the negative-cache window
        that failure opened), so a caller can present the reading as
        not-live even while its age is still under a freshness threshold.
        """
        now = time.monotonic()
        self._sweep_expired(now)

        cached = self._results.get(key)
        if cached is not None and cached[1] > now:
            return cached[0], self._served_stale.get(key, False)

        in_flight = self._in_flight.get(key)
        if in_flight is not None:
            result = await in_flight
            return result, self._served_stale.get(key, False)

        loop = asyncio.get_running_loop()
        future: asyncio.Future[Any] = loop.create_future()
        self._in_flight[key] = future
        try:
            result = await compute_fn()
        except BaseException as exc:
            self._in_flight.pop(key, None)
            # Serve-stale-on-failure: a slow/timing-out compute must not 500
            # the dashboard or trigger a recompute storm. If we have a prior
            # successful result, return it and negative-cache it for
            # stale_retry_ttl so the next poll doesn't immediately re-run the
            # failing compute (which would re-starve the connection pool).
            stale = self._last_good.get(key)
            if stale is not None and not isinstance(
                exc, (asyncio.CancelledError, KeyboardInterrupt)
            ):
                logger.warning(
                    "[DIGEST_CACHE] compute failed for key=%r; serving stale "
                    "result and negative-caching for %.0fs: %s",
                    key,
                    self._stale_retry_ttl,
                    exc,
                )
                self._results[key] = (
                    stale,
                    time.monotonic() + self._stale_retry_ttl,
                )
                self._served_stale[key] = True
                if not future.done():
                    future.set_result(stale)
                return stale, True
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
            self._last_good[key] = result
            self._served_stale[key] = False
            self._in_flight.pop(key, None)
            if not future.done():
                future.set_result(result)
            return result, False

    def _sweep_expired(self, now: float) -> None:
        expired = [k for k, (_, exp) in self._results.items() if exp <= now]
        for k in expired:
            self._results.pop(k, None)


_default_cache = DigestCache()


def get_digest_cache() -> DigestCache:
    """FastAPI dependency. Tests override via `app.dependency_overrides`."""
    return _default_cache

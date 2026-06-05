"""Unit tests for the in-flight dedup + TTL cache used by /api/admin/digest."""

import asyncio

import pytest

from app.digest_cache import DigestCache


@pytest.mark.asyncio
async def test_cache_returns_same_result_within_ttl():
    cache = DigestCache(default_ttl=60)
    calls = 0

    async def compute():
        nonlocal calls
        calls += 1
        return {"value": calls}

    first = await cache.get_or_compute("k", compute)
    second = await cache.get_or_compute("k", compute)
    assert first == second == {"value": 1}
    assert calls == 1


@pytest.mark.asyncio
async def test_cache_recomputes_after_ttl_expires():
    cache = DigestCache(default_ttl=0.05)
    calls = 0

    async def compute():
        nonlocal calls
        calls += 1
        return calls

    assert await cache.get_or_compute("k", compute) == 1
    await asyncio.sleep(0.1)
    assert await cache.get_or_compute("k", compute) == 2


@pytest.mark.asyncio
async def test_cache_dedups_concurrent_requests():
    """The key OOM-fix guarantee: N concurrent awaiters → 1 compute."""
    cache = DigestCache(default_ttl=60)
    calls = 0
    release = asyncio.Event()

    async def compute():
        nonlocal calls
        calls += 1
        await release.wait()
        return calls

    async def fire():
        return await cache.get_or_compute("k", compute)

    tasks = [asyncio.create_task(fire()) for _ in range(8)]
    await asyncio.sleep(0)  # let all 8 enter get_or_compute
    release.set()
    results = await asyncio.gather(*tasks)
    assert calls == 1
    assert all(r == 1 for r in results)


@pytest.mark.asyncio
async def test_cache_separates_keys():
    cache = DigestCache(default_ttl=60)

    async def compute_a():
        return "a"

    async def compute_b():
        return "b"

    assert await cache.get_or_compute("a", compute_a) == "a"
    assert await cache.get_or_compute("b", compute_b) == "b"
    # Re-fetch hits cache, doesn't swap values
    assert await cache.get_or_compute("a", compute_a) == "a"


@pytest.mark.asyncio
async def test_cache_evicts_in_flight_on_exception():
    """A failed compute must not poison the cache — next caller retries cleanly."""
    cache = DigestCache(default_ttl=60)
    calls = 0

    async def compute():
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("boom")
        return "ok"

    with pytest.raises(RuntimeError):
        await cache.get_or_compute("k", compute)
    # Second call retries because nothing was cached
    assert await cache.get_or_compute("k", compute) == "ok"
    assert calls == 2


@pytest.mark.asyncio
async def test_cache_concurrent_callers_share_failure():
    cache = DigestCache(default_ttl=60)
    calls = 0
    release = asyncio.Event()

    async def compute():
        nonlocal calls
        calls += 1
        await release.wait()
        raise RuntimeError("boom")

    async def fire():
        return await cache.get_or_compute("k", compute)

    tasks = [asyncio.create_task(fire()) for _ in range(4)]
    await asyncio.sleep(0)
    release.set()
    results = await asyncio.gather(*tasks, return_exceptions=True)
    assert calls == 1
    assert all(isinstance(r, RuntimeError) for r in results)


@pytest.mark.asyncio
async def test_cache_clear_drops_cached_results():
    cache = DigestCache(default_ttl=60)
    calls = 0

    async def compute():
        nonlocal calls
        calls += 1
        return calls

    await cache.get_or_compute("k", compute)
    cache.clear()
    await cache.get_or_compute("k", compute)
    assert calls == 2


@pytest.mark.asyncio
async def test_cache_serves_stale_on_compute_failure():
    """After a successful compute, a later failure serves the last good result
    instead of raising — the 2026-05-29 anti-death-spiral guard (a digest query
    that times out must not 500 the dashboard)."""
    cache = DigestCache(default_ttl=0.05, stale_retry_ttl=60)
    calls = 0

    async def compute():
        nonlocal calls
        calls += 1
        if calls == 1:
            return {"value": "good"}
        raise RuntimeError("canceling statement due to statement timeout")

    assert await cache.get_or_compute("k", compute) == {"value": "good"}
    await asyncio.sleep(0.1)  # expire the fresh entry
    # Compute now fails, but the last good result is served (no exception).
    assert await cache.get_or_compute("k", compute) == {"value": "good"}
    assert calls == 2


@pytest.mark.asyncio
async def test_cache_negative_caches_stale_to_break_recompute_storm():
    """A served-stale result is reused for stale_retry_ttl so the failing
    compute is not re-run on every poll (which would re-starve the pool)."""
    cache = DigestCache(default_ttl=0.05, stale_retry_ttl=60)
    calls = 0

    async def compute():
        nonlocal calls
        calls += 1
        if calls == 1:
            return "good"
        raise RuntimeError("timeout")

    assert await cache.get_or_compute("k", compute) == "good"
    await asyncio.sleep(0.1)
    # First post-expiry call recomputes (calls=2), fails, serves + caches stale.
    assert await cache.get_or_compute("k", compute) == "good"
    assert calls == 2
    # Subsequent calls within stale_retry_ttl serve stale WITHOUT recomputing.
    assert await cache.get_or_compute("k", compute) == "good"
    assert await cache.get_or_compute("k", compute) == "good"
    assert calls == 2


@pytest.mark.asyncio
async def test_cache_propagates_when_no_prior_good_result():
    """With no last-good result to fall back to, the exception propagates
    (preserves the original retry-clean behavior)."""
    cache = DigestCache(default_ttl=60)

    async def boom():
        raise RuntimeError("boom")

    with pytest.raises(RuntimeError):
        await cache.get_or_compute("k", boom)


@pytest.mark.asyncio
async def test_get_or_compute_meta_reports_served_stale():
    """get_or_compute_meta returns served_stale=False on a fresh compute,
    True when a failure falls back to the last good result, and clears the
    flag again on recovery."""
    cache = DigestCache(default_ttl=0.05, stale_retry_ttl=60)
    calls = 0
    fail = False

    async def compute():
        nonlocal calls
        calls += 1
        if fail:
            raise RuntimeError("statement timeout")
        return {"value": calls}

    # Fresh compute → not stale.
    result, served_stale = await cache.get_or_compute_meta("k", compute)
    assert result == {"value": 1}
    assert served_stale is False

    # Expire the fresh entry, then make the compute fail → last-good served.
    await asyncio.sleep(0.1)
    fail = True
    result, served_stale = await cache.get_or_compute_meta("k", compute)
    assert result == {"value": 1}
    assert served_stale is True

    # Reads inside the negative-cache window also report served_stale.
    result, served_stale = await cache.get_or_compute_meta("k", compute)
    assert result == {"value": 1}
    assert served_stale is True

    # Recovery: expire the negative cache, let the compute succeed → flag clears.
    await asyncio.sleep(0.1)
    cache._results.clear()  # drop the negative-cache entry to force a recompute
    fail = False
    result, served_stale = await cache.get_or_compute_meta("k", compute)
    assert result["value"] >= 2
    assert served_stale is False


@pytest.mark.asyncio
async def test_get_or_compute_delegates_to_meta_result_only():
    """The result-only wrapper returns just the value (not the tuple) so the
    three unrelated callers stay byte-compatible."""
    cache = DigestCache(default_ttl=60)

    async def compute():
        return {"ok": True}

    assert await cache.get_or_compute("k", compute) == {"ok": True}


@pytest.mark.asyncio
async def test_cache_does_not_serve_stale_on_cancellation():
    """CancelledError must propagate even with a prior good result — a
    cancelled request is not a compute failure to mask with stale data."""
    cache = DigestCache(default_ttl=0.05, stale_retry_ttl=60)
    calls = 0

    async def compute():
        nonlocal calls
        calls += 1
        if calls == 1:
            return "good"
        raise asyncio.CancelledError()

    assert await cache.get_or_compute("k", compute) == "good"
    await asyncio.sleep(0.1)
    with pytest.raises(asyncio.CancelledError):
        await cache.get_or_compute("k", compute)

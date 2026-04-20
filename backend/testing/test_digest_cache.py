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

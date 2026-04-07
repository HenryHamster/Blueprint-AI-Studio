"""Shared async Redis connection with graceful fallback to in-memory.

All features that use Redis (provision semaphore, webhook dedup, rate limiter)
fall back to per-instance in-memory state when REDIS_URL is not configured.
"""

import os
import logging
import time

logger = logging.getLogger(__name__)

_redis = None
_last_attempt: float = 0.0
_RETRY_INTERVAL: float = 300.0  # Retry every 5 minutes


async def get_redis():
    """Get shared Redis connection. Returns None if unavailable."""
    global _redis, _last_attempt
    if _redis is not None:
        try:
            await _redis.ping()
            return _redis
        except Exception:
            logger.warning("[REDIS] Connection lost, will retry")
            _redis = None

    # Rate-limit reconnection attempts
    now = time.monotonic()
    if now - _last_attempt < _RETRY_INTERVAL:
        return None
    _last_attempt = now

    url = os.getenv("REDIS_URL")
    if not url:
        return None

    try:
        import redis.asyncio as aioredis
        _redis = aioredis.from_url(url, decode_responses=True, socket_timeout=5)
        await _redis.ping()
        logger.info("[REDIS] Connected successfully")
    except Exception as e:
        logger.warning(f"[REDIS] Connection failed, falling back to in-memory: {e}")
        _redis = None
    return _redis


async def close_redis():
    """Close Redis connection on shutdown."""
    global _redis, _last_attempt
    if _redis:
        await _redis.close()
        _redis = None
    _last_attempt = 0.0

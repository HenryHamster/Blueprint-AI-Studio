"""Focused tests for cross-instance VM provision admission."""

import pytest

from app import compute


class _AtomicProvisionRedis:
    def __init__(self, result: int):
        self.result = result
        self.eval_calls: list[tuple] = []

    async def eval(self, *args):
        self.eval_calls.append(args)
        return self.result


@pytest.mark.asyncio
@pytest.mark.parametrize(("redis_result", "expected"), [(1, True), (0, False)])
async def test_acquire_provision_slot_uses_one_atomic_redis_eval(
    monkeypatch, redis_result, expected
):
    redis = _AtomicProvisionRedis(redis_result)

    async def _get_redis():
        return redis

    monkeypatch.setattr("app.redis_client.get_redis", _get_redis)
    monkeypatch.setattr(compute, "_MAX_GLOBAL_PROVISIONS", 3)
    monkeypatch.setattr(compute.time, "time", lambda: 1000.0)

    assert await compute._acquire_provision_slot("agent-vm-atomic") is expected
    assert redis.eval_calls == [
        (
            compute._ACQUIRE_PROVISION_SLOT_LUA,
            1,
            compute._PROVISION_REDIS_KEY,
            1000.0,
            3,
            "agent-vm-atomic",
            1000.0 + compute._PROVISION_SLOT_TTL,
        )
    ]


def test_atomic_provision_slot_script_renews_existing_lease_before_capacity_check():
    script = compute._ACQUIRE_PROVISION_SLOT_LUA
    assert script.index("ZSCORE") < script.index("ZCARD")
    assert script.count("ZADD") == 2

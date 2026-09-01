from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from app.max_plan_pool import (
    EXACT_MODEL_ROUTE_CONTEXT_KEY,
    _account_by_id,
    _max_plan_pools,
    select_llm_runtime_for_task,
)
from app.providers.base import UsageSnapshot


_TEST_USER = "strategic-runtime-user"


def _utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


@pytest.fixture()
def clean_runtime_pool():
    orig_pools = {key: list(value) for key, value in _max_plan_pools.items()}
    orig_by_id = dict(_account_by_id)
    _max_plan_pools.clear()
    _account_by_id.clear()
    yield
    _max_plan_pools.clear()
    _account_by_id.clear()
    _max_plan_pools.update(orig_pools)
    _account_by_id.update(orig_by_id)


def _task(*, context=None, resident: bool = True):
    task_context = dict(context or {})
    if resident:
        task_context["_resident_session"] = True
    return SimpleNamespace(
        payloads=[{"type": "strategic_review", "quality_tier": "opus"}],
        context=task_context,
    )


def _typed_fast_task(*, context=None):
    task_context = dict(context or {})
    task_context["_resident_session"] = True
    return SimpleNamespace(
        payloads=[{
            "type": "strategic_review",
            "quality_tier": "opus",
            "typed_fast_enabled": True,
        }],
        context=task_context,
    )


def _known_usage(utilization: float, now: datetime) -> UsageSnapshot:
    return UsageSnapshot(
        known=True,
        window_utilizations={"five_hour": utilization},
        probed_at=now.isoformat(),
    )


def _runtime_account(account_id: str, provider: str) -> dict:
    auth_mode = "oauth" if provider in {"anthropic", "openai"} else "api_key"
    return {
        "id": account_id,
        "user_id": _TEST_USER,
        "name": account_id,
        "provider": provider,
        "auth_mode": auth_mode,
        "oauth_token": "oauth-test" if auth_mode == "oauth" else None,
        "runtime_secret": "api-test" if auth_mode == "api_key" else None,
        "runtime_ready": True,
        "enabled": True,
        "active_tasks": 0,
    }


def _register(*accounts: dict) -> None:
    _max_plan_pools.setdefault(_TEST_USER, []).extend(accounts)
    for account in accounts:
        _account_by_id[account["id"]] = account


def _config(mode: str = "preferred") -> dict:
    return {
        "provider_selection_mode": mode,
        "provider_tier_model_map": {
            "anthropic": {"opus": "claude-opus-4-7"},
            "openai": {"opus": "gpt-5.6-sol"},
            "zai": {"opus": "glm-5"},
            "deepseek": {"opus": "deepseek-v4-pro"},
        },
        "task_type_provider_preference": {
            "strategic_review": ["anthropic", "openai", "zai", "deepseek"]
        },
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("selection_mode", ["preferred", "balanced"])
async def test_strategic_runtime_global_usage_precedence_prefers_fresh_known(
    clean_runtime_pool,
    selection_mode,
):
    now = _utcnow()
    unknown_claude = _runtime_account("claude-unknown", "anthropic")
    unknown_claude.update(
        usage_snapshot=UsageSnapshot.unknown(now.isoformat()),
        usage_snapshot_at=now,
    )
    healthy_codex = _runtime_account("codex-18", "openai")
    healthy_codex.update(
        usage_snapshot=_known_usage(18.0, now), usage_snapshot_at=now
    )
    saturated_zai = _runtime_account("zai-99", "zai")
    saturated_zai.update(
        usage_snapshot=_known_usage(99.0, now), usage_snapshot_at=now
    )
    failed_deepseek = _runtime_account("deepseek-auth-failed", "deepseek")
    failed_deepseek.update(
        usage_snapshot=_known_usage(2.0, now),
        usage_snapshot_at=now,
        usage_probe_failure_kind="authentication",
        usage_probe_attempt_at=now,
    )
    _register(unknown_claude, healthy_codex, saturated_zai, failed_deepseek)

    result = await select_llm_runtime_for_task(
        user_id=_TEST_USER,
        task=_task(),
        runtime_config=_config(selection_mode),
    )

    assert result is not None
    assert (result.provider, result.model, result.account_id) == (
        "openai",
        "gpt-5.5",
        "codex-18",
    )
    assert healthy_codex["active_tasks"] == 1
    assert all(
        account["active_tasks"] == 0
        for account in (unknown_claude, saturated_zai, failed_deepseek)
    )

    # A 99%-used account must lose to a healthier sibling even when the
    # ordinary configurable gate is effectively disabled at 100%.
    _max_plan_pools.clear()
    _account_by_id.clear()
    saturated_codex = _runtime_account("codex-99", "openai")
    saturated_codex.update(
        usage_snapshot=_known_usage(99.0, now), usage_snapshot_at=now
    )
    healthy_codex_sibling = _runtime_account("codex-18-sibling", "openai")
    healthy_codex_sibling.update(
        usage_snapshot=_known_usage(18.0, now), usage_snapshot_at=now
    )
    _register(saturated_codex, healthy_codex_sibling)
    high_threshold_config = _config(selection_mode)
    high_threshold_config["usage_headroom_threshold_pct"] = 100

    result = await select_llm_runtime_for_task(
        user_id=_TEST_USER,
        task=_task(),
        runtime_config=high_threshold_config,
    )

    assert result is not None
    assert (result.provider, result.account_id) == (
        "openai",
        "codex-18-sibling",
    )
    assert saturated_codex["active_tasks"] == 0

    # The same nearly exhausted preferred route must not shadow a healthy
    # compatible lower-precedence provider.
    _max_plan_pools.clear()
    _account_by_id.clear()
    saturated_codex = _runtime_account("codex-99", "openai")
    saturated_codex.update(
        usage_snapshot=_known_usage(99.0, now), usage_snapshot_at=now
    )
    healthy_deepseek = _runtime_account("deepseek-18", "deepseek")
    healthy_deepseek.update(
        usage_snapshot=_known_usage(18.0, now), usage_snapshot_at=now
    )
    _register(saturated_codex, healthy_deepseek)

    result = await select_llm_runtime_for_task(
        user_id=_TEST_USER,
        task=_task(),
        runtime_config=high_threshold_config,
    )

    assert result is not None
    assert (result.provider, result.account_id) == (
        "deepseek",
        "deepseek-18",
    )
    assert saturated_codex["active_tasks"] == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("selection_mode", ["preferred", "balanced"])
async def test_typed_fast_resident_skips_provider_without_qualified_foreground_adapter(
    clean_runtime_pool,
    selection_mode,
):
    now = _utcnow()
    healthy_codex = _runtime_account("codex-healthy", "openai")
    healthy_codex.update(
        usage_snapshot=_known_usage(2.0, now), usage_snapshot_at=now
    )
    healthy_deepseek = _runtime_account("deepseek-compatible", "deepseek")
    healthy_deepseek.update(
        usage_snapshot=_known_usage(18.0, now), usage_snapshot_at=now
    )
    _register(healthy_codex, healthy_deepseek)
    routing_diagnostics = []

    result = await select_llm_runtime_for_task(
        user_id=_TEST_USER,
        task=_typed_fast_task(),
        runtime_config=_config(selection_mode),
        failure_reasons=routing_diagnostics,
    )

    assert result is not None
    assert (result.provider, result.model, result.account_id) == (
        "deepseek",
        "deepseek-v4-pro",
        "deepseek-compatible",
    )
    assert healthy_codex["active_tasks"] == 0
    assert routing_diagnostics == [
        "typed Fast route excluded providers without a qualified foreground "
        "request_workday adapter: openai"
    ]


@pytest.mark.asyncio
async def test_typed_fast_resident_fails_closed_on_incompatible_exact_route(
    clean_runtime_pool,
):
    now = _utcnow()
    codex = _runtime_account("codex-exact-incompatible", "openai")
    codex.update(usage_snapshot=_known_usage(2.0, now), usage_snapshot_at=now)
    _register(codex)
    diagnostics = []

    result = await select_llm_runtime_for_task(
        user_id=_TEST_USER,
        task=_typed_fast_task(context={
            EXACT_MODEL_ROUTE_CONTEXT_KEY: {
                "provider": "openai",
                "model": "gpt-5.5",
                "effort": "max",
            }
        }),
        runtime_config=_config(),
        failure_reasons=diagnostics,
    )

    assert result is None
    assert codex["active_tasks"] == 0
    assert diagnostics == [
        "server-authored exact route is incompatible with the typed Fast "
        "request_workday adapter: openai"
    ]


@pytest.mark.asyncio
async def test_strategic_runtime_uses_credential_healthy_before_failed_probe(
    clean_runtime_pool,
):
    now = _utcnow()
    fresh_claude = _runtime_account("claude-fresh", "anthropic")
    fresh_claude.update(
        usage_snapshot=_known_usage(20.0, now), usage_snapshot_at=now
    )
    fresh_codex = _runtime_account("codex-fresh", "openai")
    fresh_codex.update(
        usage_snapshot=_known_usage(18.0, now), usage_snapshot_at=now
    )
    _register(fresh_claude, fresh_codex)

    result = await select_llm_runtime_for_task(
        user_id=_TEST_USER,
        task=_task(),
        runtime_config=_config(),
    )

    assert result is not None
    assert (result.provider, result.account_id) == (
        "anthropic",
        "claude-fresh",
    )

    _max_plan_pools.clear()
    _account_by_id.clear()
    unknown_claude = _runtime_account("claude-unknown", "anthropic")
    stale_codex = _runtime_account("codex-stale", "openai")
    stale_codex.update(
        usage_snapshot=_known_usage(8.0, now),
        usage_snapshot_at=now - timedelta(hours=1),
    )
    saturated_zai = _runtime_account("zai-99", "zai")
    saturated_zai.update(
        usage_snapshot=_known_usage(99.0, now), usage_snapshot_at=now
    )
    failed_deepseek = _runtime_account("deepseek-rate-limited", "deepseek")
    failed_deepseek.update(
        usage_snapshot=_known_usage(1.0, now),
        usage_snapshot_at=now,
        usage_probe_failure_kind="rate_limited",
        usage_probe_attempt_at=now,
    )
    _register(unknown_claude, stale_codex, saturated_zai, failed_deepseek)

    result = await select_llm_runtime_for_task(
        user_id=_TEST_USER,
        task=_task(),
        runtime_config=_config(),
    )

    assert result is not None
    assert (result.provider, result.account_id) == ("anthropic", "claude-unknown")
    assert unknown_claude["active_tasks"] == 1
    assert failed_deepseek["active_tasks"] == 0

    # A fresh auth/rate-limit failure is newer evidence than the preferred
    # account's low last-good utilization. It must quarantine that account
    # while a healthy compatible route exists.
    for failure_kind in ("authentication", "rate_limited"):
        _max_plan_pools.clear()
        _account_by_id.clear()
        failed_claude = _runtime_account(
            f"claude-{failure_kind}-failed", "anthropic"
        )
        failed_claude.update(
            usage_snapshot=_known_usage(2.0, now),
            usage_snapshot_at=now - timedelta(minutes=5),
            usage_probe_failure_kind=failure_kind,
            usage_probe_attempt_at=now,
        )
        healthy_codex = _runtime_account(
            f"codex-after-{failure_kind}", "openai"
        )
        healthy_codex.update(
            usage_snapshot=_known_usage(18.0, now), usage_snapshot_at=now
        )
        _register(failed_claude, healthy_codex)

        result = await select_llm_runtime_for_task(
            user_id=_TEST_USER,
            task=_task(),
            runtime_config=_config(),
        )

        assert result is not None
        assert (result.provider, result.account_id) == (
            "openai",
            f"codex-after-{failure_kind}",
        )
        assert failed_claude["active_tasks"] == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("failure_kind", ["authentication", "rate_limited"])
async def test_strategic_runtime_failed_probe_is_last_resort(
    clean_runtime_pool,
    failure_kind,
):
    now = _utcnow()
    failed_codex = _runtime_account("codex-last-resort", "openai")
    failed_codex.update(
        usage_snapshot=_known_usage(4.0, now),
        usage_snapshot_at=now,
        usage_probe_failure_kind=failure_kind,
        usage_probe_attempt_at=now,
    )
    _register(failed_codex)
    config = _config()
    config["task_type_provider_preference"]["strategic_review"] = ["openai"]

    result = await select_llm_runtime_for_task(
        user_id=_TEST_USER,
        task=_task(),
        runtime_config=config,
    )

    assert result is not None
    assert (result.provider, result.account_id) == ("openai", "codex-last-resort")
    assert failed_codex["active_tasks"] == 1


@pytest.mark.asyncio
async def test_strategic_usage_precedence_does_not_widen_exact_route(
    clean_runtime_pool,
):
    now = _utcnow()
    exact_codex = _runtime_account("codex-exact", "openai")
    exact_codex.update(
        usage_snapshot=_known_usage(99.0, now), usage_snapshot_at=now
    )
    alternate_claude = _runtime_account("claude-alternate", "anthropic")
    alternate_claude.update(
        usage_snapshot=_known_usage(18.0, now), usage_snapshot_at=now
    )
    _register(exact_codex, alternate_claude)

    result = await select_llm_runtime_for_task(
        user_id=_TEST_USER,
        task=_task(
            context={
                EXACT_MODEL_ROUTE_CONTEXT_KEY: {
                    "provider": "openai",
                    "model": "gpt-5.6-sol",
                    "effort": "high",
                }
            }
        ),
        runtime_config=_config("balanced"),
    )

    assert result is not None
    assert (result.provider, result.model, result.account_id) == (
        "openai",
        "gpt-5.6-sol",
        "codex-exact",
    )
    assert exact_codex["active_tasks"] == 1
    assert alternate_claude["active_tasks"] == 0


@pytest.mark.asyncio
async def test_openai_compatibility_override_is_resident_only(clean_runtime_pool):
    now = _utcnow()
    codex = _runtime_account("codex-ordinary-review", "openai")
    codex.update(
        usage_snapshot=_known_usage(18.0, now), usage_snapshot_at=now
    )
    _register(codex)
    config = _config()
    config["task_type_provider_preference"]["strategic_review"] = ["openai"]

    result = await select_llm_runtime_for_task(
        user_id=_TEST_USER,
        task=_task(resident=False),
        runtime_config=config,
    )

    assert result is not None
    assert (result.provider, result.model, result.account_id) == (
        "openai",
        "gpt-5.6-sol",
        "codex-ordinary-review",
    )

"""Cross-provider route fallback when an account is disabled.

Regression cover for the 2026-08-24 strand: research task 9ca108c5 held
PROVISIONING for 68+ minutes on

    Runner pull skipped (no_eligible_account): zai/claude-opus-5: glm: account
    disabled (manually)

while four other providers on the SAME route had healthy enabled accounts. Two
independent defects produced that line, and each is pinned here:

  * ``infer_provider_for_model`` read only ``provider_tier_model_map``, so
    ``claude-opus-5`` — anthropic's live max-effort model, configured in
    ``provider_settings_by_effort`` — inferred to no provider at all.
  * an explicit model that infers to no provider then TRIMMED the routed
    provider list to its first entry, discarding every configured fallback.

These live in their own module rather than in ``test_llm_runtime.py`` because
that file is at its reviewability cap; the fixtures below are deliberate local
copies so this file stands alone.
"""

import pytest
from types import SimpleNamespace

from app.max_plan_pool import (
    _account_by_id,
    _max_plan_pools,
    infer_provider_for_model,
    select_llm_runtime_for_task,
)

_TEST_USER = "route-fallback-user"


@pytest.fixture
def clean_runtime_pool():
    orig_pools = {k: list(v) for k, v in _max_plan_pools.items()}
    orig_by_id = dict(_account_by_id)
    _max_plan_pools.clear()
    _account_by_id.clear()
    yield
    _max_plan_pools.clear()
    _account_by_id.clear()
    _max_plan_pools.update(orig_pools)
    _account_by_id.update(orig_by_id)


def _task(*, payload=None, context=None):
    return SimpleNamespace(payloads=[payload or {}], context=context or {})


# ── Regression: an operator-configured model must not strand the task ─────────
# Live incident 2026-08-24: a `research` task sat in PROVISIONING for over an
# hour behind `Runner pull skipped (no_eligible_account): zai/claude-opus-5:
# glm: account disabled (manually)`. The route for `research` listed FIVE
# providers and four of them had healthy enabled accounts, yet only ONE attempt
# was ever made — against a provider that could never serve that model.
def _incident_runtime_config() -> dict:
    """The live 2026-08-24 routing config, reduced to the keys that matter.

    The load-bearing detail is that ``provider_tier_model_map['anthropic']``
    pins the opus tier to ``claude-opus-4-8`` while
    ``provider_settings_by_effort['anthropic']['max']`` pins the model actually
    dispatched to ``claude-opus-5``. A model the operator configured for
    anthropic is therefore absent from the tier map.
    """
    return {
        "provider_selection_mode": "preferred",
        "provider_tier_model_map": {
            "anthropic": {
                "opus": "claude-opus-4-8",
                "sonnet": "claude-sonnet-4-6",
                "haiku": "claude-haiku-4-5-20251001",
            },
            "zai": {"opus": "glm-5.2", "sonnet": "glm-5.2", "haiku": "glm-5.2"},
            "deepseek": {
                "opus": "deepseek-v4-flash",
                "sonnet": "deepseek-v4-flash",
                "haiku": "deepseek-v4-flash",
            },
        },
        "provider_settings_by_effort": {
            "anthropic": {
                "max": {"model": "claude-opus-5", "effort": "xhigh"},
                "high": {"model": "claude-opus-5", "effort": "medium"},
                "medium": {"model": "claude-sonnet-5", "effort": "medium"},
                "low": {"model": "claude-sonnet-5", "effort": "low"},
            },
            "zai": {"max": {"model": "glm-5.2", "effort": "max"}},
            "deepseek": {"max": {"model": "deepseek-v4-flash", "effort": "max"}},
        },
        "task_type_categories": {"initiative": ["research", "plan"]},
        "task_type_routing": {
            "research": {
                "effort": "max",
                "providers": ["zai", "anthropic", "deepseek"],
            }
        },
    }


def _seed(entry: dict) -> dict:
    _max_plan_pools.setdefault(_TEST_USER, []).append(entry)
    _account_by_id[entry["id"]] = entry
    return entry


@pytest.mark.asyncio
async def test_disabled_first_provider_falls_back_to_healthy_provider(
    clean_runtime_pool,
):
    """The live strand: zai's only account is disabled, anthropic's is healthy.

    The task carries ``model: claude-opus-5`` — an anthropic model that is
    absent from ``provider_tier_model_map`` because the operator moved the opus
    tier to ``claude-opus-4-8``. That must NOT collapse the five-provider route
    down to the single first provider, and must NEVER pair the anthropic model
    id with the zai provider.
    """
    _seed(
        {
            "id": "acct-glm",
            "user_id": _TEST_USER,
            "name": "glm",
            "provider": "zai",
            "auth_mode": "api_key",
            "runtime_secret": "sk-glm",
            "runtime_ready": True,
            "enabled": False,
            "disabled_reason": "manually",
            "active_tasks": 0,
        }
    )
    _seed(
        {
            "id": "acct-lihuans",
            "user_id": _TEST_USER,
            "name": "lihuansClaude",
            "provider": "anthropic",
            "auth_mode": "oauth",
            "oauth_token": "sk-ant-live",
            "runtime_ready": True,
            "enabled": True,
            "active_tasks": 0,
        }
    )

    failure_reasons: list[str] = []
    result = await select_llm_runtime_for_task(
        user_id=_TEST_USER,
        task=_task(payload={"type": "research", "model": "claude-opus-5"}),
        runtime_config=_incident_runtime_config(),
        failure_reasons=failure_reasons,
    )

    assert result is not None
    # The account is the healthy anthropic one, not None and not the disabled glm.
    assert result.account_id == "acct-lihuans", (
        "a manually disabled zai account must not strand work anthropic can do; "
        f"attempts were {failure_reasons!r}"
    )
    assert result.provider == "anthropic"
    # And the model is an anthropic model — never the zai/claude-opus-5 mispair.
    assert result.model == "claude-opus-5"


@pytest.mark.asyncio
async def test_operator_configured_model_infers_its_provider(clean_runtime_pool):
    """``claude-opus-5`` is configured for anthropic in the effort matrix.

    ``infer_provider_for_model`` scanned ONLY ``provider_tier_model_map``, so a
    model the operator had explicitly configured for a provider inferred to
    None and was treated as an unknown custom id.
    """
    assert (
        infer_provider_for_model("claude-opus-5", _incident_runtime_config())
        == "anthropic"
    )
    # Tier-map models keep inferring exactly as before.
    assert (
        infer_provider_for_model("claude-opus-4-8", _incident_runtime_config())
        == "anthropic"
    )
    # A genuinely unknown id still infers to nothing.
    assert (
        infer_provider_for_model("totally-made-up-9", _incident_runtime_config())
        is None
    )


@pytest.mark.asyncio
async def test_uninferable_model_does_not_trim_the_route(clean_runtime_pool):
    """An un-inferable custom model must not DELETE the rest of the route.

    Before the fix, an explicit model id that inferred to no provider trimmed
    ``provider_preference`` to ``[:1]``, so exactly ONE provider was ever tried
    no matter how many the operator routed. Here all three routed providers are
    unroutable, which makes the full scan visible in ``failure_reasons``: three
    attempt lines, one per routed provider, each carrying THAT provider's own
    model. Pre-fix this list has exactly one entry.
    """
    for entry in (
        {
            "id": "acct-glm",
            "user_id": _TEST_USER,
            "name": "glm",
            "provider": "zai",
            "auth_mode": "api_key",
            "runtime_secret": "sk-glm",
            "runtime_ready": True,
            "enabled": False,
            "disabled_reason": "manually",
            "active_tasks": 0,
        },
        {
            "id": "acct-lihuans",
            "user_id": _TEST_USER,
            "name": "lihuansClaude",
            "provider": "anthropic",
            "auth_mode": "oauth",
            "oauth_token": "sk-ant-live",
            "runtime_ready": True,
            "enabled": False,
            "disabled_reason": "manually",
            "active_tasks": 0,
        },
        {
            "id": "acct-deepseek",
            "user_id": _TEST_USER,
            "name": "deepseek",
            "provider": "deepseek",
            "auth_mode": "api_key",
            "runtime_secret": "sk-ds",
            "runtime_ready": True,
            "enabled": False,
            "disabled_reason": "manually",
            "active_tasks": 0,
        },
    ):
        _seed(entry)

    failure_reasons: list[str] = []
    result = await select_llm_runtime_for_task(
        user_id=_TEST_USER,
        task=_task(payload={"type": "research", "model": "glm-5.2-custom-preview"}),
        runtime_config=_incident_runtime_config(),
        failure_reasons=failure_reasons,
    )

    # Nothing in the pool can serve this task, so the selector returns an
    # account-less result — this is the state the runner turns into
    # `no_eligible_account`.
    assert result is not None
    assert result.account is None

    # EVERY routed provider was attempted, not just the first. Real scan count.
    attempted = [r.split(":", 1)[0] for r in failure_reasons]
    assert len(attempted) == 3, failure_reasons
    # The custom id is scoped to the FIRST routed provider only; the others use
    # their own configured models rather than inheriting an alien model id.
    assert attempted[0] == "zai/glm-5.2-custom-preview", failure_reasons
    assert "anthropic/claude-opus-5" in attempted, failure_reasons
    assert "deepseek/deepseek-v4-flash" in attempted, failure_reasons
    # And the WHY names the disabled account, literally.
    assert any("glm: account disabled (manually)" in r for r in failure_reasons), (
        failure_reasons
    )


@pytest.mark.asyncio
async def test_route_eligibility_probe_tracks_the_disabled_account(clean_runtime_pool):
    """The self-heal probe behind the unroutable block.

    A task BLOCKED because no enabled account could serve its route must come
    back on its own the moment the account is re-enabled — otherwise the fix
    trades an unbounded PROVISIONING orbit for an unbounded BLOCKED one, and an
    operator has to hunt down and requeue the row by hand.
    """
    from app.max_plan_pool import task_route_has_eligible_account

    cfg = _incident_runtime_config()
    task = _task(payload={"type": "research", "model": "claude-opus-5"})

    glm = _seed(
        {
            "id": "acct-glm",
            "user_id": _TEST_USER,
            "name": "glm",
            "provider": "zai",
            "auth_mode": "api_key",
            "runtime_secret": "sk-glm",
            "runtime_ready": True,
            "enabled": False,
            "disabled_reason": "manually",
            "active_tasks": 0,
        }
    )
    # Every routed account disabled: nothing can serve the task.
    assert task_route_has_eligible_account(task, cfg, user_id=_TEST_USER) is False

    # Re-enabling the ONE account the operator turned off clears it. This is the
    # entire recovery path for the 2026-08-24 strand.
    glm["enabled"] = True
    assert task_route_has_eligible_account(task, cfg, user_id=_TEST_USER) is True


@pytest.mark.asyncio
async def test_route_eligibility_probe_accepts_a_fallback_provider(clean_runtime_pool):
    """A healthy account on ANY routed provider clears the block.

    The disabled account never has to come back: routing the task type at a
    provider that does have an enabled account is an equally valid recovery, and
    the probe must see it. (Pre-fix, `claude-opus-5` inferred to no provider and
    the route collapsed to one entry, so anthropic was invisible here too.)
    """
    from app.max_plan_pool import task_route_has_eligible_account

    cfg = _incident_runtime_config()
    task = _task(payload={"type": "research", "model": "claude-opus-5"})

    _seed(
        {
            "id": "acct-glm",
            "user_id": _TEST_USER,
            "name": "glm",
            "provider": "zai",
            "auth_mode": "api_key",
            "runtime_secret": "sk-glm",
            "runtime_ready": True,
            "enabled": False,
            "disabled_reason": "manually",
            "active_tasks": 0,
        }
    )
    assert task_route_has_eligible_account(task, cfg, user_id=_TEST_USER) is False

    _seed(
        {
            "id": "acct-lihuans",
            "user_id": _TEST_USER,
            "name": "lihuansClaude",
            "provider": "anthropic",
            "auth_mode": "oauth",
            "oauth_token": "sk-ant-live",
            "runtime_ready": True,
            "enabled": True,
            "active_tasks": 0,
        }
    )
    assert task_route_has_eligible_account(task, cfg, user_id=_TEST_USER) is True

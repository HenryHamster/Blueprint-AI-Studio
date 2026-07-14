from httpx import ASGITransport, AsyncClient


async def test_get_settings_includes_provider_scaffolding(client):
    res = await client.get("/api/admin/settings")

    assert res.status_code == 200
    body = res.json()
    assert "provider_tier_model_map" in body
    assert "task_type_categories" in body
    assert "category_routing" in body
    assert "task_type_routing" in body
    assert "provider_settings_by_effort" in body
    assert "task_type_model_preferences" in body
    assert "provider_selection_mode" in body
    assert body["provider_selection_mode"] in ("balanced", "preferred", "exclusive")
    # Legacy boolean is still exposed for back-compat; derived from the mode.
    assert "cross_provider_fallback_enabled" in body
    assert isinstance(body["cross_provider_fallback_enabled"], bool)
    assert "providers" in body
    assert "provider_settings_defaults" in body
    assert "task_type_provider_overrides" in body
    assert body["provider_tier_model_map"]["anthropic"]["opus"] == "claude-opus-4-8"
    assert body["provider_tier_model_map"]["openai"] == {
        "opus": "gpt-5.6-sol",
        "sonnet": "gpt-5.6-terra",
        "haiku": "gpt-5.6-luna",
    }
    assert body["task_type_model_preferences"]["smoketest"] == [
        {"provider": "openai", "model": "gpt-5.6-sol"}
    ]
    # Categories ship with sensible defaults; user can move task types between them.
    assert "build" in body["task_type_categories"]
    assert "create" in body["task_type_categories"]["build"]
    assert body["task_type_categories"]["strategic"] == [
        "strategic_review",
        "design_doc_update",
    ]
    assert body["task_type_categories"]["initiative"] == ["research", "plan"]
    # _default routing is the global allow-list + effort fallback.
    assert body["category_routing"]["_default"]["providers"] == [
        "deepseek",
        "anthropic",
        "openai",
        "gemini",
    ]
    assert body["category_routing"]["_default"]["effort"] in (
        "low",
        "medium",
        "high",
        "max",
    )
    # Effort matrix ships with sensible per-(provider, level) bags including model.
    assert (
        body["provider_settings_by_effort"]["anthropic"]["medium"]["model"]
        == "claude-sonnet-5"
    )
    assert (
        body["provider_settings_by_effort"]["openai"]["low"]["model"] == "gpt-5.6-luna"
    )
    assert body["category_routing"]["strategic"]["providers"][0] == "zai"
    assert "zai" not in body["category_routing"]["initiative"]["providers"]
    assert body["provider_settings_by_effort"]["deepseek"]["low"]["model"] == (
        "deepseek-v4-flash"
    )
    assert "service_account" in body["providers"]["gemini"]["auth_modes"]
    assert body["providers"]["gemini"]["auth_modes"]["service_account"][
        "required_metadata_fields"
    ] == ["project", "location"]


async def test_get_provider_catalog_route_returns_provider_schema(client):
    res = await client.get("/api/admin/providers")

    assert res.status_code == 200
    body = res.json()
    assert "providers" in body
    assert set(body["providers"].keys()) == {
        "anthropic",
        "openai",
        "gemini",
        "zai",
        "deepseek",
    }
    assert "oauth" in body["providers"]["openai"]["auth_modes"]
    assert list(body["providers"]["zai"]["auth_modes"].keys()) == ["api_key"]
    assert list(body["providers"]["deepseek"]["auth_modes"].keys()) == ["api_key"]
    # Schema for the settings bag is exposed per provider.
    assert body["providers"]["anthropic"]["settings_schema"]["type"] == "object"


def test_migrate_legacy_provider_config_backfills_without_rewriting_routes():
    """Backfill seeds MISSING routing keys but never rewrites explicit routes.

    Regression for the routing-persistence bug: _migrate_legacy_provider_config
    (and its _backfill_provider_runtime_defaults step) runs on EVERY persisted-
    config sync and again at dispatch-time resolution, so any forced reordering
    here silently reverted a successful operator settings PATCH on the next
    sync cycle (observed live: Strategic routing snapped back to
    zai → anthropic → deepseek after being explicitly changed).
    """
    from app.runtime_config_state import _migrate_legacy_provider_config

    config = {
        "task_type_categories": {
            "strategic": ["strategic_review", "design_doc_update", "research", "plan"],
            "build": ["create"],
            "ops": ["docs"],
        },
        "category_routing": {
            "_default": {
                "providers": ["anthropic", "openai", "gemini"],
                "effort": "medium",
            },
            "ops": {"providers": ["anthropic", "openai"], "effort": "low"},
        },
        "provider_tier_model_map": {
            "anthropic": {
                "opus": "claude-opus-4-8",
                "sonnet": "claude-sonnet-4-6",
                "haiku": "claude-haiku-4-5-20251001",
            }
        },
        "provider_settings_by_effort": {},
    }

    migrated = _migrate_legacy_provider_config(config)

    assert migrated["task_type_categories"]["strategic"] == [
        "strategic_review",
        "design_doc_update",
    ]
    assert migrated["task_type_categories"]["initiative"] == ["research", "plan"]
    # Explicit nonempty routes are operator-owned: preserved byte-for-byte.
    assert migrated["category_routing"]["_default"]["providers"] == [
        "anthropic",
        "openai",
        "gemini",
    ]
    assert migrated["category_routing"]["ops"]["providers"] == [
        "anthropic",
        "openai",
    ]
    # Missing categories are still seeded from the current defaults.
    assert migrated["category_routing"]["strategic"]["providers"][0] == "zai"
    assert migrated["category_routing"]["build"]["providers"][0] == "deepseek"
    assert migrated["provider_selection_mode"] == "preferred"
    assert migrated["provider_tier_model_map"]["deepseek"]["haiku"] == (
        "deepseek-v4-flash"
    )
    assert migrated["provider_settings_by_effort"]["deepseek"]["low"]["model"] == (
        "deepseek-v4-flash"
    )
    assert migrated["provider_settings_defaults"]["deepseek"] == {}


def test_migrate_legacy_provider_config_is_stable_across_repeated_syncs():
    """An explicit operator routing survives repeated reload/dispatch cycles.

    Mirrors production traffic: sync_runtime_config_from_state re-migrates the
    persisted JSON every cycle, and dispatch-time resolution migrates again.
    The PATCHed routing must be identical after every pass — including a
    single-provider route and a reordered multi-provider route.
    """
    import copy

    from app.runtime_config_state import _migrate_legacy_provider_config

    operator_routing = {
        # Multi-provider route deliberately ordered anthropic-first with
        # deepseek removed — the exact shape the old backfill clobbered.
        "strategic": {"providers": ["anthropic", "zai"], "effort": "max"},
        # Single-provider route (the documented workaround shape).
        "review": {"providers": ["anthropic"], "effort": "high"},
        "_default": {"providers": ["deepseek", "anthropic"], "effort": "medium"},
    }
    config = {
        "category_routing": copy.deepcopy(operator_routing),
        "provider_selection_mode": "exclusive",
        "provider_settings_by_effort": {},
    }

    migrated = _migrate_legacy_provider_config(dict(config))
    for _ in range(3):
        migrated = _migrate_legacy_provider_config(copy.deepcopy(migrated))

    for category, route in operator_routing.items():
        assert migrated["category_routing"][category]["providers"] == (
            route["providers"]
        ), f"category_routing.{category} was rewritten by backfill"
        assert migrated["category_routing"][category]["effort"] == route["effort"]
    assert migrated["provider_selection_mode"] == "exclusive"


def test_migrate_live_openai_constraint_to_gpt_5_6_smoke():
    """The known live 5.5/5.4-mini shape was a staged-rollout constraint.

    Once the backend schema accepts the 5.6 ids, config sync advances that exact
    shape, its stock effort bags, and the missing smoketest preference together.
    Custom settings survive and the migration is idempotent.
    """
    import copy

    from app.max_plan_pool import infer_provider_for_model
    from app.runtime_config_state import _migrate_legacy_provider_config

    config = {
        "category_routing": {
            "_default": {"providers": ["openai"], "effort": "medium"}
        },
        "provider_tier_model_map": {
            "openai": {
                "opus": "gpt-5.5",
                "sonnet": "gpt-5.5",
                "haiku": "gpt-5.4-mini",
            }
        },
        "provider_settings_by_effort": {
            "openai": {
                "low": {
                    "model": "gpt-5.4-mini",
                    "reasoning_effort": "low",
                    "verbosity": "low",
                },
                "medium": {
                    "model": "gpt-5.5",
                    "reasoning_effort": "medium",
                    "verbosity": "high",
                },
                "high": {"model": "gpt-5.5", "reasoning_effort": "high"},
                # A custom model is operator-owned even when the tier map is the
                # known rollout shape.
                "max": {"model": "gpt-custom-internal", "reasoning_effort": "xhigh"},
            }
        },
        "task_type_model_preferences": {
            "create": [{"provider": "anthropic", "model": "claude-opus-4-8"}]
        },
    }

    migrated = _migrate_legacy_provider_config(copy.deepcopy(config))

    assert migrated["provider_tier_model_map"]["openai"] == {
        "opus": "gpt-5.6-sol",
        "sonnet": "gpt-5.6-terra",
        "haiku": "gpt-5.6-luna",
    }
    openai_bags = migrated["provider_settings_by_effort"]["openai"]
    assert openai_bags["low"]["model"] == "gpt-5.6-luna"
    assert openai_bags["medium"]["model"] == "gpt-5.6-terra"
    assert openai_bags["medium"]["verbosity"] == "high"
    assert openai_bags["high"]["model"] == "gpt-5.6-sol"
    assert openai_bags["max"]["model"] == "gpt-custom-internal"
    assert migrated["task_type_model_preferences"]["smoketest"] == [
        {"provider": "openai", "model": "gpt-5.6-sol"}
    ]
    assert infer_provider_for_model("gpt-5.6-sol", migrated) == "openai"
    assert _migrate_legacy_provider_config(copy.deepcopy(migrated)) == migrated


def test_migrate_preserves_custom_openai_map_and_explicit_smoke_disable():
    from app.runtime_config_state import _migrate_legacy_provider_config

    config = {
        "category_routing": {
            "_default": {"providers": ["anthropic"], "effort": "medium"}
        },
        "provider_tier_model_map": {
            "openai": {
                "opus": "gpt-custom-frontier",
                "sonnet": "gpt-5.5",
                "haiku": "gpt-5.4-mini",
            }
        },
        "provider_settings_by_effort": {},
        "task_type_model_preferences": {"smoketest": []},
    }

    migrated = _migrate_legacy_provider_config(config)

    assert migrated["provider_tier_model_map"]["openai"]["opus"] == (
        "gpt-custom-frontier"
    )
    assert migrated["task_type_model_preferences"]["smoketest"] == []


async def test_patch_settings_accepts_routing_and_matrix(client):
    payload = {
        "provider_tier_model_map": {
            "anthropic": {
                "opus": "claude-opus-4-7",
                "sonnet": "claude-sonnet-4-6",
                "haiku": "claude-haiku-4-5-20251001",
            },
            "openai": {
                "opus": "gpt-5.4",
                "sonnet": "gpt-5.3-codex",
                "haiku": "gpt-5.4-mini",
            },
            "gemini": {
                "opus": "gemini-2.5-pro",
                "sonnet": "gemini-2.5-flash",
                "haiku": "gemini-2.5-flash-lite",
            },
        },
        "category_routing": {
            "_default": {
                "providers": ["anthropic", "openai", "gemini"],
                "effort": "medium",
            },
            "strategic": {"providers": ["anthropic"], "effort": "max"},
        },
        "task_type_routing": {
            "debug": {"providers": ["openai"], "effort": "high"},
            "docs": {"effort": "low"},
        },
        "task_type_model_preferences": {
            "create": [
                {"provider": "openai", "model": "gpt-5.4"},
                {"provider": "anthropic", "model": "claude-opus-4-7"},
            ],
        },
        "cross_provider_fallback_enabled": True,
    }
    patch_res = await client.patch("/api/admin/settings", json=payload)
    assert patch_res.status_code == 200, patch_res.text

    body = (await client.get("/api/admin/settings")).json()
    assert body["category_routing"]["strategic"] == {
        "providers": ["anthropic"],
        "effort": "max",
    }
    assert body["task_type_routing"]["debug"] == {
        "providers": ["openai"],
        "effort": "high",
    }
    assert body["task_type_routing"]["docs"] == {"effort": "low"}
    assert body["task_type_model_preferences"]["create"][0]["provider"] == "openai"
    assert body["cross_provider_fallback_enabled"] is True


async def test_patch_settings_rejects_category_routing_without_effort(client):
    res = await client.patch(
        "/api/admin/settings",
        json={"category_routing": {"build": {"providers": ["anthropic"]}}},
    )
    assert res.status_code == 400
    assert "effort" in res.text


async def test_patch_settings_rejects_invalid_effort_level(client):
    res = await client.patch(
        "/api/admin/settings",
        json={
            "category_routing": {
                "build": {"providers": ["anthropic"], "effort": "turbo"}
            }
        },
    )
    assert res.status_code == 400
    assert "effort" in res.text


async def test_patch_settings_rejects_task_type_routing_with_unknown_key(client):
    res = await client.patch(
        "/api/admin/settings",
        json={"task_type_routing": {"debug": {"providers": ["openai"], "bogus": "x"}}},
    )
    assert res.status_code == 400
    assert "bogus" in res.text


async def test_patch_settings_accepts_effort_matrix_with_model_per_level(client):
    payload = {
        "provider_settings_by_effort": {
            "openai": {
                "low": {"model": "gpt-5.4-mini", "reasoning_effort": "low"},
                "medium": {"model": "gpt-5.4-mini", "reasoning_effort": "medium"},
                "high": {"model": "gpt-5.4", "reasoning_effort": "high"},
                "max": {
                    "model": "gpt-5.4",
                    "reasoning_effort": "xhigh",
                    "verbosity": "high",
                },
            },
        },
    }
    res = await client.patch("/api/admin/settings", json=payload)
    assert res.status_code == 200, res.text
    body = (await client.get("/api/admin/settings")).json()
    assert body["provider_settings_by_effort"]["openai"]["high"]["model"] == "gpt-5.4"
    assert body["provider_settings_by_effort"]["openai"]["max"]["verbosity"] == "high"


async def test_patch_settings_rejects_unknown_effort_level_in_matrix(client):
    res = await client.patch(
        "/api/admin/settings",
        json={
            "provider_settings_by_effort": {
                "openai": {"ludicrous": {"model": "gpt-5.4"}}
            }
        },
    )
    assert res.status_code == 400
    assert "ludicrous" in res.text or "effort level" in res.text


async def test_providers_endpoint_exposes_settings_schema(client):
    res = await client.get("/api/admin/providers")
    assert res.status_code == 200
    body = res.json()
    anthropic_schema = body["providers"]["anthropic"]["settings_schema"]
    assert anthropic_schema["type"] == "object"
    assert "model" in anthropic_schema["properties"]
    assert "advisor" in anthropic_schema["properties"]
    gemini_schema = body["providers"]["gemini"]["settings_schema"]
    assert "thinking_budget" in gemini_schema["properties"]


async def test_get_settings_includes_provider_settings_defaults(client):
    res = await client.get("/api/admin/settings")
    assert res.status_code == 200
    body = res.json()
    assert "provider_settings_defaults" in body
    assert "task_type_provider_overrides" in body
    # Defaults are the effort-independent floor; real settings live in the matrix.
    assert isinstance(body["provider_settings_defaults"]["anthropic"], dict)


async def test_patch_settings_accepts_warm_pool_account_retarget(client):
    """The warm-pool account-retarget flags must be settable via PATCH /settings.

    They lived in the code gate (``is_warm_pool_account_retarget_enabled``) but
    were absent from the validated settings allowlist, so the operator had no way
    to enable account-agnostic leasing — which is why a small pool never hit on a
    multi-account fleet (every member is pinned to one account; strict equality
    misses the rest). Mutates the shared runtime_config, so it restores defaults.
    """
    from app.shared_state import runtime_config
    from app.warm_pool import (
        is_warm_pool_account_retarget_enabled,
        is_warm_pool_cross_provider_retarget_enabled,
    )

    try:
        res = await client.patch(
            "/api/admin/settings",
            json={
                "warm_pool_account_retarget_enabled": True,
                "warm_pool_cross_provider_retarget_enabled": False,
            },
        )
        assert res.status_code == 200, res.text
        assert is_warm_pool_account_retarget_enabled() is True
        assert is_warm_pool_cross_provider_retarget_enabled() is False

        body = (await client.get("/api/admin/settings")).json()
        assert body["warm_pool_account_retarget_enabled"] is True
        assert body["warm_pool_cross_provider_retarget_enabled"] is False
    finally:
        runtime_config["warm_pool_account_retarget_enabled"] = False
        runtime_config["warm_pool_cross_provider_retarget_enabled"] = False


async def test_patch_settings_rejects_non_bool_account_retarget(client):
    res = await client.patch(
        "/api/admin/settings",
        json={"warm_pool_account_retarget_enabled": "yes"},
    )
    assert res.status_code == 400


async def test_patch_settings_accepts_valid_provider_settings_bag(client):
    payload = {
        "provider_settings_defaults": {
            "anthropic": {"model": "claude-opus-4-7"},
        },
        "task_type_provider_overrides": {
            "docs": {"anthropic": {"model": "claude-haiku-4-5-20251001"}},
        },
    }
    res = await client.patch("/api/admin/settings", json=payload)
    assert res.status_code == 200, res.text
    body = (await client.get("/api/admin/settings")).json()
    assert (
        body["task_type_provider_overrides"]["docs"]["anthropic"]["model"]
        == "claude-haiku-4-5-20251001"
    )


async def test_patch_settings_rejects_unknown_settings_key(client):
    payload = {
        "provider_settings_defaults": {
            "anthropic": {"model": "claude-opus-4-7", "bogus_key": "oops"},
        },
    }
    res = await client.patch("/api/admin/settings", json=payload)
    assert res.status_code == 400
    assert "unknown key" in res.text.lower() or "bogus_key" in res.text


async def test_patch_settings_rejects_invalid_enum_in_settings(client):
    payload = {
        "task_type_provider_overrides": {
            "review": {"anthropic": {"effort": "absurd"}},
        },
    }
    res = await client.patch("/api/admin/settings", json=payload)
    assert res.status_code == 400


async def test_patch_settings_accepts_provider_selection_mode(client):
    for mode, expected_legacy in (
        ("balanced", True),
        ("preferred", True),
        ("exclusive", False),
    ):
        patch_res = await client.patch(
            "/api/admin/settings", json={"provider_selection_mode": mode}
        )
        assert patch_res.status_code == 200, patch_res.text
        body = (await client.get("/api/admin/settings")).json()
        assert body["provider_selection_mode"] == mode
        assert body["cross_provider_fallback_enabled"] is expected_legacy


async def test_patch_settings_rejects_invalid_provider_selection_mode(client):
    res = await client.patch(
        "/api/admin/settings", json={"provider_selection_mode": "round_robin"}
    )
    assert res.status_code == 400
    assert "provider_selection_mode" in res.text


async def test_patch_settings_legacy_cross_provider_fallback_boolean_shim(client):
    patch_res = await client.patch(
        "/api/admin/settings", json={"cross_provider_fallback_enabled": False}
    )
    assert patch_res.status_code == 200, patch_res.text
    body = (await client.get("/api/admin/settings")).json()
    assert body["provider_selection_mode"] == "exclusive"
    assert body["cross_provider_fallback_enabled"] is False

    patch_res = await client.patch(
        "/api/admin/settings", json={"cross_provider_fallback_enabled": True}
    )
    assert patch_res.status_code == 200, patch_res.text
    body = (await client.get("/api/admin/settings")).json()
    assert body["provider_selection_mode"] == "preferred"
    assert body["cross_provider_fallback_enabled"] is True


async def test_get_settings_publishes_task_capability_contract(client):
    """GET /settings embeds the published task-capability contract; it must
    cover every registered task type and carry the consumed flags."""
    from app.task_capabilities import ALL_TASK_TYPES

    body = (await client.get("/api/admin/settings")).json()
    assert "task_capabilities" in body
    contract = body["task_capabilities"]
    assert set(contract) == set(ALL_TASK_TYPES)
    create = contract["create"]
    assert create["label"] == "Create"
    assert create["user_launchable"] is True
    assert create["reservable"] is True
    assert create["delivers_product"] is True
    # A read-only, non-launchable type is published with the right flags.
    assert contract["security_audit"]["periodic_eligible"] is True
    assert contract["security_audit"]["agent_mode"] == "read_only"


async def test_task_capabilities_endpoint_mirrors_settings_contract(client):
    """The standalone endpoint returns the same contract as GET /settings."""
    endpoint = (await client.get("/api/admin/task-capabilities")).json()
    settings = (await client.get("/api/admin/settings")).json()
    assert endpoint["task_capabilities"] == settings["task_capabilities"]


async def test_task_capabilities_endpoint_requires_admin(override_get_session):
    """Non-admin callers are blocked from the standalone admin contract route."""
    from fastapi import HTTPException

    from app.auth import get_current_user as _gcu, require_admin as _ra
    from app.main import app as _app

    async def _non_admin():
        return {
            "user_id": "non-admin-user",
            "email": "user@example.com",
            "role": "user",
        }

    async def _reject_admin():
        raise HTTPException(status_code=403, detail="Forbidden")

    _app.dependency_overrides[_gcu] = _non_admin
    _app.dependency_overrides[_ra] = _reject_admin
    try:
        async with AsyncClient(
            transport=ASGITransport(app=_app), base_url="http://test"
        ) as ac:
            resp = await ac.get("/api/admin/task-capabilities")
        assert resp.status_code == 403
    finally:
        _app.dependency_overrides.pop(_gcu, None)
        _app.dependency_overrides.pop(_ra, None)


async def test_patch_rejects_unknown_task_type_in_routing(client):
    res = await client.patch(
        "/api/admin/settings",
        json={"task_type_routing": {"nonsense_type": {"effort": "low"}}},
    )
    assert res.status_code == 400
    assert "nonsense_type" in res.text


async def test_patch_rejects_unknown_task_type_in_model_preferences(client):
    res = await client.patch(
        "/api/admin/settings",
        json={
            "task_type_model_preferences": {
                "typoed_type": [{"provider": "anthropic", "model": "claude-opus-4-8"}]
            }
        },
    )
    assert res.status_code == 400
    assert "typoed_type" in res.text


async def test_patch_rejects_unknown_task_type_in_provider_overrides(client):
    res = await client.patch(
        "/api/admin/settings",
        json={"task_type_provider_overrides": {"bogus": {"anthropic": {}}}},
    )
    assert res.status_code == 400
    assert "bogus" in res.text


async def test_patch_rejects_unknown_task_type_in_categories(client):
    res = await client.patch(
        "/api/admin/settings",
        json={"task_type_categories": {"build": ["create", "not_a_type"]}},
    )
    assert res.status_code == 400
    assert "not_a_type" in res.text


async def test_patch_rejects_non_reservable_task_type_in_reserved_slots(client):
    # `review` is a known task type but is NOT reservable — the surface must
    # reject it on capability-flag grounds, not just unknown-type grounds.
    res = await client.patch(
        "/api/admin/settings",
        json={"reserved_slots_for_types": {"review": 1}},
    )
    assert res.status_code == 400
    assert "review" in res.text


async def test_patch_accepts_reservable_task_types(client):
    res = await client.patch(
        "/api/admin/settings",
        json={"reserved_slots_for_types": {"create": 2, "plan": 1, "research": 1}},
    )
    assert res.status_code == 200, res.text
    body = (await client.get("/api/admin/settings")).json()
    assert body["reserved_slots_for_types"]["plan"] == 1


async def test_patch_rejects_non_periodic_task_type(client):
    # `create` is known but not periodic-eligible.
    res = await client.patch(
        "/api/admin/settings",
        json={"autonomous_periodic_tasks": {"create": {"enabled": True}}},
    )
    assert res.status_code == 400
    assert "create" in res.text


async def test_settings_maps_validate_from_registry_without_per_surface_allowlist(
    client,
):
    """A task type's eligibility for each map derives from its capability flags,
    so the accept/reject decision tracks the registry — no surface keeps its own
    hand-maintained allowlist. We prove the linkage holds for every registered
    type against the routing surface (gated on provider_configurable)."""
    from app.task_capabilities import (
        PROVIDER_CONFIGURABLE_TASK_TYPES,
        TASK_CAPABILITIES,
    )

    configurable = set(PROVIDER_CONFIGURABLE_TASK_TYPES)
    for cap in TASK_CAPABILITIES:
        tt = cap.id.value
        res = await client.patch(
            "/api/admin/settings",
            json={"task_type_routing": {tt: {"effort": "low"}}},
        )
        if tt in configurable:
            assert res.status_code == 200, f"{tt}: {res.text}"
        else:
            assert res.status_code == 400, f"{tt} should be rejected"


async def test_patch_settings_mode_wins_over_legacy_boolean_in_same_payload(client):
    res = await client.patch(
        "/api/admin/settings",
        json={
            "provider_selection_mode": "balanced",
            "cross_provider_fallback_enabled": False,
        },
    )
    assert res.status_code == 200, res.text
    body = (await client.get("/api/admin/settings")).json()
    assert body["provider_selection_mode"] == "balanced"
    assert body["cross_provider_fallback_enabled"] is True


def test_deepseek_preferred_for_build_initiative_tasks():
    """Verify that DeepSeek is the first preferred provider for low/medium-effort
    task types (build, initiative, ops, _default categories) and NOT for high/max-effort
    categories (strategic, review), exercising resolve_deepseek_preference."""
    from app.max_plan_pool import resolve_deepseek_preference

    config = {
        "task_type_categories": {
            "build": ["create", "refactor", "migrate"],
            "initiative": ["research", "plan"],
            "ops": ["docs", "deps", "smoketest"],
            "strategic": ["strategic_review", "design_doc_update"],
            "review": ["review", "qa", "security_audit", "perf", "debug", "fix"],
        },
        "category_routing": {
            "_default": {
                "providers": ["deepseek", "anthropic", "openai", "gemini"],
                "effort": "medium",
            },
            "strategic": {
                "providers": ["zai", "anthropic", "deepseek", "openai", "gemini"],
                "effort": "max",
            },
            "initiative": {
                "providers": ["deepseek", "anthropic", "openai", "gemini"],
                "effort": "medium",
            },
            "build": {
                "providers": ["deepseek", "anthropic", "openai", "gemini"],
                "effort": "medium",
            },
            "review": {
                "providers": ["anthropic", "deepseek", "openai", "gemini"],
                "effort": "high",
            },
            "ops": {
                "providers": ["deepseek", "anthropic", "openai", "gemini"],
                "effort": "low",
            },
        },
    }

    # Low/medium-effort categories should prefer DeepSeek first
    for task_type in ("create", "refactor", "migrate", "research", "plan",
                      "docs", "smoketest", "deps"):
        assert resolve_deepseek_preference(task_type, config), (
            f"DeepSeek should be the first preferred provider for {task_type}"
        )

    # High/max-effort categories should NOT prefer DeepSeek first
    for task_type in ("strategic_review", "design_doc_update", "review",
                      "qa", "security_audit", "perf"):
        assert not resolve_deepseek_preference(task_type, config), (
            f"DeepSeek should NOT be the first preferred provider for {task_type}"
        )

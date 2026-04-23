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
    assert body["provider_tier_model_map"]["anthropic"]["opus"] == "claude-opus-4-7"
    # Categories ship with sensible defaults; user can move task types between them.
    assert "build" in body["task_type_categories"]
    assert "create" in body["task_type_categories"]["build"]
    # _default routing is the global allow-list + effort fallback.
    assert body["category_routing"]["_default"]["providers"] == ["anthropic", "openai", "gemini"]
    assert body["category_routing"]["_default"]["effort"] in ("low", "medium", "high", "max")
    # Effort matrix ships with sensible per-(provider, level) bags including model.
    assert body["provider_settings_by_effort"]["anthropic"]["medium"]["model"] == "claude-sonnet-4-6"
    assert body["provider_settings_by_effort"]["openai"]["low"]["model"] == "gpt-5.4-mini"
    assert "service_account" in body["providers"]["gemini"]["auth_modes"]
    assert body["providers"]["gemini"]["auth_modes"]["service_account"]["required_metadata_fields"] == ["project", "location"]


async def test_get_provider_catalog_route_returns_provider_schema(client):
    res = await client.get("/api/admin/providers")

    assert res.status_code == 200
    body = res.json()
    assert "providers" in body
    assert set(body["providers"].keys()) == {"anthropic", "openai", "gemini"}
    assert "oauth" in body["providers"]["openai"]["auth_modes"]
    # Schema for the settings bag is exposed per provider.
    assert body["providers"]["anthropic"]["settings_schema"]["type"] == "object"


async def test_patch_settings_accepts_routing_and_matrix(client):
    payload = {
        "provider_tier_model_map": {
            "anthropic": {"opus": "claude-opus-4-7", "sonnet": "claude-sonnet-4-6", "haiku": "claude-haiku-4-5-20251001"},
            "openai": {"opus": "gpt-5.4", "sonnet": "gpt-5.3-codex", "haiku": "gpt-5.4-mini"},
            "gemini": {"opus": "gemini-2.5-pro", "sonnet": "gemini-2.5-flash", "haiku": "gemini-2.5-flash-lite"},
        },
        "category_routing": {
            "_default": {"providers": ["anthropic", "openai", "gemini"], "effort": "medium"},
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
    assert body["category_routing"]["strategic"] == {"providers": ["anthropic"], "effort": "max"}
    assert body["task_type_routing"]["debug"] == {"providers": ["openai"], "effort": "high"}
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
        json={"category_routing": {"build": {"providers": ["anthropic"], "effort": "turbo"}}},
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
                "max": {"model": "gpt-5.4", "reasoning_effort": "xhigh", "verbosity": "high"},
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
        json={"provider_settings_by_effort": {"openai": {"ludicrous": {"model": "gpt-5.4"}}}},
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
    assert body["task_type_provider_overrides"]["docs"]["anthropic"]["model"] == "claude-haiku-4-5-20251001"


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

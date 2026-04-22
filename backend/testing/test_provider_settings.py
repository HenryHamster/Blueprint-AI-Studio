async def test_get_settings_includes_provider_scaffolding(client):
    res = await client.get("/api/admin/settings")

    assert res.status_code == 200
    body = res.json()
    assert "provider_tier_model_map" in body
    assert "task_type_provider_preference" in body
    assert "task_type_categories" in body
    assert "provider_preference_by_category" in body
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
    # Categories ship with sensible defaults; user can move task types
    # between them via the admin PATCH endpoint.
    assert "build" in body["task_type_categories"]
    assert "create" in body["task_type_categories"]["build"]
    # _default is the global allow-list that every provider falls back to.
    assert body["provider_preference_by_category"]["_default"] == ["anthropic", "openai", "gemini"]
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


async def test_patch_settings_accepts_provider_scaffolding(client):
    payload = {
        "provider_tier_model_map": {
            "anthropic": {"opus": "claude-opus-4-7", "sonnet": "claude-sonnet-4-6", "haiku": "claude-haiku-4-5-20251001"},
            "openai": {"opus": "gpt-5", "sonnet": "gpt-4.1", "haiku": "gpt-4o-mini"},
            "gemini": {"opus": "gemini-2.5-pro", "sonnet": "gemini-2.5-flash", "haiku": "gemini-2.5-flash-lite"},
        },
        "task_type_provider_preference": {
            "create": ["anthropic", "openai"],
            "research": ["anthropic", "gemini"],
        },
        "task_type_model_preferences": {
            "create": [
                {"provider": "openai", "model": "gpt-5"},
                {"provider": "anthropic", "model": "claude-opus-4-7"},
            ],
        },
        "cross_provider_fallback_enabled": True,
    }
    patch_res = await client.patch("/api/admin/settings", json=payload)
    assert patch_res.status_code == 200, patch_res.text

    get_res = await client.get("/api/admin/settings")
    assert get_res.status_code == 200
    body = get_res.json()
    assert body["task_type_provider_preference"]["create"] == ["anthropic", "openai"]
    assert body["task_type_model_preferences"]["create"][0]["provider"] == "openai"
    assert body["cross_provider_fallback_enabled"] is True
    assert body["provider_tier_model_map"]["openai"]["sonnet"] == "gpt-4.1"


async def test_providers_endpoint_exposes_settings_schema(client):
    res = await client.get("/api/admin/providers")
    assert res.status_code == 200
    body = res.json()
    anthropic_schema = body["providers"]["anthropic"]["settings_schema"]
    assert anthropic_schema["type"] == "object"
    assert "model" in anthropic_schema["properties"]
    # Anthropic has advisor; Gemini doesn't.
    assert "advisor" in anthropic_schema["properties"]
    gemini_schema = body["providers"]["gemini"]["settings_schema"]
    assert "thinking_budget" in gemini_schema["properties"]


async def test_get_settings_includes_provider_settings_defaults(client):
    res = await client.get("/api/admin/settings")
    assert res.status_code == 200
    body = res.json()
    assert "provider_settings_defaults" in body
    assert "task_type_provider_overrides" in body
    assert "effort_profiles" in body
    assert "task_type_effort_profile" in body
    # provider_settings_defaults is the provider-agnostic floor; effort levels
    # live in effort_profiles (defined once per provider) rather than being
    # copy-pasted into every task-type override.
    assert isinstance(body["provider_settings_defaults"]["anthropic"], dict)
    assert body["effort_profiles"]["medium"]["anthropic"]["effort"] == "medium"
    assert body["task_type_effort_profile"]["_default"] == "medium"
    assert body["task_type_effort_profile"]["strategic_review"] == "max"


async def test_patch_settings_accepts_valid_provider_settings_bag(client):
    payload = {
        "provider_settings_defaults": {
            "anthropic": {"model": "claude-opus-4-7", "effort": "medium"},
            "openai": {"model": "gpt-5", "reasoning_effort": "medium"},
        },
        "task_type_provider_overrides": {
            "review": {"anthropic": {"effort": "high"}},
            "docs": {"anthropic": {"model": "claude-sonnet-4-6"}},
        },
    }
    res = await client.patch("/api/admin/settings", json=payload)
    assert res.status_code == 200, res.text
    get_res = await client.get("/api/admin/settings")
    body = get_res.json()
    assert body["task_type_provider_overrides"]["review"]["anthropic"]["effort"] == "high"
    assert body["task_type_provider_overrides"]["docs"]["anthropic"]["model"] == "claude-sonnet-4-6"


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
    # Explicit mode round-trips; legacy boolean in GET is derived from it.
    for mode, expected_legacy in (
        ("balanced", True),
        ("preferred", True),
        ("exclusive", False),
    ):
        patch_res = await client.patch(
            "/api/admin/settings", json={"provider_selection_mode": mode}
        )
        assert patch_res.status_code == 200, patch_res.text
        get_res = await client.get("/api/admin/settings")
        body = get_res.json()
        assert body["provider_selection_mode"] == mode
        assert body["cross_provider_fallback_enabled"] is expected_legacy


async def test_patch_settings_rejects_invalid_provider_selection_mode(client):
    res = await client.patch(
        "/api/admin/settings", json={"provider_selection_mode": "round_robin"}
    )
    assert res.status_code == 400
    assert "provider_selection_mode" in res.text


async def test_patch_settings_legacy_cross_provider_fallback_boolean_shim(client):
    # Sending the deprecated boolean maps to the new enum: True→preferred,
    # False→exclusive. Round-trip returns the derived mode.
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


async def test_patch_settings_accepts_effort_profiles_and_task_mapping(client):
    payload = {
        "effort_profiles": {
            "medium": {
                "anthropic": {"effort": "medium"},
                "openai": {"reasoning_effort": "medium", "verbosity": "medium"},
                "gemini": {"thinking_budget": 4096},
            },
            "premium": {
                "anthropic": {"effort": "xhigh"},
                "openai": {"reasoning_effort": "high"},
                "gemini": {"thinking_budget": 16384},
            },
        },
        "task_type_effort_profile": {
            "_default": "medium",
            "strategic_review": "premium",
        },
    }
    res = await client.patch("/api/admin/settings", json=payload)
    assert res.status_code == 200, res.text
    body = (await client.get("/api/admin/settings")).json()
    assert body["effort_profiles"]["premium"]["gemini"]["thinking_budget"] == 16384
    assert body["task_type_effort_profile"]["strategic_review"] == "premium"


async def test_patch_settings_rejects_unknown_profile_in_task_mapping(client):
    res = await client.patch(
        "/api/admin/settings",
        json={"task_type_effort_profile": {"create": "does_not_exist"}},
    )
    assert res.status_code == 400
    assert "undefined profile" in res.text.lower() or "does_not_exist" in res.text


async def test_patch_settings_rejects_reserved_default_key_as_profile_name(client):
    res = await client.patch(
        "/api/admin/settings",
        json={"effort_profiles": {"_default": {"anthropic": {"effort": "high"}}}},
    )
    assert res.status_code == 400
    assert "_default" in res.text


async def test_patch_settings_accepts_category_preference_edits(client):
    # Users can rearrange categories + per-category preferences freely.
    payload = {
        "task_type_categories": {
            "coding": ["create", "refactor", "fix"],
            "analysis": ["research", "review"],
        },
        "provider_preference_by_category": {
            "_default": ["anthropic", "openai", "gemini"],
            "coding": ["anthropic"],
            "analysis": ["anthropic", "openai"],
        },
    }
    res = await client.patch("/api/admin/settings", json=payload)
    assert res.status_code == 200, res.text
    body = (await client.get("/api/admin/settings")).json()
    assert body["task_type_categories"]["coding"] == ["create", "refactor", "fix"]
    assert body["provider_preference_by_category"]["coding"] == ["anthropic"]
    assert body["provider_preference_by_category"]["_default"] == ["anthropic", "openai", "gemini"]


async def test_patch_settings_rejects_reserved_default_key_as_category_name(client):
    res = await client.patch(
        "/api/admin/settings",
        json={"task_type_categories": {"_default": ["create"]}},
    )
    assert res.status_code == 400
    assert "_default" in res.text


async def test_patch_settings_rejects_invalid_provider_in_category_preference(client):
    res = await client.patch(
        "/api/admin/settings",
        json={"provider_preference_by_category": {"coding": ["anthropic", "fakeprov"]}},
    )
    assert res.status_code == 400
    assert "fakeprov" in res.text


async def test_patch_settings_mode_wins_over_legacy_boolean_in_same_payload(client):
    # When both the new enum and the legacy boolean arrive together, the enum
    # is authoritative — otherwise the compat shim would fight the new value.
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

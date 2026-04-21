async def test_get_settings_includes_provider_scaffolding(client):
    res = await client.get("/api/admin/settings")

    assert res.status_code == 200
    body = res.json()
    assert "provider_tier_model_map" in body
    assert "task_type_provider_preference" in body
    assert "task_type_model_preferences" in body
    assert "cross_provider_fallback_enabled" in body
    assert "provider_execution_modes" in body
    assert "provider_mode_capabilities" in body
    assert "providers" in body
    assert "task_type_execution_defaults" in body
    assert body["provider_tier_model_map"]["anthropic"]["opus"] == "claude-opus-4-7"
    assert body["task_type_provider_preference"]["create"] == ["anthropic"]
    assert "advisor" in body["provider_execution_modes"]["anthropic"]
    assert "service_account" in body["providers"]["gemini"]["auth_modes"]
    assert body["providers"]["gemini"]["auth_modes"]["service_account"]["required_metadata_fields"] == ["project", "location"]


async def test_get_provider_catalog_route_returns_provider_schema(client):
    res = await client.get("/api/admin/providers")

    assert res.status_code == 200
    body = res.json()
    assert "providers" in body
    assert set(body["providers"].keys()) == {"anthropic", "openai", "gemini"}
    assert "oauth" in body["providers"]["openai"]["auth_modes"]
    assert "advisor" in body["providers"]["anthropic"]["supported_execution_modes"]


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
        "provider_execution_modes": {
            "anthropic": ["default", "advisor"],
            "openai": ["default"],
            "gemini": ["default"],
        },
        "provider_mode_capabilities": {
            "anthropic": {
                "default": {"read_only_preferred": False},
                "advisor": {"read_only_preferred": True},
            },
            "openai": {
                "default": {"read_only_preferred": False},
            },
            "gemini": {
                "default": {"read_only_preferred": False},
            },
        },
        "task_type_execution_defaults": {
            "research": "advisor",
            "create": "default",
        },
        "task_type_models": {
            "create": "gpt-5",
            "research": "gemini-2.5-pro",
        },
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
    assert body["task_type_execution_defaults"]["research"] == "advisor"
    assert body["provider_mode_capabilities"]["anthropic"]["advisor"]["read_only_preferred"] is True
    assert body["task_type_models"]["create"] == "gpt-5"


async def test_patch_settings_rejects_unknown_provider_mode_capabilities(client):
    payload = {
        "provider_mode_capabilities": {
            "anthropic": {
                "nonexistent": {"read_only_preferred": True},
            },
        },
    }

    res = await client.patch("/api/admin/settings", json=payload)

    assert res.status_code == 400
    assert "Invalid execution mode" in res.text

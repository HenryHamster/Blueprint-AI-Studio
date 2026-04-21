from unittest.mock import AsyncMock

from sqlmodel import select

from app.models import LLMAuthMode, LLMProviderName, MaxPlanAccount


async def test_add_max_plan_account_legacy_anthropic_payload(client, db_session, mocker):
    mocker.patch(
        "app.secrets_manager.store_user_secret",
        new=AsyncMock(return_value="projects/test/secrets/user-system-llm-account/versions/1"),
    )

    res = await client.post(
        "/api/admin/max-plan-accounts",
        json={"name": "Legacy Anthropic", "oauth_token": "sk-ant-oat01-valid_token"},
    )

    assert res.status_code == 200, res.text
    body = res.json()
    assert body["provider"] == "anthropic"
    assert body["auth_mode"] == "oauth"

    acct = (
        await db_session.execute(select(MaxPlanAccount).where(MaxPlanAccount.name == "Legacy Anthropic"))
    ).scalars().one()
    assert acct.provider == LLMProviderName.ANTHROPIC
    assert acct.auth_mode == LLMAuthMode.OAUTH
    assert acct.secret_blob_ref == "projects/test/secrets/user-system-llm-account/versions/1"
    assert acct.oauth_token == "projects/test/secrets/user-system-llm-account/versions/1"


async def test_add_max_plan_account_openai_provider_payload(client, db_session, mocker):
    mocker.patch(
        "app.secrets_manager.store_user_secret",
        new=AsyncMock(return_value="projects/test/secrets/user-system-llm-account/versions/2"),
    )

    res = await client.post(
        "/api/admin/max-plan-accounts",
        json={
            "name": "OpenAI API",
            "provider": "openai",
            "auth_mode": "api_key",
            "secret": "sk-proj-test_token",
        },
    )

    assert res.status_code == 200, res.text
    body = res.json()
    assert body["provider"] == "openai"
    assert body["auth_mode"] == "api_key"

    acct = (
        await db_session.execute(select(MaxPlanAccount).where(MaxPlanAccount.name == "OpenAI API"))
    ).scalars().one()
    assert acct.provider == LLMProviderName.OPENAI
    assert acct.auth_mode == LLMAuthMode.API_KEY
    assert acct.secret_blob_ref == "projects/test/secrets/user-system-llm-account/versions/2"
    assert acct.oauth_token is None


async def test_add_max_plan_account_gemini_service_account_requires_metadata(client):
    res = await client.post(
        "/api/admin/max-plan-accounts",
        json={
            "name": "Gemini Vertex",
            "provider": "gemini",
            "auth_mode": "service_account",
            "secret": '{"type":"service_account","client_email":"bot@example.com","private_key":"abc"}',
        },
    )

    assert res.status_code == 400
    assert "Missing provider metadata fields: project, location" in res.text


async def test_update_max_plan_account_requires_new_secret_when_provider_changes(client, db_session):
    acct = MaxPlanAccount(
        user_id="test-user-123",
        name="Anthropic OAuth",
        provider=LLMProviderName.ANTHROPIC,
        auth_mode=LLMAuthMode.OAUTH,
        oauth_token="projects/test/secrets/acct/versions/1",
        secret_blob_ref="projects/test/secrets/acct/versions/1",
    )
    db_session.add(acct)
    await db_session.commit()

    res = await client.patch(
        f"/api/admin/max-plan-accounts/{acct.id}",
        json={"provider": "openai", "auth_mode": "api_key"},
    )

    assert res.status_code == 400
    assert "Changing provider or auth_mode requires a new secret" in res.text


async def test_update_max_plan_account_allows_auth_mode_change_with_existing_claude_config(client, db_session, mocker):
    mocker.patch(
        "app.secrets_manager.store_user_secret",
        new=AsyncMock(return_value="projects/test/secrets/acct/versions/2"),
    )
    acct = MaxPlanAccount(
        user_id="test-user-123",
        name="Anthropic OAuth",
        provider=LLMProviderName.ANTHROPIC,
        auth_mode=LLMAuthMode.OAUTH,
        oauth_token="projects/test/secrets/acct/versions/1",
        secret_blob_ref="projects/test/secrets/acct/versions/1",
        claude_config_json='{"onboardingAccepted":true}',
    )
    db_session.add(acct)
    await db_session.commit()

    res = await client.patch(
        f"/api/admin/max-plan-accounts/{acct.id}",
        json={"auth_mode": "api_key", "secret": "sk-ant-api03-updatedtoken"},
    )

    assert res.status_code == 200, res.text

    await db_session.refresh(acct)
    assert acct.auth_mode == LLMAuthMode.API_KEY
    assert acct.claude_config_json is None

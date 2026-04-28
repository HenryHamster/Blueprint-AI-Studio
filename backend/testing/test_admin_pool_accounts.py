import uuid
from datetime import datetime, timedelta
from unittest.mock import AsyncMock

import pytest
from sqlmodel import select

from app.max_plan_pool import _account_by_id, _max_plan_pools
from app.models import LLMAuthMode, LLMProviderName, MaxPlanAccount, Project, Task, TaskStatus


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


async def test_add_max_plan_account_accepts_usage_caps(client, db_session, mocker):
    mocker.patch(
        "app.secrets_manager.store_user_secret",
        new=AsyncMock(return_value="projects/test/secrets/caps/versions/1"),
    )

    res = await client.post(
        "/api/admin/max-plan-accounts",
        json={
            "name": "Capped Account",
            "oauth_token": "sk-ant-oat01-capped",
            "daily_task_cap": 20,
            "max_concurrent_tasks": 3,
            "auto_disable_after_rate_limits": 5,
        },
    )

    assert res.status_code == 200, res.text
    acct = (
        await db_session.execute(select(MaxPlanAccount).where(MaxPlanAccount.name == "Capped Account"))
    ).scalars().one()
    assert acct.daily_task_cap == 20
    assert acct.max_concurrent_tasks == 3
    assert acct.auto_disable_after_rate_limits == 5


async def test_add_max_plan_account_rejects_zero_cap(client):
    res = await client.post(
        "/api/admin/max-plan-accounts",
        json={
            "name": "Zero Cap",
            "oauth_token": "sk-ant-oat01-zero",
            "daily_task_cap": 0,
        },
    )
    assert res.status_code == 400
    assert "daily_task_cap" in res.text


async def test_patch_max_plan_account_updates_and_clears_caps(client, db_session):
    acct = MaxPlanAccount(
        user_id="test-user-123",
        name="Patch Caps",
        provider=LLMProviderName.ANTHROPIC,
        auth_mode=LLMAuthMode.OAUTH,
        oauth_token="projects/test/secrets/patch/versions/1",
        secret_blob_ref="projects/test/secrets/patch/versions/1",
        daily_task_cap=10,
        max_concurrent_tasks=2,
    )
    db_session.add(acct)
    await db_session.commit()

    res = await client.patch(
        f"/api/admin/max-plan-accounts/{acct.id}",
        json={"daily_task_cap": 50, "max_concurrent_tasks": None},
    )
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["daily_task_cap"] == 50
    assert body["max_concurrent_tasks"] is None

    await db_session.refresh(acct)
    assert acct.daily_task_cap == 50
    assert acct.max_concurrent_tasks is None


async def test_patch_reenables_clears_disabled_reason(client, db_session):
    acct = MaxPlanAccount(
        user_id="test-user-123",
        name="Auto Disabled",
        provider=LLMProviderName.ANTHROPIC,
        auth_mode=LLMAuthMode.OAUTH,
        oauth_token="projects/test/secrets/reenable/versions/1",
        secret_blob_ref="projects/test/secrets/reenable/versions/1",
        enabled=False,
        disabled_reason="auto-disabled after 5 rate-limit hit(s)",
    )
    db_session.add(acct)
    await db_session.commit()

    res = await client.patch(
        f"/api/admin/max-plan-accounts/{acct.id}",
        json={"enabled": True},
    )
    assert res.status_code == 200, res.text

    await db_session.refresh(acct)
    assert acct.enabled is True
    assert acct.disabled_reason is None


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


# ── /max-plan-accounts/health ──────────────────────────────────────────────

@pytest.fixture
def inject_pool():
    """Inject test accounts into the in-memory pool with auto-cleanup."""
    injected: dict[str, list[str]] = {}  # user_id -> [account_ids]

    def _inject(user_id: str, accounts: list):
        _max_plan_pools[user_id] = accounts
        for a in accounts:
            _account_by_id[a["id"]] = a
        injected.setdefault(user_id, []).extend(a["id"] for a in accounts)

    yield _inject

    for uid, acct_ids in injected.items():
        _max_plan_pools.pop(uid, None)
        for acct_id in acct_ids:
            _account_by_id.pop(acct_id, None)


async def test_account_health_summary_returns_per_account_data(client, inject_pool):
    """Health endpoint returns one entry per in-pool account with correct fields."""
    now = datetime.utcnow()
    acct_id = str(uuid.uuid4())
    acct = {
        "id": acct_id,
        "name": "Test Account",
        "provider": "anthropic",
        "auth_mode": "oauth",
        "oauth_token": "sk-ant-test",
        "enabled": True,
        "active_tasks": 0,
        "completion_events": [{"at": now - timedelta(hours=1), "duration_seconds": 30}],
        "outcome_events": [
            {"at": now - timedelta(hours=1), "success": True},
            {"at": now - timedelta(hours=2), "success": True},
            {"at": now - timedelta(hours=3), "success": False},
        ],
        "rl_events": [now - timedelta(hours=2)],
    }
    inject_pool("test-user-123", [acct])

    res = await client.get("/api/admin/max-plan-accounts/health")
    assert res.status_code == 200
    body = res.json()
    assert len(body["accounts"]) == 1
    a = body["accounts"][0]
    assert a["account_id"] == acct_id
    assert a["label"] == "Test Account"
    assert a["state"] == "healthy"
    assert a["completions_24h"] == 1
    assert a["rl_events_24h"] == 1
    assert a["health_score"] == -1.0  # 1 completion - 2 * 1 rl_event
    assert a["success_rate_7d"] == pytest.approx(2 / 3)
    assert set(a["failure_class_counts"].keys()) == set(
        ["CODE_ERROR", "RATE_LIMIT", "EXTERNAL_API",
         "PROVISION_FAILURE", "MIGRATION_ERROR", "AGENT_TIMEOUT", "SCOPE_OVERFLOW"]
    )
    assert all(v == 0 for v in a["failure_class_counts"].values())
    assert a["rate_limited_models"] == []


async def test_account_health_summary_failure_class_window_filter(client, db_session, inject_pool):
    """Only tasks updated within the 24h window contribute to failure_class_counts."""
    now = datetime.utcnow()
    acct_id = str(uuid.uuid4())
    acct = {
        "id": acct_id, "name": "Window Test", "provider": "anthropic",
        "auth_mode": "oauth", "oauth_token": "sk-ant-test",
        "enabled": True, "active_tasks": 0,
    }
    inject_pool("test-user-123", [acct])

    project = Project(name="p", user_id="test-user-123")
    db_session.add(project)
    await db_session.flush()

    task_in = Task(
        project_id=project.id, status=TaskStatus.FAILED,
        latest_status_message="tests failed: assertion error",
        account_id=acct_id,
        updated_at=now - timedelta(hours=12),
    )
    task_out = Task(
        project_id=project.id, status=TaskStatus.FAILED,
        latest_status_message="tests failed: assertion error",
        account_id=acct_id,
        updated_at=now - timedelta(hours=36),
    )
    db_session.add_all([task_in, task_out])
    await db_session.commit()

    res = await client.get("/api/admin/max-plan-accounts/health")
    assert res.status_code == 200
    a = res.json()["accounts"][0]
    assert a["failure_class_counts"]["CODE_ERROR"] == 1  # only task_in counted
    assert sum(a["failure_class_counts"].values()) == 1
    db_session.expunge_all()
    reloaded = await db_session.get(Task, task_in.id)
    assert reloaded is not None
    assert reloaded.account_id == acct_id


async def test_account_health_summary_user_isolation(client, inject_pool):
    """Health endpoint only returns accounts belonging to the authenticated user."""
    acct_other = {
        "id": str(uuid.uuid4()), "name": "Other User Account",
        "provider": "anthropic", "auth_mode": "oauth",
        "oauth_token": "sk-ant-other", "enabled": True, "active_tasks": 0,
    }
    inject_pool("other-user-456", [acct_other])
    inject_pool("test-user-123", [])

    res = await client.get("/api/admin/max-plan-accounts/health")
    assert res.status_code == 200
    assert res.json()["accounts"] == []


async def test_account_health_summary_fully_disabled_state(client, inject_pool):
    """Accounts with enabled=False show state='disabled' regardless of health score."""
    acct_id = str(uuid.uuid4())
    acct = {
        "id": acct_id, "name": "Disabled Account", "provider": "anthropic",
        "auth_mode": "oauth", "oauth_token": "sk-ant-test",
        "enabled": False, "active_tasks": 0,
        "disabled_reason": "auto-disabled after 5 rate-limit hit(s)",
    }
    inject_pool("test-user-123", [acct])

    res = await client.get("/api/admin/max-plan-accounts/health")
    assert res.status_code == 200
    a = res.json()["accounts"][0]
    assert a["state"] == "disabled"
    assert a["disabled_reason"] == "auto-disabled after 5 rate-limit hit(s)"


async def test_account_health_summary_disqualified_state(client, inject_pool):
    """Healthy accounts with health score below the disqualify threshold show state='disqualified'."""
    now = datetime.utcnow()
    acct_id = str(uuid.uuid4())
    # 0 completions, 3 RL events → score = 0 - 2*3 = -6, below threshold of -4
    acct = {
        "id": acct_id, "name": "Disqualified Account", "provider": "anthropic",
        "auth_mode": "oauth", "oauth_token": "sk-ant-test",
        "enabled": True, "active_tasks": 0,
        "rl_events": [now - timedelta(hours=1), now - timedelta(hours=2), now - timedelta(hours=3)],
    }
    inject_pool("test-user-123", [acct])

    res = await client.get("/api/admin/max-plan-accounts/health")
    assert res.status_code == 200
    a = res.json()["accounts"][0]
    assert a["state"] == "disqualified"
    assert a["health_score"] < -4


async def test_account_health_summary_disabled_models_with_kind(client, inject_pool):
    """Disabled models list distinguishes quota_exhausted from exponential cooldown."""
    now = datetime.utcnow()
    acct_id = str(uuid.uuid4())
    acct = {
        "id": acct_id, "name": "RL Account", "provider": "anthropic",
        "auth_mode": "oauth", "oauth_token": "sk-ant-test",
        "enabled": True, "active_tasks": 0,
        "disabled_models": {
            "claude-opus-4-7": {"hits": 1, "at": now - timedelta(minutes=5)},
            "claude-sonnet-4-6": {"hits": 2, "at": now - timedelta(hours=1), "kind": "quota_exhausted"},
        },
    }
    inject_pool("test-user-123", [acct])

    res = await client.get("/api/admin/max-plan-accounts/health")
    assert res.status_code == 200
    a = res.json()["accounts"][0]
    mods = {m["model"]: m for m in a["rate_limited_models"]}
    assert mods["claude-opus-4-7"]["cooldown_type"] == "rate_limit"
    assert mods["claude-sonnet-4-6"]["cooldown_type"] == "quota_exhausted"
    assert mods["claude-sonnet-4-6"]["cooldown_seconds_remaining"] > 3600  # flat 24h

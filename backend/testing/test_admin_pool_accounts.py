import asyncio
import json
import uuid
from datetime import timedelta
from typing import Any, Dict
from unittest.mock import AsyncMock

import pytest
from sqlmodel import select

from app.digest_cache import DigestCache, get_digest_cache
from app.main import app
from app.providers.base import UsageSnapshot
from app.routers import admin_pool_accounts as _admin_pool_accounts
from app.max_plan_pool import _account_by_id, _max_plan_pools
from app.models import (
    LLMAuthMode,
    LLMProviderName,
    MaxPlanAccount,
    PoolVm,
    PoolVmState,
    Project,
    ProjectMember,
    Task,
    TaskStatus,
)


async def test_add_max_plan_account_legacy_anthropic_payload(
    client, db_session, mocker
):
    mocker.patch(
        "app.secrets_manager.store_user_secret",
        new=AsyncMock(
            return_value="projects/test/secrets/user-system-llm-account/versions/1"
        ),
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
        (
            await db_session.execute(
                select(MaxPlanAccount).where(MaxPlanAccount.name == "Legacy Anthropic")
            )
        )
        .scalars()
        .one()
    )
    assert acct.provider == LLMProviderName.ANTHROPIC
    assert acct.auth_mode == LLMAuthMode.OAUTH
    # secret_blob_ref / oauth_token are encrypted at rest (Fernet) whenever an
    # encryption key is configured (Postgres CI harness), so assert against the
    # value read through the same decrypting accessor the app uses. Under the
    # keyless SQLite run decrypt_field is a pass-through, so this holds in both.
    from app.field_encryption import decrypt_field

    assert (
        decrypt_field(acct.secret_blob_ref)
        == "projects/test/secrets/user-system-llm-account/versions/1"
    )
    assert (
        decrypt_field(acct.oauth_token)
        == "projects/test/secrets/user-system-llm-account/versions/1"
    )


async def test_add_max_plan_account_openai_provider_payload(client, db_session, mocker):
    mocker.patch(
        "app.secrets_manager.store_user_secret",
        new=AsyncMock(
            return_value="projects/test/secrets/user-system-llm-account/versions/2"
        ),
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
        (
            await db_session.execute(
                select(MaxPlanAccount).where(MaxPlanAccount.name == "OpenAI API")
            )
        )
        .scalars()
        .one()
    )
    assert acct.provider == LLMProviderName.OPENAI
    assert acct.auth_mode == LLMAuthMode.API_KEY
    # secret_blob_ref is encrypted at rest (Fernet) when an encryption key is
    # configured (Postgres CI harness); read it back through decrypt_field, which
    # is a pass-through under the keyless SQLite run.
    from app.field_encryption import decrypt_field

    assert (
        decrypt_field(acct.secret_blob_ref)
        == "projects/test/secrets/user-system-llm-account/versions/2"
    )
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


async def test_update_max_plan_account_requires_new_secret_when_provider_changes(
    client, db_session
):
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
        (
            await db_session.execute(
                select(MaxPlanAccount).where(MaxPlanAccount.name == "Capped Account")
            )
        )
        .scalars()
        .one()
    )
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


async def test_update_max_plan_account_allows_auth_mode_change_with_existing_claude_config(
    client, db_session, mocker
):
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


async def test_max_plan_accounts_list_is_lightweight_authoritative_and_mutations_invalidate_cached_health(
    client, db_session, inject_pool, mocker
):
    live_id = uuid.uuid4()
    db_only_id = uuid.uuid4()
    db_configured_id = uuid.uuid4()
    db_session.add_all(
        [
            MaxPlanAccount(
                id=live_id,
                user_id="test-user-123",
                name="Live Account",
                provider=LLMProviderName.ANTHROPIC,
                auth_mode=LLMAuthMode.OAUTH,
                oauth_token="projects/test/secrets/live/versions/1",
                secret_blob_ref="projects/test/secrets/live/versions/1",
                provider_metadata={"available_models": ["claude-opus-4-8"]},
                total_tasks=7,
                daily_task_cap=20,
                max_concurrent_tasks=2,
                claude_config_json='{"secret":"do-not-return"}',
            ),
            MaxPlanAccount(
                id=db_only_id,
                user_id="test-user-123",
                name="DB Only Account",
                provider=LLMProviderName.OPENAI,
                auth_mode=LLMAuthMode.API_KEY,
                secret_blob_ref="projects/test/secrets/db-only/versions/1",
                provider_metadata={"available_models": ["gpt-4.1"]},
                enabled=False,
                disabled_reason="manual maintenance",
            ),
            MaxPlanAccount(
                id=db_configured_id,
                user_id="test-user-123",
                name="Configured DB Only Account",
                provider=LLMProviderName.GEMINI,
                auth_mode=LLMAuthMode.SERVICE_ACCOUNT,
                secret_blob_ref="projects/test/secrets/configured-db-only/versions/1",
                provider_metadata={"project": "p", "location": "us-central1"},
                enabled=True,
            ),
        ]
    )
    await db_session.commit()

    inject_pool(
        "test-user-123",
        [
            {
                "id": str(live_id),
                "user_id": "test-user-123",
                "name": "Live Account",
                "provider": "anthropic",
                "auth_mode": "oauth",
                "runtime_secret": "sk-ant-oat01-secret",
                "oauth_token": "sk-ant-oat01-secret",
                "secret_blob_ref": "projects/test/secrets/live/versions/1",
                "provider_metadata": {
                    "available_models": ["claude-opus-4-8", "claude-sonnet-4-6"]
                },
                "enabled": True,
                "active_tasks": 3,
                "total_tasks": 99,
                "daily_task_count": 4,
                "daily_task_count_reset_at": _admin_pool_accounts._utcnow(),
                "daily_task_cap": 20,
                "max_concurrent_tasks": 2,
                "auto_disable_after_rate_limits": 5,
                "disabled_reason": "rate limited on opus",
                "disabled_models": {
                    "claude-opus-4-8": {
                        "hits": 2,
                            "at": _admin_pool_accounts._utcnow(),
                        "kind": "quota_exhausted",
                    }
                },
                "runtime_ready": True,
            }
        ],
    )

    cache = DigestCache(default_ttl=300.0)
    app.dependency_overrides[get_digest_cache] = lambda: cache
    health_spy = AsyncMock(
        return_value={
            "accounts": [{"account_id": str(live_id)}],
            "generated_at": _admin_pool_accounts._utcnow().isoformat(),
        }
    )
    breakdown_spy = AsyncMock(
        return_value={
            "rows": [{"provider": "anthropic", "model": "opus"}],
            "generated_at": _admin_pool_accounts._utcnow().isoformat(),
        }
    )
    mocker.patch.object(
        _admin_pool_accounts, "_compute_account_health_summary", health_spy
    )
    mocker.patch.object(
        _admin_pool_accounts, "_compute_provider_model_breakdown", breakdown_spy
    )

    try:
        first_health = await client.get("/api/admin/max-plan-accounts/health")
        first_breakdown = await client.get(
            "/api/admin/max-plan-accounts/provider-model-breakdown"
        )
        assert first_health.status_code == 200
        assert first_breakdown.status_code == 200
        assert health_spy.await_count == 1
        assert breakdown_spy.await_count == 1

        roster = await client.get("/api/admin/max-plan-accounts")
        assert roster.status_code == 200, roster.text
        accounts = {a["id"]: a for a in roster.json()["accounts"]}
        assert set(accounts) == {
            str(live_id),
            str(db_only_id),
            str(db_configured_id),
        }
        assert accounts[str(live_id)]["active_tasks"] == 3
        assert accounts[str(live_id)]["total_tasks"] == 7
        assert accounts[str(live_id)]["provider_metadata"]["available_models"] == [
            "claude-opus-4-8",
            "claude-sonnet-4-6",
        ]
        assert accounts[str(live_id)]["disabled_models"]["claude-opus-4-8"][
            "kind"
        ] == "quota_exhausted"
        assert accounts[str(live_id)]["credential_configured"] is True
        assert accounts[str(live_id)]["runtime_ready"] is True
        assert accounts[str(live_id)]["account_status"] == "at_cap"
        assert "concurrent_cap_reached" in accounts[str(live_id)]["status_reasons"]
        assert accounts[str(live_id)]["usage"]["daily_task_remaining"] == 16
        assert accounts[str(live_id)]["usage"]["daily_task_usage_fraction"] == 0.2
        assert accounts[str(live_id)]["usage"]["concurrent_task_remaining"] == 0
        assert accounts[str(live_id)]["usage"]["at_any_cap"] is True
        assert accounts[str(live_id)]["usage_snapshot"]["known"] is False
        assert accounts[str(db_only_id)]["name"] == "DB Only Account"
        assert accounts[str(db_only_id)]["enabled"] is False
        assert accounts[str(db_only_id)]["account_status"] == "disabled"
        assert "disabled" in accounts[str(db_only_id)]["status_reasons"]
        assert accounts[str(db_only_id)]["usage_snapshot"]["known"] is False
        configured = accounts[str(db_configured_id)]
        assert configured["credential_configured"] is True
        assert configured["runtime_ready"] is False
        assert configured["account_status"] == "unavailable"
        assert configured["status_reasons"] == ["runtime_not_loaded"]
        assert configured["usage"]["at_any_cap"] is False
        assert configured["usage_snapshot"]["known"] is False
        serialized = json.dumps(roster.json())
        assert "sk-ant-oat01-secret" not in serialized
        for forbidden in (
            "oauth_token",
            "secret_blob_ref",
            "runtime_secret",
            "claude_config_json",
        ):
            assert forbidden not in serialized

        patch = await client.patch(
            f"/api/admin/max-plan-accounts/{live_id}",
            json={"name": "Edited Live Account", "daily_task_cap": 30},
        )
        assert patch.status_code == 200, patch.text

        second_health = await client.get("/api/admin/max-plan-accounts/health")
        second_breakdown = await client.get(
            "/api/admin/max-plan-accounts/provider-model-breakdown"
        )
        assert second_health.status_code == 200
        assert second_breakdown.status_code == 200
        assert health_spy.await_count == 2
        assert breakdown_spy.await_count == 2

        edited = await client.get("/api/admin/max-plan-accounts")
        edited_accounts = {a["id"]: a for a in edited.json()["accounts"]}
        assert edited_accounts[str(live_id)]["name"] == "Edited Live Account"
        assert edited_accounts[str(live_id)]["daily_task_cap"] == 30
    finally:
        app.dependency_overrides.pop(get_digest_cache, None)


async def test_delete_max_plan_account_conflicts_then_force_removes_db_and_memory(
    client, db_session, inject_pool, mocker
):
    acct_id = uuid.uuid4()
    project = Project(name="delete-conflict", user_id="test-user-123")
    db_session.add(project)
    await db_session.flush()
    acct = MaxPlanAccount(
        id=acct_id,
        user_id="test-user-123",
        name="Delete Guard",
        provider=LLMProviderName.ANTHROPIC,
        auth_mode=LLMAuthMode.OAUTH,
        oauth_token="projects/test/secrets/delete/versions/1",
        secret_blob_ref="projects/test/secrets/delete/versions/1",
    )
    active_task = Task(
        project_id=project.id,
        status=TaskStatus.IN_PROGRESS,
        account_id=str(acct_id),
        context={"llm_account_id": str(acct_id)},
    )
    warm_member = PoolVm(
        vm_instance_id="warm-delete-guard",
        project_id=project.id,
        account_id=str(acct_id),
        state=PoolVmState.IDLE,
    )
    db_session.add_all([acct, active_task, warm_member])
    await db_session.commit()
    inject_pool(
        "test-user-123",
        [
            {
                "id": str(acct_id),
                "user_id": "test-user-123",
                "name": "Delete Guard",
                "provider": "anthropic",
                "auth_mode": "oauth",
                "enabled": True,
                "active_tasks": 1,
                "total_tasks": 0,
            }
        ],
    )
    delete_secret = mocker.patch(
        "app.secrets_manager.delete_user_secret",
        new=AsyncMock(side_effect=RuntimeError("secret cleanup failed")),
    )

    conflict = await client.delete(f"/api/admin/max-plan-accounts/{acct_id}")
    assert conflict.status_code == 409
    conflict_body = conflict.json()
    detail = (
        conflict_body.get("detail")
        or (conflict_body.get("error") or {}).get("message")
        or conflict.text
    )
    assert "active task(s)" in detail
    assert "live in-memory reservation(s)" in detail
    assert "warm-pool VM reference(s)" in detail
    assert "force=true" in detail
    delete_secret.assert_not_awaited()

    forced = await client.delete(f"/api/admin/max-plan-accounts/{acct_id}?force=true")
    assert forced.status_code == 200, forced.text
    assert forced.json()["message"] == "Account removed"
    db_session.expunge_all()
    assert await db_session.get(MaxPlanAccount, acct_id) is None
    assert str(acct_id) not in _account_by_id
    assert all(
        a["id"] != str(acct_id) for a in _max_plan_pools.get("test-user-123", [])
    )
    delete_secret.assert_awaited_once_with("projects/test/secrets/delete/versions/1")


async def test_delete_max_plan_account_conflict_detected_via_llm_account_id_column(
    client, db_session, inject_pool, mocker
):
    """Delete conflict is detected via Task.llm_account_id column (not JSONB path)."""
    acct_id = uuid.uuid4()
    project = Project(name="col-conflict", user_id="test-user-123")
    db_session.add(project)
    await db_session.flush()
    acct = MaxPlanAccount(
        id=acct_id,
        user_id="test-user-123",
        name="Column Guard",
        provider=LLMProviderName.ANTHROPIC,
        auth_mode=LLMAuthMode.OAUTH,
        oauth_token="projects/test/secrets/col/versions/1",
        secret_blob_ref="projects/test/secrets/col/versions/1",
    )
    # Task with llm_account_id column set — account_id is intentionally unset
    # so the conflict is found only via the indexed column, not account_id.
    active_task = Task(
        project_id=project.id,
        status=TaskStatus.IN_PROGRESS,
        llm_account_id=str(acct_id),
    )
    db_session.add_all([acct, active_task])
    await db_session.commit()
    inject_pool(
        "test-user-123",
        [
            {
                "id": str(acct_id),
                "user_id": "test-user-123",
                "name": "Column Guard",
                "provider": "anthropic",
                "auth_mode": "oauth",
                "enabled": True,
                "active_tasks": 0,
            }
        ],
    )
    mocker.patch(
        "app.secrets_manager.delete_user_secret",
        new=AsyncMock(return_value=None),
    )

    conflict = await client.delete(f"/api/admin/max-plan-accounts/{acct_id}")
    assert conflict.status_code == 409
    body = conflict.json()
    detail = (
        body.get("detail") or (body.get("error") or {}).get("message") or ""
    )
    assert "active task(s)" in detail


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
    now = _admin_pool_accounts._utcnow()
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
        "usage_snapshot": UsageSnapshot(
            known=True,
            window_utilizations={"five_hour": 42.0, "seven_day": 63.5},
            resets_at={"five_hour": "2026-06-30T12:00:00Z", "seven_day": None},
            probed_at="2026-06-30T01:00:00",
        ),
        "usage_snapshot_at": now,
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
    assert set(a["failure_class_counts"].keys()) == {
        "CODE_ERROR",
        "RATE_LIMIT",
        "EXTERNAL_API",
        "PROVISION_FAILURE",
        "MIGRATION_ERROR",
        "AGENT_TIMEOUT",
        "SCOPE_OVERFLOW",
    }
    assert all(v == 0 for v in a["failure_class_counts"].values())
    assert a["rate_limited_models"] == []
    assert a["usage_snapshot"]["known"] is True
    assert a["usage_snapshot"]["five_hour_utilization"] == 42.0
    assert a["usage_snapshot"]["seven_day_utilization"] == 63.5
    assert a["usage_snapshot"]["resets_at"]["five_hour"] == "2026-06-30T12:00:00Z"
    assert a["usage_snapshot"]["stale"] is False


async def test_usage_snapshot_falls_back_to_persisted_db_blob_on_non_leader(
    client, db_session, inject_pool
):
    """Non-leader repro: the in-memory probe result is absent (probes run only
    on the leader instance), but the shared DB has a persisted rate_limit_usage
    blob. Both the roster and the health endpoint must surface it as known —
    otherwise the operator's usage meter renders blank on every non-leader."""
    now = _admin_pool_accounts._utcnow()
    acct_id = uuid.uuid4()
    persisted = {
        "updated_at": now.isoformat(),
        "source": "oauth_usage_probe",
        "five_hour": {"utilization": 37.0, "resets_at": "2026-07-14T18:00:00Z"},
        "seven_day": {"utilization": 58.0},
    }
    db_session.add(
        MaxPlanAccount(
            id=acct_id,
            user_id="test-user-123",
            name="Non-Leader Account",
            provider=LLMProviderName.ANTHROPIC,
            auth_mode=LLMAuthMode.OAUTH,
            oauth_token="sk-ant-oat-token",
            secret_blob_ref="sk-ant-oat-token",
            provider_metadata={"available_models": ["claude-opus-4-8"]},
            enabled=True,
            rate_limit_usage=persisted,
        )
    )
    await db_session.commit()

    # In-memory pool entry WITHOUT any usage_snapshot — exactly what a
    # non-leader instance holds after loading the pool with no probe of its own.
    inject_pool(
        "test-user-123",
        [
            {
                "id": str(acct_id),
                "user_id": "test-user-123",
                "name": "Non-Leader Account",
                "provider": "anthropic",
                "auth_mode": "oauth",
                "oauth_token": "sk-ant-oat-token",
                "runtime_secret": "sk-ant-oat-token",
                "enabled": True,
                "active_tasks": 0,
            }
        ],
    )

    # Roster endpoint (SettingsAccountsSection data source).
    roster = await client.get("/api/admin/max-plan-accounts")
    assert roster.status_code == 200, roster.text
    acct = {a["id"]: a for a in roster.json()["accounts"]}[str(acct_id)]
    assert acct["usage_snapshot"]["known"] is True
    assert acct["usage_snapshot"]["five_hour_utilization"] == 37.0
    assert acct["usage_snapshot"]["seven_day_utilization"] == 58.0
    assert acct["usage_snapshot"]["stale"] is False

    # Health endpoint (RuntimeAccountsPanel + PoolAccountHealthPanel source).
    health = await client.get("/api/admin/max-plan-accounts/health")
    assert health.status_code == 200
    h = {a["account_id"]: a for a in health.json()["accounts"]}[str(acct_id)]
    assert h["usage_snapshot"]["known"] is True
    assert h["usage_snapshot"]["five_hour_utilization"] == 37.0
    assert h["usage_snapshot"]["seven_day_utilization"] == 58.0


async def test_account_health_summary_failure_class_window_filter(
    client, db_session, inject_pool
):
    """Only tasks updated within the 24h window contribute to failure_class_counts."""
    now = _admin_pool_accounts._utcnow()
    acct_id = str(uuid.uuid4())
    acct = {
        "id": acct_id,
        "name": "Window Test",
        "provider": "anthropic",
        "auth_mode": "oauth",
        "oauth_token": "sk-ant-test",
        "enabled": True,
        "active_tasks": 0,
    }
    inject_pool("test-user-123", [acct])

    project = Project(name="p", user_id="test-user-123")
    db_session.add(project)
    await db_session.flush()

    task_in = Task(
        project_id=project.id,
        status=TaskStatus.FAILED,
        latest_status_message="tests failed: assertion error",
        account_id=acct_id,
        updated_at=now - timedelta(hours=12),
    )
    task_out = Task(
        project_id=project.id,
        status=TaskStatus.FAILED,
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


async def test_account_health_summary_matches_context_account_ids(
    client, db_session, inject_pool
):
    """Context-only account matches are counted via the SQLite-safe predicate path."""
    acct_id = str(uuid.uuid4())
    acct = {
        "id": acct_id,
        "name": "Context Match",
        "provider": "anthropic",
        "auth_mode": "oauth",
        "oauth_token": "sk-ant-test",
        "enabled": True,
        "active_tasks": 0,
    }
    inject_pool("test-user-123", [acct])

    project = Project(name="context-match", user_id="test-user-123")
    db_session.add(project)
    await db_session.flush()

    tasks = [
        Task(
            project_id=project.id,
            status=TaskStatus.FAILED,
            latest_status_message="tests failed: assertion error",
            context={"llm_account_id": acct_id},
            updated_at=_admin_pool_accounts._utcnow() - timedelta(hours=1),
        ),
        Task(
            project_id=project.id,
            status=TaskStatus.CANCELLED,
            latest_status_message="tests failed: assertion error",
            context={"max_plan_account_id": acct_id},
            updated_at=_admin_pool_accounts._utcnow() - timedelta(hours=2),
        ),
    ]
    db_session.add_all(tasks)
    await db_session.commit()

    res = await client.get("/api/admin/max-plan-accounts/health")
    assert res.status_code == 200, res.text
    a = res.json()["accounts"][0]
    assert a["failure_class_counts"]["CODE_ERROR"] == 2
    assert sum(a["failure_class_counts"].values()) == 2


async def test_account_health_summary_user_isolation(client, inject_pool):
    """Health endpoint only returns accounts belonging to the authenticated user."""
    acct_other = {
        "id": str(uuid.uuid4()),
        "name": "Other User Account",
        "provider": "anthropic",
        "auth_mode": "oauth",
        "oauth_token": "sk-ant-other",
        "enabled": True,
        "active_tasks": 0,
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
        "id": acct_id,
        "name": "Disabled Account",
        "provider": "anthropic",
        "auth_mode": "oauth",
        "oauth_token": "sk-ant-test",
        "enabled": False,
        "active_tasks": 0,
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
    now = _admin_pool_accounts._utcnow()
    acct_id = str(uuid.uuid4())
    # 0 completions, 3 RL events → score = 0 - 2*3 = -6, below threshold of -4
    acct = {
        "id": acct_id,
        "name": "Disqualified Account",
        "provider": "anthropic",
        "auth_mode": "oauth",
        "oauth_token": "sk-ant-test",
        "enabled": True,
        "active_tasks": 0,
        "rl_events": [
            now - timedelta(hours=1),
            now - timedelta(hours=2),
            now - timedelta(hours=3),
        ],
    }
    inject_pool("test-user-123", [acct])

    res = await client.get("/api/admin/max-plan-accounts/health")
    assert res.status_code == 200
    a = res.json()["accounts"][0]
    assert a["state"] == "disqualified"
    assert a["health_score"] < -4


async def test_account_health_summary_disabled_models_with_kind(client, inject_pool):
    """Disabled models list distinguishes quota_exhausted from exponential cooldown."""
    now = _admin_pool_accounts._utcnow()
    acct_id = str(uuid.uuid4())
    acct = {
        "id": acct_id,
        "name": "RL Account",
        "provider": "anthropic",
        "auth_mode": "oauth",
        "oauth_token": "sk-ant-test",
        "enabled": True,
        "active_tasks": 0,
        "disabled_models": {
            "claude-opus-4-7": {"hits": 1, "at": now - timedelta(minutes=5)},
            "claude-sonnet-4-6": {
                "hits": 2,
                "at": now - timedelta(hours=1),
                "kind": "quota_exhausted",
            },
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


async def test_provider_model_breakdown_aggregation_and_median(client, db_session):
    """Breakdown groups by (provider, model), computes median, filters low-sample rows."""
    project = Project(name="pm-test", user_id="test-user-123")
    db_session.add(project)
    await db_session.flush()

    acct_a_id, acct_b_id = uuid.uuid4(), uuid.uuid4()
    acct_a, acct_b = str(acct_a_id), str(acct_b_id)
    mp_a = MaxPlanAccount(
        id=acct_a_id,
        user_id="test-user-123",
        name="PM-A",
        provider=LLMProviderName.ANTHROPIC,
        auth_mode=LLMAuthMode.OAUTH,
    )
    mp_b = MaxPlanAccount(
        id=acct_b_id,
        user_id="test-user-123",
        name="PM-B",
        provider=LLMProviderName.ANTHROPIC,
        auth_mode=LLMAuthMode.OAUTH,
    )
    db_session.add_all([mp_a, mp_b])
    await db_session.flush()

    # anthropic/claude-3-5: acct_a=5C+1F (rate≈0.833), acct_b=2C+1F (rate≈0.667)
    # Both have >= 3 tasks, so both are included in the median calculation.
    tasks = (
        [
            Task(
                project_id=project.id,
                provider="anthropic",
                model="claude-3-5",
                account_id=acct_a,
                status=TaskStatus.COMPLETED,
            )
            for _ in range(5)
        ]
        + [
            Task(
                project_id=project.id,
                provider="anthropic",
                model="claude-3-5",
                account_id=acct_a,
                status=TaskStatus.FAILED,
            ),
            Task(
                project_id=project.id,
                provider="anthropic",
                model="claude-3-5",
                account_id=acct_b,
                status=TaskStatus.COMPLETED,
            ),
            Task(
                project_id=project.id,
                provider="anthropic",
                model="claude-3-5",
                account_id=acct_b,
                status=TaskStatus.COMPLETED,
            ),
            Task(
                project_id=project.id,
                provider="anthropic",
                model="claude-3-5",
                account_id=acct_b,
                status=TaskStatus.FAILED,
            ),
        ]
        + [
            # openai/gpt-4: only 3 tasks — below _PROVIDER_MODEL_MIN_SAMPLE=5, excluded
            Task(
                project_id=project.id,
                provider="openai",
                model="gpt-4",
                account_id=acct_a,
                status=TaskStatus.COMPLETED,
            )
            for _ in range(3)
        ]
    )
    db_session.add_all(tasks)
    await db_session.commit()

    res = await client.get("/api/admin/max-plan-accounts/provider-model-breakdown")
    assert res.status_code == 200
    rows = res.json()["rows"]
    assert len(rows) == 1
    row = rows[0]
    assert row["provider"] == "anthropic"
    assert row["model"] == "claude-3-5"
    assert row["total_tasks"] == 9
    assert row["success_count"] == 7
    assert row["distinct_accounts"] == 2
    # Median of sorted per-account rates [0.667, 0.833] = (0.667 + 0.833) / 2 = 0.750
    assert abs(row["project_median_success_rate"] - 0.750) < 0.001


async def test_provider_model_breakdown_expand_accounts(client, db_session):
    """expand_accounts=true adds per-account breakdown with below_median flag."""
    project = Project(name="pm-expand", user_id="test-user-123")
    db_session.add(project)
    await db_session.flush()

    acct_good_id, acct_bad_id = uuid.uuid4(), uuid.uuid4()
    acct_good, acct_bad = str(acct_good_id), str(acct_bad_id)
    mp_good = MaxPlanAccount(
        id=acct_good_id,
        user_id="test-user-123",
        name="Expand-Good",
        provider=LLMProviderName.ANTHROPIC,
        auth_mode=LLMAuthMode.OAUTH,
    )
    mp_bad = MaxPlanAccount(
        id=acct_bad_id,
        user_id="test-user-123",
        name="Expand-Bad",
        provider=LLMProviderName.ANTHROPIC,
        auth_mode=LLMAuthMode.OAUTH,
    )
    db_session.add_all([mp_good, mp_bad])
    await db_session.flush()

    tasks = [
        Task(
            project_id=project.id,
            provider="anthropic",
            model="sonnet",
            account_id=acct_good,
            status=TaskStatus.COMPLETED,
        )
        for _ in range(5)
    ] + [
        Task(
            project_id=project.id,
            provider="anthropic",
            model="sonnet",
            account_id=acct_bad,
            status=TaskStatus.FAILED,
        )
        for _ in range(5)
    ]
    db_session.add_all(tasks)
    await db_session.commit()

    res = await client.get(
        "/api/admin/max-plan-accounts/provider-model-breakdown?expand_accounts=true"
    )
    assert res.status_code == 200
    row = res.json()["rows"][0]
    assert "accounts" in row
    acct_data = {a["account_id"]: a for a in row["accounts"]}
    assert acct_data[acct_good]["success_rate"] == 1.0
    assert acct_data[acct_bad]["success_rate"] == 0.0
    assert acct_data[acct_bad]["below_median"] is True
    assert acct_data[acct_good]["below_median"] is False


# ── account_model_regression helpers ─────────────────────────────────────────


async def test_provider_model_breakdown_excludes_infra_failures(client, db_session):
    """RATE_LIMIT/PROVISION_FAILURE tasks are excluded from total_tasks and success_count."""
    project = Project(name="infra-excl", user_id="test-user-123")
    db_session.add(project)
    await db_session.flush()

    acct_a_id, acct_b_id = uuid.uuid4(), uuid.uuid4()
    acct_a, acct_b = str(acct_a_id), str(acct_b_id)
    db_session.add_all(
        [
            MaxPlanAccount(
                id=acct_a_id,
                user_id="test-user-123",
                name="Infra-A",
                provider=LLMProviderName.ANTHROPIC,
                auth_mode=LLMAuthMode.OAUTH,
            ),
            MaxPlanAccount(
                id=acct_b_id,
                user_id="test-user-123",
                name="Infra-B",
                provider=LLMProviderName.ANTHROPIC,
                auth_mode=LLMAuthMode.OAUTH,
            ),
        ]
    )
    await db_session.flush()

    # 5 legit completions (should count)
    tasks = (
        [
            Task(
                project_id=project.id,
                provider="anthropic",
                model="claude-3",
                account_id=acct_a,
                status=TaskStatus.COMPLETED,
            )
            for _ in range(3)
        ]
        + [
            Task(
                project_id=project.id,
                provider="anthropic",
                model="claude-3",
                account_id=acct_b,
                status=TaskStatus.COMPLETED,
            )
            for _ in range(2)
        ]
        + [
            # Infra failures — should be excluded
            Task(
                project_id=project.id,
                provider="anthropic",
                model="claude-3",
                account_id=acct_a,
                status=TaskStatus.FAILED,
                context={"failure_category": "RATE_LIMIT"},
            ),
            Task(
                project_id=project.id,
                provider="anthropic",
                model="claude-3",
                account_id=acct_b,
                status=TaskStatus.FAILED,
                context={"failure_category": "PROVISION_FAILURE"},
            ),
            Task(
                project_id=project.id,
                provider="anthropic",
                model="claude-3",
                account_id=acct_a,
                status=TaskStatus.CANCELLED,
                context={"failure_category": "AGENT_TIMEOUT"},
            ),
        ]
    )
    db_session.add_all(tasks)
    await db_session.commit()

    res = await client.get("/api/admin/max-plan-accounts/provider-model-breakdown")
    assert res.status_code == 200
    rows = res.json()["rows"]
    assert len(rows) == 1
    row = rows[0]
    # Only the 5 non-infra tasks should be counted
    assert row["total_tasks"] == 5
    assert row["success_count"] == 5
    assert row["success_rate"] == 1.0


async def test_provider_model_breakdown_code_error_failures_included(
    client, db_session
):
    """CODE_ERROR failures are counted in the denominator (non-infra failures stay in)."""
    project = Project(name="code-err", user_id="test-user-123")
    db_session.add(project)
    await db_session.flush()

    acct_a_id, acct_b_id = uuid.uuid4(), uuid.uuid4()
    acct_a, acct_b = str(acct_a_id), str(acct_b_id)
    db_session.add_all(
        [
            MaxPlanAccount(
                id=acct_a_id,
                user_id="test-user-123",
                name="Code-A",
                provider=LLMProviderName.ANTHROPIC,
                auth_mode=LLMAuthMode.OAUTH,
            ),
            MaxPlanAccount(
                id=acct_b_id,
                user_id="test-user-123",
                name="Code-B",
                provider=LLMProviderName.ANTHROPIC,
                auth_mode=LLMAuthMode.OAUTH,
            ),
        ]
    )
    await db_session.flush()

    tasks = (
        [
            Task(
                project_id=project.id,
                provider="anthropic",
                model="sonnet",
                account_id=acct_a,
                status=TaskStatus.COMPLETED,
            )
            for _ in range(3)
        ]
        + [
            Task(
                project_id=project.id,
                provider="anthropic",
                model="sonnet",
                account_id=acct_b,
                status=TaskStatus.COMPLETED,
            )
            for _ in range(2)
        ]
        + [
            # CODE_ERROR — not infra, should remain in denominator
            Task(
                project_id=project.id,
                provider="anthropic",
                model="sonnet",
                account_id=acct_a,
                status=TaskStatus.FAILED,
                context={"failure_category": "CODE_ERROR"},
            ),
        ]
    )
    db_session.add_all(tasks)
    await db_session.commit()

    res = await client.get("/api/admin/max-plan-accounts/provider-model-breakdown")
    assert res.status_code == 200
    row = res.json()["rows"][0]
    assert row["total_tasks"] == 6  # CODE_ERROR still counted
    assert row["success_count"] == 5


async def test_find_regressions_skips_single_account_groups(db_session):
    """find_account_model_regressions returns nothing for a single-account (provider, model) group."""
    from app.account_model_regression import find_account_model_regressions

    project = Project(name="single-acct", user_id="test-user-123")
    db_session.add(project)
    await db_session.flush()

    acct_id = uuid.uuid4()
    acct = str(acct_id)
    db_session.add(
        MaxPlanAccount(
            id=acct_id,
            user_id="test-user-123",
            name="Solo",
            provider=LLMProviderName.ANTHROPIC,
            auth_mode=LLMAuthMode.OAUTH,
        )
    )
    await db_session.flush()

    # 5 failed tasks — would look like a regression, but only one account
    tasks = [
        Task(
            project_id=project.id,
            provider="anthropic",
            model="opus",
            account_id=acct,
            status=TaskStatus.FAILED,
        )
        for _ in range(5)
    ]
    db_session.add_all(tasks)
    await db_session.commit()

    candidates = await find_account_model_regressions(db_session, [acct])
    assert candidates == []


async def test_find_regressions_requires_both_thresholds(db_session):
    """Regression fires only when BOTH relative (< 80%) AND absolute (>= 0.15) gaps are met."""
    from app.account_model_regression import find_account_model_regressions

    project = Project(name="threshold-test", user_id="test-user-123")
    db_session.add(project)
    await db_session.flush()

    # Two accounts; acct_good has very high rate (~0.90), acct_bad has rate 0.75.
    # Relative gap: 0.75 < 0.90 * 0.8 = 0.72 → NOT met (0.75 > 0.72).
    # So no regression should fire.
    acct_good_id, acct_bad_id = uuid.uuid4(), uuid.uuid4()
    acct_good, acct_bad = str(acct_good_id), str(acct_bad_id)
    db_session.add_all(
        [
            MaxPlanAccount(
                id=acct_good_id,
                user_id="test-user-123",
                name="TGood",
                provider=LLMProviderName.ANTHROPIC,
                auth_mode=LLMAuthMode.OAUTH,
            ),
            MaxPlanAccount(
                id=acct_bad_id,
                user_id="test-user-123",
                name="TBad",
                provider=LLMProviderName.ANTHROPIC,
                auth_mode=LLMAuthMode.OAUTH,
            ),
        ]
    )
    await db_session.flush()

    # acct_good: 9 success, 1 fail → rate 0.9
    # acct_bad:  3 success, 1 fail → rate 0.75; median of [0.75, 0.9] = 0.825
    # relative gap: 0.75 < 0.825 * 0.8 = 0.66 → NOT met
    tasks = [
        Task(
            project_id=project.id,
            provider="anthropic",
            model="haiku",
            account_id=acct_good,
            status=TaskStatus.COMPLETED,
        )
        for _ in range(9)
    ] + [
        Task(
            project_id=project.id,
            provider="anthropic",
            model="haiku",
            account_id=acct_good,
            status=TaskStatus.FAILED,
        ),
        Task(
            project_id=project.id,
            provider="anthropic",
            model="haiku",
            account_id=acct_bad,
            status=TaskStatus.COMPLETED,
        ),
        Task(
            project_id=project.id,
            provider="anthropic",
            model="haiku",
            account_id=acct_bad,
            status=TaskStatus.COMPLETED,
        ),
        Task(
            project_id=project.id,
            provider="anthropic",
            model="haiku",
            account_id=acct_bad,
            status=TaskStatus.COMPLETED,
        ),
        Task(
            project_id=project.id,
            provider="anthropic",
            model="haiku",
            account_id=acct_bad,
            status=TaskStatus.FAILED,
        ),
    ]
    db_session.add_all(tasks)
    await db_session.commit()

    candidates = await find_account_model_regressions(db_session, [acct_good, acct_bad])
    assert candidates == []


async def test_find_regressions_requires_three_distinct_accounts(db_session):
    """Regression must NOT fire with only 2 distinct accounts (degenerate median-of-two).

    This is the documented HenryHamster false-positive class: with 2 accounts the
    'median' is the midpoint of two rates, making the weaker account systematically
    eligible for a 6h quarantine on a chance run of failures.
    """
    from app.account_model_regression import find_account_model_regressions

    project = Project(name="regression-two-acct", user_id="test-user-123")
    db_session.add(project)
    await db_session.flush()

    acct_good_id, acct_bad_id = uuid.uuid4(), uuid.uuid4()
    acct_good, acct_bad = str(acct_good_id), str(acct_bad_id)
    db_session.add_all(
        [
            MaxPlanAccount(
                id=acct_good_id,
                user_id="test-user-123",
                name="RGood2",
                provider=LLMProviderName.ANTHROPIC,
                auth_mode=LLMAuthMode.OAUTH,
            ),
            MaxPlanAccount(
                id=acct_bad_id,
                user_id="test-user-123",
                name="RBad2",
                provider=LLMProviderName.ANTHROPIC,
                auth_mode=LLMAuthMode.OAUTH,
            ),
        ]
    )
    await db_session.flush()

    # acct_good: 10 success → rate 1.0
    # acct_bad:  0 success, 10 fail → rate 0.0
    # Old code: would fire (median=0.5, gap=0.5 ≥ 0.15, rate < 0.4)
    # New code: must NOT fire because distinct_accounts == 2 < 3 (_DISTINCT_ACCOUNTS_MIN)
    tasks = [
        Task(
            project_id=project.id,
            provider="anthropic",
            model="claude-r2",
            account_id=acct_good,
            status=TaskStatus.COMPLETED,
        )
        for _ in range(10)
    ] + [
        Task(
            project_id=project.id,
            provider="anthropic",
            model="claude-r2",
            account_id=acct_bad,
            status=TaskStatus.FAILED,
        )
        for _ in range(10)
    ]
    db_session.add_all(tasks)
    await db_session.commit()

    candidates = await find_account_model_regressions(db_session, [acct_good, acct_bad])
    assert candidates == [], (
        "Must not quarantine with only 2 accounts — the comparator is the degenerate "
        "average-of-two which has no statistical significance"
    )


async def test_find_regressions_returns_below_median_accounts(db_session):
    """A three-account group where one account clearly underperforms returns a candidate."""
    from app.account_model_regression import find_account_model_regressions

    project = Project(name="regression-hit", user_id="test-user-123")
    db_session.add(project)
    await db_session.flush()

    acct_good1_id, acct_good2_id, acct_bad_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    acct_good1 = str(acct_good1_id)
    acct_good2 = str(acct_good2_id)
    acct_bad = str(acct_bad_id)
    db_session.add_all(
        [
            MaxPlanAccount(
                id=acct_good1_id,
                user_id="test-user-123",
                name="RGood1",
                provider=LLMProviderName.ANTHROPIC,
                auth_mode=LLMAuthMode.OAUTH,
            ),
            MaxPlanAccount(
                id=acct_good2_id,
                user_id="test-user-123",
                name="RGood2b",
                provider=LLMProviderName.ANTHROPIC,
                auth_mode=LLMAuthMode.OAUTH,
            ),
            MaxPlanAccount(
                id=acct_bad_id,
                user_id="test-user-123",
                name="RBad",
                provider=LLMProviderName.ANTHROPIC,
                auth_mode=LLMAuthMode.OAUTH,
            ),
        ]
    )
    await db_session.flush()

    # acct_good1: 10 success → rate 1.0
    # acct_good2: 10 success → rate 1.0
    # acct_bad:   0 success, 10 fail → rate 0.0
    # pool (excl. acct_bad): 20/20 = 1.0; Wilson lower ~0.83
    # acct_bad Wilson upper ~0.31; 0.31 < 0.83 → statistically significant → fires
    all_account_ids = [acct_good1, acct_good2, acct_bad]
    tasks = (
        [
            Task(
                project_id=project.id,
                provider="anthropic",
                model="claude-r",
                account_id=acct_good1,
                status=TaskStatus.COMPLETED,
            )
            for _ in range(10)
        ]
        + [
            Task(
                project_id=project.id,
                provider="anthropic",
                model="claude-r",
                account_id=acct_good2,
                status=TaskStatus.COMPLETED,
            )
            for _ in range(10)
        ]
        + [
            Task(
                project_id=project.id,
                provider="anthropic",
                model="claude-r",
                account_id=acct_bad,
                status=TaskStatus.FAILED,
            )
            for _ in range(10)
        ]
    )
    db_session.add_all(tasks)
    await db_session.commit()

    candidates = await find_account_model_regressions(db_session, all_account_ids)
    assert len(candidates) == 1
    c = candidates[0]
    assert c.account_id == acct_bad
    assert c.provider == "anthropic"
    assert c.model == "claude-r"
    assert c.account_rate == 0.0
    assert c.sample_size == 10
    assert c.distinct_accounts == 3


async def test_provider_model_breakdown_quarantine_status(client, db_session):
    """quarantine_status is populated when rate_limit_state has a regression entry."""
    project = Project(name="qs-test", user_id="test-user-123")
    db_session.add(project)
    await db_session.flush()

    acct_good_id, acct_bad_id = uuid.uuid4(), uuid.uuid4()
    acct_good, acct_bad = str(acct_good_id), str(acct_bad_id)

    quarantine_entry = {
        "disabled_models": {
            "claude-q": {
                "kind": "regression",
                "at": "2026-04-28T12:00:00+00:00",
                "account_rate": 0.1,
                "project_median": 0.9,
                "sample_size": 7,
            }
        }
    }
    db_session.add_all(
        [
            MaxPlanAccount(
                id=acct_good_id,
                user_id="test-user-123",
                name="QGood",
                provider=LLMProviderName.ANTHROPIC,
                auth_mode=LLMAuthMode.OAUTH,
            ),
            MaxPlanAccount(
                id=acct_bad_id,
                user_id="test-user-123",
                name="QBad",
                provider=LLMProviderName.ANTHROPIC,
                auth_mode=LLMAuthMode.OAUTH,
                rate_limit_state=quarantine_entry,
            ),
        ]
    )
    await db_session.flush()

    tasks = [
        Task(
            project_id=project.id,
            provider="anthropic",
            model="claude-q",
            account_id=acct_good,
            status=TaskStatus.COMPLETED,
        )
        for _ in range(5)
    ] + [
        Task(
            project_id=project.id,
            provider="anthropic",
            model="claude-q",
            account_id=acct_bad,
            status=TaskStatus.FAILED,
        )
        for _ in range(5)
    ]
    db_session.add_all(tasks)
    await db_session.commit()

    res = await client.get(
        "/api/admin/max-plan-accounts/provider-model-breakdown?expand_accounts=true"
    )
    assert res.status_code == 200
    row = res.json()["rows"][0]
    acct_data = {a["account_id"]: a for a in row["accounts"]}

    qs = acct_data[acct_bad]["quarantine_status"]
    assert qs is not None
    assert qs["kind"] == "regression"
    assert qs["started_at"] == "2026-04-28T12:00:00+00:00"
    assert qs["account_rate"] == pytest.approx(0.1)
    assert qs["project_median"] == pytest.approx(0.9)
    assert qs["sample_size"] == 7

    # Good account has no quarantine entry
    assert acct_data[acct_good]["quarantine_status"] is None


# ── auto-quarantine + recovery endpoint integration ──────────────────────────


async def test_find_regressions_excludes_account_with_only_infra_failures(db_session):
    """An account whose only failures are infra (RATE_LIMIT, PROVISION_FAILURE,
    AGENT_TIMEOUT, etc.) must NOT be flagged as a regression — those failures
    reflect infrastructure issues, not account/model quality."""
    from app.account_model_regression import find_account_model_regressions

    project = Project(name="infra-only", user_id="test-user-123")
    db_session.add(project)
    await db_session.flush()

    acct_good_id, acct_bad_id = uuid.uuid4(), uuid.uuid4()
    acct_good, acct_bad = str(acct_good_id), str(acct_bad_id)
    db_session.add_all(
        [
            MaxPlanAccount(
                id=acct_good_id,
                user_id="test-user-123",
                name="IGood",
                provider=LLMProviderName.ANTHROPIC,
                auth_mode=LLMAuthMode.OAUTH,
            ),
            MaxPlanAccount(
                id=acct_bad_id,
                user_id="test-user-123",
                name="IBad",
                provider=LLMProviderName.ANTHROPIC,
                auth_mode=LLMAuthMode.OAUTH,
            ),
        ]
    )
    await db_session.flush()

    # acct_good: 5 successes (rate 1.0)
    # acct_bad:  5 RATE_LIMIT failures — infra-only, must be filtered out so
    #            the account drops below the 3-task floor.
    tasks = [
        Task(
            project_id=project.id,
            provider="anthropic",
            model="claude-i",
            account_id=acct_good,
            status=TaskStatus.COMPLETED,
        )
        for _ in range(5)
    ] + [
        Task(
            project_id=project.id,
            provider="anthropic",
            model="claude-i",
            account_id=acct_bad,
            status=TaskStatus.FAILED,
            context={"failure_category": "RATE_LIMIT"},
        )
        for _ in range(5)
    ]
    db_session.add_all(tasks)
    await db_session.commit()

    candidates = await find_account_model_regressions(db_session, [acct_good, acct_bad])
    # acct_bad's infra failures are stripped, leaving 0 samples, which is
    # below the per-(account, provider, model) min-sample threshold (3).
    # No regression should fire.
    assert candidates == []


async def test_clear_regression_endpoint_clears_only_regression_kind(
    client, db_session
):
    """POST /max-plan-accounts/{id}/regression-quarantine/clear removes a
    regression entry but refuses to touch a rate_limit entry."""
    from app.max_plan_pool import _account_by_id, _max_plan_pool_lock

    acct_id = uuid.uuid4()
    db_session.add(
        MaxPlanAccount(
            id=acct_id,
            user_id="test-user-123",
            name="ClearMe",
            provider=LLMProviderName.ANTHROPIC,
            auth_mode=LLMAuthMode.OAUTH,
        )
    )
    await db_session.commit()

    # Seed in-memory regression entry directly.
    async with _max_plan_pool_lock:
        _account_by_id[str(acct_id)] = {
            "id": str(acct_id),
            "name": "ClearMe",
            "enabled": True,
            "active_tasks": 0,
            "user_id": "test-user-123",
            "provider": "anthropic",
            "disabled_models": {
                "claude-q": {
                    "kind": "regression",
                        "at": _admin_pool_accounts._utcnow(),
                    "hits": 1,
                    "provider": "anthropic",
                    "regression_stats": {
                        "account_rate": 0.1,
                        "project_median": 0.9,
                        "sample_size": 5,
                        "distinct_accounts": 2,
                        "started_at": "2026-04-29T00:00:00+00:00",
                    },
                },
                # And a rate_limit entry on a different model — must be untouched.
                "other-model": {
                    "kind": None,
                        "at": _admin_pool_accounts._utcnow(),
                    "hits": 1,
                },
            },
        }
    try:
        from unittest.mock import AsyncMock as _AM
        from unittest.mock import patch as _P

        with _P(
            "app.max_plan_pool._persist_rate_limit_state", new=_AM(return_value=None)
        ):
            res = await client.post(
                f"/api/admin/max-plan-accounts/{acct_id}/regression-quarantine/clear"
                "?provider=anthropic&model=claude-q"
            )
        assert res.status_code == 200, res.text
        body = res.json()
        assert body["cleared"] is True
        assert body["model"] == "claude-q"

        # claude-q is gone, other-model still present.
        dm = _account_by_id[str(acct_id)]["disabled_models"]
        assert "claude-q" not in dm
        assert "other-model" in dm

        # A second clear of the same model must 404 (no regression entry left).
        with _P(
            "app.max_plan_pool._persist_rate_limit_state", new=_AM(return_value=None)
        ):
            res = await client.post(
                f"/api/admin/max-plan-accounts/{acct_id}/regression-quarantine/clear"
                "?provider=anthropic&model=claude-q"
            )
        assert res.status_code == 404

        # And clearing the non-regression "other-model" must also 404.
        with _P(
            "app.max_plan_pool._persist_rate_limit_state", new=_AM(return_value=None)
        ):
            res = await client.post(
                f"/api/admin/max-plan-accounts/{acct_id}/regression-quarantine/clear"
                "?provider=anthropic&model=other-model"
            )
        assert res.status_code == 404
        # other-model entry must still be present.
        assert "other-model" in _account_by_id[str(acct_id)]["disabled_models"]
    finally:
        async with _max_plan_pool_lock:
            _account_by_id.pop(str(acct_id), None)


async def test_clear_regression_endpoint_rejects_other_users_accounts(
    client, db_session
):
    """An account owned by another user must not be clearable."""
    from app.max_plan_pool import _account_by_id, _max_plan_pool_lock

    acct_id = uuid.uuid4()
    db_session.add(
        MaxPlanAccount(
            id=acct_id,
            user_id="some-other-user",
            name="NotMine",
            provider=LLMProviderName.ANTHROPIC,
            auth_mode=LLMAuthMode.OAUTH,
        )
    )
    await db_session.commit()

    async with _max_plan_pool_lock:
        _account_by_id[str(acct_id)] = {
            "id": str(acct_id),
            "name": "NotMine",
            "enabled": True,
            "active_tasks": 0,
            "user_id": "some-other-user",
            "provider": "anthropic",
            "disabled_models": {
                    "claude-q": {
                        "kind": "regression",
                        "at": _admin_pool_accounts._utcnow(),
                        "hits": 1,
                    }
            },
        }
    try:
        res = await client.post(
            f"/api/admin/max-plan-accounts/{acct_id}/regression-quarantine/clear"
            "?provider=anthropic&model=claude-q"
        )
        assert res.status_code == 404
        # Entry untouched.
        assert "claude-q" in _account_by_id[str(acct_id)]["disabled_models"]
    finally:
        async with _max_plan_pool_lock:
            _account_by_id.pop(str(acct_id), None)


async def test_scan_account_model_regressions_dedupes_notifications(db_session):
    """Calling the scanner twice in a row must only produce one fire event for
    the same (account, provider, model) — dedupe holds across ticks while the
    cooldown is active."""
    from app.max_plan_pool import (
        _account_by_id,
        _max_plan_pool_lock,
        scan_account_model_regressions,
    )

    project = Project(name="dedupe-test", user_id="test-user-123")
    db_session.add(project)
    await db_session.flush()

    # 3 distinct accounts (>= _DISTINCT_ACCOUNTS_MIN) with a genuinely
    # significant regression so the statistical gate fires: two good accounts
    # at 10/10 and one bad account at 0/10. acct_bad's Wilson upper bound
    # (~0.21) is well below the pool's Wilson lower bound (~0.88), and 10 tasks
    # clears _ACCOUNT_MIN_SAMPLE (10). This isolates the NOTIFICATION DEDUP
    # behaviour: the same regression must fire exactly once across two ticks.
    acct_good1_id, acct_good2_id, acct_bad_id = (
        uuid.uuid4(),
        uuid.uuid4(),
        uuid.uuid4(),
    )
    acct_good1, acct_good2, acct_bad = (
        str(acct_good1_id),
        str(acct_good2_id),
        str(acct_bad_id),
    )
    db_session.add_all(
        [
            MaxPlanAccount(
                id=acct_good1_id,
                user_id="test-user-123",
                name="DGood1",
                provider=LLMProviderName.ANTHROPIC,
                auth_mode=LLMAuthMode.OAUTH,
            ),
            MaxPlanAccount(
                id=acct_good2_id,
                user_id="test-user-123",
                name="DGood2",
                provider=LLMProviderName.ANTHROPIC,
                auth_mode=LLMAuthMode.OAUTH,
            ),
            MaxPlanAccount(
                id=acct_bad_id,
                user_id="test-user-123",
                name="DBad",
                provider=LLMProviderName.ANTHROPIC,
                auth_mode=LLMAuthMode.OAUTH,
            ),
        ]
    )
    await db_session.flush()

    # 10 success on each good account, 10 fail on bad → bad regresses.
    tasks = (
        [
            Task(
                project_id=project.id,
                provider="anthropic",
                model="claude-d",
                account_id=acct_good1,
                status=TaskStatus.COMPLETED,
            )
            for _ in range(10)
        ]
        + [
            Task(
                project_id=project.id,
                provider="anthropic",
                model="claude-d",
                account_id=acct_good2,
                status=TaskStatus.COMPLETED,
            )
            for _ in range(10)
        ]
        + [
            Task(
                project_id=project.id,
                provider="anthropic",
                model="claude-d",
                account_id=acct_bad,
                status=TaskStatus.FAILED,
            )
            for _ in range(10)
        ]
    )
    db_session.add_all(tasks)
    await db_session.commit()

    # Inject in-memory pool entries so the scanner sees the accounts.
    async with _max_plan_pool_lock:
        _account_by_id[acct_good1] = {
            "id": acct_good1,
            "name": "DGood1",
            "enabled": True,
            "active_tasks": 0,
            "user_id": "test-user-123",
            "provider": "anthropic",
        }
        _account_by_id[acct_good2] = {
            "id": acct_good2,
            "name": "DGood2",
            "enabled": True,
            "active_tasks": 0,
            "user_id": "test-user-123",
            "provider": "anthropic",
        }
        _account_by_id[acct_bad] = {
            "id": acct_bad,
            "name": "DBad",
            "enabled": True,
            "active_tasks": 0,
            "user_id": "test-user-123",
            "provider": "anthropic",
        }

    try:
        from unittest.mock import AsyncMock as _AM
        from unittest.mock import patch as _P

        with _P(
            "app.max_plan_pool._persist_rate_limit_state", new=_AM(return_value=None)
        ):
            fired_first = await scan_account_model_regressions(db_session)
            fired_second = await scan_account_model_regressions(db_session)
        assert len(fired_first) == 1
        ev = fired_first[0]
        assert ev["account_id"] == acct_bad
        assert ev["provider"] == "anthropic"
        assert ev["model"] == "claude-d"
        # Notification payload must include the numeric stats.
        assert ev["account_rate"] == 0.0
        # median of per-account rates [0.0, 1.0, 1.0] = 1.0
        assert ev["project_median"] == 1.0
        assert ev["sample_size"] == 10
        # Second tick must dedupe — same regression, no new fire event.
        assert fired_second == []
    finally:
        async with _max_plan_pool_lock:
            _account_by_id.pop(acct_good1, None)
            _account_by_id.pop(acct_good2, None)
            _account_by_id.pop(acct_bad, None)


# ── Secret Manager failure: hard 503, no plaintext stored ─────────────────────


async def test_add_max_plan_account_secret_manager_failure_returns_503(
    client, db_session, mocker
):
    """When store_user_secret raises, add endpoint returns 503 and leaves no account row."""
    mocker.patch(
        "app.secrets_manager.store_user_secret",
        new=AsyncMock(side_effect=RuntimeError("GCP unavailable")),
    )

    res = await client.post(
        "/api/admin/max-plan-accounts",
        json={"name": "SM Failure Test", "oauth_token": "sk-ant-oat01-valid_token"},
    )

    assert res.status_code == 503
    assert "Secret storage temporarily unavailable" in res.text

    # No account row must remain — the orphaned row was deleted before raising.
    remaining = (
        (
            await db_session.execute(
                select(MaxPlanAccount).where(MaxPlanAccount.name == "SM Failure Test")
            )
        )
        .scalars()
        .all()
    )
    assert remaining == [], (
        "Orphaned MaxPlanAccount row must not persist after SM failure"
    )

    # None of the surviving rows may carry a plaintext secret (decrypt to check SM path).
    from app.field_encryption import decrypt_field as _df
    all_accounts = (await db_session.execute(select(MaxPlanAccount))).scalars().all()
    for acct in all_accounts:
        ref = _df(acct.secret_blob_ref)
        assert not (
            ref and not ref.startswith("projects/")
        ), (
            f"Account {acct.id} has a plaintext secret_blob_ref: {acct.secret_blob_ref!r}"
        )


async def test_patch_max_plan_account_secret_manager_failure_returns_503(
    client, db_session, mocker
):
    """When store_user_secret raises on PATCH, update returns 503 and existing secret is unchanged."""
    mocker.patch(
        "app.secrets_manager.store_user_secret",
        new=AsyncMock(side_effect=RuntimeError("GCP unavailable")),
    )

    acct = MaxPlanAccount(
        user_id="test-user-123",
        name="Pre-existing Account",
        provider=LLMProviderName.ANTHROPIC,
        auth_mode=LLMAuthMode.OAUTH,
        oauth_token="projects/test/secrets/existing/versions/1",
        secret_blob_ref="projects/test/secrets/existing/versions/1",
    )
    db_session.add(acct)
    await db_session.commit()

    res = await client.patch(
        f"/api/admin/max-plan-accounts/{acct.id}",
        json={"secret": "sk-ant-oat01-new_token"},
    )

    assert res.status_code == 503
    assert "Secret storage temporarily unavailable" in res.text

    # Existing secret_blob_ref must be unchanged — no plaintext stored.
    await db_session.refresh(acct)
    assert acct.secret_blob_ref == "projects/test/secrets/existing/versions/1", (
        f"secret_blob_ref must not be overwritten with plaintext on SM failure: {acct.secret_blob_ref!r}"
    )


# ── project_id scoping on provider-model breakdown ───────────────────────────


async def test_provider_model_breakdown_honors_project_id(client, db_session):
    """When project_id is supplied, only tasks from that project contribute to the breakdown."""
    project_a = Project(name="pmb-proj-a", user_id="test-user-123")
    project_b = Project(name="pmb-proj-b", user_id="test-user-123")
    db_session.add_all([project_a, project_b])
    await db_session.flush()

    # The test user must be a ProjectMember of project_a to pass the auth check.
    db_session.add(
        ProjectMember(project_id=project_a.id, user_id="test-user-123", role="owner")
    )
    await db_session.flush()

    acct_id = uuid.uuid4()
    acct_str = str(acct_id)
    db_session.add(
        MaxPlanAccount(
            id=acct_id,
            user_id="test-user-123",
            name="PMB-Acct",
            provider=LLMProviderName.ANTHROPIC,
            auth_mode=LLMAuthMode.OAUTH,
        )
    )
    await db_session.flush()

    # 7 completed tasks in project_a with provider/model "anthropic/claude-scope"
    tasks_a = [
        Task(
            project_id=project_a.id,
            provider="anthropic",
            model="claude-scope",
            account_id=acct_str,
            status=TaskStatus.COMPLETED,
        )
        for _ in range(7)
    ]
    # 8 completed tasks in project_b with the SAME provider/model
    tasks_b = [
        Task(
            project_id=project_b.id,
            provider="anthropic",
            model="claude-scope",
            account_id=acct_str,
            status=TaskStatus.COMPLETED,
        )
        for _ in range(8)
    ]
    db_session.add_all(tasks_a + tasks_b)
    await db_session.commit()

    # Without project_id: all 15 tasks are included.
    res_all = await client.get("/api/admin/max-plan-accounts/provider-model-breakdown")
    assert res_all.status_code == 200
    rows_all = res_all.json()["rows"]
    assert len(rows_all) == 1
    assert rows_all[0]["total_tasks"] == 15

    # With project_a: only the 7 tasks from project_a are counted.
    res_scoped = await client.get(
        f"/api/admin/max-plan-accounts/provider-model-breakdown?project_id={project_a.id}"
    )
    assert res_scoped.status_code == 200
    rows_scoped = res_scoped.json()["rows"]
    assert len(rows_scoped) == 1
    assert rows_scoped[0]["total_tasks"] == 7, (
        "project_id filter must restrict breakdown to tasks in the specified project"
    )


async def test_provider_model_breakdown_project_id_rejects_non_member(
    client, db_session
):
    """A project the caller is NOT a member of must return 404."""
    # Create a project owned by a different user; test-user-123 has no membership.
    other_project = Project(name="pmb-other", user_id="other-user-999")
    db_session.add(other_project)
    await db_session.commit()

    res = await client.get(
        f"/api/admin/max-plan-accounts/provider-model-breakdown?project_id={other_project.id}"
    )
    assert res.status_code == 404


# ── response-level served-stale freshness annotation ────────────────────────


async def test_provider_model_breakdown_serve_stale_annotates_response(
    client, db_session, mocker
):
    """When the live compute fails and the cache serves the last-good payload,
    the breakdown response must carry served_stale=True plus a large
    precompute_age_seconds derived from the payload's own generated_at — so a
    hours-old fallback can never be shown as a live reading. A fresh compute
    reports served_stale=False.

    Mirrors the short-TTL + compute-fails-after-one-success pattern from
    test_admin_endpoint_cache.py.
    """
    # An account must exist for test-user-123 so the endpoint reaches the cache
    # path rather than short-circuiting on the empty-accounts branch.
    acct = MaxPlanAccount(
        id=uuid.uuid4(),
        user_id="test-user-123",
        name="stale-test",
        provider=LLMProviderName.ANTHROPIC,
        auth_mode=LLMAuthMode.OAUTH,
    )
    db_session.add(acct)
    await db_session.commit()

    short_cache = DigestCache(default_ttl=0.05, stale_retry_ttl=60)
    app.dependency_overrides[get_digest_cache] = lambda: short_cache

    old_iso = (_admin_pool_accounts._utcnow() - timedelta(hours=2)).isoformat()
    good: Dict[str, Any] = {
        "rows": [
            {
                "provider": "anthropic",
                "model": "claude-3-5",
                "total_tasks": 7,
                "success_count": 6,
                "success_rate": 6 / 7,
                "distinct_accounts": 1,
                "project_median_success_rate": 6 / 7,
            }
        ],
        "generated_at": old_iso,
    }
    spy = AsyncMock(side_effect=[good, RuntimeError("statement timeout")])
    mocker.patch.object(_admin_pool_accounts, "_compute_provider_model_breakdown", spy)

    try:
        r1 = await client.get("/api/admin/max-plan-accounts/provider-model-breakdown")
        await asyncio.sleep(0.1)  # expire the fresh entry → next poll recomputes
        r2 = await client.get("/api/admin/max-plan-accounts/provider-model-breakdown")
    finally:
        app.dependency_overrides.pop(get_digest_cache, None)

    # Fresh compute: live verdict.
    assert r1.status_code == 200
    b1 = r1.json()
    assert b1["rows"][0]["model"] == "claude-3-5"
    assert b1["served_stale"] is False
    assert b1["precomputed"] is False

    # Served-stale fallback: same last-good rows, but explicitly marked not-live.
    assert r2.status_code == 200
    b2 = r2.json()
    assert b2["rows"][0]["model"] == "claude-3-5"  # last-good payload preserved
    assert b2["served_stale"] is True
    assert b2["precompute_age_seconds"] > 120  # derived from 2h-old generated_at
    assert spy.call_count == 2


async def test_account_health_summary_annotates_fresh_freshness(
    client, db_session, mocker
):
    """The health endpoint stamps a truthful live-freshness signal on a fresh
    compute: served_stale=False, precomputed=False, and a generated_at the
    panel can render an as-of label from."""
    short_cache = DigestCache(default_ttl=60.0)
    app.dependency_overrides[get_digest_cache] = lambda: short_cache

    generated = _admin_pool_accounts._utcnow().isoformat()
    payload: Dict[str, Any] = {"accounts": [], "generated_at": generated}
    spy = AsyncMock(return_value=payload)
    mocker.patch.object(_admin_pool_accounts, "_compute_account_health_summary", spy)

    try:
        res = await client.get("/api/admin/max-plan-accounts/health")
    finally:
        app.dependency_overrides.pop(get_digest_cache, None)

    assert res.status_code == 200
    body = res.json()
    assert body["accounts"] == []
    assert body["served_stale"] is False
    assert body["precomputed"] is False
    assert body["generated_at"] == generated
    assert body["precompute_age_seconds"] is not None
    assert body["precompute_age_seconds"] < 60


async def test_provider_model_breakdown_empty_accounts_carries_freshness(
    client, db_session
):
    """The no-accounts early-return path is annotated consistently so the
    response still carries served_stale / generated_at (never a bare rows:[])."""
    res = await client.get("/api/admin/max-plan-accounts/provider-model-breakdown")
    assert res.status_code == 200
    body = res.json()
    assert body["rows"] == []
    assert body["served_stale"] is False
    assert "generated_at" in body
    assert "precompute_age_seconds" in body


# ── reset-rate-limits ──────────────────────────────────────────────────────


async def test_reset_rate_limits_clears_disabled_at_column(client, db_session):
    """reset-rate-limits must clear disabled_at in the DB so a subsequent manual
    disable is not silently undone by the auto-recovery sweep."""
    past = _admin_pool_accounts._utcnow() - timedelta(hours=2)
    acct = MaxPlanAccount(
        user_id="test-user-123",
        name="Rate Limited Account",
        provider=LLMProviderName.ANTHROPIC,
        auth_mode=LLMAuthMode.OAUTH,
        oauth_token="projects/test/secrets/rl/versions/1",
        secret_blob_ref="projects/test/secrets/rl/versions/1",
        enabled=False,
        disabled_at=past,
        disabled_reason="auto-disabled after 3 rate-limit hit(s)",
        rate_limit_state={"disabled_models": ["claude-opus-4-8"]},
    )
    db_session.add(acct)
    await db_session.commit()

    res = await client.post(f"/api/admin/max-plan-accounts/{acct.id}/reset-rate-limits")
    assert res.status_code == 200, res.text
    assert "Blueprint cooldown state cleared" in res.json()["message"]
    assert res.json()["provider_quota_changed"] is False
    assert res.json()["provider_reset_credit_consumed"] is False
    assert res.json()["account_reenabled"] is True

    await db_session.refresh(acct)
    assert acct.disabled_at is None
    assert acct.enabled is True
    assert acct.disabled_reason is None


async def test_reset_rate_limits_preserves_auth_disable(client, db_session):
    acct = MaxPlanAccount(
        user_id="test-user-123",
        name="Auth Disabled Account",
        provider=LLMProviderName.OPENAI,
        auth_mode=LLMAuthMode.OAUTH,
        oauth_token="projects/test/secrets/openai/versions/1",
        secret_blob_ref="projects/test/secrets/openai/versions/1",
        enabled=False,
        disabled_at=_admin_pool_accounts._utcnow() - timedelta(minutes=10),
        disabled_reason="OAuth refresh token revoked",
        rate_limit_state={"disabled_models": {"gpt-5.6-sol": {"hits": 1}}},
    )
    db_session.add(acct)
    await db_session.commit()

    res = await client.post(f"/api/admin/max-plan-accounts/{acct.id}/reset-rate-limits")

    assert res.status_code == 200, res.text
    assert res.json()["account_reenabled"] is False
    await db_session.refresh(acct)
    # Clearing cooldowns advances a durable generation tombstone so a stale
    # cross-pod write cannot resurrect pre-click rate-limit state.
    assert acct.rate_limit_state is not None
    assert acct.rate_limit_state.get("cooldown_reset_at")
    assert "disabled_models" not in acct.rate_limit_state
    assert acct.enabled is False
    assert acct.disabled_reason == "OAuth refresh token revoked"
    assert acct.disabled_at is not None


@pytest.mark.anyio
async def test_claude_config_json_encrypted_at_rest_on_create(
    client, db_session, mocker
):
    """claude_config_json is encrypted in DB and omitted from the API response."""
    from cryptography.fernet import Fernet
    from app.field_encryption import decrypt_field

    key = Fernet.generate_key().decode()
    mocker.patch.dict("os.environ", {"GITHUB_PAT_ENCRYPTION_KEY": key})
    mocker.patch(
        "app.secrets_manager.store_user_secret",
        new=AsyncMock(return_value="projects/test/secrets/acct/versions/1"),
    )

    plaintext_config = '{"onboardingAccepted":true}'
    res = await client.post(
        "/api/admin/max-plan-accounts",
        json={
            "name": "Anthropic OAuth Encrypted",
            "provider": "anthropic",
            "auth_mode": "oauth",
            "secret": "sk-ant-oat01-valid",
            "claude_config_json": plaintext_config,
        },
    )
    assert res.status_code == 200, res.text
    body = res.json()
    # Credential material must not be echoed back by the create endpoint.
    assert "claude_config_json" not in body
    assert plaintext_config not in res.text

    # DB row stores the encrypted value (not the original plaintext)
    from app.models import MaxPlanAccount
    from sqlmodel import select

    acct = await db_session.scalar(select(MaxPlanAccount))
    assert acct is not None
    assert acct.claude_config_json != plaintext_config
    assert decrypt_field(acct.claude_config_json) == plaintext_config


@pytest.mark.anyio
async def test_claude_config_json_encrypted_at_rest_on_update(
    client, db_session, mocker
):
    """Updating claude_config_json via PATCH stores the value encrypted in DB."""
    from cryptography.fernet import Fernet
    from app.field_encryption import decrypt_field

    key = Fernet.generate_key().decode()
    mocker.patch.dict("os.environ", {"GITHUB_PAT_ENCRYPTION_KEY": key})
    mocker.patch(
        "app.secrets_manager.store_user_secret",
        new=AsyncMock(return_value="projects/test/secrets/acct/versions/1"),
    )

    acct = MaxPlanAccount(
        user_id="test-user-123",
        name="Anthropic OAuth Update Test",
        provider=LLMProviderName.ANTHROPIC,
        auth_mode=LLMAuthMode.OAUTH,
        oauth_token="projects/test/secrets/acct/versions/1",
        secret_blob_ref="projects/test/secrets/acct/versions/1",
    )
    db_session.add(acct)
    await db_session.commit()

    new_config = '{"sessionToken":"secret-session-abc"}'
    res = await client.patch(
        f"/api/admin/max-plan-accounts/{acct.id}",
        json={"claude_config_json": new_config},
    )
    assert res.status_code == 200, res.text

    await db_session.refresh(acct)
    # DB value is encrypted
    assert acct.claude_config_json != new_config
    assert decrypt_field(acct.claude_config_json) == new_config
    assert acct.rate_limit_state is None


@pytest.mark.anyio
async def test_secret_blob_ref_encrypted_at_rest_on_create(
    client, db_session, mocker
):
    """secret_blob_ref is stored encrypted in DB on create."""
    from cryptography.fernet import Fernet
    from app.field_encryption import decrypt_field

    key = Fernet.generate_key().decode()
    mocker.patch.dict("os.environ", {"GITHUB_PAT_ENCRYPTION_KEY": key})
    sm_ref = "projects/test/secrets/acct-enc/versions/1"
    mocker.patch(
        "app.secrets_manager.store_user_secret",
        new=AsyncMock(return_value=sm_ref),
    )

    res = await client.post(
        "/api/admin/max-plan-accounts",
        json={"name": "Encrypted SBR Create", "oauth_token": "sk-ant-oat01-enc"},
    )
    assert res.status_code == 200, res.text

    acct = (
        (
            await db_session.execute(
                select(MaxPlanAccount).where(
                    MaxPlanAccount.name == "Encrypted SBR Create"
                )
            )
        )
        .scalars()
        .one()
    )
    # Raw DB value must be encrypted (not equal to the SM path).
    assert acct.secret_blob_ref != sm_ref
    # Decrypted value must equal the original SM path.
    assert decrypt_field(acct.secret_blob_ref) == sm_ref


@pytest.mark.anyio
async def test_secret_blob_ref_encrypted_at_rest_on_update(
    client, db_session, mocker
):
    """Updating the secret via PATCH stores secret_blob_ref encrypted in DB."""
    from cryptography.fernet import Fernet
    from app.field_encryption import decrypt_field

    key = Fernet.generate_key().decode()
    mocker.patch.dict("os.environ", {"GITHUB_PAT_ENCRYPTION_KEY": key})
    sm_ref = "projects/test/secrets/acct-enc-patch/versions/1"
    mocker.patch(
        "app.secrets_manager.store_user_secret",
        new=AsyncMock(return_value=sm_ref),
    )

    acct = MaxPlanAccount(
        user_id="test-user-123",
        name="SBR Patch Enc Test",
        provider=LLMProviderName.ANTHROPIC,
        auth_mode=LLMAuthMode.OAUTH,
        oauth_token="projects/test/secrets/old/versions/1",
        secret_blob_ref="projects/test/secrets/old/versions/1",
    )
    db_session.add(acct)
    await db_session.commit()

    res = await client.patch(
        f"/api/admin/max-plan-accounts/{acct.id}",
        json={"secret": "sk-ant-oat01-new_secret"},
    )
    assert res.status_code == 200, res.text

    await db_session.refresh(acct)
    # Raw DB value must be encrypted.
    assert acct.secret_blob_ref != sm_ref
    # Decrypted value must equal the new SM path.
    assert decrypt_field(acct.secret_blob_ref) == sm_ref


# ── per-project cap CRUD + enforcement ───────────────────────────────────


async def test_set_per_project_cap_is_enforced(client, db_session, inject_pool):
    """Setting a per-project cap via PATCH .../per-project-cap persists to DB,
    syncs to the in-memory pool entry, and is enforced by _project_at_cap."""
    from app.max_plan_pool import (
        _account_by_id,
        _max_plan_pool_lock,
        _project_at_cap,
    )

    project = Project(name="pp-cap-enforce", user_id="test-user-123")
    db_session.add(project)
    await db_session.flush()

    acct_id = uuid.uuid4()
    acct = MaxPlanAccount(
        id=acct_id,
        user_id="test-user-123",
        name="Per-Project Cap Test",
        provider=LLMProviderName.ANTHROPIC,
        auth_mode=LLMAuthMode.OAUTH,
        oauth_token="projects/test/secrets/ppcap/versions/1",
        secret_blob_ref="projects/test/secrets/ppcap/versions/1",
    )
    db_session.add(acct)
    await db_session.commit()

    # Seed the in-memory pool with matching cap fields as empty dicts so the
    # PATCH handler can sync updates onto them.
    inject_pool(
        "test-user-123",
        [
            {
                "id": str(acct_id),
                "user_id": "test-user-123",
                "name": "Per-Project Cap Test",
                "provider": "anthropic",
                "auth_mode": "oauth",
                "enabled": True,
                "active_tasks": 0,
                "daily_task_count": 0,
                "daily_task_cap": None,
                "max_concurrent_tasks": None,
                "project_daily_task_cap": {},
                "project_max_concurrent_tasks": {},
                "project_state": {},
                "runtime_ready": True,
            }
        ],
    )

    pid = str(project.id)

    # ── 1. Set daily cap only ─────────────────────────────────────────────
    res = await client.patch(
        f"/api/admin/max-plan-accounts/{acct_id}/per-project-cap",
        json={"project_id": pid, "daily_task_cap": 5},
    )
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["project_id"] == pid
    assert body["project_daily_task_cap"] == {pid: 5}

    await db_session.refresh(acct)
    assert acct.project_daily_task_cap == {pid: 5}
    assert acct.project_max_concurrent_tasks is None  # untouched

    async with _max_plan_pool_lock:
        mem = _account_by_id.get(str(acct_id))
    assert mem is not None
    assert mem.get("project_daily_task_cap") == {pid: 5}

    # ── 2. Below daily cap — not blocked ──────────────────────────────────
    async with _max_plan_pool_lock:
        _account_by_id[str(acct_id)].setdefault("project_state", {})[pid] = {
            "daily_task_count": 3,
            "active_tasks": 0,
        }
    mem = _account_by_id.get(str(acct_id))
    reason = _project_at_cap(mem, pid)
    assert reason is None, f"Expected no cap, got: {reason}"

    # ── 3. At daily cap — blocked ─────────────────────────────────────────
    async with _max_plan_pool_lock:
        _account_by_id[str(acct_id)]["project_state"][pid] = {
            "daily_task_count": 5,
            "active_tasks": 0,
        }
    mem = _account_by_id.get(str(acct_id))
    reason = _project_at_cap(mem, pid)
    assert reason is not None
    assert "project daily cap" in reason.lower()
    assert "5" in reason

    # ── 4. Add concurrent cap ─────────────────────────────────────────────
    res = await client.patch(
        f"/api/admin/max-plan-accounts/{acct_id}/per-project-cap",
        json={"project_id": pid, "max_concurrent_tasks": 2},
    )
    assert res.status_code == 200, res.text

    await db_session.refresh(acct)
    assert acct.project_daily_task_cap == {pid: 5}  # unchanged
    assert acct.project_max_concurrent_tasks == {pid: 2}

    async with _max_plan_pool_lock:
        mem = _account_by_id.get(str(acct_id))
    assert mem.get("project_max_concurrent_tasks") == {pid: 2}

    # ── 5. Below concurrent cap — not blocked ─────────────────────────────
    async with _max_plan_pool_lock:
        _account_by_id[str(acct_id)]["project_state"][pid] = {
            "daily_task_count": 1,
            "active_tasks": 1,
        }
    mem = _account_by_id.get(str(acct_id))
    reason = _project_at_cap(mem, pid)
    assert reason is None

    # ── 6. At concurrent cap — blocked ────────────────────────────────────
    async with _max_plan_pool_lock:
        _account_by_id[str(acct_id)]["project_state"][pid] = {
            "daily_task_count": 1,
            "active_tasks": 2,
        }
    mem = _account_by_id.get(str(acct_id))
    reason = _project_at_cap(mem, pid)
    assert reason is not None
    assert "project concurrent cap" in reason.lower()
    assert "2" in reason

    # ── 7. Clear daily cap — concurrent cap still active ──────────────────
    res = await client.patch(
        f"/api/admin/max-plan-accounts/{acct_id}/per-project-cap",
        json={"project_id": pid, "daily_task_cap": None},
    )
    assert res.status_code == 200, res.text

    await db_session.refresh(acct)
    assert acct.project_daily_task_cap == {} or acct.project_daily_task_cap is None
    assert acct.project_max_concurrent_tasks == {pid: 2}

    # With daily cap cleared, at-daily-cap should not block but at-concurrent
    # should still block.
    async with _max_plan_pool_lock:
        _account_by_id[str(acct_id)]["project_state"][pid] = {
            "daily_task_count": 999,
            "active_tasks": 2,
        }
    mem = _account_by_id.get(str(acct_id))
    reason = _project_at_cap(mem, pid)
    assert reason is not None
    assert "concurrent" in reason.lower()

    # ── 8. Clear concurrent cap — no more per-project blocking ────────────
    res = await client.patch(
        f"/api/admin/max-plan-accounts/{acct_id}/per-project-cap",
        json={"project_id": pid, "max_concurrent_tasks": None},
    )
    assert res.status_code == 200, res.text

    await db_session.refresh(acct)
    assert acct.project_max_concurrent_tasks == {} or acct.project_max_concurrent_tasks is None

    async with _max_plan_pool_lock:
        _account_by_id[str(acct_id)]["project_state"][pid] = {
            "daily_task_count": 999,
            "active_tasks": 999,
        }
    mem = _account_by_id.get(str(acct_id))
    reason = _project_at_cap(mem, pid)
    assert reason is None, f"Expected no cap after clearing both, got: {reason}"


async def test_set_per_project_cap_rejects_missing_project_id(client, db_session):
    """PATCH .../per-project-cap requires a valid project_id."""
    acct = MaxPlanAccount(
        user_id="test-user-123",
        name="No PID",
        provider=LLMProviderName.ANTHROPIC,
        auth_mode=LLMAuthMode.OAUTH,
        oauth_token="projects/test/secrets/nopid/versions/1",
        secret_blob_ref="projects/test/secrets/nopid/versions/1",
    )
    db_session.add(acct)
    await db_session.commit()

    res = await client.patch(
        f"/api/admin/max-plan-accounts/{acct.id}/per-project-cap",
        json={},
    )
    assert res.status_code == 400
    assert "project_id" in res.text


async def test_set_per_project_cap_rejects_invalid_values(client, db_session):
    """Negative and zero caps are rejected."""
    acct = MaxPlanAccount(
        user_id="test-user-123",
        name="Bad Val",
        provider=LLMProviderName.ANTHROPIC,
        auth_mode=LLMAuthMode.OAUTH,
        oauth_token="projects/test/secrets/badval/versions/1",
        secret_blob_ref="projects/test/secrets/badval/versions/1",
    )
    db_session.add(acct)
    await db_session.commit()

    res = await client.patch(
        f"/api/admin/max-plan-accounts/{acct.id}/per-project-cap",
        json={"project_id": str(uuid.uuid4()), "daily_task_cap": 0},
    )
    assert res.status_code == 400
    assert "daily_task_cap" in res.text

    res = await client.patch(
        f"/api/admin/max-plan-accounts/{acct.id}/per-project-cap",
        json={"project_id": str(uuid.uuid4()), "max_concurrent_tasks": -1},
    )
    assert res.status_code == 400
    assert "max_concurrent_tasks" in res.text


async def test_set_per_project_cap_rejects_other_users_account(client, db_session):
    """An account owned by another user must return 404."""
    acct = MaxPlanAccount(
        user_id="other-user",
        name="Not Mine",
        provider=LLMProviderName.ANTHROPIC,
        auth_mode=LLMAuthMode.OAUTH,
    )
    db_session.add(acct)
    await db_session.commit()

    res = await client.patch(
        f"/api/admin/max-plan-accounts/{acct.id}/per-project-cap",
        json={"project_id": str(uuid.uuid4()), "daily_task_cap": 5},
    )
    assert res.status_code == 404

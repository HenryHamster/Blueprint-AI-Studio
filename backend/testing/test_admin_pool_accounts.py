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


async def test_provider_model_breakdown_aggregation_and_median(client, db_session):
    """Breakdown groups by (provider, model), computes median, filters low-sample rows."""
    project = Project(name="pm-test", user_id="test-user-123")
    db_session.add(project)
    await db_session.flush()

    acct_a_id, acct_b_id = uuid.uuid4(), uuid.uuid4()
    acct_a, acct_b = str(acct_a_id), str(acct_b_id)
    mp_a = MaxPlanAccount(
        id=acct_a_id, user_id="test-user-123", name="PM-A",
        provider=LLMProviderName.ANTHROPIC, auth_mode=LLMAuthMode.OAUTH,
    )
    mp_b = MaxPlanAccount(
        id=acct_b_id, user_id="test-user-123", name="PM-B",
        provider=LLMProviderName.ANTHROPIC, auth_mode=LLMAuthMode.OAUTH,
    )
    db_session.add_all([mp_a, mp_b])
    await db_session.flush()

    # anthropic/claude-3-5: acct_a=5C+1F (rate≈0.833), acct_b=2C+1F (rate≈0.667)
    # Both have >= 3 tasks, so both are included in the median calculation.
    tasks = [
        Task(project_id=project.id, provider="anthropic", model="claude-3-5",
             account_id=acct_a, status=TaskStatus.COMPLETED)
        for _ in range(5)
    ] + [
        Task(project_id=project.id, provider="anthropic", model="claude-3-5",
             account_id=acct_a, status=TaskStatus.FAILED),
        Task(project_id=project.id, provider="anthropic", model="claude-3-5",
             account_id=acct_b, status=TaskStatus.COMPLETED),
        Task(project_id=project.id, provider="anthropic", model="claude-3-5",
             account_id=acct_b, status=TaskStatus.COMPLETED),
        Task(project_id=project.id, provider="anthropic", model="claude-3-5",
             account_id=acct_b, status=TaskStatus.FAILED),
    ] + [
        # openai/gpt-4: only 3 tasks — below _PROVIDER_MODEL_MIN_SAMPLE=5, excluded
        Task(project_id=project.id, provider="openai", model="gpt-4",
             account_id=acct_a, status=TaskStatus.COMPLETED)
        for _ in range(3)
    ]
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
        id=acct_good_id, user_id="test-user-123", name="Expand-Good",
        provider=LLMProviderName.ANTHROPIC, auth_mode=LLMAuthMode.OAUTH,
    )
    mp_bad = MaxPlanAccount(
        id=acct_bad_id, user_id="test-user-123", name="Expand-Bad",
        provider=LLMProviderName.ANTHROPIC, auth_mode=LLMAuthMode.OAUTH,
    )
    db_session.add_all([mp_good, mp_bad])
    await db_session.flush()

    tasks = [
        Task(project_id=project.id, provider="anthropic", model="sonnet",
             account_id=acct_good, status=TaskStatus.COMPLETED)
        for _ in range(5)
    ] + [
        Task(project_id=project.id, provider="anthropic", model="sonnet",
             account_id=acct_bad, status=TaskStatus.FAILED)
        for _ in range(5)
    ]
    db_session.add_all(tasks)
    await db_session.commit()

    res = await client.get("/api/admin/max-plan-accounts/provider-model-breakdown?expand_accounts=true")
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
    db_session.add_all([
        MaxPlanAccount(id=acct_a_id, user_id="test-user-123", name="Infra-A",
                       provider=LLMProviderName.ANTHROPIC, auth_mode=LLMAuthMode.OAUTH),
        MaxPlanAccount(id=acct_b_id, user_id="test-user-123", name="Infra-B",
                       provider=LLMProviderName.ANTHROPIC, auth_mode=LLMAuthMode.OAUTH),
    ])
    await db_session.flush()

    # 5 legit completions (should count)
    tasks = [
        Task(project_id=project.id, provider="anthropic", model="claude-3",
             account_id=acct_a, status=TaskStatus.COMPLETED)
        for _ in range(3)
    ] + [
        Task(project_id=project.id, provider="anthropic", model="claude-3",
             account_id=acct_b, status=TaskStatus.COMPLETED)
        for _ in range(2)
    ] + [
        # Infra failures — should be excluded
        Task(project_id=project.id, provider="anthropic", model="claude-3",
             account_id=acct_a, status=TaskStatus.FAILED,
             context={"failure_category": "RATE_LIMIT"}),
        Task(project_id=project.id, provider="anthropic", model="claude-3",
             account_id=acct_b, status=TaskStatus.FAILED,
             context={"failure_category": "PROVISION_FAILURE"}),
        Task(project_id=project.id, provider="anthropic", model="claude-3",
             account_id=acct_a, status=TaskStatus.CANCELLED,
             context={"failure_category": "AGENT_TIMEOUT"}),
    ]
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


async def test_provider_model_breakdown_code_error_failures_included(client, db_session):
    """CODE_ERROR failures are counted in the denominator (non-infra failures stay in)."""
    project = Project(name="code-err", user_id="test-user-123")
    db_session.add(project)
    await db_session.flush()

    acct_a_id, acct_b_id = uuid.uuid4(), uuid.uuid4()
    acct_a, acct_b = str(acct_a_id), str(acct_b_id)
    db_session.add_all([
        MaxPlanAccount(id=acct_a_id, user_id="test-user-123", name="Code-A",
                       provider=LLMProviderName.ANTHROPIC, auth_mode=LLMAuthMode.OAUTH),
        MaxPlanAccount(id=acct_b_id, user_id="test-user-123", name="Code-B",
                       provider=LLMProviderName.ANTHROPIC, auth_mode=LLMAuthMode.OAUTH),
    ])
    await db_session.flush()

    tasks = [
        Task(project_id=project.id, provider="anthropic", model="sonnet",
             account_id=acct_a, status=TaskStatus.COMPLETED)
        for _ in range(3)
    ] + [
        Task(project_id=project.id, provider="anthropic", model="sonnet",
             account_id=acct_b, status=TaskStatus.COMPLETED)
        for _ in range(2)
    ] + [
        # CODE_ERROR — not infra, should remain in denominator
        Task(project_id=project.id, provider="anthropic", model="sonnet",
             account_id=acct_a, status=TaskStatus.FAILED,
             context={"failure_category": "CODE_ERROR"}),
    ]
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
    db_session.add(MaxPlanAccount(
        id=acct_id, user_id="test-user-123", name="Solo",
        provider=LLMProviderName.ANTHROPIC, auth_mode=LLMAuthMode.OAUTH,
    ))
    await db_session.flush()

    # 5 failed tasks — would look like a regression, but only one account
    tasks = [
        Task(project_id=project.id, provider="anthropic", model="opus",
             account_id=acct, status=TaskStatus.FAILED)
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
    db_session.add_all([
        MaxPlanAccount(id=acct_good_id, user_id="test-user-123", name="TGood",
                       provider=LLMProviderName.ANTHROPIC, auth_mode=LLMAuthMode.OAUTH),
        MaxPlanAccount(id=acct_bad_id, user_id="test-user-123", name="TBad",
                       provider=LLMProviderName.ANTHROPIC, auth_mode=LLMAuthMode.OAUTH),
    ])
    await db_session.flush()

    # acct_good: 9 success, 1 fail → rate 0.9
    # acct_bad:  3 success, 1 fail → rate 0.75; median of [0.75, 0.9] = 0.825
    # relative gap: 0.75 < 0.825 * 0.8 = 0.66 → NOT met
    tasks = [
        Task(project_id=project.id, provider="anthropic", model="haiku",
             account_id=acct_good, status=TaskStatus.COMPLETED)
        for _ in range(9)
    ] + [
        Task(project_id=project.id, provider="anthropic", model="haiku",
             account_id=acct_good, status=TaskStatus.FAILED),
        Task(project_id=project.id, provider="anthropic", model="haiku",
             account_id=acct_bad, status=TaskStatus.COMPLETED),
        Task(project_id=project.id, provider="anthropic", model="haiku",
             account_id=acct_bad, status=TaskStatus.COMPLETED),
        Task(project_id=project.id, provider="anthropic", model="haiku",
             account_id=acct_bad, status=TaskStatus.COMPLETED),
        Task(project_id=project.id, provider="anthropic", model="haiku",
             account_id=acct_bad, status=TaskStatus.FAILED),
    ]
    db_session.add_all(tasks)
    await db_session.commit()

    candidates = await find_account_model_regressions(
        db_session, [acct_good, acct_bad]
    )
    assert candidates == []


async def test_find_regressions_returns_below_median_accounts(db_session):
    """A two-account group where one account clearly underperforms returns a candidate."""
    from app.account_model_regression import find_account_model_regressions

    project = Project(name="regression-hit", user_id="test-user-123")
    db_session.add(project)
    await db_session.flush()

    acct_good_id, acct_bad_id = uuid.uuid4(), uuid.uuid4()
    acct_good, acct_bad = str(acct_good_id), str(acct_bad_id)
    db_session.add_all([
        MaxPlanAccount(id=acct_good_id, user_id="test-user-123", name="RGood",
                       provider=LLMProviderName.ANTHROPIC, auth_mode=LLMAuthMode.OAUTH),
        MaxPlanAccount(id=acct_bad_id, user_id="test-user-123", name="RBad",
                       provider=LLMProviderName.ANTHROPIC, auth_mode=LLMAuthMode.OAUTH),
    ])
    await db_session.flush()

    # acct_good: 5 success → rate 1.0
    # acct_bad:  0 success, 5 fail → rate 0.0
    # median of [0.0, 1.0] = 0.5; relative: 0.0 < 0.5*0.8=0.4 ✓; absolute: 0.5-0.0=0.5 ≥ 0.15 ✓
    tasks = [
        Task(project_id=project.id, provider="anthropic", model="claude-r",
             account_id=acct_good, status=TaskStatus.COMPLETED)
        for _ in range(5)
    ] + [
        Task(project_id=project.id, provider="anthropic", model="claude-r",
             account_id=acct_bad, status=TaskStatus.FAILED)
        for _ in range(5)
    ]
    db_session.add_all(tasks)
    await db_session.commit()

    candidates = await find_account_model_regressions(
        db_session, [acct_good, acct_bad]
    )
    assert len(candidates) == 1
    c = candidates[0]
    assert c.account_id == acct_bad
    assert c.provider == "anthropic"
    assert c.model == "claude-r"
    assert c.account_rate == 0.0
    assert c.project_median == 0.5
    assert c.sample_size == 5
    assert c.distinct_accounts == 2


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
    db_session.add_all([
        MaxPlanAccount(id=acct_good_id, user_id="test-user-123", name="QGood",
                       provider=LLMProviderName.ANTHROPIC, auth_mode=LLMAuthMode.OAUTH),
        MaxPlanAccount(id=acct_bad_id, user_id="test-user-123", name="QBad",
                       provider=LLMProviderName.ANTHROPIC, auth_mode=LLMAuthMode.OAUTH,
                       rate_limit_state=quarantine_entry),
    ])
    await db_session.flush()

    tasks = [
        Task(project_id=project.id, provider="anthropic", model="claude-q",
             account_id=acct_good, status=TaskStatus.COMPLETED)
        for _ in range(5)
    ] + [
        Task(project_id=project.id, provider="anthropic", model="claude-q",
             account_id=acct_bad, status=TaskStatus.FAILED)
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
    db_session.add_all([
        MaxPlanAccount(id=acct_good_id, user_id="test-user-123", name="IGood",
                       provider=LLMProviderName.ANTHROPIC, auth_mode=LLMAuthMode.OAUTH),
        MaxPlanAccount(id=acct_bad_id, user_id="test-user-123", name="IBad",
                       provider=LLMProviderName.ANTHROPIC, auth_mode=LLMAuthMode.OAUTH),
    ])
    await db_session.flush()

    # acct_good: 5 successes (rate 1.0)
    # acct_bad:  5 RATE_LIMIT failures — infra-only, must be filtered out so
    #            the account drops below the 3-task floor.
    tasks = [
        Task(project_id=project.id, provider="anthropic", model="claude-i",
             account_id=acct_good, status=TaskStatus.COMPLETED)
        for _ in range(5)
    ] + [
        Task(project_id=project.id, provider="anthropic", model="claude-i",
             account_id=acct_bad, status=TaskStatus.FAILED,
             context={"failure_category": "RATE_LIMIT"})
        for _ in range(5)
    ]
    db_session.add_all(tasks)
    await db_session.commit()

    candidates = await find_account_model_regressions(
        db_session, [acct_good, acct_bad]
    )
    # acct_bad's infra failures are stripped, leaving 0 samples, which is
    # below the per-(account, provider, model) min-sample threshold (3).
    # No regression should fire.
    assert candidates == []


async def test_clear_regression_endpoint_clears_only_regression_kind(client, db_session):
    """POST /max-plan-accounts/{id}/regression-quarantine/clear removes a
    regression entry but refuses to touch a rate_limit entry."""
    from app.max_plan_pool import _account_by_id, _max_plan_pool_lock

    acct_id = uuid.uuid4()
    db_session.add(MaxPlanAccount(
        id=acct_id, user_id="test-user-123", name="ClearMe",
        provider=LLMProviderName.ANTHROPIC, auth_mode=LLMAuthMode.OAUTH,
    ))
    await db_session.commit()

    # Seed in-memory regression entry directly.
    async with _max_plan_pool_lock:
        _account_by_id[str(acct_id)] = {
            "id": str(acct_id), "name": "ClearMe", "enabled": True,
            "active_tasks": 0, "user_id": "test-user-123",
            "provider": "anthropic",
            "disabled_models": {
                "claude-q": {
                    "kind": "regression",
                    "at": datetime.utcnow(),
                    "hits": 1,
                    "provider": "anthropic",
                    "regression_stats": {
                        "account_rate": 0.1, "project_median": 0.9,
                        "sample_size": 5, "distinct_accounts": 2,
                        "started_at": "2026-04-29T00:00:00+00:00",
                    },
                },
                # And a rate_limit entry on a different model — must be untouched.
                "other-model": {
                    "kind": None, "at": datetime.utcnow(), "hits": 1,
                },
            },
        }
    try:
        from unittest.mock import AsyncMock as _AM
        from unittest.mock import patch as _P

        with _P("app.max_plan_pool._persist_rate_limit_state", new=_AM(return_value=None)):
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
        with _P("app.max_plan_pool._persist_rate_limit_state", new=_AM(return_value=None)):
            res = await client.post(
                f"/api/admin/max-plan-accounts/{acct_id}/regression-quarantine/clear"
                "?provider=anthropic&model=claude-q"
            )
        assert res.status_code == 404

        # And clearing the non-regression "other-model" must also 404.
        with _P("app.max_plan_pool._persist_rate_limit_state", new=_AM(return_value=None)):
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


async def test_clear_regression_endpoint_rejects_other_users_accounts(client, db_session):
    """An account owned by another user must not be clearable."""
    from app.max_plan_pool import _account_by_id, _max_plan_pool_lock

    acct_id = uuid.uuid4()
    db_session.add(MaxPlanAccount(
        id=acct_id, user_id="some-other-user", name="NotMine",
        provider=LLMProviderName.ANTHROPIC, auth_mode=LLMAuthMode.OAUTH,
    ))
    await db_session.commit()

    async with _max_plan_pool_lock:
        _account_by_id[str(acct_id)] = {
            "id": str(acct_id), "name": "NotMine", "enabled": True,
            "active_tasks": 0, "user_id": "some-other-user",
            "provider": "anthropic",
            "disabled_models": {
                "claude-q": {"kind": "regression", "at": datetime.utcnow(), "hits": 1}
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

    acct_good_id, acct_bad_id = uuid.uuid4(), uuid.uuid4()
    acct_good, acct_bad = str(acct_good_id), str(acct_bad_id)
    db_session.add_all([
        MaxPlanAccount(id=acct_good_id, user_id="test-user-123", name="DGood",
                       provider=LLMProviderName.ANTHROPIC, auth_mode=LLMAuthMode.OAUTH),
        MaxPlanAccount(id=acct_bad_id, user_id="test-user-123", name="DBad",
                       provider=LLMProviderName.ANTHROPIC, auth_mode=LLMAuthMode.OAUTH),
    ])
    await db_session.flush()

    # 5 success on good, 5 fail on bad → bad regresses.
    tasks = [
        Task(project_id=project.id, provider="anthropic", model="claude-d",
             account_id=acct_good, status=TaskStatus.COMPLETED)
        for _ in range(5)
    ] + [
        Task(project_id=project.id, provider="anthropic", model="claude-d",
             account_id=acct_bad, status=TaskStatus.FAILED)
        for _ in range(5)
    ]
    db_session.add_all(tasks)
    await db_session.commit()

    # Inject in-memory pool entries so the scanner sees the accounts.
    async with _max_plan_pool_lock:
        _account_by_id[acct_good] = {
            "id": acct_good, "name": "DGood", "enabled": True,
            "active_tasks": 0, "user_id": "test-user-123", "provider": "anthropic",
        }
        _account_by_id[acct_bad] = {
            "id": acct_bad, "name": "DBad", "enabled": True,
            "active_tasks": 0, "user_id": "test-user-123", "provider": "anthropic",
        }

    try:
        from unittest.mock import AsyncMock as _AM
        from unittest.mock import patch as _P
        with _P("app.max_plan_pool._persist_rate_limit_state", new=_AM(return_value=None)):
            fired_first = await scan_account_model_regressions(db_session)
            fired_second = await scan_account_model_regressions(db_session)
        assert len(fired_first) == 1
        ev = fired_first[0]
        assert ev["account_id"] == acct_bad
        assert ev["provider"] == "anthropic"
        assert ev["model"] == "claude-d"
        # Notification payload must include the numeric stats.
        assert ev["account_rate"] == 0.0
        assert ev["project_median"] == 0.5
        assert ev["sample_size"] == 5
        # Second tick must dedupe — same regression, no new fire event.
        assert fired_second == []
    finally:
        async with _max_plan_pool_lock:
            _account_by_id.pop(acct_good, None)
            _account_by_id.pop(acct_bad, None)

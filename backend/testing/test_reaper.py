"""Targeted unit tests for app/reaper.py.

Focus: the high-risk branches of reaper logic — orphan detection state
machine, provision failure classification (infra vs task-specific), rate-limit
requeue path, non-provision failure classification & retry-delay selection,
the retry_after sweep (Step 5b), the _copy_analysis_artifacts_to_previous
helper, and the dependent-task unblocking pass that runs after terminal
failures.
"""
import uuid as _uuid_mod
import pytest
import pytest_asyncio
from datetime import datetime, timedelta
from unittest.mock import AsyncMock
from sqlalchemy import select
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool
from sqlalchemy.sql.elements import TextClause
from sqlmodel import SQLModel

from sqlmodel import select as _select

from app.models import Alert, AlertSeverity, Project, Task, TaskStatus
from app.reaper import reaper_service, _copy_analysis_artifacts_to_previous, _reaper_skip_counts, _heartbeat_stale_cutoff
from app.shared_state import runtime_config as _runtime_config


def test_heartbeat_stale_cutoff_respects_runtime_config_override():
    """_heartbeat_stale_cutoff reads per-scope thresholds from runtime_config."""
    original = _runtime_config.get("heartbeat_stale_seconds_by_scope")
    try:
        _runtime_config["heartbeat_stale_seconds_by_scope"] = {
            "small": 60, "medium": 120, "large": 900,
        }

        class _T:
            context = {"scope": "large"}
            first_payload = None

        now = datetime(2026, 1, 1, 12, 0, 0)
        cutoff = _heartbeat_stale_cutoff(_T(), now)
        assert cutoff == now - timedelta(seconds=900)

        _T.context = {"scope": "small"}
        assert _heartbeat_stale_cutoff(_T(), now) == now - timedelta(seconds=60)
    finally:
        if original is None:
            _runtime_config.pop("heartbeat_stale_seconds_by_scope", None)
        else:
            _runtime_config["heartbeat_stale_seconds_by_scope"] = original


def test_heartbeat_stale_cutoff_effort_floor_extends_small_scope_max_effort():
    """Effort floor (max=600s) extends small-scope cutoff (300s) when effort is higher."""
    class _T:
        context = {"scope": "small", "effort": "max"}
        first_payload = None

    now = datetime(2026, 1, 1, 12, 0, 0)
    assert _heartbeat_stale_cutoff(_T(), now) == now - timedelta(seconds=600)


def test_heartbeat_stale_cutoff_effort_floor_does_not_weaken_large_scope():
    """Large-scope threshold (600s) wins over low-effort floor (300s)."""
    class _T:
        context = {"scope": "large", "effort": "low"}
        first_payload = None

    now = datetime(2026, 1, 1, 12, 0, 0)
    assert _heartbeat_stale_cutoff(_T(), now) == now - timedelta(seconds=600)


def test_heartbeat_stale_cutoff_effort_from_task_type_fallback():
    """Effort resolves from task_type_efforts when not explicit in context."""
    class _T:
        context = {"scope": "small", "type": "strategic_review"}
        first_payload = None

    now = datetime(2026, 1, 1, 12, 0, 0)
    # strategic_review -> "max" effort -> 600s floor > 300s small scope
    assert _heartbeat_stale_cutoff(_T(), now) == now - timedelta(seconds=600)


def test_heartbeat_stale_cutoff_unknown_effort_uses_scope_threshold():
    """Unknown effort string falls back to scope-only behavior."""
    class _T:
        context = {"scope": "small", "effort": "unknown_effort_xyz"}
        first_payload = None

    now = datetime(2026, 1, 1, 12, 0, 0)
    assert _heartbeat_stale_cutoff(_T(), now) == now - timedelta(seconds=300)


class _StopLoop(Exception):
    """Raised from mocked asyncio.sleep to break the reaper's infinite loop."""


@pytest_asyncio.fixture()
async def reaper_db(mocker):
    """Isolated in-memory DB bound into app.reaper.AsyncSessionLocal."""
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    async with engine.begin() as conn:
        await conn.run_sync(SQLModel.metadata.create_all)

    Session = sessionmaker(
        bind=engine, class_=AsyncSession,
        autocommit=False, autoflush=False, expire_on_commit=False,
    )
    mocker.patch("app.reaper.AsyncSessionLocal", Session)

    async with Session() as s:
        yield s

    async with engine.begin() as conn:
        await conn.run_sync(SQLModel.metadata.drop_all)
    await engine.dispose()


@pytest.fixture()
def stop_after_one_cycle(mocker):
    """Force reaper_service to exit after a single loop iteration."""
    mocker.patch("asyncio.sleep", side_effect=_StopLoop())


@pytest.fixture()
def reaper_side_effects(mocker):
    """Neutralize the side-effecting deferred imports used by reaper_service."""
    mocker.patch("app.main._ensure_vm_deleted", new_callable=AsyncMock, return_value=True)
    mocker.patch("app.main.delete_agent_vm", return_value=True)
    mocker.patch("app.main._maybe_create_failure_alert", new_callable=AsyncMock)
    mocker.patch("app.rollup_management._check_and_trigger_rollup", new_callable=AsyncMock)
    mocker.patch("app.reaper._capture_serial_for_vm", return_value=None)
    mocker.patch("app.reaper.record_zone_failure")
    # Disable infra-recovery resurrection so FAILED tasks stay FAILED where asserted.
    mocker.patch("app.reaper.any_zone_available", return_value=True)


async def _make_project(db: AsyncSession) -> Project:
    project = Project(name="reaper-unit", user_id="user1")
    db.add(project)
    await db.commit()
    await db.refresh(project)
    return project


async def _run_reaper_once():
    try:
        await reaper_service()
    except _StopLoop:
        pass


# ── _copy_analysis_artifacts_to_previous (pure helper) ────────────────────


def test_copy_analysis_artifacts_mirrors_all_keys():
    ctx = {
        "_pr_diff_summary": [{"file": "a.py"}],
        "_test_failure_summary": "boom",
        "_self_review_outcome": {"verdict": "needs_changes"},
        "_analysis_plan": "plan text",
    }
    _copy_analysis_artifacts_to_previous(ctx)
    assert ctx["_previous_diff_summary"] == [{"file": "a.py"}]
    assert ctx["_previous_test_failure_summary"] == "boom"
    assert ctx["_previous_self_review_outcome"] == {"verdict": "needs_changes"}
    assert ctx["_previous_analysis_plan"] == "plan text"


def test_copy_analysis_artifacts_skips_missing_and_falsy():
    ctx = {
        "_pr_diff_summary": None,
        "_test_failure_summary": "",
        "_self_review_outcome": {},
        # _analysis_plan intentionally absent
        "unrelated": "keep",
    }
    _copy_analysis_artifacts_to_previous(ctx)
    assert "_previous_diff_summary" not in ctx
    assert "_previous_test_failure_summary" not in ctx
    assert "_previous_self_review_outcome" not in ctx
    assert "_previous_analysis_plan" not in ctx
    assert ctx["unrelated"] == "keep"


def test_copy_analysis_artifacts_overwrites_existing_previous():
    ctx = {
        "_pr_diff_summary": "new",
        "_previous_diff_summary": "stale",
    }
    _copy_analysis_artifacts_to_previous(ctx)
    assert ctx["_previous_diff_summary"] == "new"


# ── Orphan detection state machine ────────────────────────────────────────


@pytest.mark.asyncio
async def test_provisioning_absolute_15min_timeout_reaped(
    reaper_db, stop_after_one_cycle, reaper_side_effects
):
    """PROVISIONING with started_at > 15min ago → orphaned (absolute ceiling)."""
    project = await _make_project(reaper_db)
    t = Task(
        project_id=project.id,
        payloads=[{"type": "create"}],
        vm_instance_id="vm-absolute",
        status=TaskStatus.PROVISIONING,
        started_at=datetime.utcnow() - timedelta(minutes=16),
        context={"provision_zone": "us-east1-a"},
    )
    reaper_db.add(t)
    await reaper_db.commit()

    await _run_reaper_once()
    await reaper_db.refresh(t)

    assert t.status != TaskStatus.PROVISIONING, "reaper must flip the task off PROVISIONING"


@pytest.mark.asyncio
async def test_provisioning_just_inside_15min_not_reaped(
    reaper_db, stop_after_one_cycle, reaper_side_effects
):
    """PROVISIONING with started_at 14 min ago is inside the ceiling — left alone."""
    project = await _make_project(reaper_db)
    t = Task(
        project_id=project.id,
        payloads=[{"type": "create"}],
        vm_instance_id="vm-inside-15",
        status=TaskStatus.PROVISIONING,
        # vm_provisioned_at < 6 min ago AND last_heartbeat None — below the
        # pre-claim-death threshold too, so nothing else can trip.
        started_at=datetime.utcnow() - timedelta(minutes=14),
        vm_provisioned_at=datetime.utcnow() - timedelta(minutes=4),
    )
    reaper_db.add(t)
    await reaper_db.commit()

    await _run_reaper_once()
    await reaper_db.refresh(t)

    assert t.status == TaskStatus.PROVISIONING


@pytest.mark.asyncio
async def test_provisioning_pre_claim_death_reaped(
    reaper_db, stop_after_one_cycle, reaper_side_effects
):
    """PROVISIONING + vm_provisioned_at >6min + no heartbeat = silent pre-claim death."""
    project = await _make_project(reaper_db)
    t = Task(
        project_id=project.id,
        payloads=[{"type": "create"}],
        vm_instance_id="vm-silent",
        status=TaskStatus.PROVISIONING,
        started_at=datetime.utcnow() - timedelta(minutes=7),
        vm_provisioned_at=datetime.utcnow() - timedelta(minutes=7),
        last_heartbeat=None,
        context={"provision_zone": "us-east1-a"},
    )
    reaper_db.add(t)
    await reaper_db.commit()

    await _run_reaper_once()
    await reaper_db.refresh(t)

    assert t.status != TaskStatus.PROVISIONING


@pytest.mark.asyncio
async def test_in_progress_heartbeat_timeout_reaped(
    reaper_db, stop_after_one_cycle, reaper_side_effects
):
    """IN_PROGRESS with last_heartbeat > 5 min → orphaned."""
    project = await _make_project(reaper_db)
    t = Task(
        project_id=project.id,
        payloads=[{"type": "create"}],
        vm_instance_id="vm-hb",
        status=TaskStatus.IN_PROGRESS,
        attempt_count=0,
        last_heartbeat=datetime.utcnow() - timedelta(minutes=6),
    )
    reaper_db.add(t)
    await reaper_db.commit()

    await _run_reaper_once()
    await reaper_db.refresh(t)

    # Reaper retries (attempt_count < class budget), so status flips to QUEUED.
    assert t.status == TaskStatus.QUEUED
    assert t.attempt_count == 1


@pytest.mark.asyncio
async def test_in_progress_heartbeat_just_inside_threshold_not_reaped(
    reaper_db, stop_after_one_cycle, reaper_side_effects
):
    """IN_PROGRESS with last_heartbeat 4 min ago — inside 5min threshold."""
    project = await _make_project(reaper_db)
    t = Task(
        project_id=project.id,
        payloads=[{"type": "create"}],
        vm_instance_id="vm-fresh",
        status=TaskStatus.IN_PROGRESS,
        attempt_count=0,
        last_heartbeat=datetime.utcnow() - timedelta(minutes=4),
    )
    reaper_db.add(t)
    await reaper_db.commit()

    await _run_reaper_once()
    await reaper_db.refresh(t)

    assert t.status == TaskStatus.IN_PROGRESS


@pytest.mark.asyncio
async def test_in_progress_no_heartbeat_started_6min_reaped(
    reaper_db, stop_after_one_cycle, reaper_side_effects
):
    """IN_PROGRESS + no heartbeat + started_at >6min → defensive post-claim-crash reap."""
    project = await _make_project(reaper_db)
    t = Task(
        project_id=project.id,
        payloads=[{"type": "create"}],
        vm_instance_id="vm-no-hb",
        status=TaskStatus.IN_PROGRESS,
        attempt_count=0,
        started_at=datetime.utcnow() - timedelta(minutes=7),
        last_heartbeat=None,
    )
    reaper_db.add(t)
    await reaper_db.commit()

    await _run_reaper_once()
    await reaper_db.refresh(t)

    assert t.status == TaskStatus.QUEUED


@pytest.mark.asyncio
async def test_orphan_ignored_without_vm_instance_id(
    reaper_db, stop_after_one_cycle, reaper_side_effects
):
    """The reaper's first guard: skip any active task with no vm_instance_id."""
    project = await _make_project(reaper_db)
    t = Task(
        project_id=project.id,
        payloads=[{"type": "create"}],
        vm_instance_id=None,
        status=TaskStatus.IN_PROGRESS,
        attempt_count=0,
        last_heartbeat=datetime.utcnow() - timedelta(minutes=30),
    )
    reaper_db.add(t)
    await reaper_db.commit()

    await _run_reaper_once()
    await reaper_db.refresh(t)

    assert t.status == TaskStatus.IN_PROGRESS


@pytest.mark.asyncio
async def test_large_scope_heartbeat_within_10min_not_reaped(
    reaper_db, stop_after_one_cycle, reaper_side_effects
):
    """Large-scope task gets a 10min heartbeat budget — 8min stale stays IN_PROGRESS."""
    project = await _make_project(reaper_db)
    t = Task(
        project_id=project.id,
        payloads=[{"type": "create"}],
        vm_instance_id="vm-large-fresh",
        status=TaskStatus.IN_PROGRESS,
        attempt_count=0,
        last_heartbeat=datetime.utcnow() - timedelta(minutes=8),
        context={"scope": "large"},
    )
    reaper_db.add(t)
    await reaper_db.commit()

    await _run_reaper_once()
    await reaper_db.refresh(t)

    assert t.status == TaskStatus.IN_PROGRESS


@pytest.mark.asyncio
async def test_large_scope_heartbeat_past_10min_reaped(
    reaper_db, stop_after_one_cycle, reaper_side_effects
):
    """Large-scope task past the 10min threshold is still reaped."""
    project = await _make_project(reaper_db)
    t = Task(
        project_id=project.id,
        payloads=[{"type": "create"}],
        vm_instance_id="vm-large-dead",
        status=TaskStatus.IN_PROGRESS,
        attempt_count=0,
        last_heartbeat=datetime.utcnow() - timedelta(minutes=11),
        context={"scope": "large"},
    )
    reaper_db.add(t)
    await reaper_db.commit()

    await _run_reaper_once()
    await reaper_db.refresh(t)

    assert t.status == TaskStatus.QUEUED
    assert t.attempt_count == 1


@pytest.mark.asyncio
async def test_medium_scope_heartbeat_within_7min_not_reaped(
    reaper_db, stop_after_one_cycle, reaper_side_effects
):
    """Medium-scope task gets a 7min heartbeat budget — 6min stale stays IN_PROGRESS."""
    project = await _make_project(reaper_db)
    t = Task(
        project_id=project.id,
        payloads=[{"type": "create"}],
        vm_instance_id="vm-med-fresh",
        status=TaskStatus.IN_PROGRESS,
        attempt_count=0,
        last_heartbeat=datetime.utcnow() - timedelta(minutes=6),
        context={"scope": "medium"},
    )
    reaper_db.add(t)
    await reaper_db.commit()

    await _run_reaper_once()
    await reaper_db.refresh(t)

    assert t.status == TaskStatus.IN_PROGRESS


# ── Provision failure classification ─────────────────────────────────────


@pytest.mark.asyncio
async def test_provision_infra_failure_does_not_burn_retry_budget(
    reaper_db, stop_after_one_cycle, reaper_side_effects, mocker
):
    """Infra-wide failure → provision_failures_infra increments; provision_failures stays 0."""
    mocker.patch("app.reaper.get_provision_error", return_value="")
    mocker.patch("app.reaper.get_vm_zone", return_value="us-east1-a")

    project = await _make_project(reaper_db)
    t = Task(
        project_id=project.id,
        payloads=[{"type": "create"}],
        vm_instance_id="vm-infra",
        status=TaskStatus.PROVISIONING,
        started_at=datetime.utcnow() - timedelta(minutes=16),
        latest_status_message="QUOTA exceeded: cpus_all_regions",
        context={"provision_zone": "us-east1-a"},
    )
    reaper_db.add(t)
    await reaper_db.commit()

    await _run_reaper_once()
    await reaper_db.refresh(t)

    ctx = t.context or {}
    assert ctx.get("provision_failures_infra") == 1
    # provision_failures is decremented back to 0 on the infra path
    assert ctx.get("provision_failures", 0) == 0
    assert ctx.get("_infra_wait") is True
    assert t.status == TaskStatus.QUEUED


@pytest.mark.asyncio
async def test_provision_task_specific_failure_burns_budget(
    reaper_db, stop_after_one_cycle, reaper_side_effects, mocker
):
    """Task-specific provision failure → provision_failures increments, no infra counter."""
    mocker.patch("app.reaper.get_provision_error", return_value="startup-script exit 1")
    mocker.patch("app.reaper.get_vm_zone", return_value="us-east1-a")
    # any_zone_available already mocked True in reaper_side_effects, so a non-infra
    # message with a known zone forces the task-specific branch.

    project = await _make_project(reaper_db)
    t = Task(
        project_id=project.id,
        payloads=[{"type": "create"}],
        vm_instance_id="vm-task-specific",
        status=TaskStatus.PROVISIONING,
        started_at=datetime.utcnow() - timedelta(minutes=16),
        latest_status_message="agent /entrypoint.sh returned 1",
        context={"provision_zone": "us-east1-a"},
    )
    reaper_db.add(t)
    await reaper_db.commit()

    await _run_reaper_once()
    await reaper_db.refresh(t)

    ctx = t.context or {}
    assert ctx.get("provision_failures") == 1
    assert "provision_failures_infra" not in ctx
    assert ctx.get("_infra_wait") is not True
    assert t.status == TaskStatus.QUEUED


# ── Rate-limit detection ─────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_rate_limit_requeue_blacklists_account_and_skips_budget(
    reaper_db, stop_after_one_cycle, reaper_side_effects, mocker
):
    """Rate-limit classification → requeued with blacklisted account and no
    provision/attempt budget burned; disable_account_model invoked for the pair."""
    disable_mock = mocker.patch(
        "app.max_plan_pool.disable_account_model", new_callable=AsyncMock
    )
    mocker.patch("app.max_plan_pool.release_max_plan_account", new_callable=AsyncMock)

    project = await _make_project(reaper_db)
    t = Task(
        project_id=project.id,
        # _extract_task_model reads payloads[0]["model"]
        payloads=[{"type": "create", "model": "sonnet"}],
        vm_instance_id="vm-rl",
        status=TaskStatus.IN_PROGRESS,
        attempt_count=1,
        last_heartbeat=datetime.utcnow() - timedelta(minutes=10),
        context={
            "max_plan_account_id": "acct-123",
            "last_error": "API Error: rate limit reached",
            "retry_after": "2099-01-01T00:00:00",  # should be cleared
            "provision_failures": 2,  # should be dropped (account-level issue)
        },
    )
    reaper_db.add(t)
    await reaper_db.commit()

    await _run_reaper_once()
    await reaper_db.refresh(t)

    ctx = t.context or {}
    assert t.status == TaskStatus.QUEUED
    assert t.attempt_count == 1, "rate-limit path must not burn attempt budget"
    assert "acct-123" in (ctx.get("blacklisted_accounts") or [])
    assert "max_plan_account_id" not in ctx
    assert "retry_after" not in ctx, "stale retry_after must be cleared on rate-limit requeue"
    assert "provision_failures" not in ctx
    disable_mock.assert_awaited_with("acct-123", "sonnet")


# ── Non-provision failure classification & retry delay selection ─────────


@pytest.mark.asyncio
async def test_heartbeat_timeout_empty_logs_classified_as_agent_timeout(
    reaper_db, stop_after_one_cycle, reaper_side_effects
):
    """Silent death (heartbeat timeout, no logs) must not default to CODE_ERROR."""
    project = await _make_project(reaper_db)
    t = Task(
        project_id=project.id,
        payloads=[{"type": "create"}],
        vm_instance_id="vm-silent-crash",
        status=TaskStatus.IN_PROGRESS,
        attempt_count=0,
        last_heartbeat=datetime.utcnow() - timedelta(minutes=7),
        # No latest_status_message, no context.last_error — pure heartbeat timeout.
    )
    reaper_db.add(t)
    await reaper_db.commit()

    await _run_reaper_once()
    await reaper_db.refresh(t)

    ctx = t.context or {}
    assert t.status == TaskStatus.QUEUED
    assert ctx.get("_previous_failure_class") == "AGENT_TIMEOUT"


@pytest.mark.asyncio
async def test_heartbeat_timeout_api_error_logs_classified_as_external_api(
    reaper_db, stop_after_one_cycle, reaper_side_effects
):
    """Heartbeat timeout with API-error logs routes through EXTERNAL_API."""
    project = await _make_project(reaper_db)
    t = Task(
        project_id=project.id,
        payloads=[{"type": "create"}],
        vm_instance_id="vm-api-err",
        status=TaskStatus.IN_PROGRESS,
        attempt_count=0,
        last_heartbeat=datetime.utcnow() - timedelta(minutes=7),
        context={"last_error": "api.github.com: 502 bad gateway"},
    )
    reaper_db.add(t)
    await reaper_db.commit()

    await _run_reaper_once()
    await reaper_db.refresh(t)

    ctx = t.context or {}
    assert t.status == TaskStatus.QUEUED
    assert ctx.get("_previous_failure_class") == "EXTERNAL_API"


@pytest.mark.asyncio
async def test_external_api_retry_delay_escalates_with_attempt_count(
    reaper_db, stop_after_one_cycle, reaper_side_effects, mocker
):
    """EXTERNAL_API attempt_count=2→3 picks the 30-minute tier of [5,15,30,60]."""
    # Pin jitter below 1.0 so retry_after stays under the Step 5b 30-min cutoff
    # (which runs in the same cycle and would otherwise clear a 30-min delay).
    mocker.patch("app.reaper.random.uniform", return_value=0.9)

    project = await _make_project(reaper_db)
    t = Task(
        project_id=project.id,
        payloads=[{"type": "create"}],
        vm_instance_id="vm-api-escalate",
        status=TaskStatus.IN_PROGRESS,
        attempt_count=2,
        last_heartbeat=datetime.utcnow() - timedelta(minutes=7),
        # Non-transient error (DNS resolution) routes through the long
        # [5,15,30,60] sequence rather than the adaptive-5xx fast path.
        context={"last_error": "name resolution failed for api.github.com", "pr_number": 42},
    )
    reaper_db.add(t)
    await reaper_db.commit()

    before = datetime.utcnow()
    await _run_reaper_once()
    await reaper_db.refresh(t)

    assert t.status == TaskStatus.QUEUED
    assert t.attempt_count == 3
    ra = (t.context or {}).get("retry_after")
    assert ra, "retry_after must be set after a reaped retry"
    delay_min = (datetime.fromisoformat(ra) - before).total_seconds() / 60
    # 30 min * 0.9 = 27 min. Without escalation (e.g. general backoff at attempt 3)
    # we'd see min(2^2, 15) * 0.9 = 3.6 min, so the 20-min floor confirms escalation.
    assert 20 <= delay_min <= 29, f"expected ~27min (30min * 0.9 jitter), got {delay_min:.1f}min"


@pytest.mark.asyncio
async def test_external_api_transient_5xx_uses_short_first_delay(
    reaper_db, stop_after_one_cycle, reaper_side_effects, mocker
):
    """Transient 5xx with no committed work + unhealthy github_api uses 1.5min first-attempt delay."""
    mocker.patch("app.reaper.random.uniform", return_value=1.0)
    # Mark github_api as currently failing so the fast-path "skip delay" branch
    # isn't taken — we want to verify the 1.5-minute first-attempt delay here.
    mocker.patch.dict(
        "app.reaper._external_health",
        {"consecutive_failures": {"github_api": 2}},
    )

    project = await _make_project(reaper_db)
    t = Task(
        project_id=project.id,
        payloads=[{"type": "create"}],
        vm_instance_id="vm-api-transient",
        status=TaskStatus.IN_PROGRESS,
        attempt_count=0,
        last_heartbeat=datetime.utcnow() - timedelta(minutes=7),
        context={"last_error": "HTTP 503 Service Unavailable from api.github.com"},
    )
    reaper_db.add(t)
    await reaper_db.commit()

    before = datetime.utcnow()
    await _run_reaper_once()
    await reaper_db.refresh(t)

    assert t.status == TaskStatus.QUEUED
    assert t.attempt_count == 1
    ra = (t.context or {}).get("retry_after")
    assert ra
    delay_min = (datetime.fromisoformat(ra) - before).total_seconds() / 60
    # 1.5 min * 1.0 jitter, allowing margin for test latency.
    assert 0.5 <= delay_min <= 3.0, f"expected ~1.5min, got {delay_min:.1f}min"


@pytest.mark.asyncio
async def test_external_api_transient_5xx_skips_delay_when_healthy(
    reaper_db, stop_after_one_cycle, reaper_side_effects, mocker
):
    """Transient 5xx + github_api recovered (consecutive_failures==0) → immediate requeue."""
    mocker.patch("app.reaper.random.uniform", return_value=1.0)
    mocker.patch.dict(
        "app.reaper._external_health",
        {"consecutive_failures": {"github_api": 0}},
    )

    project = await _make_project(reaper_db)
    t = Task(
        project_id=project.id,
        payloads=[{"type": "create"}],
        vm_instance_id="vm-api-healthy",
        status=TaskStatus.IN_PROGRESS,
        attempt_count=0,
        last_heartbeat=datetime.utcnow() - timedelta(minutes=7),
        context={"last_error": "502 Bad Gateway from api.github.com"},
    )
    reaper_db.add(t)
    await reaper_db.commit()

    before = datetime.utcnow()
    await _run_reaper_once()
    await reaper_db.refresh(t)

    assert t.status == TaskStatus.QUEUED
    # Fast-path: transient 5xx + recovered dependency + no committed work
    # must not burn the retry budget.
    assert t.attempt_count == 0
    ra = (t.context or {}).get("retry_after")
    assert ra
    delay_min = (datetime.fromisoformat(ra) - before).total_seconds() / 60
    assert delay_min < 1.0, f"expected near-zero delay, got {delay_min:.1f}min"


@pytest.mark.asyncio
async def test_external_api_transient_5xx_with_committed_work_takes_fast_path(
    reaper_db, stop_after_one_cycle, reaper_side_effects, mocker
):
    """Transient 5xx + recovered dependency: fast-path applies even when the task
    already pushed a PR. A transient 502 after commit shouldn't burn a retry."""
    mocker.patch("app.reaper.random.uniform", return_value=1.0)
    mocker.patch.dict(
        "app.reaper._external_health",
        {"consecutive_failures": {"github_api": 0}},
    )

    project = await _make_project(reaper_db)
    t = Task(
        project_id=project.id,
        payloads=[{"type": "create"}],
        vm_instance_id="vm-api-with-pr",
        status=TaskStatus.IN_PROGRESS,
        attempt_count=0,
        last_heartbeat=datetime.utcnow() - timedelta(minutes=7),
        context={
            "last_error": "502 Bad Gateway from api.github.com",
            "pr_number": 123,
        },
    )
    reaper_db.add(t)
    await reaper_db.commit()

    await _run_reaper_once()
    await reaper_db.refresh(t)

    assert t.status == TaskStatus.QUEUED
    # Fast-path: transient 5xx + healthy dependency must not burn the retry
    # budget, regardless of whether a PR was created.
    assert t.attempt_count == 0


@pytest.mark.asyncio
async def test_general_exponential_backoff_capped_at_15_minutes(
    reaper_db, stop_after_one_cycle, reaper_side_effects, mocker
):
    """Non-provision, non-EXTERNAL_API retries cap at min(2^(n-1), 15) minutes."""
    # Raise the AGENT_TIMEOUT budget so attempt_count=5 still retries; pin jitter
    # to 1.0 so the asserted delay is exactly the cap value.
    mocker.patch.dict(
        "app.reaper._runtime_config",
        {"retry_budgets_by_class": {"AGENT_TIMEOUT": 10, "CODE_ERROR": 2, "EXTERNAL_API": 4}},
    )
    mocker.patch("app.reaper.random.uniform", return_value=1.0)

    project = await _make_project(reaper_db)
    t = Task(
        project_id=project.id,
        payloads=[{"type": "create"}],
        vm_instance_id="vm-expo-cap",
        status=TaskStatus.IN_PROGRESS,
        attempt_count=5,  # will become 6 after the reaper retry increment
        last_heartbeat=datetime.utcnow() - timedelta(minutes=7),
        # Empty logs → classify_failure returns AGENT_TIMEOUT → general backoff.
    )
    reaper_db.add(t)
    await reaper_db.commit()

    before = datetime.utcnow()
    await _run_reaper_once()
    await reaper_db.refresh(t)

    assert t.status == TaskStatus.QUEUED
    assert t.attempt_count == 6
    ra = (t.context or {}).get("retry_after")
    assert ra, "retry_after must be set after a reaped retry"
    delay_min = (datetime.fromisoformat(ra) - before).total_seconds() / 60
    # Cap: min(2^5, 15) = 15. Without the cap it would be 32 → clamped at 30 by set_retry_after.
    assert 14 <= delay_min <= 16, f"expected 15-min cap, got {delay_min:.1f}min"


@pytest.mark.asyncio
async def test_per_class_budget_exhaustion_fails_task(
    reaper_db, stop_after_one_cycle, reaper_side_effects
):
    """CODE_ERROR budget is 2; a task reaped at attempt_count=2 terminally fails."""
    project = await _make_project(reaper_db)
    t = Task(
        project_id=project.id,
        payloads=[{"type": "create"}],
        vm_instance_id="vm-budget-exhausted",
        status=TaskStatus.IN_PROGRESS,
        attempt_count=2,  # equals CODE_ERROR budget → can_retry must be False
        last_heartbeat=datetime.utcnow() - timedelta(minutes=7),
        # Plain code-style error that classifies as CODE_ERROR (no special keywords).
        context={"last_error": "NameError: foo is not defined"},
    )
    reaper_db.add(t)
    await reaper_db.commit()

    await _run_reaper_once()
    await reaper_db.refresh(t)

    assert t.status == TaskStatus.FAILED
    assert t.attempt_count == 2, "fail path must not increment attempt_count"
    msg = (t.latest_status_message or "").lower()
    assert "failed after 2 retries" in msg
    assert "code_error" in msg


@pytest.mark.asyncio
async def test_scope_aware_budget_small_code_error_gets_extra_retry(
    reaper_db, stop_after_one_cycle, reaper_side_effects
):
    """scope=small lifts CODE_ERROR budget from 2→3, so attempt_count=2 still retries."""
    project = await _make_project(reaper_db)
    t = Task(
        project_id=project.id,
        payloads=[{"type": "create"}],
        vm_instance_id="vm-small-code-error",
        status=TaskStatus.IN_PROGRESS,
        attempt_count=2,
        last_heartbeat=datetime.utcnow() - timedelta(minutes=7),
        context={"scope": "small", "last_error": "NameError: foo is not defined"},
    )
    reaper_db.add(t)
    await reaper_db.commit()

    await _run_reaper_once()
    await reaper_db.refresh(t)

    assert t.status == TaskStatus.QUEUED
    assert t.attempt_count == 3


@pytest.mark.asyncio
async def test_scope_aware_budget_large_code_error_fails_immediately(
    reaper_db, stop_after_one_cycle, reaper_side_effects
):
    """scope=large cuts CODE_ERROR budget to 1, so attempt_count=1 terminally fails."""
    project = await _make_project(reaper_db)
    t = Task(
        project_id=project.id,
        payloads=[{"type": "create"}],
        vm_instance_id="vm-large-code-error",
        status=TaskStatus.IN_PROGRESS,
        attempt_count=1,
        # scope=large uses a 10-minute heartbeat cutoff, so stale must exceed that.
        last_heartbeat=datetime.utcnow() - timedelta(minutes=11),
        context={"scope": "large", "last_error": "NameError: foo is not defined"},
    )
    reaper_db.add(t)
    await reaper_db.commit()

    await _run_reaper_once()
    await reaper_db.refresh(t)

    assert t.status == TaskStatus.FAILED


# ── Step 5b: retry_after sweep on QUEUED tasks ───────────────────────────


@pytest.mark.asyncio
async def test_retry_after_sweep_clears_unparseable(
    reaper_db, stop_after_one_cycle, reaper_side_effects
):
    project = await _make_project(reaper_db)
    t = Task(
        project_id=project.id,
        payloads=[{"type": "create"}],
        status=TaskStatus.QUEUED,
        context={"retry_after": "not-a-timestamp"},
    )
    reaper_db.add(t)
    await reaper_db.commit()

    await _run_reaper_once()
    await reaper_db.refresh(t)

    assert "retry_after" not in (t.context or {})


@pytest.mark.asyncio
async def test_retry_after_sweep_clears_far_future(
    reaper_db, stop_after_one_cycle, reaper_side_effects
):
    """Anything beyond RETRY_AFTER_MAX_MINUTES (30) must be cleared."""
    project = await _make_project(reaper_db)
    far = (datetime.utcnow() + timedelta(hours=6)).isoformat()
    t = Task(
        project_id=project.id,
        payloads=[{"type": "create"}],
        status=TaskStatus.QUEUED,
        context={"retry_after": far},
    )
    reaper_db.add(t)
    await reaper_db.commit()

    await _run_reaper_once()
    await reaper_db.refresh(t)

    assert "retry_after" not in (t.context or {})


# ── Step 3: EVALUATING parent sweep (re-trigger rollup on stale parents) ──


@pytest.mark.asyncio
async def test_step3_evaluating_parent_with_completed_child_triggers_rollup(
    reaper_db, stop_after_one_cycle, reaper_side_effects, mocker
):
    """Stale EVALUATING parent + COMPLETED child → rollup re-entry with the child."""
    rollup_mock = mocker.patch(
        "app.rollup_management._check_and_trigger_rollup", new_callable=AsyncMock
    )

    project = await _make_project(reaper_db)
    parent = Task(
        project_id=project.id,
        payloads=[{"type": "research"}],
        status=TaskStatus.EVALUATING,
        updated_at=datetime.utcnow() - timedelta(minutes=30),
    )
    reaper_db.add(parent)
    await reaper_db.commit()
    await reaper_db.refresh(parent)

    child = Task(
        project_id=project.id,
        parent_task_id=parent.id,
        payloads=[{"type": "create"}],
        status=TaskStatus.COMPLETED,
    )
    reaper_db.add(child)
    await reaper_db.commit()

    await _run_reaper_once()

    assert rollup_mock.await_count >= 1
    called_child = rollup_mock.await_args_list[0].args[0]
    assert called_child.id == child.id


@pytest.mark.asyncio
async def test_step3_evaluating_parent_without_completed_child_skipped(
    reaper_db, stop_after_one_cycle, reaper_side_effects, mocker
):
    """Stale EVALUATING parent with no COMPLETED child → Step 3 does not call rollup."""
    rollup_mock = mocker.patch(
        "app.rollup_management._check_and_trigger_rollup", new_callable=AsyncMock
    )
    # Prevent Step 4 from rescuing (stub _requeue_parent_task so the assertion is clean).
    mocker.patch("app.task_lifecycle._requeue_parent_task", new_callable=AsyncMock)

    project = await _make_project(reaper_db)
    parent = Task(
        project_id=project.id,
        payloads=[{"type": "research"}],
        status=TaskStatus.EVALUATING,
        updated_at=datetime.utcnow() - timedelta(minutes=30),
    )
    reaper_db.add(parent)
    await reaper_db.commit()
    await reaper_db.refresh(parent)

    child = Task(
        project_id=project.id,
        parent_task_id=parent.id,
        payloads=[{"type": "create"}],
        status=TaskStatus.FAILED,
    )
    reaper_db.add(child)
    await reaper_db.commit()

    await _run_reaper_once()

    assert rollup_mock.await_count == 0


# ── Step 4: Rescue all-children-failed EVALUATING parents ────────────────


@pytest.mark.asyncio
async def test_step4_all_failed_children_triggers_requeue(
    reaper_db, stop_after_one_cycle, reaper_side_effects, mocker
):
    """Stale EVALUATING parent whose children all FAILED → _requeue_parent_task called."""
    mocker.patch(
        "app.rollup_management._check_and_trigger_rollup", new_callable=AsyncMock
    )
    requeue_mock = mocker.patch(
        "app.task_lifecycle._requeue_parent_task", new_callable=AsyncMock
    )

    project = await _make_project(reaper_db)
    parent = Task(
        project_id=project.id,
        payloads=[{"type": "research"}],
        status=TaskStatus.EVALUATING,
        updated_at=datetime.utcnow() - timedelta(minutes=30),
    )
    reaper_db.add(parent)
    await reaper_db.commit()
    await reaper_db.refresh(parent)

    for _ in range(2):
        reaper_db.add(Task(
            project_id=project.id,
            parent_task_id=parent.id,
            payloads=[{"type": "create"}],
            status=TaskStatus.FAILED,
            context={"failure_category": "CODE_ERROR"},
        ))
    await reaper_db.commit()

    await _run_reaper_once()

    assert requeue_mock.await_count == 1
    assert requeue_mock.await_args.args[0].id == parent.id


@pytest.mark.asyncio
async def test_step4_mixed_active_children_not_rescued(
    reaper_db, stop_after_one_cycle, reaper_side_effects, mocker
):
    """Some children still active → Step 4 must not rescue the parent."""
    mocker.patch(
        "app.rollup_management._check_and_trigger_rollup", new_callable=AsyncMock
    )
    requeue_mock = mocker.patch(
        "app.task_lifecycle._requeue_parent_task", new_callable=AsyncMock
    )

    project = await _make_project(reaper_db)
    parent = Task(
        project_id=project.id,
        payloads=[{"type": "research"}],
        status=TaskStatus.EVALUATING,
        updated_at=datetime.utcnow() - timedelta(minutes=30),
    )
    reaper_db.add(parent)
    await reaper_db.commit()
    await reaper_db.refresh(parent)

    reaper_db.add(Task(
        project_id=project.id,
        parent_task_id=parent.id,
        payloads=[{"type": "create"}],
        status=TaskStatus.FAILED,
    ))
    reaper_db.add(Task(
        project_id=project.id,
        parent_task_id=parent.id,
        payloads=[{"type": "create"}],
        status=TaskStatus.IN_PROGRESS,
        vm_instance_id=None,  # avoid Step 2 orphan reap
        last_heartbeat=datetime.utcnow(),
    ))
    await reaper_db.commit()

    await _run_reaper_once()

    assert requeue_mock.await_count == 0


@pytest.mark.asyncio
async def test_step4_diagnostic_summarizes_failure_categories(
    reaper_db, stop_after_one_cycle, reaper_side_effects, mocker
):
    """Rescue diagnostic message must include counts and failure categories from child contexts."""
    mocker.patch(
        "app.rollup_management._check_and_trigger_rollup", new_callable=AsyncMock
    )
    mocker.patch("app.task_lifecycle._requeue_parent_task", new_callable=AsyncMock)

    project = await _make_project(reaper_db)
    parent = Task(
        project_id=project.id,
        payloads=[{"type": "research"}],
        status=TaskStatus.EVALUATING,
        updated_at=datetime.utcnow() - timedelta(minutes=30),
    )
    reaper_db.add(parent)
    await reaper_db.commit()
    await reaper_db.refresh(parent)

    reaper_db.add(Task(
        project_id=project.id, parent_task_id=parent.id,
        payloads=[{"type": "create"}], status=TaskStatus.FAILED,
        context={"failure_category": "CODE_ERROR"},
    ))
    reaper_db.add(Task(
        project_id=project.id, parent_task_id=parent.id,
        payloads=[{"type": "create"}], status=TaskStatus.FAILED,
        context={"failure_category": "AGENT_TIMEOUT"},
    ))
    reaper_db.add(Task(
        project_id=project.id, parent_task_id=parent.id,
        payloads=[{"type": "create"}], status=TaskStatus.CANCELLED,
    ))
    await reaper_db.commit()

    await _run_reaper_once()
    await reaper_db.refresh(parent)

    msg = parent.latest_status_message or ""
    assert "2 failed" in msg
    assert "CODE_ERROR" in msg
    assert "AGENT_TIMEOUT" in msg
    assert "1 cancelled" in msg


@pytest.mark.asyncio
async def test_retry_after_sweep_preserves_valid(
    reaper_db, stop_after_one_cycle, reaper_side_effects
):
    """A retry_after within the 30-min ceiling must be left intact."""
    project = await _make_project(reaper_db)
    valid = (datetime.utcnow() + timedelta(minutes=5)).isoformat()
    t = Task(
        project_id=project.id,
        payloads=[{"type": "create"}],
        status=TaskStatus.QUEUED,
        context={"retry_after": valid},
    )
    reaper_db.add(t)
    await reaper_db.commit()

    await _run_reaper_once()
    await reaper_db.refresh(t)

    assert (t.context or {}).get("retry_after") == valid


# ── skip_locked / force-lock mechanism (Step 2) ──────────────────────────

class _FakeResult:
    """Minimal stand-in for an async result so scalars().all()/first() work."""
    def __init__(self, items):
        self._items = list(items)
    def scalars(self):
        return self
    def all(self):
        return list(self._items)
    def first(self):
        return self._items[0] if self._items else None


@pytest.fixture()
def clean_skip_counts():
    _reaper_skip_counts.clear()
    yield _reaper_skip_counts
    _reaper_skip_counts.clear()


def _install_lock_filter(mocker, skip_ids, *, force_raises=False):
    """Patch AsyncSession.execute so for_update queries skip rows in skip_ids on
    the first call (simulating skip_locked contention) and either succeed
    or raise on the second call (the force-lock query)."""
    real_execute = AsyncSession.execute
    state = {"for_update_calls": 0}

    async def mock_execute(self, stmt, *args, **kwargs):
        # SQLite doesn't understand "SET LOCAL lock_timeout"; short-circuit it.
        if isinstance(stmt, TextClause):
            return _FakeResult([])
        if getattr(stmt, "_for_update_arg", None) is not None:
            state["for_update_calls"] += 1
            if state["for_update_calls"] == 1:
                # Skip-locked contention: filter out skip_ids
                result = await real_execute(self, stmt, *args, **kwargs)
                rows = result.scalars().all()
                return _FakeResult([r for r in rows if getattr(r, "id", None) not in skip_ids])
            # Second for_update call is the force-lock query.
            if force_raises:
                raise RuntimeError("force-lock timeout")
            result = await real_execute(self, stmt, *args, **kwargs)
            return _FakeResult(result.scalars().all())
        return await real_execute(self, stmt, *args, **kwargs)

    mocker.patch.object(AsyncSession, "execute", mock_execute)
    return state


@pytest.mark.asyncio
async def test_skip_locked_adds_to_skip_counts(
    reaper_db, stop_after_one_cycle, reaper_side_effects, clean_skip_counts, mocker
):
    """Orphans whose lock is skipped accumulate in _reaper_skip_counts."""
    project = await _make_project(reaper_db)
    t = Task(
        project_id=project.id,
        payloads=[{"type": "create"}],
        vm_instance_id="vm-skip",
        status=TaskStatus.IN_PROGRESS,
        attempt_count=0,
        last_heartbeat=datetime.utcnow() - timedelta(minutes=10),
    )
    reaper_db.add(t)
    await reaper_db.commit()
    _install_lock_filter(mocker, {t.id})

    await _run_reaper_once()
    await reaper_db.refresh(t)

    assert clean_skip_counts.get(t.id) == 1
    assert t.status == TaskStatus.IN_PROGRESS, "skipped task must not be reaped this cycle"


@pytest.mark.asyncio
async def test_force_lock_triggered_after_five_skips(
    reaper_db, stop_after_one_cycle, reaper_side_effects, clean_skip_counts, mocker
):
    """With skip count already at 4, one more skip triggers force-lock; on
    success the task is reaped and its skip-count entry is cleared."""
    project = await _make_project(reaper_db)
    t = Task(
        project_id=project.id,
        payloads=[{"type": "create"}],
        vm_instance_id="vm-force",
        status=TaskStatus.IN_PROGRESS,
        attempt_count=0,
        last_heartbeat=datetime.utcnow() - timedelta(minutes=10),
    )
    reaper_db.add(t)
    await reaper_db.commit()
    clean_skip_counts[t.id] = 4  # threshold is 5
    state = _install_lock_filter(mocker, {t.id})

    await _run_reaper_once()
    await reaper_db.refresh(t)

    assert state["for_update_calls"] >= 2, "force-lock query must run after skip threshold"
    assert t.id not in clean_skip_counts, "skip count cleared once force-lock succeeds"
    assert t.status == TaskStatus.QUEUED, "force-locked task gets reaped/retried like any orphan"
    assert t.attempt_count == 1


@pytest.mark.asyncio
async def test_force_lock_timeout_resets_skip_count(
    reaper_db, stop_after_one_cycle, reaper_side_effects, clean_skip_counts, mocker
):
    """If the force-lock query raises, the skip count for that id is popped
    so the next cycle retries from scratch."""
    project = await _make_project(reaper_db)
    t = Task(
        project_id=project.id,
        payloads=[{"type": "create"}],
        vm_instance_id="vm-force-timeout",
        status=TaskStatus.IN_PROGRESS,
        attempt_count=0,
        last_heartbeat=datetime.utcnow() - timedelta(minutes=10),
    )
    reaper_db.add(t)
    await reaper_db.commit()
    clean_skip_counts[t.id] = 4
    _install_lock_filter(mocker, {t.id}, force_raises=True)

    await _run_reaper_once()
    await reaper_db.refresh(t)

    assert t.id not in clean_skip_counts, "force-lock failure must pop skip-count entry"
    assert t.status == TaskStatus.IN_PROGRESS, "task remains active until next cycle succeeds"


@pytest.mark.asyncio
async def test_heartbeat_refresh_between_step1_and_step2_not_reaped(
    reaper_db, stop_after_one_cycle, reaper_side_effects, mocker
):
    """Race: task is flagged orphaned in Step 1 (heartbeat 6 min old) but
    receives a fresh heartbeat before Step 2's FOR UPDATE lock acquires. The
    re-verification after expire_all()+lock must see the fresh heartbeat and
    skip the reap — status stays IN_PROGRESS and retry budget is untouched."""
    from sqlalchemy import update as _sa_update

    project = await _make_project(reaper_db)
    t = Task(
        project_id=project.id,
        payloads=[{"type": "create"}],
        vm_instance_id="vm-race-refresh",
        status=TaskStatus.IN_PROGRESS,
        attempt_count=0,
        last_heartbeat=datetime.utcnow() - timedelta(minutes=6),
    )
    reaper_db.add(t)
    await reaper_db.commit()
    task_id = t.id

    # Patch AsyncSession.execute: when the Step-2 FOR UPDATE query fires, first
    # update last_heartbeat to "now" and commit. expire_all() ran before the
    # lock query, so the lock's fresh SELECT returns the refreshed row.
    real_execute = AsyncSession.execute
    refreshed = {"done": False}
    fresh_ts = datetime.utcnow()

    async def mock_execute(self, stmt, *args, **kwargs):
        if not refreshed["done"] and getattr(stmt, "_for_update_arg", None) is not None:
            refreshed["done"] = True
            await real_execute(
                self,
                _sa_update(Task).where(Task.id == task_id).values(last_heartbeat=fresh_ts),
            )
            await self.commit()
        return await real_execute(self, stmt, *args, **kwargs)

    mocker.patch.object(AsyncSession, "execute", mock_execute)

    await _run_reaper_once()
    await reaper_db.refresh(t)

    assert refreshed["done"], "for_update lock query must have fired (race injection point)"
    assert t.status == TaskStatus.IN_PROGRESS, "task with refreshed heartbeat must NOT be reaped"
    assert t.attempt_count == 0, "retry budget must not be burned when Step 2 re-verify skips reap"
    assert t.last_heartbeat == fresh_ts, "refreshed heartbeat must be persisted"


@pytest.mark.asyncio
async def test_skip_count_stale_entries_cleaned(
    reaper_db, stop_after_one_cycle, reaper_side_effects, clean_skip_counts
):
    """Entries in _reaper_skip_counts whose ids aren't in the current orphaned
    set are removed (prevents unbounded growth)."""
    stale_id = _uuid_mod.uuid4()
    clean_skip_counts[stale_id] = 3

    project = await _make_project(reaper_db)
    t = Task(
        project_id=project.id,
        payloads=[{"type": "create"}],
        vm_instance_id="vm-live",
        status=TaskStatus.IN_PROGRESS,
        attempt_count=0,
        last_heartbeat=datetime.utcnow() - timedelta(minutes=10),
    )
    reaper_db.add(t)
    await reaper_db.commit()

    await _run_reaper_once()

    assert stale_id not in clean_skip_counts


# ── Step 6: QUEUED age alert tier escalation ─────────────────────────────


@pytest.mark.asyncio
async def test_step6_tier1_info_alert_fires_past_threshold(
    reaper_db, stop_after_one_cycle, reaper_side_effects
):
    """QUEUED task older than 1× threshold (2h) → tier-1 INFO alert with correct
    title/message, and tier persisted in task.context."""
    project = await _make_project(reaper_db)
    t = Task(
        project_id=project.id,
        title="stalled-task",
        payloads=[{"type": "create"}],
        status=TaskStatus.QUEUED,
        created_at=datetime.utcnow() - timedelta(hours=2, minutes=30),
    )
    reaper_db.add(t)
    await reaper_db.commit()

    await _run_reaper_once()
    await reaper_db.refresh(t)

    alerts = (await reaper_db.execute(
        _select(Alert).where(Alert.task_id == t.id)
    )).scalars().all()
    assert len(alerts) == 1
    assert alerts[0].severity == AlertSeverity.INFO
    assert "stalled-task" in alerts[0].title
    assert "Task stalled in queue" in alerts[0].title
    assert "tier 1" in alerts[0].message
    assert "Queued for" in alerts[0].message
    assert (t.context or {}).get("queued_age_alert_tier") == 1


@pytest.mark.asyncio
async def test_step6_tier2_warning_skips_tier1_when_age_exceeds_3x(
    reaper_db, stop_after_one_cycle, reaper_side_effects
):
    """A task older than 3× threshold (6h) goes straight to tier-2 WARNING
    without first firing a tier-1 INFO alert."""
    project = await _make_project(reaper_db)
    t = Task(
        project_id=project.id,
        payloads=[{"type": "create"}],
        status=TaskStatus.QUEUED,
        created_at=datetime.utcnow() - timedelta(hours=7),
    )
    reaper_db.add(t)
    await reaper_db.commit()

    await _run_reaper_once()
    await reaper_db.refresh(t)

    alerts = (await reaper_db.execute(
        _select(Alert).where(Alert.task_id == t.id)
    )).scalars().all()
    assert len(alerts) == 1
    assert alerts[0].severity == AlertSeverity.WARNING
    assert "tier 2" in alerts[0].message
    assert (t.context or {}).get("queued_age_alert_tier") == 2


@pytest.mark.asyncio
async def test_step6_duplicate_tier1_suppressed(
    reaper_db, stop_after_one_cycle, reaper_side_effects
):
    """A task whose context already records queued_age_alert_tier=1 must not
    re-fire a tier-1 alert on the same age bucket."""
    project = await _make_project(reaper_db)
    t = Task(
        project_id=project.id,
        payloads=[{"type": "create"}],
        status=TaskStatus.QUEUED,
        created_at=datetime.utcnow() - timedelta(hours=3),
        context={"queued_age_alert_tier": 1},
    )
    reaper_db.add(t)
    await reaper_db.commit()

    await _run_reaper_once()

    alerts = (await reaper_db.execute(
        _select(Alert).where(Alert.task_id == t.id)
    )).scalars().all()
    assert alerts == []


@pytest.mark.asyncio
async def test_step6_requires_approval_sets_likely_cause(
    reaper_db, stop_after_one_cycle, reaper_side_effects
):
    """When the task has requires_approval=True, the alert message diagnoses
    the cause as awaiting operator approval."""
    project = await _make_project(reaper_db)
    t = Task(
        project_id=project.id,
        payloads=[{"type": "create"}],
        status=TaskStatus.QUEUED,
        requires_approval=True,
        created_at=datetime.utcnow() - timedelta(hours=2, minutes=30),
    )
    reaper_db.add(t)
    await reaper_db.commit()

    await _run_reaper_once()

    alerts = (await reaper_db.execute(
        _select(Alert).where(Alert.task_id == t.id)
    )).scalars().all()
    assert len(alerts) == 1
    assert "awaiting operator approval" in alerts[0].message


# ── Terminal failure finalization (non-provision budget exhausted) ────────


@pytest.mark.asyncio
async def test_terminal_failure_non_provision_finalization(
    reaper_db, stop_after_one_cycle, reaper_side_effects, mocker
):
    """IN_PROGRESS task with attempt_count >= class budget reaches the terminal
    failure path: status → FAILED, failure_category persisted, alert invoked,
    broadcast sent, and the status message carries class + retry count."""
    alert_mock = mocker.patch(
        "app.main._maybe_create_failure_alert", new_callable=AsyncMock
    )
    broadcast_mock = mocker.patch(
        "app.broadcast.broadcast_to_project", new_callable=AsyncMock
    )
    record_mock = mocker.patch(
        "app.routers.agent_status.record_task_result"
    )

    project = await _make_project(reaper_db)
    # latest_status_message falls through classify_failure to CODE_ERROR (budget=2),
    # so attempt_count=2 forces can_retry=False → terminal failure branch.
    t = Task(
        project_id=project.id,
        payloads=[{"type": "create"}],
        vm_instance_id="vm-terminal",
        status=TaskStatus.IN_PROGRESS,
        attempt_count=2,
        last_heartbeat=datetime.utcnow() - timedelta(minutes=10),
        latest_status_message="TypeError: 'NoneType' object is not subscriptable",
        context={"last_error": "TypeError in handler.py"},
    )
    reaper_db.add(t)
    await reaper_db.commit()

    await _run_reaper_once()
    await reaper_db.refresh(t)

    assert t.status == TaskStatus.FAILED

    ctx = t.context or {}
    # persist_failure_category writes the class returned by the terminal-path
    # classify_failure call. Budget exhaustion is decided by the earlier call
    # (CODE_ERROR), but the terminal call re-classifies after the reaper
    # rewrites latest_status_message, so the persisted class reflects the
    # refreshed signal rather than the budget-triggering class.
    persisted = ctx.get("failure_category")
    assert persisted in {"CODE_ERROR", "AGENT_TIMEOUT", "EXTERNAL_API", "RATE_LIMIT", "PROVISION_FAILURE"}, \
        f"failure_category must be persisted as a canonical class, got {persisted!r}"

    alert_mock.assert_awaited()
    assert alert_mock.await_args.args[0].id == t.id

    record_mock.assert_called_once()
    kwargs = record_mock.call_args.kwargs
    assert kwargs["success"] is False
    assert kwargs["failure_class"] == persisted, \
        "record_task_result must receive the same class that was persisted"
    assert kwargs["task_id"] == str(t.id)
    assert kwargs["task_type"] == "create"

    failed_broadcasts = [
        call for call in broadcast_mock.await_args_list
        if call.args[1] == "task_updated"
        and call.args[2].get("status") == TaskStatus.FAILED
    ]
    assert failed_broadcasts, "expected a task_updated broadcast with status FAILED"
    assert failed_broadcasts[-1].args[0] == str(project.id)

    msg = (t.latest_status_message or "")
    # fail_reason ("failed after {N} retries ({class})") is passed through
    # str.capitalize(), which lowercases everything after the first char.
    assert "code_error" in msg.lower(), \
        f"status message should carry budget class: {msg!r}"
    assert "2 retries" in msg, \
        f"status message should carry retry count: {msg!r}"


@pytest.mark.asyncio
async def test_terminal_failure_before_budget_exhausted_not_failed(
    reaper_db, stop_after_one_cycle, reaper_side_effects, mocker
):
    """Guard: with attempt_count below the class budget, the reaper retries
    (QUEUED) instead of finalizing. Ensures the terminal test above isolates
    the budget-exhaustion branch rather than always-failing behavior."""
    alert_mock = mocker.patch(
        "app.main._maybe_create_failure_alert", new_callable=AsyncMock
    )
    mocker.patch("app.broadcast.broadcast_to_project", new_callable=AsyncMock)

    project = await _make_project(reaper_db)
    t = Task(
        project_id=project.id,
        payloads=[{"type": "create"}],
        vm_instance_id="vm-still-retrying",
        status=TaskStatus.IN_PROGRESS,
        attempt_count=0,
        last_heartbeat=datetime.utcnow() - timedelta(minutes=10),
        latest_status_message="TypeError: 'NoneType' object is not subscriptable",
    )
    reaper_db.add(t)
    await reaper_db.commit()

    await _run_reaper_once()
    await reaper_db.refresh(t)

    assert t.status == TaskStatus.QUEUED
    assert (t.context or {}).get("failure_category") is None
    alert_mock.assert_not_awaited()


# ── Step 5: Infrastructure recovery resurrection ─────────────────────────


@pytest.mark.asyncio
async def test_step5_provision_failure_resurrected_when_zone_available(
    reaper_db, stop_after_one_cycle, reaper_side_effects
):
    """FAILED + failure_category=PROVISION_FAILURE → resurrected to QUEUED
    when any_zone_available() is True; infra context fields are cleared and
    _resurrection_count is incremented."""
    # reaper_side_effects already patches any_zone_available → True.
    project = await _make_project(reaper_db)
    t = Task(
        project_id=project.id,
        payloads=[{"type": "create"}],
        status=TaskStatus.FAILED,
        attempt_count=4,
        context={
            "failure_category": "PROVISION_FAILURE",
            "provision_failures": 3,
            "provision_failures_infra": 2,
            "_infra_wait": True,
            "blacklisted_accounts": ["acct-1"],
            "max_plan_account_id": "acct-1",
            "retry_after": "2030-01-01T00:00:00",
            "_slot_released": True,
            "_api_failure_retries": 4,
        },
    )
    reaper_db.add(t)
    await reaper_db.commit()

    await _run_reaper_once()
    await reaper_db.refresh(t)

    ctx = t.context or {}
    assert t.status == TaskStatus.QUEUED
    assert t.attempt_count == 0
    assert ctx.get("_resurrection_count") == 1
    assert ctx.get("_resurrected_from") == "PROVISION_FAILURE"
    for cleared in (
        "provision_failures",
        "provision_failures_infra",
        "_infra_wait",
        "blacklisted_accounts",
        "max_plan_account_id",
        "retry_after",
        "_slot_released",
        "failure_category",
        "_api_failure_retries",
    ):
        assert cleared not in ctx, f"expected {cleared} cleared on resurrection"


@pytest.mark.asyncio
async def test_step5_rate_limit_resurrected_when_account_available(
    reaper_db, stop_after_one_cycle, reaper_side_effects, mocker
):
    """FAILED + failure_category=RATE_LIMIT → resurrected when at least one
    non-blacklisted account is available for the task's model."""
    mocker.patch(
        "app.max_plan_pool._all_accounts",
        return_value=[{"id": "acct-ok"}, {"id": "acct-bad"}],
    )
    mocker.patch(
        "app.max_plan_pool._account_available_for_model",
        side_effect=lambda acct, model: acct.get("id") == "acct-ok",
    )

    project = await _make_project(reaper_db)
    t = Task(
        project_id=project.id,
        payloads=[{"type": "create", "model": "sonnet"}],
        status=TaskStatus.FAILED,
        attempt_count=2,
        context={
            "failure_category": "RATE_LIMIT",
            "blacklisted_accounts": ["acct-bad"],
        },
    )
    reaper_db.add(t)
    await reaper_db.commit()

    await _run_reaper_once()
    await reaper_db.refresh(t)

    assert t.status == TaskStatus.QUEUED
    assert (t.context or {}).get("_resurrection_count") == 1
    assert (t.context or {}).get("_resurrected_from") == "RATE_LIMIT"


@pytest.mark.asyncio
async def test_step5_rate_limit_not_resurrected_when_all_accounts_blacklisted(
    reaper_db, stop_after_one_cycle, reaper_side_effects, mocker
):
    """RATE_LIMIT with only blacklisted accounts available → NOT resurrected."""
    mocker.patch(
        "app.max_plan_pool._all_accounts",
        return_value=[{"id": "acct-bad"}],
    )
    mocker.patch(
        "app.max_plan_pool._account_available_for_model",
        return_value=True,
    )

    project = await _make_project(reaper_db)
    t = Task(
        project_id=project.id,
        payloads=[{"type": "create", "model": "sonnet"}],
        status=TaskStatus.FAILED,
        context={
            "failure_category": "RATE_LIMIT",
            "blacklisted_accounts": ["acct-bad"],
        },
    )
    reaper_db.add(t)
    await reaper_db.commit()

    await _run_reaper_once()
    await reaper_db.refresh(t)

    assert t.status == TaskStatus.FAILED
    assert "_resurrection_count" not in (t.context or {})


@pytest.mark.asyncio
async def test_step5_resurrection_count_guard_skips_second_attempt(
    reaper_db, stop_after_one_cycle, reaper_side_effects
):
    """Task with _resurrection_count >= 1 must not be resurrected again."""
    project = await _make_project(reaper_db)
    t = Task(
        project_id=project.id,
        payloads=[{"type": "create"}],
        status=TaskStatus.FAILED,
        context={
            "failure_category": "PROVISION_FAILURE",
            "_resurrection_count": 1,
        },
    )
    reaper_db.add(t)
    await reaper_db.commit()

    await _run_reaper_once()
    await reaper_db.refresh(t)

    assert t.status == TaskStatus.FAILED
    assert (t.context or {}).get("_resurrection_count") == 1


@pytest.mark.asyncio
async def test_step5_parent_task_not_resurrected(
    reaper_db, stop_after_one_cycle, reaper_side_effects
):
    """A FAILED parent with child rows must not be resurrected — parents
    recover through Step 4, not Step 5."""
    project = await _make_project(reaper_db)
    parent = Task(
        project_id=project.id,
        payloads=[{"type": "research"}],
        status=TaskStatus.FAILED,
        context={"failure_category": "PROVISION_FAILURE"},
    )
    reaper_db.add(parent)
    await reaper_db.commit()
    await reaper_db.refresh(parent)

    reaper_db.add(Task(
        project_id=project.id,
        parent_task_id=parent.id,
        payloads=[{"type": "create"}],
        status=TaskStatus.COMPLETED,
    ))
    await reaper_db.commit()

    await _run_reaper_once()
    await reaper_db.refresh(parent)

    assert parent.status == TaskStatus.FAILED
    assert "_resurrection_count" not in (parent.context or {})


@pytest.mark.asyncio
async def test_step5_resurrection_creates_info_alert(
    reaper_db, stop_after_one_cycle, reaper_side_effects
):
    """Each resurrected task gets an INFO alert referencing its failure class."""
    project = await _make_project(reaper_db)
    t = Task(
        project_id=project.id,
        title="resurrected-task",
        payloads=[{"type": "create"}],
        status=TaskStatus.FAILED,
        context={"failure_category": "PROVISION_FAILURE"},
    )
    reaper_db.add(t)
    await reaper_db.commit()

    await _run_reaper_once()

    alerts = (await reaper_db.execute(
        _select(Alert).where(Alert.task_id == t.id)
    )).scalars().all()
    assert len(alerts) == 1
    assert alerts[0].severity == AlertSeverity.INFO
    assert "PROVISION_FAILURE" in (alerts[0].message or "")


# ── Dependent-task unblocking on terminal failure ────────────────────────


def _install_dep_unblock_compat(mocker, blocked_id_str: str):
    """Replace app.reaper.AsyncSessionLocal with a smart factory that routes
    the dep-unblock session through a Python-level filter (SQLite lacks the
    JSONB @> operator used by Task.depends_on.contains(...)). The first call
    (main reaper session) returns the underlying session unchanged; every
    subsequent call returns a proxy that rewrites the dep-unblock query."""
    import app.reaper as _reaper_mod

    real_asl = _reaper_mod.AsyncSessionLocal
    call_count = {"n": 0}

    class _DepSessionProxy:
        def __init__(self, inner):
            self._inner = inner

        async def execute(self, stmt, *args, **kwargs):
            # The dep-unblock query is the only one the proxy sees. Re-issue
            # it without the JSONB .contains() predicate and filter in Python.
            plain_stmt = select(Task).where(Task.status == TaskStatus.QUEUED)
            result = await self._inner.execute(plain_stmt)
            all_queued = result.scalars().all()
            matching = [t for t in all_queued if blocked_id_str in (t.depends_on or [])]

            class _FakeResult:
                def scalars(self_):
                    class _FakeScalars:
                        def all(self_2):
                            return matching
                    return _FakeScalars()
            return _FakeResult()

        def add(self, obj):
            self._inner.add(obj)

        async def commit(self):
            await self._inner.commit()

        async def __aenter__(self):
            await self._inner.__aenter__()
            return self

        async def __aexit__(self, *args):
            return await self._inner.__aexit__(*args)

    class _SmartASL:
        def __call__(self):
            call_count["n"] += 1
            inner = real_asl()
            if call_count["n"] == 1:
                return inner
            return _DepSessionProxy(inner)

    mocker.patch("app.reaper.AsyncSessionLocal", _SmartASL())
    return call_count


@pytest.mark.asyncio
async def test_dep_unblocking_removes_failed_id_from_queued_dependent(
    reaper_db, stop_after_one_cycle, reaper_side_effects, mocker
):
    """When a task FAILs terminally, a QUEUED task with its ID in depends_on
    must have that ID removed."""
    # Disable the Step 5 infra-recovery resurrection so the task stays FAILED.
    mocker.patch("app.reaper.any_zone_available", return_value=False)

    project = await _make_project(reaper_db)
    failing = Task(
        project_id=project.id,
        payloads=[{"type": "create"}],
        vm_instance_id="vm-dep-fail",
        status=TaskStatus.IN_PROGRESS,
        attempt_count=3,  # At max_retries budget — reaper will mark FAILED, not retry.
        last_heartbeat=datetime.utcnow() - timedelta(minutes=10),
    )
    reaper_db.add(failing)
    await reaper_db.commit()
    await reaper_db.refresh(failing)

    waiting = Task(
        project_id=project.id,
        payloads=[{"type": "create"}],
        status=TaskStatus.QUEUED,
        depends_on=[str(failing.id)],
    )
    reaper_db.add(waiting)
    await reaper_db.commit()
    await reaper_db.refresh(waiting)

    _install_dep_unblock_compat(mocker, str(failing.id))

    await _run_reaper_once()
    await reaper_db.refresh(failing)
    await reaper_db.refresh(waiting)

    assert failing.status == TaskStatus.FAILED
    assert str(failing.id) not in (waiting.depends_on or [])


@pytest.mark.asyncio
async def test_dep_unblocking_clears_multiple_dependents_in_one_pass(
    reaper_db, stop_after_one_cycle, reaper_side_effects, mocker
):
    """A single terminal failure unblocks every dependent in one cycle."""
    mocker.patch("app.reaper.any_zone_available", return_value=False)

    project = await _make_project(reaper_db)
    failing = Task(
        project_id=project.id,
        payloads=[{"type": "create"}],
        vm_instance_id="vm-multi-dep",
        status=TaskStatus.IN_PROGRESS,
        attempt_count=3,
        last_heartbeat=datetime.utcnow() - timedelta(minutes=10),
    )
    reaper_db.add(failing)
    await reaper_db.commit()
    await reaper_db.refresh(failing)

    other_dep_id = str(_uuid_mod.uuid4())
    waiters = [
        Task(
            project_id=project.id,
            payloads=[{"type": "create"}],
            status=TaskStatus.QUEUED,
            depends_on=[str(failing.id)],
        ),
        Task(
            project_id=project.id,
            payloads=[{"type": "create"}],
            status=TaskStatus.QUEUED,
            # Has an extra unrelated dep — only the failed ID should be removed.
            depends_on=[str(failing.id), other_dep_id],
        ),
        Task(
            project_id=project.id,
            payloads=[{"type": "create"}],
            status=TaskStatus.QUEUED,
            depends_on=[str(failing.id)],
        ),
    ]
    for w in waiters:
        reaper_db.add(w)
    await reaper_db.commit()
    for w in waiters:
        await reaper_db.refresh(w)

    _install_dep_unblock_compat(mocker, str(failing.id))

    await _run_reaper_once()
    for w in waiters:
        await reaper_db.refresh(w)

    for w in waiters:
        assert str(failing.id) not in (w.depends_on or [])
    # Unrelated dep on the second waiter must be preserved.
    assert other_dep_id in (waiters[1].depends_on or [])


@pytest.mark.asyncio
async def test_dep_unblocking_leaves_unrelated_queued_task_alone(
    reaper_db, stop_after_one_cycle, reaper_side_effects, mocker
):
    """A QUEUED task that does not depend on the failed task must be untouched."""
    mocker.patch("app.reaper.any_zone_available", return_value=False)

    project = await _make_project(reaper_db)
    failing = Task(
        project_id=project.id,
        payloads=[{"type": "create"}],
        vm_instance_id="vm-unrelated",
        status=TaskStatus.IN_PROGRESS,
        attempt_count=3,
        last_heartbeat=datetime.utcnow() - timedelta(minutes=10),
    )
    reaper_db.add(failing)
    await reaper_db.commit()
    await reaper_db.refresh(failing)

    unrelated_dep_id = str(_uuid_mod.uuid4())
    unrelated = Task(
        project_id=project.id,
        payloads=[{"type": "create"}],
        status=TaskStatus.QUEUED,
        depends_on=[unrelated_dep_id],
    )
    reaper_db.add(unrelated)
    await reaper_db.commit()
    await reaper_db.refresh(unrelated)

    _install_dep_unblock_compat(mocker, str(failing.id))

    await _run_reaper_once()
    await reaper_db.refresh(unrelated)

    assert unrelated.status == TaskStatus.QUEUED
    assert unrelated.depends_on == [unrelated_dep_id]


@pytest.mark.asyncio
async def test_dep_unblocking_skipped_when_task_is_retried_not_failed(
    reaper_db, stop_after_one_cycle, reaper_side_effects, mocker
):
    """A task whose reap flips it back to QUEUED (retry, not terminal) must
    not trigger dependent unblocking — dependents remain blocked."""
    # any_zone_available already True via reaper_side_effects, but retry path
    # doesn't depend on it; kept default.

    project = await _make_project(reaper_db)
    retrying = Task(
        project_id=project.id,
        payloads=[{"type": "create"}],
        vm_instance_id="vm-retry",
        status=TaskStatus.IN_PROGRESS,
        attempt_count=0,  # Fresh attempt — reaper will QUEUE for retry, not FAIL.
        last_heartbeat=datetime.utcnow() - timedelta(minutes=10),
    )
    reaper_db.add(retrying)
    await reaper_db.commit()
    await reaper_db.refresh(retrying)

    waiting = Task(
        project_id=project.id,
        payloads=[{"type": "create"}],
        status=TaskStatus.QUEUED,
        depends_on=[str(retrying.id)],
    )
    reaper_db.add(waiting)
    await reaper_db.commit()
    await reaper_db.refresh(waiting)

    call_tracker = _install_dep_unblock_compat(mocker, str(retrying.id))

    await _run_reaper_once()
    await reaper_db.refresh(retrying)
    await reaper_db.refresh(waiting)

    # Reaped into retry (QUEUED) — no terminal FAIL, so dep-unblock must not run.
    assert retrying.status == TaskStatus.QUEUED
    assert retrying.attempt_count == 1
    assert waiting.depends_on == [str(retrying.id)]
    # Only the main reaper session should have opened — never the dep-session.
    assert call_tracker["n"] == 1


def test_resolve_retry_budget_external_api_scope_aware():
    """EXTERNAL_API has scope-specific budgets; missing scope falls back to uniform."""
    from app.shared_state import resolve_retry_budget, runtime_config

    assert resolve_retry_budget("EXTERNAL_API", "small") == 3
    assert resolve_retry_budget("EXTERNAL_API", "medium") == 4
    assert resolve_retry_budget("EXTERNAL_API", "large") == 5

    uniform = runtime_config.get("retry_budgets_by_class", {}).get(
        "EXTERNAL_API", runtime_config["max_retries"]
    )
    assert resolve_retry_budget("EXTERNAL_API", None) == uniform
    assert resolve_retry_budget("EXTERNAL_API", "unknown_scope") == uniform


def test_transient_5xx_patterns_constant():
    from app.reaper import _TRANSIENT_5XX_PATTERNS

    assert isinstance(_TRANSIENT_5XX_PATTERNS, frozenset)
    for p in ("http 502", "http 503", "http 504",
              "502 bad gateway", "503 service", "504 gateway",
              "server error"):
        assert p in _TRANSIENT_5XX_PATTERNS

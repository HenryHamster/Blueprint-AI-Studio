"""Targeted unit tests for app/reaper.py.

Focus: the five high-risk branches of reaper logic — orphan detection state
machine, provision failure classification (infra vs task-specific), rate-limit
requeue path, the retry_after sweep (Step 5b), and the
_copy_analysis_artifacts_to_previous helper.
"""
import pytest
import pytest_asyncio
from datetime import datetime, timedelta
from unittest.mock import AsyncMock
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool
from sqlmodel import SQLModel

from app.models import Project, Task, TaskStatus
from app.reaper import reaper_service, _copy_analysis_artifacts_to_previous


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

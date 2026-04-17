"""Targeted unit tests for app/reaper.py.

Focus: the five high-risk branches of reaper logic — orphan detection state
machine, provision failure classification (infra vs task-specific), rate-limit
requeue path, the retry_after sweep (Step 5b), and the
_copy_analysis_artifacts_to_previous helper.
"""
import uuid as _uuid_mod
import pytest
import pytest_asyncio
from datetime import datetime, timedelta
from unittest.mock import AsyncMock
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool
from sqlalchemy.sql.elements import TextClause
from sqlmodel import SQLModel

from app.models import Project, Task, TaskStatus
from app.reaper import reaper_service, _copy_analysis_artifacts_to_previous, _reaper_skip_counts


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

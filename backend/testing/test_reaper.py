"""Targeted unit tests for app/reaper.py.

Focus: the high-risk branches of reaper logic — orphan detection state
machine, provision failure classification (infra vs task-specific), rate-limit
requeue path, non-provision failure classification & retry-delay selection,
the retry_after sweep (Step 5b), the _copy_analysis_artifacts_to_previous
helper, and the dependent-task unblocking pass that runs after terminal
failures.
"""

import contextlib
import asyncio
import uuid as _uuid_mod
import pytest
import pytest_asyncio
from datetime import datetime, timedelta
from unittest.mock import AsyncMock
from sqlalchemy import inspect, select
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool
from sqlalchemy.sql.elements import TextClause
from sqlmodel import SQLModel

from sqlmodel import select as _select

from testing.conftest import USE_POSTGRES

from app.capability_registry import derive_provider_marker
from app.models import (
    Alert,
    AlertSeverity,
    Artifact,
    ArtifactType,
    Project,
    ReleaseVersion,
    StrategicCycle,
    Task,
    TaskStatus,
)
from app.reaper_evaluating_sweeps import (
    _snap_all_terminal,
    _snap_any_completed,
)
from app.reaper import (
    reaper_service,
    _copy_analysis_artifacts_to_previous,
    _reaper_skip_counts,
    _orphan_reap_crash_streaks,
    _ORPHAN_REAP_CRASH_STREAK_LIMIT,
    _heartbeat_stale_cutoff,
    _progress_stale_cutoff,
    _reaper_halt_state,
    _REAPER_CYCLE_HALT_THRESHOLD,
    _REAPER_HALF_OPEN_PROBE_LIMIT,
    get_reaper_stats,
    is_reaper_halted,
    _mark_reaper_progress,
    _orphan_priority_key,
    _select_orphans_for_cycle,
    _classify_orphan,
    _relock_orphan_task,
    _REAPER_ORPHAN_LOOP_TIME_BUDGET_DEFAULT,
    _ORPHAN_PRIORITY_RATE_LIMITED,
    _ORPHAN_PRIORITY_SPOT,
    _ORPHAN_PRIORITY_PROVISION,
    _ORPHAN_PRIORITY_DEFAULT,
    _normalize_reaper_retry_failure_class,
    _build_active_scan_stmt,
    _startup_orphan_grace_remaining,
)
from app.reaper_orphan_classify import (
    _RUNNER_REPORT_CLAIM_GRACE_SECONDS,
    _classify_resident_session,
    _runner_report_excludes_task,
)
from app.retry_prompt import _build_retry_prompt_suffix
from app.shared_state import runtime_config as _runtime_config
from app.strategic_resident_session import RESIDENT_SESSION_KEY
import app.reaper_sweeps as _rs
import app.strategic_cycle as _strategic_cycle
from app.reaper_sweeps import event_driven_unblock_on_knowledge
from app.time_utils import _utcnow
from app.workbench_state import (
    STRATEGIC_LOCAL_RUNNER_HOLD_CONTEXT_KEY,
    STRATEGIC_LOCAL_RUNNER_REQUIRED_CONTEXT_KEY,
)


def test_heartbeat_stale_cutoff_respects_runtime_config_override():
    """_heartbeat_stale_cutoff reads per-scope thresholds from runtime_config."""
    original = _runtime_config.get("heartbeat_stale_seconds_by_scope")
    try:
        _runtime_config["heartbeat_stale_seconds_by_scope"] = {
            "small": 60,
            "medium": 120,
            "large": 900,
        }

        class _T:
            context = {"scope": "large"}  # noqa: RUF012
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


def test_reaper_restart_scope_overflow_raw_lowercase_label_escalates():
    retry_ctx = {"failure_category": "scope_overflow", "scope": "small"}

    retry_class = _normalize_reaper_retry_failure_class("CODE_ERROR", retry_ctx)
    guidance, scope_override = _build_retry_prompt_suffix(
        retry_class,
        "agent reported lower-case scope_overflow",
        1,
        "small",
        retry_ctx,
    )

    assert retry_class == "SCOPE_OVERFLOW"
    assert scope_override == "medium"
    assert "file-count budget" in guidance


def test_heartbeat_stale_cutoff_effort_floor_extends_small_scope_max_effort():
    """Effort floor (max=720s) extends small-scope cutoff (300s) when effort is higher."""

    class _T:
        context = {"scope": "small", "effort": "max"}  # noqa: RUF012
        first_payload = None

    now = datetime(2026, 1, 1, 12, 0, 0)
    assert _heartbeat_stale_cutoff(_T(), now) == now - timedelta(seconds=720)


def test_heartbeat_stale_cutoff_effort_floor_does_not_weaken_large_scope():
    """Large-scope threshold (900s) wins over low-effort floor (300s)."""

    class _T:
        context = {"scope": "large", "effort": "low"}  # noqa: RUF012
        first_payload = None

    now = datetime(2026, 1, 1, 12, 0, 0)
    assert _heartbeat_stale_cutoff(_T(), now) == now - timedelta(seconds=900)


def test_heartbeat_stale_cutoff_large_scope_default_is_900s():
    """Large-scope default threshold is 900s (15 minutes) for analysis-heavy tasks."""

    class _T:
        context = {"scope": "large"}  # noqa: RUF012
        first_payload = None

    now = datetime(2026, 1, 1, 12, 0, 0)
    assert _heartbeat_stale_cutoff(_T(), now) == now - timedelta(seconds=900)


def test_heartbeat_stale_cutoff_effort_from_task_type_fallback():
    """Effort falls back to the Anthropic settings bag when not explicit in context."""

    class _T:
        context = {"scope": "small", "type": "strategic_review"}  # noqa: RUF012
        first_payload = None

    # Isolate from other tests that may have mutated runtime_config via PATCH.
    original_overrides = _runtime_config.get("task_type_provider_overrides")
    original_defaults = _runtime_config.get("provider_settings_defaults")
    try:
        _runtime_config["provider_settings_defaults"] = {
            "anthropic": {"effort": "medium"}
        }
        _runtime_config["task_type_provider_overrides"] = {
            "strategic_review": {"anthropic": {"effort": "max"}},
        }
        now = datetime(2026, 1, 1, 12, 0, 0)
        # strategic_review: effort=max gives 720s effort floor, but the thinking-heavy
        # task-type floor (1500s) wins — heartbeat window must be at least as wide as
        # the progress-stale window so a live-but-thinking agent isn't heartbeat-reaped
        # before its progress-stale budget expires.
        assert _heartbeat_stale_cutoff(_T(), now) == now - timedelta(seconds=1500)
    finally:
        if original_overrides is None:
            _runtime_config.pop("task_type_provider_overrides", None)
        else:
            _runtime_config["task_type_provider_overrides"] = original_overrides
        if original_defaults is None:
            _runtime_config.pop("provider_settings_defaults", None)
        else:
            _runtime_config["provider_settings_defaults"] = original_defaults


def test_heartbeat_stale_cutoff_unknown_effort_uses_scope_threshold():
    """Unknown effort string falls back to scope-only behavior."""

    class _T:
        context = {"scope": "small", "effort": "unknown_effort_xyz"}  # noqa: RUF012
        first_payload = None

    now = datetime(2026, 1, 1, 12, 0, 0)
    assert _heartbeat_stale_cutoff(_T(), now) == now - timedelta(seconds=300)


def test_heartbeat_stale_cutoff_thinking_heavy_type_raises_floor_to_1500s():
    """Thinking-heavy task types get a 1500s heartbeat floor matching their progress-stale window.

    A research/plan/investigate task with a small-scope heartbeat cutoff (300s) or
    medium-effort floor (300s) would be heartbeat-reaped while it is legitimately deep
    in a reasoning pass. The floor matches _THINKING_HEAVY_PROGRESS_STALE_SECONDS so
    the heartbeat and progress-stale windows are co-incident.
    """

    class _T:
        context = {"scope": "small", "type": "research"}  # noqa: RUF012
        first_payload = None

    now = datetime(2026, 1, 1, 12, 0, 0)
    # Small-scope default is 300s; thinking-heavy floor raises it to 1500s.
    assert _heartbeat_stale_cutoff(_T(), now) == now - timedelta(seconds=1500)


def test_heartbeat_stale_cutoff_thinking_heavy_does_not_weaken_larger_cutoff():
    """A large-scope task whose threshold already exceeds 1500s is not weakened."""

    class _T:
        context = {"scope": "large", "effort": "max", "type": "research"}  # noqa: RUF012
        first_payload = None

    now = datetime(2026, 1, 1, 12, 0, 0)
    # large-scope=900s, max-effort floor=720s → max(900, 720, 1500) = 1500s.
    assert _heartbeat_stale_cutoff(_T(), now) == now - timedelta(seconds=1500)


def test_heartbeat_stale_cutoff_non_thinking_type_unaffected():
    """Non-thinking-heavy task types are not affected by the thinking-heavy floor."""

    class _T:
        context = {"scope": "small", "type": "create"}  # noqa: RUF012
        first_payload = None

    now = datetime(2026, 1, 1, 12, 0, 0)
    assert _heartbeat_stale_cutoff(_T(), now) == now - timedelta(seconds=300)


# ── _progress_stale_cutoff ────────────────────────────────────────────────────


def test_progress_stale_cutoff_small_scope_default_is_480s():
    """Small-scope default progress-stale threshold is 480s (8 minutes)."""

    class _T:
        context = {"scope": "small"}  # noqa: RUF012
        first_payload = None

    now = datetime(2026, 1, 1, 12, 0, 0)
    assert _progress_stale_cutoff(_T(), now) == now - timedelta(seconds=480)


def test_progress_stale_cutoff_strategic_review_uses_thinking_heavy_budget():
    """strategic_review gets the 1500s thinking-heavy budget regardless of scope.

    Regression for b2410541 (2026-05-08): Claude Opus on strategic_review
    was reaped at 484s with the small-scope 480s cutoff while still in
    legitimate pass-1 reasoning. Actual completion took 19 min — the
    480s budget is structurally incompatible with reasoning-model thinking.
    """

    class _T:
        context = {"scope": "small"}  # noqa: RUF012
        first_payload = {"type": "strategic_review"}  # noqa: RUF012
        payloads = [{"type": "strategic_review"}]  # noqa: RUF012

    now = datetime(2026, 1, 1, 12, 0, 0)
    assert _progress_stale_cutoff(_T(), now) == now - timedelta(seconds=1500)


def test_progress_stale_cutoff_research_design_doc_investigate_plan_thinking_heavy():
    """All thinking-heavy types (research, design_doc_update, investigate, plan)
    use the 1500s budget."""
    now = datetime(2026, 1, 1, 12, 0, 0)
    for ttype in ("research", "design_doc_update", "investigate", "plan"):

        class _T:
            context = {"scope": "small"}  # noqa: RUF012
            first_payload = {"type": ttype}  # noqa: RUF012
            payloads = [{"type": ttype}]  # noqa: RUF012

        assert _progress_stale_cutoff(_T(), now) == now - timedelta(seconds=1500), (
            f"{ttype} should use thinking-heavy 1500s cutoff"
        )


def test_progress_stale_cutoff_memo_uses_thinking_heavy_budget():
    """Memo (pure-reasoning documentation) tasks use the 1500s thinking-heavy budget.

    Regression: memo tasks are read-only (no repo, no tools) and may spend 5-10+ min
    producing a reasoning artifact without any tool calls. The short 480s cutoff
    (small scope) would incorrectly classify a heartbeating memo in mid-reasoning as
    stalled, causing premature reap. Memo must use thinking-heavy budget.
    """

    class _T:
        context = {"scope": "small"}  # noqa: RUF012
        first_payload = {"type": "memo"}  # noqa: RUF012
        payloads = [{"type": "memo"}]  # noqa: RUF012

    now = datetime(2026, 1, 1, 12, 0, 0)
    assert _progress_stale_cutoff(_T(), now) == now - timedelta(seconds=1500)


def test_progress_stale_cutoff_fix_review_create_use_scope_default():
    """Tool-cadence task types (fix, review, create) keep scope-based budgets."""
    now = datetime(2026, 1, 1, 12, 0, 0)
    for ttype in ("fix", "review", "create"):

        class _T:
            context = {"scope": "small"}  # noqa: RUF012
            first_payload = {"type": ttype}  # noqa: RUF012
            payloads = [{"type": ttype}]  # noqa: RUF012

        assert _progress_stale_cutoff(_T(), now) == now - timedelta(seconds=480), (
            f"{ttype} should use small-scope 480s cutoff, not thinking-heavy"
        )


def test_progress_stale_cutoff_medium_scope_default_is_720s():
    """Medium-scope default progress-stale threshold is 720s (12 minutes)."""

    class _T:
        context = {"scope": "medium"}  # noqa: RUF012
        first_payload = None

    now = datetime(2026, 1, 1, 12, 0, 0)
    assert _progress_stale_cutoff(_T(), now) == now - timedelta(seconds=720)


def test_progress_stale_cutoff_large_scope_default_is_1080s():
    """Large-scope default progress-stale threshold is 1080s (18 minutes)."""

    class _T:
        context = {"scope": "large"}  # noqa: RUF012
        first_payload = None

    now = datetime(2026, 1, 1, 12, 0, 0)
    assert _progress_stale_cutoff(_T(), now) == now - timedelta(seconds=1080)


def test_progress_stale_cutoff_respects_runtime_config_override():
    """_progress_stale_cutoff reads per-scope thresholds from runtime_config."""
    original = _runtime_config.get("progress_stale_seconds_by_scope")
    try:
        _runtime_config["progress_stale_seconds_by_scope"] = {
            "small": 300,
            "medium": 600,
            "large": 900,
        }

        class _T:
            context = {"scope": "medium"}  # noqa: RUF012
            first_payload = None

        now = datetime(2026, 1, 1, 12, 0, 0)
        assert _progress_stale_cutoff(_T(), now) == now - timedelta(seconds=600)
    finally:
        if original is None:
            _runtime_config.pop("progress_stale_seconds_by_scope", None)
        else:
            _runtime_config["progress_stale_seconds_by_scope"] = original


def test_progress_stale_cutoff_unknown_scope_falls_back_to_small():
    """Unknown scope string falls back to the small-scope threshold (480s)."""

    class _T:
        context = {"scope": "unknown_scope_xyz"}  # noqa: RUF012
        first_payload = None

    now = datetime(2026, 1, 1, 12, 0, 0)
    assert _progress_stale_cutoff(_T(), now) == now - timedelta(seconds=480)


def test_progress_stale_cutoff_reads_scope_from_payload():
    """Scope falls back to first_payload when context has no scope key."""

    class _T:
        context = {}  # noqa: RUF012
        first_payload = {"scope": "large"}  # noqa: RUF012

    now = datetime(2026, 1, 1, 12, 0, 0)
    assert _progress_stale_cutoff(_T(), now) == now - timedelta(seconds=1080)


class _StopLoop(Exception):
    """Raised from mocked asyncio.sleep to break the reaper's infinite loop."""


@pytest_asyncio.fixture()
async def reaper_db(mocker):
    """Isolated in-memory DB bound into app.reaper.AsyncSessionLocal."""
    original_startup_grace = _runtime_config.get(
        "reaper_startup_orphan_grace_seconds"
    )
    # Existing service tests intentionally exercise orphan handling immediately.
    # Individual reconnect-grace tests opt back in explicitly.
    _runtime_config["reaper_startup_orphan_grace_seconds"] = 0
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    async with engine.begin() as conn:
        await conn.run_sync(SQLModel.metadata.create_all)

    Session = sessionmaker(
        bind=engine,
        class_=AsyncSession,
        autocommit=False,
        autoflush=False,
        expire_on_commit=False,
    )
    mocker.patch("app.reaper.AsyncSessionLocal", Session)
    mocker.patch("app.reaper_halt.AsyncSessionLocal", Session)
    mocker.patch("app.reaper_post_orphan_sweep.AsyncSessionLocal", Session)

    try:
        async with Session() as s:
            yield s
    finally:
        if original_startup_grace is None:
            _runtime_config.pop("reaper_startup_orphan_grace_seconds", None)
        else:
            _runtime_config["reaper_startup_orphan_grace_seconds"] = (
                original_startup_grace
            )
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
    mocker.patch(
        "app.vm_lifecycle._ensure_vm_deleted", new_callable=AsyncMock, return_value=True
    )
    mocker.patch("app.vm_lifecycle.delete_agent_vm", return_value=True)
    mocker.patch(
        "app.alert_dispatch._maybe_create_failure_alert", new_callable=AsyncMock
    )
    mocker.patch(
        "app.rollup_management._check_and_trigger_rollup", new_callable=AsyncMock
    )
    mocker.patch("app.reaper_orphan_io._capture_serial_for_vm", return_value=None)
    mocker.patch("app.reaper.record_zone_failure")
    # Prevent task-failure results from polluting deploy_monitoring.task_results,
    # which is a global deque shared across test sessions and would cause the
    # degradation-gate to fire in subsequent research_completion tests.
    # Patch at the source (record_task_result is imported lazily inside reaper_service).
    mocker.patch("app.routers.agent_status.record_task_result")
    # Disable infra-recovery resurrection so FAILED tasks stay FAILED where asserted.
    mocker.patch("app.reaper.any_zone_available", return_value=True)


async def _make_project(db: AsyncSession) -> Project:
    project = Project(name="reaper-unit", user_id="user1")
    db.add(project)
    await db.commit()
    await db.refresh(project)
    return project


async def _run_reaper_once():
    try:  # noqa: SIM105
        await reaper_service()
    except _StopLoop:
        pass


@pytest.mark.asyncio
async def test_parked_rate_limit_task_expires_back_to_cold_requeue(reaper_db, mocker):
    """An expired queued park must tear the held VM down and restore the cold
    requeue shape so a warm VM cannot leak indefinitely."""
    from app.reaper_infra_recovery import run_infra_recovery_and_hold_maintenance

    project = await _make_project(reaper_db)
    old_vm_id = "agent-vm-park-expired"
    task = Task(
        project_id=project.id,
        payloads=[{"type": "create"}],
        vm_instance_id=old_vm_id,
        status=TaskStatus.QUEUED,
        retry_after_until=_utcnow() - timedelta(minutes=1),
        context={
            "parked_for_retarget": True,
            "park_deadline": (_utcnow() - timedelta(seconds=5)).isoformat(),
            "warm_target_vm_id": old_vm_id,
            "warm_handoff_deadline": (
                _utcnow() - timedelta(seconds=5)
            ).isoformat(),
            "warm_target_pr": "4242",
            "blacklisted_accounts": ["acct-failed"],
        },
    )
    reaper_db.add(task)
    await reaper_db.commit()
    await reaper_db.refresh(task)

    delete_vm = mocker.patch(
        "app.vm_lifecycle._ensure_vm_deleted", new_callable=AsyncMock, return_value=True
    )

    now = _utcnow()
    await run_infra_recovery_and_hold_maintenance(reaper_db, now)

    await reaper_db.refresh(task)
    ctx = task.context or {}
    assert task.status == TaskStatus.QUEUED
    assert task.vm_instance_id != old_vm_id
    assert task.retry_after_until is None
    assert (
        task.latest_status_message
        == "Park-and-retarget budget expired — cold requeueing"
    )
    assert "parked_for_retarget" not in ctx
    assert "park_deadline" not in ctx
    assert "warm_target_vm_id" not in ctx
    assert "warm_handoff_deadline" not in ctx
    assert "warm_target_pr" not in ctx
    assert ctx.get("blacklisted_accounts") == ["acct-failed"]
    delete_vm.assert_awaited_once_with(old_vm_id, reason="park_retarget_budget_expired")


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
    project = Project(
        name="reaper-merged-main",
        user_id="user1",
        integration_branch="main",
        promotion_target="main",
    )
    reaper_db.add(project)
    await reaper_db.commit()
    await reaper_db.refresh(project)
    t = Task(
        project_id=project.id,
        payloads=[{"type": "create"}],
        vm_instance_id="vm-absolute",
        status=TaskStatus.PROVISIONING,
        started_at=_utcnow() - timedelta(minutes=16),
        context={"provision_zone": "us-east1-a"},
    )
    reaper_db.add(t)
    await reaper_db.commit()

    await _run_reaper_once()
    await reaper_db.refresh(t)

    assert t.status != TaskStatus.PROVISIONING, (
        "reaper must flip the task off PROVISIONING"
    )


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
        started_at=_utcnow() - timedelta(minutes=14),
        vm_provisioned_at=_utcnow() - timedelta(minutes=4),
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
        started_at=_utcnow() - timedelta(minutes=7),
        vm_provisioned_at=_utcnow() - timedelta(minutes=7),
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
        last_heartbeat=_utcnow() - timedelta(minutes=6),
    )
    reaper_db.add(t)
    await reaper_db.commit()

    await _run_reaper_once()
    await reaper_db.refresh(t)

    # Reaper retries (attempt_count < class budget), so status flips to QUEUED.
    assert t.status == TaskStatus.QUEUED
    assert t.attempt_count == 1
    assert str(t.vm_instance_id).startswith("agent-vm-")
    assert "runner_target_id" not in (t.context or {})


@pytest.mark.asyncio
async def test_orphan_reaped_resident_restores_authoritative_runner_binding(
    reaper_db, stop_after_one_cycle, reaper_side_effects
):
    """A silent Resident death retries on its exact session runner identity."""
    project = await _make_project(reaper_db)
    runner_id = str(_uuid_mod.uuid4())
    session_id = "ss-orphan-retry"
    worker_generation = 7
    task = Task(
        project_id=project.id,
        payloads=[{"type": "strategic_review"}],
        vm_instance_id="agent-vm-dead-resident",
        status=TaskStatus.IN_PROGRESS,
        attempt_count=0,
        last_heartbeat=_utcnow() - timedelta(minutes=30),
        context={
            "_resident_session": True,
            "strategic_session_id": session_id,
            "_resident_worker_generation": worker_generation,
        },
    )
    reaper_db.add(task)
    await reaper_db.commit()
    await reaper_db.refresh(task)

    project.vision = {
        RESIDENT_SESSION_KEY: {
            "version": 2,
            "session_id": session_id,
            "task_id": str(task.id),
            "worker_generation": worker_generation,
            "runner_id": runner_id,
            "status": "active",
            "stop_requested": False,
        }
    }
    reaper_db.add(project)
    await reaper_db.commit()

    await _run_reaper_once()
    await reaper_db.refresh(task)

    assert task.status == TaskStatus.PROVISIONING
    assert task.attempt_count == 1
    assert task.vm_instance_id == f"runner:{runner_id}"
    assert task.vm_provisioned_at is not None
    assert task.retry_after_until is not None
    assert task.context["_resident_session"] is True
    assert task.context["strategic_session_id"] == session_id
    assert task.context["_resident_worker_generation"] == worker_generation
    assert task.context["runner_target_id"] == runner_id
    assert task.context["preferred_runner_target_id"] == runner_id


@pytest.mark.asyncio
async def test_orphan_reaped_superseded_resident_does_not_restore_old_binding(
    reaper_db, stop_after_one_cycle, reaper_side_effects
):
    """A newer persisted generation fences an orphan retry from its old runner."""
    project = await _make_project(reaper_db)
    runner_id = str(_uuid_mod.uuid4())
    session_id = "ss-superseded-orphan"
    task = Task(
        project_id=project.id,
        payloads=[{"type": "strategic_review"}],
        vm_instance_id="agent-vm-dead-resident",
        status=TaskStatus.IN_PROGRESS,
        attempt_count=0,
        last_heartbeat=_utcnow() - timedelta(minutes=30),
        context={
            "_resident_session": True,
            "strategic_session_id": session_id,
            "_resident_worker_generation": 7,
        },
    )
    reaper_db.add(task)
    await reaper_db.commit()
    await reaper_db.refresh(task)

    project.vision = {
        RESIDENT_SESSION_KEY: {
            "version": 2,
            "session_id": session_id,
            "task_id": str(task.id),
            "worker_generation": 8,
            "runner_id": runner_id,
            "status": "active",
            "stop_requested": False,
        }
    }
    reaper_db.add(project)
    await reaper_db.commit()

    await _run_reaper_once()
    await reaper_db.refresh(task)

    assert task.status == TaskStatus.QUEUED
    assert task.attempt_count == 1
    assert str(task.vm_instance_id).startswith("agent-vm-")
    assert task.vm_instance_id != f"runner:{runner_id}"
    assert "runner_target_id" not in task.context
    assert "preferred_runner_target_id" not in task.context


@pytest.mark.asyncio
async def test_reap_appends_durable_task_history_reason(
    reaper_db, stop_after_one_cycle, reaper_side_effects
):
    """Every confirmed reap appends a durable TaskHistory row (action="REAPED")
    carrying the full reap_reason + code — the reason survives even after the
    next reap overwrites latest_status_message (surface REASONS, not states)."""
    from app.models import TaskHistory

    project = await _make_project(reaper_db)
    t = Task(
        project_id=project.id,
        payloads=[{"type": "create"}],
        vm_instance_id="vm-history",
        status=TaskStatus.IN_PROGRESS,
        attempt_count=0,
        last_heartbeat=_utcnow() - timedelta(minutes=6),
    )
    reaper_db.add(t)
    await reaper_db.commit()

    await _run_reaper_once()

    rows = (
        (
            await reaper_db.execute(
                select(TaskHistory).where(
                    TaskHistory.task_id == t.id, TaskHistory.action == "REAPED"
                )
            )
        )
        .scalars()
        .all()
    )
    assert len(rows) == 1, "confirmed reap must append exactly one REAPED history row"
    details = rows[0].details
    assert details["code"] == "heartbeat_timeout_post_claim"
    assert "No heartbeat for" in details["reap_reason"]
    assert details["vm_instance_id"] == "vm-history"


@pytest.mark.asyncio
async def test_healthy_task_gets_no_reap_history_row(
    reaper_db, stop_after_one_cycle, reaper_side_effects
):
    """A live (fresh-heartbeat) task must not accrue REAPED history rows."""
    from app.models import TaskHistory

    project = await _make_project(reaper_db)
    t = Task(
        project_id=project.id,
        payloads=[{"type": "create"}],
        vm_instance_id="vm-healthy",
        status=TaskStatus.IN_PROGRESS,
        attempt_count=0,
        last_heartbeat=_utcnow(),
    )
    reaper_db.add(t)
    await reaper_db.commit()

    await _run_reaper_once()

    rows = (
        (
            await reaper_db.execute(
                select(TaskHistory).where(TaskHistory.task_id == t.id)
            )
        )
        .scalars()
        .all()
    )
    assert rows == []
    await reaper_db.refresh(t)
    assert t.status == TaskStatus.IN_PROGRESS


@pytest.mark.asyncio
async def test_execution_timeout_retry_escalates_effort(
    reaper_db, stop_after_one_cycle, reaper_side_effects
):
    """A pure wall-clock execution-timeout requeue escalates effort one tier so
    the retry gets a larger budget, while an OOM requeue leaves effort
    unchanged. This is the acceptance evidence for the escalation feature."""
    project = await _make_project(reaper_db)

    # Execution timeout: AGENT_TIMEOUT + execution_timeout subtype → escalate.
    exec_task = Task(
        project_id=project.id,
        payloads=[{"type": "create"}],
        vm_instance_id="vm-exec-timeout",
        status=TaskStatus.IN_PROGRESS,
        attempt_count=0,
        last_heartbeat=_utcnow() - timedelta(minutes=30),
        latest_status_message="execution timed out after 3600s",
        context={
            "failure_class": "AGENT_TIMEOUT",
            "timeout_subtype": "execution_timeout",
            "effort": "medium",
        },
    )
    # OOM: AGENT_TIMEOUT + oom subtype → must NOT escalate.
    oom_task = Task(
        project_id=project.id,
        payloads=[{"type": "create"}],
        vm_instance_id="vm-oom",
        status=TaskStatus.IN_PROGRESS,
        attempt_count=0,
        last_heartbeat=_utcnow() - timedelta(minutes=30),
        latest_status_message="exit code 137 out of memory",
        context={
            "failure_class": "AGENT_TIMEOUT",
            "timeout_subtype": "oom",
            "effort": "medium",
        },
    )
    reaper_db.add(exec_task)
    reaper_db.add(oom_task)
    await reaper_db.commit()

    await _run_reaper_once()
    await reaper_db.refresh(exec_task)
    await reaper_db.refresh(oom_task)

    # Both requeued in place (attempt_count < class budget).
    assert exec_task.status == TaskStatus.QUEUED
    assert oom_task.status == TaskStatus.QUEUED

    # Execution-timeout retry escalated medium → high, with a breadcrumb.
    assert exec_task.context["effort"] == "high"
    assert exec_task.context["_effort_escalated_from"] == "medium"

    # OOM retry left effort unchanged and recorded no escalation.
    assert oom_task.context["effort"] == "medium"
    assert "_effort_escalated_from" not in oom_task.context


@pytest.mark.asyncio
async def test_reaper_retry_clears_stale_failure_verdict(
    reaper_db, stop_after_one_cycle, reaper_side_effects
):
    """Reaper in-place retry must clear stale failure verdict and blocker state.

    A task that dies for CODE_ERROR is classified and has failure_class written
    into its context at reap time.  When the reaper subsequently decides the task
    can retry (attempt_count < class budget), that stale verdict must be removed
    so the retry attempt is classified from its own evidence rather than
    inheriting the predecessor's authoritative class.  The same applies to all
    blocker-escalation markers.
    """
    project = await _make_project(reaper_db)
    task = Task(
        project_id=project.id,
        payloads=[{"type": "create", "model": "claude-sonnet-4-6"}],
        vm_instance_id="vm-stale-verdict",
        status=TaskStatus.IN_PROGRESS,
        attempt_count=0,
        last_heartbeat=_utcnow() - timedelta(minutes=30),
        latest_status_message="test failed: AssertionError",
        llm_account_id="stale-account",
        context={
            # Stale verdict from the current attempt — must be stripped on retry.
            "failure_class": "CODE_ERROR",
            "failure_category": "CODE_ERROR",
            "blocker": {"kind": "test_failure", "tests": ["test_foo"]},
            "_blocked_escalated": True,
            "_precondition_escalated_key": "some_precondition",
            "_blocked_optimistic_retries": 2,
            # Durable diagnostic keys — must survive the retry.
            "_previous_failure_class": "CODE_ERROR",
            "effort": "medium",
            "llm_account_id": "stale-account",
            "llm_provider": "anthropic",
            "llm_model": "claude-sonnet-4-6",
            "runner_target_id": "dead-runner",
            "forced_runner_target_id": "operator-pin",
            "_requested_payload_model": "gpt-5.6-sol",
            "_resolved_payload_model": "claude-sonnet-4-6",
        },
    )
    reaper_db.add(task)
    await reaper_db.commit()
    await reaper_db.refresh(task)

    await _run_reaper_once()
    await reaper_db.refresh(task)

    assert task.status == TaskStatus.QUEUED, "attempt_count < budget — must retry"

    ctx = task.context or {}
    # Stale verdict and blocker state must be cleared for fresh classification.
    assert "failure_class" not in ctx, "failure_class must be stripped on retry"
    assert "failure_category" not in ctx, "failure_category must be stripped on retry"
    assert "blocker" not in ctx, "blocker must be stripped on retry"
    assert "_blocked_escalated" not in ctx, (
        "_blocked_escalated must be stripped on retry"
    )
    assert "_precondition_escalated_key" not in ctx, (
        "_precondition_escalated_key must be stripped on retry"
    )
    assert "_blocked_optimistic_retries" not in ctx, (
        "_blocked_optimistic_retries must be stripped on retry"
    )

    # Durable diagnostic keys must survive.
    assert ctx.get("_previous_failure_class") == "CODE_ERROR", (
        "_previous_failure_class must be preserved for retry guidance"
    )
    assert task.llm_account_id is None
    assert task.first_payload["model"] == "gpt-5.6-sol"
    assert "runner_target_id" not in ctx
    assert ctx["forced_runner_target_id"] == "operator-pin"
    assert ctx.get("_fresh_attempt_started_at")


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
        last_heartbeat=_utcnow() - timedelta(minutes=4),
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
        started_at=_utcnow() - timedelta(minutes=7),
        last_heartbeat=None,
    )
    reaper_db.add(t)
    await reaper_db.commit()

    await _run_reaper_once()
    await reaper_db.refresh(t)

    assert t.status == TaskStatus.QUEUED


@pytest.mark.asyncio
async def test_in_progress_no_tool_use_past_progress_stale_reaped(
    reaper_db, stop_after_one_cycle, reaper_side_effects
):
    """Heartbeat fresh + last_tool_use_at NULL + started_at >8min + anthropic → agent_stalled.

    Anthropic-only because the agent wrapper's claude-stream-json parser is
    the only path that populates last_tool_use_at (Codex/Gemini under
    outputMode='text' never call recordToolUse).
    """
    project = await _make_project(reaper_db)
    t = Task(
        project_id=project.id,
        payloads=[{"type": "fix", "scope": "small"}],
        vm_instance_id="vm-no-tool",
        status=TaskStatus.IN_PROGRESS,
        attempt_count=0,
        started_at=_utcnow() - timedelta(minutes=10),
        last_heartbeat=_utcnow() - timedelta(seconds=30),
        last_tool_use_at=None,
        context={"scope": "small", "llm_provider": "anthropic"},
    )
    reaper_db.add(t)
    await reaper_db.commit()

    await _run_reaper_once()
    await reaper_db.refresh(t)

    assert t.status == TaskStatus.QUEUED
    assert t.attempt_count == 1


@pytest.mark.parametrize("task_type", ["verify_tests", "compute_experiment"])
@pytest.mark.parametrize(
    "tool_use_age",
    [None, timedelta(minutes=10)],
    ids=["null-tool-use", "stale-tool-use"],
)
@pytest.mark.asyncio
async def test_direct_handler_fresh_heartbeat_ignores_tool_progress_staleness(
    reaper_db,
    stop_after_one_cycle,
    reaper_side_effects,
    task_type,
    tool_use_age,
):
    """Direct non-LLM handlers use heartbeat, not impossible tool events.

    Both registered direct handlers can legitimately remain active beyond the
    default small-scope 480s progress cutoff. A NULL tool stamp is their normal
    state; a stale stamp can be carried from an earlier attempt. Neither is
    evidence of a stall while the current handler heartbeat remains fresh.
    """
    project = await _make_project(reaper_db)
    now = _utcnow()
    t = Task(
        project_id=project.id,
        payloads=[{"type": task_type, "scope": "small"}],
        vm_instance_id=f"vm-direct-{task_type}-{tool_use_age is None}",
        status=TaskStatus.IN_PROGRESS,
        attempt_count=0,
        started_at=now - timedelta(minutes=10),
        last_heartbeat=now - timedelta(seconds=30),
        last_tool_use_at=(
            None if tool_use_age is None else now - tool_use_age
        ),
        # Anthropic deliberately exercises the NULL-tool progress branch that
        # reaped verify_tests in production; direct handlers never call Claude.
        context={"scope": "small", "llm_provider": "anthropic"},
    )
    reaper_db.add(t)
    await reaper_db.commit()

    await _run_reaper_once()
    await reaper_db.refresh(t)

    assert t.status == TaskStatus.IN_PROGRESS
    assert t.attempt_count == 0


@pytest.mark.parametrize("task_type", ["verify_tests", "compute_experiment"])
@pytest.mark.asyncio
async def test_direct_handler_stale_heartbeat_still_reaped(
    reaper_db,
    stop_after_one_cycle,
    reaper_side_effects,
    task_type,
):
    """The direct-handler carve-out must never mask a dead agent."""
    project = await _make_project(reaper_db)
    now = _utcnow()
    t = Task(
        project_id=project.id,
        payloads=[{"type": task_type, "scope": "small"}],
        vm_instance_id=f"vm-dead-direct-{task_type}",
        status=TaskStatus.IN_PROGRESS,
        attempt_count=0,
        started_at=now - timedelta(minutes=10),
        last_heartbeat=now - timedelta(minutes=6),
        last_tool_use_at=None,
        context={"scope": "small", "llm_provider": "anthropic"},
    )
    reaper_db.add(t)
    await reaper_db.commit()

    await _run_reaper_once()
    await reaper_db.refresh(t)

    assert t.status == TaskStatus.QUEUED
    assert t.attempt_count == 1


@pytest.mark.asyncio
async def test_in_progress_stalled_but_fresh_offrow_tool_use_not_reaped(
    reaper_db, stop_after_one_cycle, reaper_side_effects
):
    """Fix 3b end-to-end: a task that LOOKS stalled on-row (fresh heartbeat, stale
    on-row last_tool_use_at) is NOT reaped when the off-row vmcontact carries a
    fresh last_tool_use_at — the agent's on-row tool stamp was lock-starved by the
    rollup's task-row lock together with the heartbeat, but it IS making tool
    calls. A genuine stall (stale off-row too) is still reaped (covered by
    test_in_progress_no_tool_use_past_progress_stale_reaped)."""
    from app.models import VmContact

    project = await _make_project(reaper_db)
    now = _utcnow()
    vm_id = "vm-lockstarved-progress"
    t = Task(
        project_id=project.id,
        payloads=[{"type": "fix", "scope": "small"}],
        vm_instance_id=vm_id,
        status=TaskStatus.IN_PROGRESS,
        attempt_count=0,
        # started 12min ago → past the cross-attempt guard; on-row tool use 10min
        # stale → past the 8min small progress cutoff → would be agent_stalled.
        started_at=now - timedelta(minutes=12),
        last_heartbeat=now - timedelta(seconds=30),
        last_tool_use_at=now - timedelta(minutes=10),
        context={"scope": "small", "llm_provider": "anthropic"},
    )
    reaper_db.add(t)
    # Off-row vmcontact with a FRESH tool-use stamp (1min ago, within the 8min
    # cutoff): the lock-starved heartbeat dropped the on-row write but the agent
    # IS calling tools.
    reaper_db.add(
        VmContact(
            vm_instance_id=vm_id,
            last_contact_at=now - timedelta(seconds=20),
            last_tool_use_at=now - timedelta(minutes=1),
        )
    )
    await reaper_db.commit()

    await _run_reaper_once()
    await reaper_db.refresh(t)

    assert t.status == TaskStatus.IN_PROGRESS, (
        "Reaper killed a lock-starved-but-progressing agent — off-row "
        "vmcontact.last_tool_use_at progress override (Fix 3b) regressed"
    )


@pytest.mark.asyncio
async def test_active_long_tool_lease_survives_stale_output_while_dead_task_reaps(
    reaper_db, stop_after_one_cycle, reaper_side_effects
):
    """A live long tool lease wins over stale output, without masking death.

    Regression for task 45364a8f: a medium-scope ``./check test`` remained in
    flight and heartbeated every 30 seconds, but provider-buffered output left
    its last visible progress 764 seconds old. The task-scoped active-tool lease
    must prevent that progress-only reap. A peer with a stale heartbeat and no
    lease remains a genuine orphan and must still be reaped.
    """
    from app.models import TaskContact

    project = await _make_project(reaper_db)
    now = _utcnow()
    live = Task(
        project_id=project.id,
        payloads=[{"type": "create", "scope": "medium"}],
        vm_instance_id="runner:active-long-tool",
        status=TaskStatus.IN_PROGRESS,
        attempt_count=0,
        started_at=now - timedelta(minutes=20),
        last_heartbeat=now - timedelta(seconds=15),
        last_tool_use_at=now - timedelta(minutes=14),
        context={"scope": "medium", "llm_provider": "openai"},
    )
    dead = Task(
        project_id=project.id,
        payloads=[{"type": "create", "scope": "medium"}],
        vm_instance_id="vm-dead-long-tool-control",
        status=TaskStatus.IN_PROGRESS,
        attempt_count=0,
        started_at=now - timedelta(minutes=20),
        last_heartbeat=now - timedelta(minutes=10),
        last_tool_use_at=now - timedelta(minutes=14),
        context={"scope": "medium", "llm_provider": "openai"},
    )
    reaper_db.add_all([live, dead])
    await reaper_db.flush()
    reaper_db.add(
        TaskContact(
            task_id=live.id,
            last_contact_at=now - timedelta(seconds=15),
            last_tool_use_at=now - timedelta(seconds=15),
        )
    )
    await reaper_db.commit()

    await _run_reaper_once()
    await reaper_db.refresh(live)
    await reaper_db.refresh(dead)

    assert live.status == TaskStatus.IN_PROGRESS
    assert live.attempt_count == 0
    assert dead.status == TaskStatus.QUEUED
    assert dead.attempt_count == 1


@pytest.mark.asyncio
async def test_in_progress_stalled_with_stale_offrow_tool_use_still_reaped(
    reaper_db, stop_after_one_cycle, reaper_side_effects
):
    """Fix 3b safety: a genuinely stalled agent whose off-row vmcontact tool-use is
    ALSO stale (no tool calls anywhere) is still reaped — the override must never
    mask a real stall. Distinguishes from the lock-starved case above."""
    from app.models import VmContact

    project = await _make_project(reaper_db)
    now = _utcnow()
    vm_id = "vm-genuine-stall"
    t = Task(
        project_id=project.id,
        payloads=[{"type": "fix", "scope": "small"}],
        vm_instance_id=vm_id,
        status=TaskStatus.IN_PROGRESS,
        attempt_count=0,
        started_at=now - timedelta(minutes=12),
        last_heartbeat=now - timedelta(seconds=30),
        last_tool_use_at=now - timedelta(minutes=10),
        context={"scope": "small", "llm_provider": "anthropic"},
    )
    reaper_db.add(t)
    # Off-row contact is fresh (it IS heartbeating) but its tool-use is 11min
    # stale — past the 8min cutoff → genuine stall, must still reap.
    reaper_db.add(
        VmContact(
            vm_instance_id=vm_id,
            last_contact_at=now - timedelta(seconds=20),
            last_tool_use_at=now - timedelta(minutes=11),
        )
    )
    await reaper_db.commit()

    await _run_reaper_once()
    await reaper_db.refresh(t)

    assert t.status == TaskStatus.QUEUED
    assert t.attempt_count == 1


@pytest.mark.asyncio
async def test_in_progress_no_tool_use_within_progress_stale_not_reaped(
    reaper_db, stop_after_one_cycle, reaper_side_effects
):
    """Heartbeat fresh + last_tool_use_at NULL + started_at <8min → not reaped.

    The watchdog must not fire during normal startup latency: agents
    routinely take 1-2 min before their first tool call.
    """
    project = await _make_project(reaper_db)
    t = Task(
        project_id=project.id,
        payloads=[{"type": "fix", "scope": "small"}],
        vm_instance_id="vm-warmup",
        status=TaskStatus.IN_PROGRESS,
        attempt_count=0,
        started_at=_utcnow() - timedelta(minutes=3),
        last_heartbeat=_utcnow() - timedelta(seconds=10),
        last_tool_use_at=None,
        context={"scope": "small", "llm_provider": "anthropic"},
    )
    reaper_db.add(t)
    await reaper_db.commit()

    await _run_reaper_once()
    await reaper_db.refresh(t)

    assert t.status == TaskStatus.IN_PROGRESS


@pytest.mark.asyncio
async def test_in_progress_stale_tool_use_but_fresh_attempt_not_reaped(
    reaper_db, stop_after_one_cycle, reaper_side_effects
):
    """A retry whose last_tool_use_at is stale (carried forward from a prior
    attempt) must NOT be reaped if the CURRENT attempt's started_at is recent.

    Regression for 8c8cf973 (Architecture review on Claude Opus, 2026-05-08):
    attempt 3 was reaped 22 seconds after starting because last_tool_use_at
    was 11 minutes old from attempt 1. Three attempts burned in ~3 minutes
    with each retry never getting a chance to actually run.
    """
    project = await _make_project(reaper_db)
    now = _utcnow()
    t = Task(
        project_id=project.id,
        payloads=[{"type": "design_doc_update", "scope": "small"}],
        vm_instance_id="vm-fresh-retry",
        status=TaskStatus.IN_PROGRESS,
        attempt_count=2,  # 3rd attempt
        # Started 30s ago — well within any reasonable progress window.
        started_at=now - timedelta(seconds=30),
        last_heartbeat=now - timedelta(seconds=15),
        # Tool use from a much earlier attempt — stale by 12+ minutes.
        last_tool_use_at=now - timedelta(minutes=12),
        context={"scope": "small", "llm_provider": "anthropic"},
    )
    reaper_db.add(t)
    await reaper_db.commit()

    await _run_reaper_once()
    await reaper_db.refresh(t)

    assert t.status == TaskStatus.IN_PROGRESS, (
        "Reaper killed a fresh attempt because tool_use_at was stale from "
        "a prior attempt — cross-attempt stale-tool guard regression"
    )


@pytest.mark.asyncio
async def test_in_progress_no_tool_use_non_anthropic_provider_reaped(
    reaper_db, stop_after_one_cycle, reaper_side_effects
):
    """Zero-progress non-Anthropic tasks must be reaped on the progress-stale clock.

    The agent wrapper now calls recordToolUse('model_output', ...) on every
    non-empty stdout chunk in text-mode (Gemini/Codex), so last_tool_use_at IS
    populated for any genuinely-progressing task.  A task that has last_tool_use_at
    NULL after the progress-stale cutoff has truly produced zero output and must be
    reaped just like a stalled Anthropic task.
    """
    project = await _make_project(reaper_db)
    t = Task(
        project_id=project.id,
        payloads=[{"type": "fix", "scope": "small", "model": "gpt-5.4"}],
        vm_instance_id="vm-codex",
        status=TaskStatus.IN_PROGRESS,
        attempt_count=0,
        started_at=_utcnow() - timedelta(minutes=10),
        last_heartbeat=_utcnow() - timedelta(seconds=30),
        last_tool_use_at=None,
        context={"scope": "small", "llm_provider": "openai"},
    )
    reaper_db.add(t)
    await reaper_db.commit()

    await _run_reaper_once()
    await reaper_db.refresh(t)

    # A reaped zero-progress task with attempt_count=0 is requeued for retry,
    # not moved to a terminal status (TaskStatus has no TIMED_OUT member).
    assert t.status == TaskStatus.QUEUED
    assert t.attempt_count == 1


@pytest.mark.asyncio
async def test_in_progress_memo_zero_tool_use_heartbeating_not_reaped(
    reaper_db, stop_after_one_cycle, reaper_side_effects
):
    """Memo task heartbeating with zero tool calls for 10min must NOT be reaped.

    Memo tasks are pure-reasoning read-only tasks (no repo, no tools). They
    legitimately spend 5-10+ minutes producing reasoning artifacts without
    any tool calls. The progress-stale watchdog would incorrectly classify this
    as stalled if memo used the small-scope 480s cutoff (8 min). Memo uses the
    thinking-heavy 1500s (25 min) cutoff instead.

    Regression: pre-fix, memo tasks were reaped mid-think because they were not
    in the thinking-heavy task types, causing premature termination of legitimate
    reasoning work.
    """
    project = await _make_project(reaper_db)
    t = Task(
        project_id=project.id,
        payloads=[{"type": "memo", "topic": "analyze architecture"}],
        vm_instance_id="vm-memo-reasoning",
        status=TaskStatus.IN_PROGRESS,
        attempt_count=0,
        # Started 10 minutes ago — would be stale under 480s cutoff but not 1500s
        started_at=_utcnow() - timedelta(minutes=10),
        # Heartbeat fresh — agent is alive and thinking
        last_heartbeat=_utcnow() - timedelta(seconds=30),
        # No tool calls — memo is pure reasoning, no tools needed
        last_tool_use_at=None,
        context={"scope": "small", "llm_provider": "anthropic"},
    )
    reaper_db.add(t)
    await reaper_db.commit()

    await _run_reaper_once()
    await reaper_db.refresh(t)

    assert t.status == TaskStatus.IN_PROGRESS, (
        "Memo task heartbeating for 10min with zero tools must not be reaped "
        "(uses 1500s thinking-heavy cutoff, not 480s small-scope cutoff)"
    )


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
        last_heartbeat=_utcnow() - timedelta(minutes=30),
    )
    reaper_db.add(t)
    await reaper_db.commit()

    await _run_reaper_once()
    await reaper_db.refresh(t)

    assert t.status == TaskStatus.IN_PROGRESS


@pytest.mark.asyncio
async def test_large_scope_heartbeat_within_15min_not_reaped(
    reaper_db, stop_after_one_cycle, reaper_side_effects
):
    """Large-scope task gets a 15min heartbeat budget — 8min stale stays IN_PROGRESS."""
    project = await _make_project(reaper_db)
    t = Task(
        project_id=project.id,
        payloads=[{"type": "create"}],
        vm_instance_id="vm-large-fresh",
        status=TaskStatus.IN_PROGRESS,
        attempt_count=0,
        last_heartbeat=_utcnow() - timedelta(minutes=8),
        context={"scope": "large"},
    )
    reaper_db.add(t)
    await reaper_db.commit()

    await _run_reaper_once()
    await reaper_db.refresh(t)

    assert t.status == TaskStatus.IN_PROGRESS


@pytest.mark.asyncio
async def test_large_scope_heartbeat_past_15min_reaped(
    reaper_db, stop_after_one_cycle, reaper_side_effects
):
    """Large-scope task past the 15min threshold is still reaped."""
    project = await _make_project(reaper_db)
    t = Task(
        project_id=project.id,
        payloads=[{"type": "create"}],
        vm_instance_id="vm-large-dead",
        status=TaskStatus.IN_PROGRESS,
        attempt_count=0,
        last_heartbeat=_utcnow() - timedelta(minutes=16),
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
        last_heartbeat=_utcnow() - timedelta(minutes=6),
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
        started_at=_utcnow() - timedelta(minutes=16),
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
        started_at=_utcnow() - timedelta(minutes=16),
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


@pytest.mark.asyncio
async def test_provision_noninfra_startup_failure_calls_record_account_startup_failure(
    reaper_db, stop_after_one_cycle, reaper_side_effects, mocker
):
    """Non-infra agent-startup failure → record_account_startup_failure called with
    the account ID from task context and the provision zone."""
    mocker.patch("app.reaper.get_provision_error", return_value="startup-script exit 1")
    mocker.patch("app.reaper.get_vm_zone", return_value="us-east1-a")

    record_mock = mocker.patch(
        "app.max_plan_pool.record_account_startup_failure", new_callable=AsyncMock
    )

    project = await _make_project(reaper_db)
    t = Task(
        project_id=project.id,
        payloads=[{"type": "create"}],
        vm_instance_id="vm-non-infra-sf",
        status=TaskStatus.PROVISIONING,
        started_at=_utcnow() - timedelta(minutes=16),
        latest_status_message="agent /entrypoint.sh returned 1",
        context={
            "provision_zone": "us-east1-a",
            "max_plan_account_id": "acct-startup-test",
        },
    )
    reaper_db.add(t)
    await reaper_db.commit()

    await _run_reaper_once()

    record_mock.assert_awaited_once_with("acct-startup-test", "us-east1-a")


@pytest.mark.asyncio
async def test_provision_infra_failure_does_not_call_record_account_startup_failure(
    reaper_db, stop_after_one_cycle, reaper_side_effects, mocker
):
    """Infra-wide failure → record_account_startup_failure must NOT be called."""
    mocker.patch("app.reaper.get_provision_error", return_value="")
    mocker.patch("app.reaper.get_vm_zone", return_value="us-east1-a")

    record_mock = mocker.patch(
        "app.max_plan_pool.record_account_startup_failure", new_callable=AsyncMock
    )

    project = await _make_project(reaper_db)
    t = Task(
        project_id=project.id,
        payloads=[{"type": "create"}],
        vm_instance_id="vm-infra-sf",
        status=TaskStatus.PROVISIONING,
        started_at=_utcnow() - timedelta(minutes=16),
        latest_status_message="QUOTA exceeded: cpus_all_regions",
        context={
            "provision_zone": "us-east1-a",
            "max_plan_account_id": "acct-infra-test",
        },
    )
    reaper_db.add(t)
    await reaper_db.commit()

    await _run_reaper_once()

    record_mock.assert_not_awaited()


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
    # Audit W1: retry_after_until is a column.
    t = Task(
        project_id=project.id,
        # _extract_task_model reads payloads[0]["model"]
        payloads=[{"type": "create", "model": "sonnet"}],
        vm_instance_id="vm-rl",
        status=TaskStatus.IN_PROGRESS,
        attempt_count=1,
        last_heartbeat=_utcnow() - timedelta(minutes=10),
        retry_after_until=datetime(2099, 1, 1),  # should be cleared
        context={
            "max_plan_account_id": "acct-123",
            "last_error": "API Error: rate limit reached",
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
    assert t.retry_after_until is None, (
        "stale retry_after must be cleared on rate-limit requeue"
    )
    assert "provision_failures" not in ctx
    # reaper plumbs structured rate-limit signal (retry_after_seconds, kind) through;
    # on a generic Anthropic rate-limit message, the signal resolves to the baseline
    # (no retry hint, plain rate_limit kind).
    disable_mock.assert_awaited_with(
        "acct-123", "sonnet", retry_after_seconds=None, kind="rate_limit"
    )


@pytest.mark.asyncio
async def test_rate_limit_requeue_status_message_keeps_the_reap_reason(
    reaper_db, stop_after_one_cycle, reaper_side_effects, mocker
):
    """The reaper's non-terminal requeue used to print a state and drop its
    own evidence.

    ``latest_status_message`` was overwritten with the flat literal "Rate
    limited — requeued with exhausted account blacklisted". The reaper was
    holding ``reap_reason`` at that exact point — heartbeat age, last
    heartbeat timestamp, last tool trace — and dropped it, so the operator
    saw what the engine DID and never how it knew. Same class as the
    ``f"{failure_class.title()} — requeued"`` overwrite on the agent-status
    path; both now go through ``compose_requeue_status_message``.

    The expected tail is read back out of the durable REAPED ``TaskHistory``
    row rather than pattern-matched, so this is an exact-equality pin on "the
    surfaced message carries the very reason the reaper recorded".
    """
    from app.models import TaskHistory

    mocker.patch("app.max_plan_pool.disable_account_model", new_callable=AsyncMock)
    mocker.patch("app.max_plan_pool.release_max_plan_account", new_callable=AsyncMock)

    project = await _make_project(reaper_db)
    t = Task(
        project_id=project.id,
        payloads=[{"type": "create", "model": "sonnet"}],
        vm_instance_id="vm-rl-reason",
        status=TaskStatus.IN_PROGRESS,
        attempt_count=1,
        last_heartbeat=_utcnow() - timedelta(minutes=10),
        context={
            "max_plan_account_id": "acct-reason",
            "last_error": "API Error: rate limit reached",
        },
    )
    reaper_db.add(t)
    await reaper_db.commit()

    await _run_reaper_once()
    await reaper_db.refresh(t)

    assert t.status == TaskStatus.QUEUED
    rows = (
        (
            await reaper_db.execute(
                select(TaskHistory).where(
                    TaskHistory.task_id == t.id, TaskHistory.action == "REAPED"
                )
            )
        )
        .scalars()
        .all()
    )
    assert len(rows) == 1
    recorded_reason = rows[0].details["reap_reason"]
    assert "No heartbeat for" in recorded_reason, recorded_reason
    assert t.latest_status_message == (
        "Rate limited — requeued with exhausted account blacklisted: "
        f"{recorded_reason}"
    )


@pytest.mark.asyncio
async def test_reaper_account_cooldown_silent_death_free_requeue(
    reaper_db, stop_after_one_cycle, reaper_side_effects, mocker
):
    """A silent death cannot borrow quota evidence from account state.
    """
    disable_mock = mocker.patch(
        "app.max_plan_pool.disable_account_model", new_callable=AsyncMock
    )
    mocker.patch("app.max_plan_pool.release_max_plan_account", new_callable=AsyncMock)
    account_id = "acct-rl-silent"
    project = await _make_project(reaper_db)
    t = Task(
        project_id=project.id,
        payloads=[{"type": "create", "model": "sonnet"}],
        vm_instance_id="vm-rl-silent",
        status=TaskStatus.IN_PROGRESS,
        attempt_count=1,
        last_heartbeat=_utcnow() - timedelta(minutes=10),
        context={
            "max_plan_account_id": account_id,
            # No rate-limit keywords in last_error — correlation driven purely by account state.
        },
    )
    reaper_db.add(t)
    await reaper_db.commit()

    await _run_reaper_once()
    await reaper_db.refresh(t)

    ctx = t.context or {}
    assert t.status == TaskStatus.QUEUED
    assert t.attempt_count == 2
    assert account_id not in (ctx.get("blacklisted_accounts") or [])
    assert "max_plan_account_id" not in ctx
    disable_mock.assert_not_awaited()


@pytest.mark.asyncio
async def test_reaper_account_cooldown_uses_failure_instant_not_sweep_time(
    reaper_db, stop_after_one_cycle, reaper_side_effects, mocker
):
    """at_time=last_heartbeat is passed so edge-of-cooldown failures are caught.

    Scenario: account was disabled before the task's last_heartbeat but the
    cooldown has since expired by sweep time. The reaper must pass
    at_time=task.last_heartbeat so classify_failure evaluates the account
    state at the failure instant, not at the (later) sweep instant.

    The side_effect returns True only when at_time is not None (i.e., when the
    reaper correctly passes at_time=last_heartbeat). If the reaper passed
    at_time=None instead, the side_effect returns False and the task would be
    classified AGENT_TIMEOUT — burning a retry attempt.
    """
    disable_mock = mocker.patch(
        "app.max_plan_pool.disable_account_model", new_callable=AsyncMock
    )
    mocker.patch("app.max_plan_pool.release_max_plan_account", new_callable=AsyncMock)

    account_id = "acct-rl-edge"
    project = await _make_project(reaper_db)
    t = Task(
        project_id=project.id,
        payloads=[{"type": "create", "model": "sonnet"}],
        vm_instance_id="vm-rl-edge",
        status=TaskStatus.IN_PROGRESS,
        attempt_count=1,
        last_heartbeat=_utcnow() - timedelta(minutes=20),
        context={
            "max_plan_account_id": account_id,
            # No rate-limit keywords — correlation via account state at failure instant only.
        },
    )
    reaper_db.add(t)
    await reaper_db.commit()

    await _run_reaper_once()
    await reaper_db.refresh(t)

    ctx = t.context or {}
    assert t.status == TaskStatus.QUEUED
    assert t.attempt_count == 2
    assert account_id not in (ctx.get("blacklisted_accounts") or [])
    disable_mock.assert_not_awaited()


@pytest.mark.asyncio
async def test_reaper_db_rl_cross_instance_silent_death_free_requeue(
    reaper_db, stop_after_one_cycle, reaper_side_effects, mocker
):
    """Silent death + DB-recorded cooldown (in-memory cache missed) → RATE_LIMIT free requeue.

    Regression guard for the durable DB rate-limit correlation path in the REAPER.
    Scenario: a pool-wide rate-limit block was recorded on a different Cloud Run instance
    (or before a restart), so _account_was_rate_limited returns False (cache miss).
    db_account_was_rate_limited queries durable DB state and returns True.
    Expected: RATE_LIMIT free-requeue (attempt not burned), account blacklisted, verdict persisted.
    This exercises the durable DB fallback at reaper.py:1398-1413.
    """
    disable_mock = mocker.patch(
        "app.max_plan_pool.disable_account_model", new_callable=AsyncMock
    )
    mocker.patch("app.max_plan_pool.release_max_plan_account", new_callable=AsyncMock)
    account_id = "acct-db-rl-cross-instance"
    project = await _make_project(reaper_db)
    t = Task(
        project_id=project.id,
        payloads=[{"type": "create", "model": "sonnet"}],
        vm_instance_id="vm-db-rl-silent",
        status=TaskStatus.IN_PROGRESS,
        attempt_count=1,
        last_heartbeat=_utcnow() - timedelta(minutes=10),
        context={
            "max_plan_account_id": account_id,
            # No rate-limit keywords — correlation driven purely by DB state.
        },
    )
    reaper_db.add(t)
    await reaper_db.commit()

    await _run_reaper_once()
    await reaper_db.refresh(t)

    ctx = t.context or {}
    assert t.status == TaskStatus.QUEUED, "DB-correlated RL must requeue"
    assert t.attempt_count == 2
    assert account_id not in (ctx.get("blacklisted_accounts") or [])
    assert "max_plan_account_id" not in ctx
    disable_mock.assert_not_awaited()


@pytest.mark.asyncio
async def test_reaper_db_rl_pool_wide_accountless_silent_death_free_requeue(
    reaper_db, stop_after_one_cycle, reaper_side_effects, mocker
):
    """Pool-wide cooldown (no account ID) → RATE_LIMIT free requeue without burning budget.

    Regression guard for the accountless pool-wide branch in reaper.py (the `elif`
    after db_account_was_rate_limited). Scenario: a task has no max_plan_account_id
    (either never assigned or cleared), and the provider pool was fully blocked at
    failure time. The reaper must classify it as RATE_LIMIT and requeue without
    incrementing attempt_count, since the task had no actionable account to blacklist.
    """
    mocker.patch("app.max_plan_pool.disable_account_model", new_callable=AsyncMock)
    mocker.patch("app.max_plan_pool.release_max_plan_account", new_callable=AsyncMock)
    project = await _make_project(reaper_db)
    failure_time = _utcnow() - timedelta(minutes=10)
    t = Task(
        project_id=project.id,
        payloads=[{"type": "create", "model": "sonnet"}],
        vm_instance_id="vm-pool-rl-accountless",
        status=TaskStatus.IN_PROGRESS,
        attempt_count=1,
        last_heartbeat=failure_time,
        context={
            # No max_plan_account_id — accountless task.
            "llm_provider": "anthropic",
            "llm_model": "claude-sonnet-4-6",
            # No rate-limit keywords in last_error — purely pool-wide DB signal.
        },
    )
    reaper_db.add(t)
    await reaper_db.commit()

    await _run_reaper_once()
    await reaper_db.refresh(t)

    ctx = t.context or {}
    assert t.status == TaskStatus.QUEUED, (
        "pool-wide DB-correlated RL must requeue (not FAILED)"
    )
    assert t.attempt_count == 2
    # No account to blacklist — blacklisted_accounts should be absent or empty.
    assert not ctx.get("blacklisted_accounts"), (
        "accountless RL requeue must not create a spurious blacklisted_accounts entry"
    )


@pytest.mark.asyncio
async def test_reaper_db_rl_verdict_persisted_stable_under_analytics(
    reaper_db, stop_after_one_cycle, reaper_side_effects, mocker
):
    """DB-correlated rate-limit reap persists the verdict so analytics cannot flip it.

    After the reaper classifies a silent death as RATE_LIMIT via durable DB state,
    persist_authoritative_verdict writes 'RATE_LIMIT' to task.context['failure_class'].
    Analytics paths that call classify_failure() hours later (after the cooldown lifts,
    with _account_was_rate_limited returning False) must short-circuit to the persisted
    verdict rather than re-running correlation and returning CODE_ERROR/AGENT_TIMEOUT.
    """
    mocker.patch("app.max_plan_pool.disable_account_model", new_callable=AsyncMock)
    mocker.patch("app.max_plan_pool.release_max_plan_account", new_callable=AsyncMock)
    account_id = "acct-db-rl-persist"
    project = await _make_project(reaper_db)
    t = Task(
        project_id=project.id,
        payloads=[{"type": "create", "model": "sonnet"}],
        vm_instance_id="vm-db-rl-persist",
        status=TaskStatus.IN_PROGRESS,
        attempt_count=1,
        last_heartbeat=_utcnow() - timedelta(minutes=10),
        context={"max_plan_account_id": account_id},
    )
    reaper_db.add(t)
    await reaper_db.commit()

    await _run_reaper_once()
    await reaper_db.refresh(t)

    ctx = t.context or {}
    history = ctx.get("_failure_class_history") or []
    assert history[-1]["class"] == "AGENT_TIMEOUT"
    assert all(item["class"] != "RATE_LIMIT" for item in history)


@pytest.mark.asyncio
async def test_reaper_terminal_persist_uses_failure_instant_not_sweep_time(
    reaper_db, stop_after_one_cycle, reaper_side_effects, mocker
):
    """The terminal-persist path must classify at the failure instant, not sweep time.

    Regression guard for reaper.py:2184 — the terminal-fail branch calls
    ``classify_failure_with_confidence`` with ``at_time=task.last_heartbeat or
    task.started_at`` (the failure instant). The default ``at_time=None`` would make
    the classifier resolve the cooldown check against ``_utcnow()`` (the later sweep
    instant), so a rate-limited death whose cooldown lapsed between failure and sweep
    gets mislabeled CODE_ERROR/AGENT_TIMEOUT instead of RATE_LIMIT.

    A purely behavioural RATE_LIMIT assertion is infeasible here: every reaper-rewritten
    terminal status message embeds the reap_reason ("No heartbeat for Ns ..."), and
    "no heartbeat" matches the AGENT_TIMEOUT keyword bank, short-circuiting the terminal
    classification *before* the account-state correlation that ``at_time`` feeds
    (failure_classification.py:1867 vs :1929). So we assert the wiring contract directly:
    the terminal classify call receives the failure instant. The spy still delegates to
    the real classifier, and is the LAST ``classify_failure_with_confidence`` call in the
    cycle, so its captured ``at_time`` reflects the terminal-persist invocation. If the
    ``at_time`` argument at :2184 is dropped, the captured value becomes ``None`` and this
    test fails.
    """
    import app.failure_classification as fc

    real_cwc = fc.classify_failure_with_confidence
    captured: dict = {}

    def _spy(task, logs=None, at_time=None, db_rl_override=False):
        captured["at_time"] = at_time
        return real_cwc(task, logs=logs, at_time=at_time, db_rl_override=db_rl_override)

    mocker.patch.object(fc, "classify_failure_with_confidence", side_effect=_spy)

    failure_instant = _utcnow() - timedelta(minutes=10)
    project = await _make_project(reaper_db)
    t = Task(
        project_id=project.id,
        payloads=[{"type": "create"}],
        vm_instance_id="vm-terminal-at-time",
        status=TaskStatus.IN_PROGRESS,
        # attempt_count=2 == CODE_ERROR budget → can_retry False → terminal-persist path.
        attempt_count=2,
        last_heartbeat=failure_instant,
        # Plain code-style error → CODE_ERROR (budget=2) at the budget-decision call.
        context={"last_error": "NameError: foo is not defined"},
    )
    reaper_db.add(t)
    await reaper_db.commit()

    await _run_reaper_once()
    await reaper_db.refresh(t)

    assert t.status == TaskStatus.FAILED, "task must reach the terminal-persist path"
    assert "at_time" in captured, (
        "classify_failure_with_confidence must run on the terminal-persist path"
    )
    # The fix: at_time is the failure instant, NOT None (which the classifier would
    # resolve to the later sweep time _utcnow()).
    assert captured["at_time"] is not None, (
        "terminal-persist must pass the failure instant, not None (reaper.py:2184)"
    )
    assert captured["at_time"] == failure_instant, (
        "terminal-persist at_time must equal the failure instant (last_heartbeat)"
    )


@pytest.mark.asyncio
async def test_reaper_terminal_persist_falls_back_to_started_at_when_no_heartbeat(
    reaper_db, stop_after_one_cycle, reaper_side_effects, mocker
):
    """Terminal-persist falls back to started_at when last_heartbeat is None.

    Regression guard for reaper.py:2187 — a never-claimed provision death has
    last_heartbeat=None, so passing a bare ``task.last_heartbeat`` would hand the
    classifier ``at_time=None`` (→ sweep time), the exact bug being fixed. The
    ``or task.started_at`` fallback keeps the cooldown correlation anchored at the
    failure instant, consistent with the agent_status.py call sites.
    """
    import app.failure_classification as fc

    # Empty provision error + any_zone_available (fixture) keep is_infra False, so the
    # task takes the task-specific provision path and terminal-fails on exhausted budget
    # rather than being requeued as an infra-wide wait.
    mocker.patch("app.reaper.get_provision_error", return_value="")
    mocker.patch("app.reaper.get_vm_zone", return_value=None)

    real_cwc = fc.classify_failure_with_confidence
    captured: dict = {}

    def _spy(task, logs=None, at_time=None, db_rl_override=False):
        captured["at_time"] = at_time
        return real_cwc(task, logs=logs, at_time=at_time, db_rl_override=db_rl_override)

    mocker.patch.object(fc, "classify_failure_with_confidence", side_effect=_spy)

    started_at = _utcnow() - timedelta(minutes=30)
    project = await _make_project(reaper_db)
    t = Task(
        project_id=project.id,
        payloads=[{"type": "create"}],
        vm_instance_id="vm-terminal-no-hb",
        status=TaskStatus.PROVISIONING,
        # Past the 15-min absolute provision ceiling → reaped as absolute_timeout.
        started_at=started_at,
        last_heartbeat=None,
        # Exhaust the provision retry budget so the task terminal-fails this cycle.
        context={"provision_failures": 99, "provision_zone": "us-central1-a"},
    )
    reaper_db.add(t)
    await reaper_db.commit()

    await _run_reaper_once()
    await reaper_db.refresh(t)

    assert t.status == TaskStatus.FAILED, "task must reach the terminal-persist path"
    assert captured.get("at_time") == started_at, (
        "with last_heartbeat=None, terminal-persist must fall back to started_at "
        "(reaper.py:2187), not pass None which resolves to sweep time"
    )


@pytest.mark.asyncio
async def test_provision_failure_message_agent_never_claimed_when_vm_provisioned_at_set(
    reaper_db, stop_after_one_cycle, reaper_side_effects, mocker
):
    """VM was created (vm_provisioned_at set) but the agent never claimed it —
    the terminal PROVISION_FAILURE message must classify this as an
    agent-startup failure, NOT as a GCP API failure, even when provision_zone
    is absent.

    Regression guard for the live case: the reap reason itself ("Silent
    pre-claim death — 641s since VM provisioned, no heartbeat") proves the VM
    booted, yet the message read "Provision failed ... GCP error: no GCP error
    captured" because the zone was unresolved. The zone is only a label; the
    cause discriminator is vm_provisioned_at.
    """
    mocker.patch("app.reaper.get_provision_error", return_value="")
    mocker.patch("app.reaper.get_vm_zone", return_value=None)

    project = await _make_project(reaper_db)
    t = Task(
        project_id=project.id,
        payloads=[{"type": "create"}],
        vm_instance_id="vm-provisioned-no-zone",
        status=TaskStatus.PROVISIONING,
        started_at=_utcnow() - timedelta(minutes=30),
        vm_provisioned_at=_utcnow() - timedelta(minutes=7),
        last_heartbeat=None,
        # Exhaust the provision retry budget so the task terminal-fails this cycle.
        context={"provision_failures": 99},
    )
    reaper_db.add(t)
    await reaper_db.commit()

    await _run_reaper_once()
    await reaper_db.refresh(t)

    assert t.status == TaskStatus.FAILED, "task must reach the terminal-persist path"
    msg = t.latest_status_message or ""
    assert "Agent startup failed" in msg, (
        "vm_provisioned_at set ⇒ the VM was created ⇒ the message must be "
        f"classified as agent-never-claimed, got: {msg!r}"
    )
    assert "never claimed" in msg, f"message must name the unclaimed VM: {msg!r}"
    assert "GCP error" not in msg, (
        "a VM that was created must not be reported as a cloud API provisioning "
        f"failure: {msg!r}"
    )


@pytest.mark.asyncio
async def test_provision_failure_message_gcp_api_failure_when_vm_never_provisioned(
    reaper_db, stop_after_one_cycle, reaper_side_effects, mocker
):
    """No vm_provisioned_at ⇒ the VM was never created ⇒ the terminal
    PROVISION_FAILURE message must keep the genuine GCP-API-failure wording
    even when a zone label happens to be present in context.
    """
    mocker.patch("app.reaper.get_provision_error", return_value="")
    mocker.patch("app.reaper.get_vm_zone", return_value=None)

    project = await _make_project(reaper_db)
    t = Task(
        project_id=project.id,
        payloads=[{"type": "create"}],
        vm_instance_id="vm-never-provisioned",
        status=TaskStatus.PROVISIONING,
        started_at=_utcnow() - timedelta(minutes=30),
        last_heartbeat=None,
        context={"provision_failures": 99, "provision_zone": "us-central1-a"},
    )
    reaper_db.add(t)
    await reaper_db.commit()

    await _run_reaper_once()
    await reaper_db.refresh(t)

    assert t.status == TaskStatus.FAILED, "task must reach the terminal-persist path"
    msg = t.latest_status_message or ""
    assert "Provision failed" in msg, f"expected GCP-API-failure label, got: {msg!r}"
    assert "GCP error" in msg, f"expected GCP error detail, got: {msg!r}"
    assert "never claimed" not in msg, (
        "no VM was ever provisioned, so the message must not claim one was "
        f"created: {msg!r}"
    )


@pytest.mark.asyncio
async def test_provision_failure_message_on_local_runner_has_no_gcp_vocabulary(
    reaper_db, stop_after_one_cycle, reaper_side_effects, mocker
):
    """A local-runner death must be reported in runner vocabulary, not GCP's.

    Regression guard for live tasks ``82eb352a`` and ``4cd822c3``. Both ran on
    ``hamsterwheel`` (``vm_instance_id='runner:707de5fb-…'``) and recorded a
    real ``agent_failure_category_raw='walkthrough_harness_failed'``, yet the
    terminal message read::

        Provision failed 3x — zone: unknown zone, acct: 406bb865-….
        GCP error: no GCP error captured. Last reap: Silent pre-claim death …

    Blueprint's fleet is three local bwrap runners; there is no zone and no
    GCP error to look up, so those two literal defaults sent the operator to
    inspect infrastructure that was never involved while the actual cause sat
    unread in ``task.context``. ``started_at`` is set here because the runner
    had already pulled the pin — that is what takes the task past
    ``_is_runner_pinned_awaiting_pull`` and into the reap path the live tasks
    took.
    """
    mocker.patch("app.reaper.get_provision_error", return_value="")
    mocker.patch("app.reaper.get_vm_zone", return_value=None)

    runner_uuid = "707de5fb-1c22-4f60-8a19-9d0be2f4a3c1"
    project = await _make_project(reaper_db)
    t = Task(
        project_id=project.id,
        payloads=[{"type": "create"}],
        vm_instance_id=f"runner:{runner_uuid}",
        status=TaskStatus.PROVISIONING,
        started_at=_utcnow() - timedelta(minutes=30),
        vm_provisioned_at=_utcnow() - timedelta(minutes=7),
        last_heartbeat=None,
        context={
            "provision_failures": 99,
            "runner_hostname": "hamsterwheel",
            "runner_target_id": runner_uuid,
            "agent_failure_category_raw": "walkthrough_harness_failed",
        },
    )
    reaper_db.add(t)
    await reaper_db.commit()

    await _run_reaper_once()
    await reaper_db.refresh(t)

    assert t.status == TaskStatus.FAILED, "task must reach the terminal-persist path"
    msg = t.latest_status_message or ""
    assert "GCP" not in msg, (
        f"GCP vocabulary on a fleet with no GCP: {msg!r}"
    )
    assert "zone" not in msg.lower(), (
        f"a local runner has no zone; 'unknown zone' reads as a finding: {msg!r}"
    )
    assert "hamsterwheel" in msg, f"the runner must be named: {msg!r}"
    assert "707de5fb" in msg, f"the runner id must be named: {msg!r}"
    assert "walkthrough_harness_failed" in msg, (
        f"the recorded agent category is the real cause and must surface "
        f"instead of being buried in context: {msg!r}"
    )


@pytest.mark.asyncio
async def test_reaper_enrichment_block_records_failure_class_from_initial_classification(
    reaper_db, stop_after_one_cycle, reaper_side_effects
):
    """Regression guard: the enrichment block that writes _previous_failure_class
    reuses reap_failure_class (computed at line 1663 with at_time=last_heartbeat)
    rather than calling classify_failure a second time after last_heartbeat is
    cleared to None.

    Before the fix, a re-call without at_time could diverge from reap_failure_class
    for edge-of-cooldown failures. Verified here for the AGENT_TIMEOUT case:
    the enrichment block must write 'AGENT_TIMEOUT', not another class.
    """
    project = await _make_project(reaper_db)
    t = Task(
        project_id=project.id,
        payloads=[{"type": "create", "model": "sonnet"}],
        vm_instance_id="vm-enrich-at",
        status=TaskStatus.IN_PROGRESS,
        attempt_count=0,
        last_heartbeat=_utcnow() - timedelta(minutes=20),
        # No last_error, no rate-limit keywords → AGENT_TIMEOUT
        context={},
    )
    reaper_db.add(t)
    await reaper_db.commit()

    await _run_reaper_once()
    await reaper_db.refresh(t)

    ctx = t.context or {}
    assert t.status == TaskStatus.QUEUED
    assert ctx.get("_previous_failure_class") == "AGENT_TIMEOUT", (
        "enrichment block must write the same class as reap_failure_class "
        "(AGENT_TIMEOUT for heartbeat timeout with empty logs)"
    )


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
        last_heartbeat=_utcnow() - timedelta(minutes=7),
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
        last_heartbeat=_utcnow() - timedelta(minutes=7),
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
async def test_cloud_run_task_reap_skips_vm_deletion(
    reaper_db, stop_after_one_cycle, reaper_side_effects, mocker
):
    """Reaped Cloud Run task must not enqueue a GCE VM deletion.

    Cloud Run tasks carry a synthetic vm_instance_id (not a real GCE instance
    name). Calling _ensure_vm_deleted on them fires a GCE API call against a
    non-existent resource. The guard added to reaper.py must prevent that.
    """
    ensure_deleted = mocker.patch(
        "app.vm_lifecycle._ensure_vm_deleted", new_callable=AsyncMock, return_value=True
    )

    project = await _make_project(reaper_db)
    t = Task(
        project_id=project.id,
        payloads=[{"type": "create"}],
        vm_machine_type="cloud-run-1vcpu-2gib",
        vm_instance_id="cloud-run-job-test-123",
        status=TaskStatus.IN_PROGRESS,
        attempt_count=0,
        last_heartbeat=_utcnow() - timedelta(minutes=7),
    )
    reaper_db.add(t)
    await reaper_db.commit()

    await _run_reaper_once()
    await reaper_db.refresh(t)

    # Task must have been reaped (heartbeat expired).
    assert t.status in (TaskStatus.FAILED, TaskStatus.QUEUED), (
        f"Expected task to be reaped, got status={t.status}"
    )
    # The synthetic Cloud Run vm_id must NOT have triggered a GCE deletion call.
    ensure_deleted.assert_not_called()


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
        last_heartbeat=_utcnow() - timedelta(minutes=7),
        # Non-transient error (DNS resolution) routes through the long
        # [5,15,30,60] sequence rather than the adaptive-5xx fast path.
        context={
            "last_error": "name resolution failed for api.github.com",
            "pr_number": 42,
        },
    )
    reaper_db.add(t)
    await reaper_db.commit()

    before = _utcnow()
    await _run_reaper_once()
    await reaper_db.refresh(t)

    assert t.status == TaskStatus.QUEUED
    assert t.attempt_count == 3
    # Audit W1: retry_after_until is a column.
    assert t.retry_after_until is not None, (
        "retry_after must be set after a reaped retry"
    )
    delay_min = (t.retry_after_until - before).total_seconds() / 60
    # 30 min * 0.9 = 27 min. Without escalation (e.g. general backoff at attempt 3)
    # we'd see min(2^2, 15) * 0.9 = 3.6 min, so the 20-min floor confirms escalation.
    assert 20 <= delay_min <= 29, (
        f"expected ~27min (30min * 0.9 jitter), got {delay_min:.1f}min"
    )


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
        last_heartbeat=_utcnow() - timedelta(minutes=7),
        context={"last_error": "HTTP 503 Service Unavailable from api.github.com"},
    )
    reaper_db.add(t)
    await reaper_db.commit()

    before = _utcnow()
    await _run_reaper_once()
    await reaper_db.refresh(t)

    assert t.status == TaskStatus.QUEUED
    assert t.attempt_count == 1
    # Audit W1: retry_after_until is a column.
    assert t.retry_after_until is not None
    delay_min = (t.retry_after_until - before).total_seconds() / 60
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
        last_heartbeat=_utcnow() - timedelta(minutes=7),
        context={"last_error": "502 Bad Gateway from api.github.com"},
    )
    reaper_db.add(t)
    await reaper_db.commit()

    before = _utcnow()
    await _run_reaper_once()
    await reaper_db.refresh(t)

    assert t.status == TaskStatus.QUEUED
    # Fast-path: transient 5xx + recovered dependency + no committed work
    # must not burn the retry budget.
    assert t.attempt_count == 0
    # Audit W1: retry_after_until is a column.
    assert t.retry_after_until is not None
    delay_min = (t.retry_after_until - before).total_seconds() / 60
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
        last_heartbeat=_utcnow() - timedelta(minutes=7),
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
        {
            "retry_budgets_by_class": {
                "AGENT_TIMEOUT": 10,
                "CODE_ERROR": 2,
                "EXTERNAL_API": 4,
            }
        },
    )
    mocker.patch("app.time_utils.random.uniform", return_value=1.0)

    project = await _make_project(reaper_db)
    t = Task(
        project_id=project.id,
        payloads=[{"type": "create"}],
        vm_instance_id="vm-expo-cap",
        status=TaskStatus.IN_PROGRESS,
        attempt_count=5,  # will become 6 after the reaper retry increment
        last_heartbeat=_utcnow() - timedelta(minutes=7),
        # Empty logs → classify_failure returns AGENT_TIMEOUT → general backoff.
    )
    reaper_db.add(t)
    await reaper_db.commit()

    before = _utcnow()
    await _run_reaper_once()
    await reaper_db.refresh(t)

    assert t.status == TaskStatus.QUEUED
    assert t.attempt_count == 6
    # Audit W1: retry_after_until is a column.
    assert t.retry_after_until is not None, (
        "retry_after must be set after a reaped retry"
    )
    delay_min = (t.retry_after_until - before).total_seconds() / 60
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
        last_heartbeat=_utcnow() - timedelta(minutes=7),
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
        last_heartbeat=_utcnow() - timedelta(minutes=7),
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
    # scope=large has a 15min heartbeat-stale threshold — need >15min of staleness
    # for the reaper to classify this task as orphaned and run the budget check.
    t = Task(
        project_id=project.id,
        payloads=[{"type": "create"}],
        vm_instance_id="vm-large-code-error",
        status=TaskStatus.IN_PROGRESS,
        attempt_count=1,
        last_heartbeat=_utcnow() - timedelta(minutes=16),
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
    """Audit W1: with retry_after_until as a typed column, "unparseable"
    is no longer structurally possible. The remaining failure mode is a
    far-future value (covered separately). This test now verifies that
    a NULL column doesn't get mistaken for needing-clear (no spurious sweep)."""
    project = await _make_project(reaper_db)
    t = Task(
        project_id=project.id,
        payloads=[{"type": "create"}],
        status=TaskStatus.QUEUED,
        # No retry_after_until set — sweep should leave it alone.
    )
    reaper_db.add(t)
    await reaper_db.commit()

    await _run_reaper_once()
    await reaper_db.refresh(t)

    assert t.retry_after_until is None  # remained unset


@pytest.mark.asyncio
async def test_retry_after_sweep_clears_far_future(
    reaper_db, stop_after_one_cycle, reaper_side_effects
):
    """Anything beyond RETRY_AFTER_MAX_MINUTES (30) must be cleared.

    Audit W1: retry_after_until is a column.
    """
    project = await _make_project(reaper_db)
    far_dt = _utcnow() + timedelta(hours=6)
    t = Task(
        project_id=project.id,
        payloads=[{"type": "create"}],
        status=TaskStatus.QUEUED,
        retry_after_until=far_dt,
    )
    reaper_db.add(t)
    await reaper_db.commit()

    await _run_reaper_once()
    await reaper_db.refresh(t)

    assert t.retry_after_until is None


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
        updated_at=_utcnow() - timedelta(minutes=30),
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
        updated_at=_utcnow() - timedelta(minutes=30),
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


@pytest.mark.asyncio
async def test_step3_cancelled_child_with_rollup_nudge_marker_triggers_rollup(
    reaper_db, stop_after_one_cycle, reaper_side_effects, mocker
):
    """C6 rework seam (B): a stale EVALUATING parent whose only un-rolled child is
    a CANCELLED one carrying ``_rollup_nudge_pending`` MUST still trigger the
    rollup nudge in Step 3.

    ``_propagate_pipeline_failure_to_original`` sets that marker on a child it is
    CANCELLING just before nudging the rollup; if it dies between the terminal
    commit and the nudge, the parent has NO COMPLETED child for the normal Step-3
    path to find. Without the marker-aware selection the parent would sit
    EVALUATING until the backstop.

    Contrast with ``test_step3_evaluating_parent_without_completed_child_skipped``
    (a FAILED child with NO marker correctly does NOT trigger rollup) — the
    discriminator here is the recovery marker.

    Would FAIL pre-rework: Step 3 only selected COMPLETED children, so the
    CANCELLED+marker child would be ignored and rollup never re-triggered.
    """
    rollup_mock = mocker.patch(
        "app.rollup_management._check_and_trigger_rollup", new_callable=AsyncMock
    )
    # Stub Step 4 so an all-terminal-no-completed parent isn't requeued out from
    # under the assertion (keeps the test focused on Step 3's marker selection).
    mocker.patch("app.task_lifecycle._requeue_parent_task", new_callable=AsyncMock)

    project = await _make_project(reaper_db)
    parent = Task(
        project_id=project.id,
        payloads=[{"type": "research"}],
        status=TaskStatus.EVALUATING,
        updated_at=_utcnow() - timedelta(minutes=30),
    )
    reaper_db.add(parent)
    await reaper_db.commit()
    await reaper_db.refresh(parent)

    child = Task(
        project_id=project.id,
        parent_task_id=parent.id,
        payloads=[{"type": "create"}],
        status=TaskStatus.CANCELLED,
        context={"_rollup_nudge_pending": True},
    )
    reaper_db.add(child)
    await reaper_db.commit()
    await reaper_db.refresh(child)

    await _run_reaper_once()

    assert rollup_mock.await_count >= 1, (
        "Step 3 must re-trigger rollup for a CANCELLED child carrying "
        "_rollup_nudge_pending (dropped-nudge recovery)"
    )
    called_child = rollup_mock.await_args_list[0].args[0]
    assert called_child.id == child.id
    # Nudge succeeded (mock) — the marker is cleared so the next sweep doesn't
    # keep re-finding it.
    await reaper_db.refresh(child)
    assert not (child.context or {}).get("_rollup_nudge_pending"), (
        "the recovery marker must be cleared after a successful nudge"
    )


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
        updated_at=_utcnow() - timedelta(minutes=30),
    )
    reaper_db.add(parent)
    await reaper_db.commit()
    await reaper_db.refresh(parent)

    for _ in range(2):
        reaper_db.add(
            Task(
                project_id=project.id,
                parent_task_id=parent.id,
                payloads=[{"type": "create"}],
                status=TaskStatus.FAILED,
                context={"failure_category": "CODE_ERROR"},
            )
        )
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
        updated_at=_utcnow() - timedelta(minutes=30),
    )
    reaper_db.add(parent)
    await reaper_db.commit()
    await reaper_db.refresh(parent)

    reaper_db.add(
        Task(
            project_id=project.id,
            parent_task_id=parent.id,
            payloads=[{"type": "create"}],
            status=TaskStatus.FAILED,
        )
    )
    reaper_db.add(
        Task(
            project_id=project.id,
            parent_task_id=parent.id,
            payloads=[{"type": "create"}],
            status=TaskStatus.IN_PROGRESS,
            vm_instance_id=None,  # avoid Step 2 orphan reap
            last_heartbeat=_utcnow(),
        )
    )
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
        updated_at=_utcnow() - timedelta(minutes=30),
    )
    reaper_db.add(parent)
    await reaper_db.commit()
    await reaper_db.refresh(parent)

    reaper_db.add(
        Task(
            project_id=project.id,
            parent_task_id=parent.id,
            payloads=[{"type": "create"}],
            status=TaskStatus.FAILED,
            context={"failure_category": "CODE_ERROR"},
        )
    )
    reaper_db.add(
        Task(
            project_id=project.id,
            parent_task_id=parent.id,
            payloads=[{"type": "create"}],
            status=TaskStatus.FAILED,
            context={"failure_category": "AGENT_TIMEOUT"},
        )
    )
    reaper_db.add(
        Task(
            project_id=project.id,
            parent_task_id=parent.id,
            payloads=[{"type": "create"}],
            status=TaskStatus.CANCELLED,
        )
    )
    await reaper_db.commit()

    await _run_reaper_once()
    await reaper_db.refresh(parent)

    msg = parent.latest_status_message or ""
    assert "2 failed" in msg
    assert "CODE_ERROR" in msg
    assert "AGENT_TIMEOUT" in msg
    assert "1 cancelled" in msg


@pytest.mark.asyncio
async def test_step4_rescues_parent_after_step3_rollup_expires_session(
    reaper_db, stop_after_one_cycle, reaper_side_effects, mocker
):
    """Step 4 rescues an all-children-failed parent even when Step 3's rollup
    helper expired every loaded ORM instance mid-sweep.

    Regression: Step 3/4 once iterated the live ``evaluating_parents`` batch and
    read ``parent.id`` / ``child.status`` off it. ``_check_and_trigger_rollup``
    can commit or roll back (inner DB error), expiring every instance — the next
    read fired a sync lazy-load that crashed the reaper cycle on a dead socket
    (prod 2026-05-21, reaper.py:2385/2420). The fix snapshots ids/statuses and
    re-fetches via ``session.get``. (The crash itself needs an unhealthy
    connection — not reproducible on in-memory SQLite — so this locks the
    snapshot+re-fetch contract and exercises the post-expiry code paths.)
    """

    def _expire_session(_child, session):
        # Mirror the documented "_check_and_trigger_rollup may commit or roll
        # back" behaviour — both expire every loaded ORM instance.
        session.expire_all()

    rollup_mock = mocker.patch(
        "app.rollup_management._check_and_trigger_rollup",
        new_callable=AsyncMock,
        side_effect=_expire_session,
    )
    requeue_mock = mocker.patch(
        "app.task_lifecycle._requeue_parent_task", new_callable=AsyncMock
    )

    project = await _make_project(reaper_db)

    # Parent A: has a COMPLETED child → Step 3 invokes the rollup helper, which
    # expires the reaper's session.
    parent_a = Task(
        project_id=project.id,
        payloads=[{"type": "research"}],
        status=TaskStatus.EVALUATING,
        updated_at=_utcnow() - timedelta(minutes=30),
    )
    reaper_db.add(parent_a)
    await reaper_db.commit()
    await reaper_db.refresh(parent_a)
    reaper_db.add(
        Task(
            project_id=project.id,
            parent_task_id=parent_a.id,
            payloads=[{"type": "create"}],
            status=TaskStatus.COMPLETED,
        )
    )

    # Parent B: all children FAILED → Step 4 must still rescue it after the
    # session was expired by parent A's Step 3 rollup.
    parent_b = Task(
        project_id=project.id,
        payloads=[{"type": "research"}],
        status=TaskStatus.EVALUATING,
        updated_at=_utcnow() - timedelta(minutes=30),
    )
    reaper_db.add(parent_b)
    await reaper_db.commit()
    await reaper_db.refresh(parent_b)
    for _ in range(2):
        reaper_db.add(
            Task(
                project_id=project.id,
                parent_task_id=parent_b.id,
                payloads=[{"type": "create"}],
                status=TaskStatus.FAILED,
                context={"failure_category": "CODE_ERROR"},
            )
        )
    await reaper_db.commit()

    await _run_reaper_once()

    assert rollup_mock.await_count >= 1
    requeued_ids = {call.args[0].id for call in requeue_mock.await_args_list}
    assert parent_b.id in requeued_ids
    assert parent_a.id not in requeued_ids


@pytest.mark.asyncio
async def test_step3_4_rollup_poison_rolled_back_for_later_steps(
    reaper_db, stop_after_one_cycle, reaper_side_effects, mocker
):
    """C1 seam: a REAL session poison originating in Step 3's rollup path must
    be ROLLED BACK by the C1 handlers (Step 3 outer except, reaper.py ~2721, and
    its sibling Step 4 except, ~2817) so the steps that follow run on a clean
    session instead of inheriting a PendingRollback.

    Two facts this test gets right that the original (vacuous) version did not:

      1. REAL driver-level poison. A failed *read* (``SELECT … FROM <missing
         table>`` → OperationalError) does NOT poison a SQLAlchemy session —
         empirically the next ``execute`` still succeeds, so the original
         "inject a bad SELECT" never put the session in PendingRollback and
         passed identically pre-/post-fix. Only a failed *flush/commit*
         (IntegrityError on a duplicate PK — the production shape of a racing
         writer / serialization failure that ``_check_and_trigger_rollup``'s
         internal commit can hit) actually dooms the transaction. We inject that
         via the rollup helper.

      2. Probe the SAME session the reaper uses. The poison lands on the reaper
         worker's own session (the one opened at reaper.py:638 and threaded
         through Steps 3-10), not this test's ``reaper_db``. We wrap Step 5's
         entry (``run_infra_recovery_and_hold_maintenance``) to issue a fresh
         ``execute`` on the exact session it is handed and record whether it
         succeeds.

    Flow: Step 3 calls the (mocked) rollup helper, which flushes a duplicate-PK
    row → IntegrityError → the worker session is now in PendingRollback. The
    inner per-parent except logs the raise but does not roll back, so the poison
    survives into Step 4, whose ``session.get`` for the all-failed parent then
    fails on the poisoned session. WITHOUT the C1 rollbacks the session is still
    poisoned when Step 5 starts and the probe raises PendingRollbackError (test
    fails). WITH them the session is rolled back and the probe succeeds, and no
    PendingRollbackError escapes the cycle.
    """
    from sqlalchemy import text as _text
    from sqlalchemy.exc import IntegrityError as _IntegrityError

    captured: dict = {}

    async def _poison_via_real_integrity_error(
        _child, session, progress_cb=None, **kwargs
    ):
        # Capture the worker session and poison it with a genuine flush-level
        # IntegrityError (duplicate PK), the way a racing rollup commit would.
        # C3: _check_and_trigger_rollup now takes an optional progress_cb
        # heartbeat (and may grow further kwargs); accept and ignore them here.
        captured["session"] = session
        proj_id = (
            await session.execute(_text("SELECT id FROM project LIMIT 1"))
        ).scalar()
        dup = _uuid_mod.uuid4()
        for _ in range(2):
            session.add(
                Task(
                    id=dup,
                    project_id=proj_id,
                    payloads=[{"type": "create"}],
                    status=TaskStatus.QUEUED,
                )
            )
        with contextlib.suppress(_IntegrityError):
            await session.flush()
        # Prove the poison is real: a follow-up op now raises PendingRollbackError.
        from sqlalchemy.exc import PendingRollbackError as _PRB

        _poisoned = False
        try:
            await session.execute(_text("SELECT 1"))
        except _PRB:
            _poisoned = True
        assert _poisoned, (
            "rollup injection did not actually poison the session — a follow-up "
            "execute unexpectedly succeeded, so this test would be vacuous"
        )
        raise RuntimeError("simulated rollup commit failure after real poison")

    mocker.patch(
        "app.rollup_management._check_and_trigger_rollup",
        new_callable=AsyncMock,
        side_effect=_poison_via_real_integrity_error,
    )

    # --- Probe the SAME worker session at Step 5 entry -----------------------
    import app.reaper as _reaper_mod

    real_run_infra = _reaper_mod.run_infra_recovery_and_hold_maintenance
    probe = {"ran": False, "session_clean": None}

    async def _probe_then_run(
        session, now, just_reaped_ids=None, progress_cb=None, **kwargs
    ):
        # C3: run_infra_recovery_and_hold_maintenance now takes an optional
        # progress_cb heartbeat (and may grow further kwargs); accept and
        # forward them to the real implementation.
        probe["ran"] = True
        # This is the SAME session captured by the rollup injection above.
        assert captured.get("session") is session, (
            "Step 5 ran on a different session than Step 3 poisoned"
        )
        try:
            await session.execute(_text("SELECT 1"))
            probe["session_clean"] = True
        except Exception:
            probe["session_clean"] = False
            raise
        return await real_run_infra(
            session, now, just_reaped_ids=just_reaped_ids, progress_cb=progress_cb
        )

    mocker.patch(
        "app.reaper.run_infra_recovery_and_hold_maintenance",
        side_effect=_probe_then_run,
    )

    project = await _make_project(reaper_db)

    # Parent A: COMPLETED child → Step 3 invokes the rollup helper (which
    # poisons the session). The completed child also makes Step 4 SKIP parent A.
    parent_a = Task(
        project_id=project.id,
        payloads=[{"type": "research"}],
        status=TaskStatus.EVALUATING,
        updated_at=_utcnow() - timedelta(minutes=30),
    )
    reaper_db.add(parent_a)
    await reaper_db.commit()
    await reaper_db.refresh(parent_a)
    reaper_db.add(
        Task(
            project_id=project.id,
            parent_task_id=parent_a.id,
            payloads=[{"type": "create"}],
            status=TaskStatus.COMPLETED,
        )
    )

    # Parent B: all children FAILED → Step 4 reaches ``session.get`` for it on
    # the (poisoned) session, which is where the surviving poison surfaces.
    parent_b = Task(
        project_id=project.id,
        payloads=[{"type": "research"}],
        status=TaskStatus.EVALUATING,
        updated_at=_utcnow() - timedelta(minutes=30),
    )
    reaper_db.add(parent_b)
    await reaper_db.commit()
    await reaper_db.refresh(parent_b)
    for _ in range(2):
        reaper_db.add(
            Task(
                project_id=project.id,
                parent_task_id=parent_b.id,
                payloads=[{"type": "create"}],
                status=TaskStatus.FAILED,
                context={"failure_category": "CODE_ERROR"},
            )
        )
    await reaper_db.commit()

    # Must not raise PendingRollbackError out of the cycle.
    await _run_reaper_once()

    assert captured.get("session") is not None, (
        "the rollup helper was never called — the poison did not fire, so this "
        "test would be vacuous"
    )
    assert probe["ran"], "Step 5 entry was never reached after the Step 3 poison"
    assert probe["session_clean"] is True, (
        "the reaper session was still in PendingRollback when Step 5 started — "
        "the C1 Step 3/Step 4 rollbacks did not recover the poisoned session."
    )


@pytest.mark.asyncio
async def test_retry_after_sweep_preserves_valid(
    reaper_db, stop_after_one_cycle, reaper_side_effects
):
    """A retry_after within the 30-min ceiling must be left intact.

    Audit W1: retry_after_until is a column.
    """
    project = await _make_project(reaper_db)
    valid_dt = _utcnow() + timedelta(minutes=5)
    t = Task(
        project_id=project.id,
        payloads=[{"type": "create"}],
        status=TaskStatus.QUEUED,
        retry_after_until=valid_dt,
    )
    reaper_db.add(t)
    await reaper_db.commit()

    await _run_reaper_once()
    await reaper_db.refresh(t)

    assert t.retry_after_until == valid_dt


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
                return _FakeResult(
                    [r for r in rows if getattr(r, "id", None) not in skip_ids]
                )
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
        last_heartbeat=_utcnow() - timedelta(minutes=10),
    )
    reaper_db.add(t)
    await reaper_db.commit()
    _install_lock_filter(mocker, {t.id})

    await _run_reaper_once()
    await reaper_db.refresh(t)

    assert clean_skip_counts.get(t.id) == 1
    assert t.status == TaskStatus.IN_PROGRESS, (
        "skipped task must not be reaped this cycle"
    )


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
        last_heartbeat=_utcnow() - timedelta(minutes=10),
    )
    reaper_db.add(t)
    await reaper_db.commit()
    clean_skip_counts[t.id] = 4  # threshold is 5
    state = _install_lock_filter(mocker, {t.id})

    await _run_reaper_once()
    await reaper_db.refresh(t)

    assert state["for_update_calls"] >= 2, (
        "force-lock query must run after skip threshold"
    )
    assert t.id not in clean_skip_counts, "skip count cleared once force-lock succeeds"
    assert t.status == TaskStatus.QUEUED, (
        "force-locked task gets reaped/retried like any orphan"
    )
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
        last_heartbeat=_utcnow() - timedelta(minutes=10),
    )
    reaper_db.add(t)
    await reaper_db.commit()
    clean_skip_counts[t.id] = 4
    _install_lock_filter(mocker, {t.id}, force_raises=True)

    await _run_reaper_once()
    await reaper_db.refresh(t)

    assert t.id not in clean_skip_counts, "force-lock failure must pop skip-count entry"
    assert t.status == TaskStatus.IN_PROGRESS, (
        "task remains active until next cycle succeeds"
    )


@pytest.mark.asyncio
async def test_force_lock_reverify_healthy_preserves_skip_count(
    reaper_db, stop_after_one_cycle, reaper_side_effects, clean_skip_counts, mocker
):
    """When force-lock re-verification finds the task is no longer stale
    (heartbeat arrived during lock acquisition), the skip_count entry must be
    preserved so the reaper can force-lock sooner if staleness recurs."""
    from sqlalchemy import update as _sa_update

    project = await _make_project(reaper_db)
    t = Task(
        project_id=project.id,
        payloads=[{"type": "create"}],
        vm_instance_id="vm-force-healthy",
        status=TaskStatus.IN_PROGRESS,
        attempt_count=0,
        last_heartbeat=_utcnow() - timedelta(minutes=10),
    )
    reaper_db.add(t)
    await reaper_db.commit()
    task_id = t.id
    clean_skip_counts[task_id] = 4  # one more skip will trigger force-lock

    real_execute = AsyncSession.execute
    state = {"for_update_calls": 0}
    fresh_ts = _utcnow()

    async def mock_execute(self, stmt, *args, **kwargs):
        if isinstance(stmt, TextClause):
            return _FakeResult([])
        if getattr(stmt, "_for_update_arg", None) is not None:
            state["for_update_calls"] += 1
            if state["for_update_calls"] == 1:
                # Step-2 skip-locked: filter out the task so it's not acquired
                result = await real_execute(self, stmt, *args, **kwargs)
                rows = result.scalars().all()
                return _FakeResult(
                    [r for r in rows if getattr(r, "id", None) != task_id]
                )
            # Force-lock query: refresh the heartbeat first, then acquire
            await real_execute(
                self,
                _sa_update(Task)
                .where(Task.id == task_id)
                .values(last_heartbeat=fresh_ts),
            )
            await self.commit()
            result = await real_execute(self, stmt, *args, **kwargs)
            return _FakeResult(result.scalars().all())
        return await real_execute(self, stmt, *args, **kwargs)

    mocker.patch.object(AsyncSession, "execute", mock_execute)

    await _run_reaper_once()
    await reaper_db.refresh(t)

    assert state["for_update_calls"] >= 2, "force-lock query must run"
    assert t.status == TaskStatus.IN_PROGRESS, "healthy task must not be reaped"
    assert t.attempt_count == 0, "retry budget must not be burned"
    assert clean_skip_counts.get(task_id) == 5, (
        "skip_count must be preserved (not popped) when force-lock re-verify finds task healthy"
    )


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
        last_heartbeat=_utcnow() - timedelta(minutes=6),
    )
    reaper_db.add(t)
    await reaper_db.commit()
    task_id = t.id

    # Patch AsyncSession.execute: when the Step-2 FOR UPDATE query fires, first
    # update last_heartbeat to "now" and commit. expire_all() ran before the
    # lock query, so the lock's fresh SELECT returns the refreshed row.
    real_execute = AsyncSession.execute
    refreshed = {"done": False}
    fresh_ts = _utcnow()

    async def mock_execute(self, stmt, *args, **kwargs):
        if not refreshed["done"] and getattr(stmt, "_for_update_arg", None) is not None:
            refreshed["done"] = True
            await real_execute(
                self,
                _sa_update(Task)
                .where(Task.id == task_id)
                .values(last_heartbeat=fresh_ts),
            )
            await self.commit()
        return await real_execute(self, stmt, *args, **kwargs)

    mocker.patch.object(AsyncSession, "execute", mock_execute)

    await _run_reaper_once()
    await reaper_db.refresh(t)

    assert refreshed["done"], (
        "for_update lock query must have fired (race injection point)"
    )
    assert t.status == TaskStatus.IN_PROGRESS, (
        "task with refreshed heartbeat must NOT be reaped"
    )
    assert t.attempt_count == 0, (
        "retry budget must not be burned when Step 2 re-verify skips reap"
    )
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
        last_heartbeat=_utcnow() - timedelta(minutes=10),
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
    title/message, and tier persisted in task.context."""  # noqa: RUF002
    project = await _make_project(reaper_db)
    t = Task(
        project_id=project.id,
        title="stalled-task",
        payloads=[{"type": "create"}],
        status=TaskStatus.QUEUED,
        created_at=_utcnow() - timedelta(hours=2, minutes=30),
    )
    reaper_db.add(t)
    await reaper_db.commit()

    await _run_reaper_once()
    await reaper_db.refresh(t)

    alerts = (
        (await reaper_db.execute(_select(Alert).where(Alert.task_id == t.id)))
        .scalars()
        .all()
    )
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
    without first firing a tier-1 INFO alert."""  # noqa: RUF002
    project = await _make_project(reaper_db)
    t = Task(
        project_id=project.id,
        payloads=[{"type": "create"}],
        status=TaskStatus.QUEUED,
        created_at=_utcnow() - timedelta(hours=7),
    )
    reaper_db.add(t)
    await reaper_db.commit()

    await _run_reaper_once()
    await reaper_db.refresh(t)

    alerts = (
        (await reaper_db.execute(_select(Alert).where(Alert.task_id == t.id)))
        .scalars()
        .all()
    )
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
        created_at=_utcnow() - timedelta(hours=3),
        context={"queued_age_alert_tier": 1},
    )
    reaper_db.add(t)
    await reaper_db.commit()

    await _run_reaper_once()

    alerts = (
        (await reaper_db.execute(_select(Alert).where(Alert.task_id == t.id)))
        .scalars()
        .all()
    )
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
        created_at=_utcnow() - timedelta(hours=2, minutes=30),
    )
    reaper_db.add(t)
    await reaper_db.commit()

    await _run_reaper_once()

    alerts = (
        (await reaper_db.execute(_select(Alert).where(Alert.task_id == t.id)))
        .scalars()
        .all()
    )
    assert len(alerts) == 1
    assert "awaiting operator approval" in alerts[0].message


@pytest.mark.asyncio
async def test_step6_precondition_defer_sets_likely_cause(
    reaper_db, stop_after_one_cycle, reaper_side_effects
):
    """A QUEUED task whose latest_status_message is a dispatcher precondition
    defer ('Waiting: ...') escalates with that precondition as the likely
    cause, in preference to the generic infrastructure fallback."""
    project = await _make_project(reaper_db)
    t = Task(
        project_id=project.id,
        payloads=[{"type": "create"}],
        status=TaskStatus.QUEUED,
        requires_approval=False,
        created_at=_utcnow() - timedelta(hours=2, minutes=30),
        latest_status_message=(
            "Waiting: serializing migration — task abcd1234 holds the chain"
        ),
    )
    reaper_db.add(t)
    await reaper_db.commit()

    await _run_reaper_once()

    alerts = (
        (await reaper_db.execute(_select(Alert).where(Alert.task_id == t.id)))
        .scalars()
        .all()
    )
    assert len(alerts) == 1
    assert "serializing migration — task abcd1234 holds the chain" in (
        alerts[0].message
    )
    # The precondition cause replaces — not appends to — the generic guesses.
    assert "all accounts healthy but VM slot limit reached" not in alerts[0].message


@pytest.mark.asyncio
async def test_step6_alert_broadcasts_after_commit(reaper_db, mocker):
    """Queued-age alerts must broadcast only after the alert rows are committed."""
    project = await _make_project(reaper_db)
    task = Task(
        project_id=project.id,
        title="stalled-task",
        payloads=[{"type": "create"}],
        status=TaskStatus.QUEUED,
        created_at=_utcnow() - timedelta(hours=2, minutes=30),
    )
    reaper_db.add(task)
    await reaper_db.commit()

    events: list = []
    real_commit = reaper_db.commit

    async def _tracking_commit(*a, **k):
        events.append("commit")
        return await real_commit(*a, **k)

    mocker.patch.object(reaper_db, "commit", side_effect=_tracking_commit)

    async def _tracking_broadcast(project_id, event, payload):
        events.append(("broadcast", event, payload.get("severity")))

    mocker.patch("app.broadcast.broadcast_to_project", side_effect=_tracking_broadcast)

    alerted = await _rs.sweep_queued_age_alerts(
        reaper_db,
        _utcnow(),
        runtime_config={"queued_age_alert_hours": 2},
        batch_size=5000,
    )

    assert alerted == 1
    assert "commit" in events
    assert ("broadcast", "alert_created", "INFO") in events
    assert events.index("commit") < events.index(("broadcast", "alert_created", "INFO"))


@pytest.mark.asyncio
async def test_queued_age_alert_suppressed_while_pipeline_paused(reaper_db):
    """PAUSE-AWARE queued-age alerts: while the pipeline is PAUSED nothing
    dequeues, so a QUEUED task's wall-clock age crosses the alert tiers even
    though it is stuck only because the operator paused. The tier decision
    subtracts the open pause window, so no INFO/WARNING/ERROR alert fires."""
    from app.models import DeploymentState, DeploymentStatus

    project = await _make_project(reaper_db)
    now = _utcnow()
    reaper_db.add(
        DeploymentState(
            id=1,
            status=DeploymentStatus.PAUSED,
            paused_at=now - timedelta(hours=3),
            paused_kind="manual",
        )
    )
    t = Task(
        project_id=project.id,
        title="queued during pause",
        payloads=[{"type": "create"}],
        status=TaskStatus.QUEUED,
        created_at=now - timedelta(hours=3),  # 3h raw > 2h tier-1, but all paused
    )
    reaper_db.add(t)
    await reaper_db.commit()

    alerted = await _rs.sweep_queued_age_alerts(
        reaper_db, now, runtime_config={"queued_age_alert_hours": 2}
    )

    assert alerted == 0
    await reaper_db.refresh(t)
    assert (t.context or {}).get("queued_age_alert_tier") in (None, 0)


@pytest.mark.asyncio
async def test_queued_age_alert_fires_despite_brief_pause(reaper_db):
    """MUTATION-PROOF companion: the pause-aware tier gate is not a blanket mute.
    A task queued 3h with only a 30-min pause has ~2.5h of ACTIVE age > the 2h
    tier-1 threshold and DOES fire the INFO alert."""
    from app.models import DeploymentState, DeploymentStatus

    project = await _make_project(reaper_db)
    now = _utcnow()
    reaper_db.add(
        DeploymentState(
            id=1,
            status=DeploymentStatus.PAUSED,
            paused_at=now - timedelta(minutes=30),
            paused_kind="manual",
        )
    )
    t = Task(
        project_id=project.id,
        title="queued mostly active",
        payloads=[{"type": "create"}],
        status=TaskStatus.QUEUED,
        created_at=now - timedelta(hours=3),
    )
    reaper_db.add(t)
    await reaper_db.commit()

    alerted = await _rs.sweep_queued_age_alerts(
        reaper_db, now, runtime_config={"queued_age_alert_hours": 2}
    )

    assert alerted == 1
    await reaper_db.refresh(t)
    assert (t.context or {}).get("queued_age_alert_tier") == 1


@pytest.mark.asyncio
async def test_step6_alert_does_not_broadcast_on_commit_failure(reaper_db, mocker):
    """If the batch commit fails, queued-age alerts must not be broadcast."""
    project = await _make_project(reaper_db)
    task = Task(
        project_id=project.id,
        title="stalled-task",
        payloads=[{"type": "create"}],
        status=TaskStatus.QUEUED,
        created_at=_utcnow() - timedelta(hours=2, minutes=30),
    )
    reaper_db.add(task)
    await reaper_db.commit()

    broadcast = mocker.patch(
        "app.broadcast.broadcast_to_project", new_callable=AsyncMock
    )
    mocker.patch.object(
        reaper_db, "commit", side_effect=RuntimeError("queued-age commit boom")
    )

    with pytest.raises(RuntimeError, match="queued-age commit boom"):
        await _rs.sweep_queued_age_alerts(
            reaper_db,
            _utcnow(),
            runtime_config={"queued_age_alert_hours": 2},
            batch_size=5000,
        )

    broadcast.assert_not_called()


# ── Terminal failure finalization (non-provision budget exhausted) ────────


@pytest.mark.asyncio
async def test_terminal_failure_non_provision_finalization(
    reaper_db, stop_after_one_cycle, reaper_side_effects, mocker
):
    """IN_PROGRESS task with attempt_count >= class budget reaches the terminal
    failure path: status → FAILED, failure_category persisted, alert invoked,
    broadcast sent, and the status message carries class + retry count."""
    alert_mock = mocker.patch(
        "app.alert_dispatch._maybe_create_failure_alert", new_callable=AsyncMock
    )
    # Snapshot the alerted task's identity WHILE the reaper's session is still live.
    # The reaper passes a Task loaded in its own session, which closes when the
    # cycle ends; reading ``.id`` off ``await_args`` afterward raises
    # DetachedInstanceError under some full-shard orderings (a sibling test can
    # leave the shared session factory expiring instances on commit). Capturing at
    # call-time makes the identity assertion independent of post-cycle session state.
    _alerted_task_ids: list = []

    async def _capture_alerted_task(task, *args, **kwargs):
        _alerted_task_ids.append(task.id)

    alert_mock.side_effect = _capture_alerted_task
    broadcast_mock = mocker.patch(
        "app.broadcast.broadcast_to_project", new_callable=AsyncMock
    )
    record_mock = mocker.patch("app.routers.agent_status.record_task_result")

    project = await _make_project(reaper_db)
    # latest_status_message falls through classify_failure to CODE_ERROR (budget=2),
    # so attempt_count=2 forces can_retry=False → terminal failure branch.
    t = Task(
        project_id=project.id,
        payloads=[{"type": "create"}],
        vm_instance_id="vm-terminal",
        status=TaskStatus.IN_PROGRESS,
        attempt_count=2,
        last_heartbeat=_utcnow() - timedelta(minutes=10),
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
    assert persisted in {
        "CODE_ERROR",
        "AGENT_TIMEOUT",
        "EXTERNAL_API",
        "RATE_LIMIT",
        "PROVISION_FAILURE",
    }, f"failure_category must be persisted as a canonical class, got {persisted!r}"

    alert_mock.assert_awaited()
    assert _alerted_task_ids[-1] == t.id, (
        "the terminal-failure alert must be created for the finalized task"
    )

    record_mock.assert_called_once()
    kwargs = record_mock.call_args.kwargs
    assert kwargs["success"] is False
    assert kwargs["failure_class"] == persisted, (
        "record_task_result must receive the same class that was persisted"
    )
    assert kwargs["task_id"] == str(t.id)
    assert kwargs["task_type"] == "create"

    failed_broadcasts = [
        call
        for call in broadcast_mock.await_args_list
        if call.args[1] == "task_updated"
        and call.args[2].get("status") == TaskStatus.FAILED
    ]
    assert failed_broadcasts, "expected a task_updated broadcast with status FAILED"
    assert failed_broadcasts[-1].args[0] == str(project.id)

    msg = t.latest_status_message or ""
    # fail_reason ("failed after {N} retries ({class})") is passed through
    # str.capitalize(), which lowercases everything after the first char.
    assert "code_error" in msg.lower(), (
        f"status message should carry budget class: {msg!r}"
    )
    assert "2 retries" in msg, f"status message should carry retry count: {msg!r}"


@pytest.mark.asyncio
async def test_terminal_failure_before_budget_exhausted_not_failed(
    reaper_db, stop_after_one_cycle, reaper_side_effects, mocker
):
    """Guard: with attempt_count below the class budget, the reaper retries
    (QUEUED) instead of finalizing. Ensures the terminal test above isolates
    the budget-exhaustion branch rather than always-failing behavior."""
    alert_mock = mocker.patch(
        "app.alert_dispatch._maybe_create_failure_alert", new_callable=AsyncMock
    )
    mocker.patch("app.broadcast.broadcast_to_project", new_callable=AsyncMock)

    project = await _make_project(reaper_db)
    t = Task(
        project_id=project.id,
        payloads=[{"type": "create"}],
        vm_instance_id="vm-still-retrying",
        status=TaskStatus.IN_PROGRESS,
        attempt_count=0,
        last_heartbeat=_utcnow() - timedelta(minutes=10),
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
    # Audit W1: retry_after_until is a column.
    t = Task(
        project_id=project.id,
        payloads=[{"type": "create"}],
        status=TaskStatus.FAILED,
        attempt_count=4,
        retry_after_until=datetime(2030, 1, 1),
        context={
            "failure_category": "PROVISION_FAILURE",
            "provision_failures": 3,
            "provision_failures_infra": 2,
            "_infra_wait": True,
            "blacklisted_accounts": ["acct-1"],
            "max_plan_account_id": "acct-1",
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
    assert t.retry_after_until is None  # column source of truth
    for cleared in (
        "provision_failures",
        "provision_failures_infra",
        "_infra_wait",
        "blacklisted_accounts",
        "max_plan_account_id",
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

    reaper_db.add(
        Task(
            project_id=project.id,
            parent_task_id=parent.id,
            payloads=[{"type": "create"}],
            status=TaskStatus.COMPLETED,
        )
    )
    await reaper_db.commit()

    await _run_reaper_once()
    await reaper_db.refresh(parent)

    assert parent.status == TaskStatus.FAILED
    assert "_resurrection_count" not in (parent.context or {})


@pytest.mark.asyncio
async def test_step5_resurrection_records_status_without_durable_alert(
    reaper_db, stop_after_one_cycle, reaper_side_effects
):
    """Resurrection is a timeline fact, not a permanent unresolved alert."""
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
    await reaper_db.refresh(t)

    alerts = (
        (await reaper_db.execute(_select(Alert).where(Alert.task_id == t.id)))
        .scalars()
        .all()
    )
    assert t.status == TaskStatus.QUEUED
    assert "PROVISION_FAILURE" in (t.latest_status_message or "")
    assert alerts == []


@pytest.mark.asyncio
async def test_step5_pr_closed_unmerged_skips_resurrection(
    reaper_db, stop_after_one_cycle, reaper_side_effects
):
    """FAILED + EXTERNAL_API + pr_closed_unmerged=True → NOT resurrected.
    Operator-rejected PRs share the merge-blocking 'closed without merging'
    classification but are terminal, not infrastructure. Re-running produces
    the same rejected code."""
    project = await _make_project(reaper_db)
    t = Task(
        project_id=project.id,
        payloads=[{"type": "create"}],
        status=TaskStatus.FAILED,
        attempt_count=2,
        context={
            "failure_category": "EXTERNAL_API",
            "pr_closed_unmerged": True,
        },
    )
    reaper_db.add(t)
    await reaper_db.commit()

    await _run_reaper_once()
    await reaper_db.refresh(t)

    assert t.status == TaskStatus.FAILED
    assert "_resurrection_count" not in (t.context or {})


@pytest.mark.asyncio
async def test_step5_pr_closed_empty_skips_resurrection(
    reaper_db, stop_after_one_cycle, reaper_side_effects
):
    """Empty-rollup short-circuit closure → NOT resurrected. The branch was
    equivalent to base; re-running produces the same empty PR."""
    project = await _make_project(reaper_db)
    t = Task(
        project_id=project.id,
        payloads=[{"type": "create"}],
        status=TaskStatus.FAILED,
        attempt_count=2,
        context={
            "failure_category": "EXTERNAL_API",
            "pr_closed_empty": True,
        },
    )
    reaper_db.add(t)
    await reaper_db.commit()

    await _run_reaper_once()
    await reaper_db.refresh(t)

    assert t.status == TaskStatus.FAILED
    assert "_resurrection_count" not in (t.context or {})


@pytest.mark.asyncio
async def test_step5_pr_rejected_skips_resurrection(
    reaper_db, stop_after_one_cycle, reaper_side_effects
):
    """Review-pipeline rejection (pr_rejected) → NOT resurrected."""
    project = await _make_project(reaper_db)
    t = Task(
        project_id=project.id,
        payloads=[{"type": "create"}],
        status=TaskStatus.FAILED,
        attempt_count=2,
        context={
            "failure_category": "EXTERNAL_API",
            "pr_rejected": True,
        },
    )
    reaper_db.add(t)
    await reaper_db.commit()

    await _run_reaper_once()
    await reaper_db.refresh(t)

    assert t.status == TaskStatus.FAILED
    assert "_resurrection_count" not in (t.context or {})


@pytest.mark.asyncio
async def test_step5_idempotency_collision_skipped_and_downstream_sweep_survives(
    reaper_db, stop_after_one_cycle, reaper_side_effects
):
    """A FAILED infra task whose idempotency_key is already held by an ACTIVE
    task must NOT be resurrected: flipping it to QUEUED would violate the
    partial unique index ux_task_active_idempotency_key and, on the reaper's
    shared session, cascade PendingRollbackError into every downstream sweep
    (the production restart-loop incident). Asserts the collision is skipped
    AND a later sweep (Step 5b far-future retry_after clearing) still runs —
    before the fix the poisoned session would skip it."""
    project = await _make_project(reaper_db)

    # Live duplicate already occupying the idempotency_key in an ACTIVE state.
    live = Task(
        project_id=project.id,
        payloads=[{"type": "create"}],
        status=TaskStatus.QUEUED,
        idempotency_key="dup-key",
    )
    # FAILED infra task sharing that key — the resurrection candidate that
    # would collide on commit.
    failed_dup = Task(
        project_id=project.id,
        payloads=[{"type": "create"}],
        status=TaskStatus.FAILED,
        attempt_count=4,
        idempotency_key="dup-key",
        context={"failure_category": "PROVISION_FAILURE"},
    )
    # Independent QUEUED task with a far-future retry_after — Step 5b must clear
    # it. If the collision poisoned the shared session, Step 5b would be skipped
    # and this would stay set.
    stale_retry = Task(
        project_id=project.id,
        payloads=[{"type": "create"}],
        status=TaskStatus.QUEUED,
        retry_after_until=_utcnow() + timedelta(hours=6),
    )
    reaper_db.add_all([live, failed_dup, stale_retry])
    await reaper_db.commit()

    await _run_reaper_once()
    for _t in (live, failed_dup, stale_retry):
        await reaper_db.refresh(_t)

    # Collision skipped: the FAILED dup is never resurrected.
    assert failed_dup.status == TaskStatus.FAILED
    assert "_resurrection_count" not in (failed_dup.context or {})
    # The live duplicate is untouched.
    assert live.status == TaskStatus.QUEUED
    # Downstream sweep survived the (skipped) collision: retry_after cleared.
    assert stale_retry.retry_after_until is None


@pytest.mark.asyncio
async def test_step5_idempotency_collision_with_parked_holder_skipped(
    reaper_db, stop_after_one_cycle, reaper_side_effects
):
    """Same collision guard, but the active key-holder is PARKED — a status the
    ux_task_active_idempotency_key index counts (migration 0199) but the
    resurrection pre-query historically omitted. Before the guard was widened to
    include PARKED/BLOCKED, the FAILED dup would be flipped to QUEUED and collide
    on the partial unique index → the same all-batch PendingRollbackError the
    QUEUED-holder case guards against. The holder must be detected and the
    resurrection skipped."""
    project = await _make_project(reaper_db)

    # Active key-holder is PARKED (non-terminal; index counts it).
    parked_holder = Task(
        project_id=project.id,
        payloads=[{"type": "create"}],
        status=TaskStatus.PARKED,
        idempotency_key="dup-key-parked",
    )
    failed_dup = Task(
        project_id=project.id,
        payloads=[{"type": "create"}],
        status=TaskStatus.FAILED,
        attempt_count=4,
        idempotency_key="dup-key-parked",
        context={"failure_category": "PROVISION_FAILURE"},
    )
    reaper_db.add_all([parked_holder, failed_dup])
    await reaper_db.commit()

    await _run_reaper_once()
    for _t in (parked_holder, failed_dup):
        await reaper_db.refresh(_t)

    # The PARKED holder is detected by the widened guard → resurrection skipped.
    assert failed_dup.status == TaskStatus.FAILED
    assert "_resurrection_count" not in (failed_dup.context or {})
    assert parked_holder.status == TaskStatus.PARKED


@pytest.mark.asyncio
async def test_step5_resurrection_clears_failure_class(
    reaper_db, stop_after_one_cycle, reaper_side_effects
):
    """Resurrection sweep clears stale failure_class so subsequent failures
    are reclassified instead of short-circuited to the old verdict.

    When a task is resurrected from an infra failure, both failure_category
    AND failure_class must be cleared so the resurrected task's next failure
    is properly classified via _classify_failure_class, not short-circuited
    to the stale verdict. Without this fix, a CODE_ERROR on the resurrected
    task would be misclassified as the old PROVISION_FAILURE."""
    project = await _make_project(reaper_db)

    # Create a FAILED task with a stale infra failure_class that will be
    # short-circuited by _classify_failure_class if not cleared.
    t = Task(
        project_id=project.id,
        payloads=[{"type": "create"}],
        status=TaskStatus.FAILED,
        attempt_count=4,
        context={
            "failure_category": "PROVISION_FAILURE",
            "failure_class": "PROVISION_FAILURE",
            "provision_failures": 3,
            "provision_failures_infra": 2,
            "_infra_wait": True,
        },
    )
    reaper_db.add(t)
    await reaper_db.commit()

    # Resurrect via the infra-recovery sweep.
    await _run_reaper_once()
    await reaper_db.refresh(t)

    ctx = t.context or {}

    # Verify resurrection occurred.
    assert t.status == TaskStatus.QUEUED
    assert t.attempt_count == 0
    assert ctx.get("_resurrection_count") == 1

    # Verify BOTH failure_class AND failure_category are cleared
    # so the next failure is properly reclassified.
    assert "failure_class" not in ctx, "failure_class must be cleared on resurrection"
    assert "failure_category" not in ctx, (
        "failure_category must be cleared on resurrection"
    )


# ── Dependent-task unblocking on terminal failure ────────────────────────


def _install_dep_unblock_compat(mocker, blocked_id_str: str):
    """Replace app.reaper_post_orphan_sweep.AsyncSessionLocal with a proxy
    factory that routes the dep-unblock session through a Python-level filter
    (SQLite lacks the JSONB @> operator used by Task.depends_on.contains(...)).
    The dep-unblocking logic was extracted to reaper_post_orphan_sweep.py and
    opens its own session via AsyncSessionLocal() — every call through that
    module returns a proxy that rewrites the dep-unblock query."""
    import app.reaper_post_orphan_sweep as _post_sweep_mod

    real_asl = _post_sweep_mod.AsyncSessionLocal
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
            # All calls in reaper_post_orphan_sweep are dependency-unblock
            # sessions — always return the proxy.
            return _DepSessionProxy(inner)

    mocker.patch("app.reaper_post_orphan_sweep.AsyncSessionLocal", _SmartASL())
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
        last_heartbeat=_utcnow() - timedelta(minutes=10),
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
        last_heartbeat=_utcnow() - timedelta(minutes=10),
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
        last_heartbeat=_utcnow() - timedelta(minutes=10),
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
        last_heartbeat=_utcnow() - timedelta(minutes=10),
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
    # No dep-unblock session should open — the tracker only counts
    # reaper_post_orphan_sweep.AsyncSessionLocal dependency-unblock calls,
    # which do not fire for a retried (non-terminal) task.
    assert call_tracker["n"] == 0


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


def test_resolve_retry_budget_agent_timeout_scope_aware():
    """AGENT_TIMEOUT has scope-specific budgets; missing scope falls back to uniform."""
    from app.shared_state import resolve_retry_budget, runtime_config

    assert resolve_retry_budget("AGENT_TIMEOUT", "small") == 2
    assert resolve_retry_budget("AGENT_TIMEOUT", "medium") == 3
    assert resolve_retry_budget("AGENT_TIMEOUT", "large") == 4

    uniform = runtime_config.get("retry_budgets_by_class", {}).get(
        "AGENT_TIMEOUT", runtime_config["max_retries"]
    )
    assert uniform == 3
    assert resolve_retry_budget("AGENT_TIMEOUT", None) == uniform
    assert resolve_retry_budget("AGENT_TIMEOUT", "unknown_scope") == uniform


def test_transient_5xx_patterns_constant():
    from app.reaper import _TRANSIENT_5XX_PATTERNS

    assert isinstance(_TRANSIENT_5XX_PATTERNS, frozenset)
    for p in (
        "http 502",
        "http 503",
        "http 504",
        "502 bad gateway",
        "503 service",
        "504 gateway",
        "server error",
    ):
        assert p in _TRANSIENT_5XX_PATTERNS


# ── _sweep_orphan_direction_holds ──────────────────────────────────────────


@pytest.mark.asyncio
async def test_orphan_sweep_clears_hold_when_direction_missing(reaper_db):
    """A held direction whose id is absent from every project's directions
    list must have its hold cleared — otherwise tasks stay stuck forever."""
    import app.shared_state as _ss
    from app.reaper_infra_recovery import _sweep_orphan_direction_holds

    _ss.direction_failure_streaks.clear()
    # Project has no directions at all — all held directions are orphans.
    await _make_project(reaper_db)
    orphan_did = "d-" + _uuid_mod.uuid4().hex[:8]
    _ss.direction_failure_streaks[orphan_did] = {
        "held": True,
        "held_at": datetime(2026, 1, 1, 12, 0, 0),
        "held_reason": "code",
        "held_failure_class": "CODE_ERROR",
        "consecutive_failures": 4,
        "consecutive_infra_failures": 0,
        "last_failure_class": "CODE_ERROR",
    }

    cleared = await _sweep_orphan_direction_holds(reaper_db)
    assert cleared == 1
    assert _ss.direction_failure_streaks[orphan_did]["held"] is False


@pytest.mark.asyncio
async def test_orphan_sweep_preserves_hold_when_direction_live(reaper_db):
    """A held direction that still exists (status=active) must NOT be cleared."""
    import app.shared_state as _ss
    from app.reaper_infra_recovery import _sweep_orphan_direction_holds

    _ss.direction_failure_streaks.clear()
    live_did = "d-" + _uuid_mod.uuid4().hex[:8]
    project = Project(
        name="has-live-dir",
        user_id="user1",
        directions=[{"id": live_did, "status": "active", "text": "do the thing"}],
    )
    reaper_db.add(project)
    await reaper_db.commit()

    _ss.direction_failure_streaks[live_did] = {
        "held": True,
        "held_at": datetime(2026, 1, 1, 12, 0, 0),
        "held_reason": "code",
        "held_failure_class": "CODE_ERROR",
        "consecutive_failures": 4,
        "consecutive_infra_failures": 0,
        "last_failure_class": "CODE_ERROR",
    }

    cleared = await _sweep_orphan_direction_holds(reaper_db)
    assert cleared == 0
    assert _ss.direction_failure_streaks[live_did]["held"] is True


@pytest.mark.asyncio
async def test_orphan_sweep_clears_hold_when_direction_completed(reaper_db):
    """A direction with status=completed is no longer live — hold is orphaned."""
    import app.shared_state as _ss
    from app.reaper_infra_recovery import _sweep_orphan_direction_holds

    _ss.direction_failure_streaks.clear()
    completed_did = "d-" + _uuid_mod.uuid4().hex[:8]
    project = Project(
        name="has-completed-dir",
        user_id="user1",
        directions=[{"id": completed_did, "status": "completed", "text": "done"}],
    )
    reaper_db.add(project)
    await reaper_db.commit()

    _ss.direction_failure_streaks[completed_did] = {
        "held": True,
        "held_at": datetime(2026, 1, 1, 12, 0, 0),
        "held_reason": "code",
        "held_failure_class": "CODE_ERROR",
        "consecutive_failures": 4,
        "consecutive_infra_failures": 0,
        "last_failure_class": "CODE_ERROR",
    }

    cleared = await _sweep_orphan_direction_holds(reaper_db)
    assert cleared == 1
    assert _ss.direction_failure_streaks[completed_did]["held"] is False


@pytest.mark.asyncio
async def test_orphan_sweep_ignores_unheld_streaks(reaper_db):
    """Streaks without held=True are untouched regardless of direction state."""
    import app.shared_state as _ss
    from app.reaper_infra_recovery import _sweep_orphan_direction_holds

    _ss.direction_failure_streaks.clear()
    await _make_project(reaper_db)
    unheld_did = "d-" + _uuid_mod.uuid4().hex[:8]
    _ss.direction_failure_streaks[unheld_did] = {
        "held": False,
        "consecutive_failures": 2,
        "consecutive_infra_failures": 0,
        "last_failure_class": "CODE_ERROR",
    }

    cleared = await _sweep_orphan_direction_holds(reaper_db)
    assert cleared == 0
    # Streak dict itself is untouched
    assert _ss.direction_failure_streaks[unheld_did]["consecutive_failures"] == 2


# ── _sweep_stale_residual_cases ───────────────────────────────────────────


@pytest.mark.asyncio
async def test_sweep_stale_residual_cases_closes_active_old(reaper_db):
    """An ACTIVE case older than 72 h must be closed and its fingerprint marked."""
    import app.failure_classification as _fc
    from app.models import ResidualRemediationCase, ResidualCaseStatus
    from app.reaper_infra_recovery import _sweep_stale_residual_cases

    _fc._residual_closed_fingerprints.clear()
    stale_case = ResidualRemediationCase(
        fingerprint="old-fp-abc123",
        status=ResidualCaseStatus.ACTIVE,
        last_seen_at=_utcnow() - timedelta(hours=73),
    )
    reaper_db.add(stale_case)
    await reaper_db.commit()
    await reaper_db.refresh(stale_case)

    closed = await _sweep_stale_residual_cases(reaper_db)

    assert closed == 1
    await reaper_db.refresh(stale_case)
    assert stale_case.status == ResidualCaseStatus.CLOSED_ORGANICALLY
    assert "old-fp-abc123" in _fc._residual_closed_fingerprints


@pytest.mark.asyncio
async def test_sweep_stale_residual_cases_ignores_recent(reaper_db):
    """An ACTIVE case seen within 72 h must not be touched."""
    import app.failure_classification as _fc
    from app.models import ResidualRemediationCase, ResidualCaseStatus
    from app.reaper_infra_recovery import _sweep_stale_residual_cases

    _fc._residual_closed_fingerprints.clear()
    recent_case = ResidualRemediationCase(
        fingerprint="recent-fp-xyz",
        status=ResidualCaseStatus.ACTIVE,
        last_seen_at=_utcnow() - timedelta(hours=1),
    )
    reaper_db.add(recent_case)
    await reaper_db.commit()
    await reaper_db.refresh(recent_case)

    closed = await _sweep_stale_residual_cases(reaper_db)

    assert closed == 0
    await reaper_db.refresh(recent_case)
    assert recent_case.status == ResidualCaseStatus.ACTIVE
    assert "recent-fp-xyz" not in _fc._residual_closed_fingerprints


@pytest.mark.asyncio
async def test_sweep_stale_residual_cases_ignores_already_closed(reaper_db):
    """A case already in CLOSED_ORGANICALLY must not be counted or re-touched."""
    import app.failure_classification as _fc
    from app.models import ResidualRemediationCase, ResidualCaseStatus
    from app.reaper_infra_recovery import _sweep_stale_residual_cases

    _fc._residual_closed_fingerprints.clear()
    already_closed = ResidualRemediationCase(
        fingerprint="closed-fp-already",
        status=ResidualCaseStatus.CLOSED_ORGANICALLY,
        last_seen_at=_utcnow() - timedelta(hours=100),
    )
    reaper_db.add(already_closed)
    await reaper_db.commit()
    await reaper_db.refresh(already_closed)

    closed = await _sweep_stale_residual_cases(reaper_db)

    assert closed == 0
    assert "closed-fp-already" not in _fc._residual_closed_fingerprints


@pytest.mark.asyncio
async def test_sweep_stale_residual_cases_resolves_stale_remediating(reaper_db, mocker):
    """A REMEDIATING case quiet beyond the cutoff auto-resolves and emits an event."""
    import app.failure_classification as _fc
    from app.models import ResidualRemediationCase, ResidualCaseStatus
    from app.reaper_infra_recovery import _sweep_stale_residual_cases

    event_mock = mocker.patch(
        "app.notification_dispatch.post_event_message", new_callable=AsyncMock
    )
    _fc._residual_closed_fingerprints.clear()
    stale_case = ResidualRemediationCase(
        fingerprint="remediating-old-fp",
        status=ResidualCaseStatus.REMEDIATING,
        project_id=_uuid_mod.uuid4(),
        last_seen_at=_utcnow() - timedelta(hours=73),
        last_spawned_at=_utcnow() - timedelta(hours=73),
        last_spawned_task_id=_uuid_mod.uuid4(),
    )
    reaper_db.add(stale_case)
    await reaper_db.commit()
    await reaper_db.refresh(stale_case)

    closed = await _sweep_stale_residual_cases(reaper_db)

    assert closed == 1
    await reaper_db.refresh(stale_case)
    assert stale_case.status == ResidualCaseStatus.RESOLVED
    assert "remediating-old-fp" in _fc._residual_closed_fingerprints
    event_mock.assert_awaited_once()


@pytest.mark.asyncio
async def test_sweep_stale_residual_cases_keeps_recent_remediating(reaper_db, mocker):
    """A REMEDIATING case with recent spawn/seen timestamps should not auto-resolve."""
    import app.failure_classification as _fc
    from app.models import ResidualRemediationCase, ResidualCaseStatus
    from app.reaper_infra_recovery import _sweep_stale_residual_cases

    event_mock = mocker.patch(
        "app.notification_dispatch.post_event_message", new_callable=AsyncMock
    )
    _fc._residual_closed_fingerprints.clear()
    recent_case = ResidualRemediationCase(
        fingerprint="remediating-recent-fp",
        status=ResidualCaseStatus.REMEDIATING,
        project_id=_uuid_mod.uuid4(),
        last_seen_at=_utcnow() - timedelta(hours=1),
        last_spawned_at=_utcnow() - timedelta(hours=1),
        last_spawned_task_id=_uuid_mod.uuid4(),
    )
    reaper_db.add(recent_case)
    await reaper_db.commit()
    await reaper_db.refresh(recent_case)

    closed = await _sweep_stale_residual_cases(reaper_db)

    assert closed == 0
    await reaper_db.refresh(recent_case)
    assert recent_case.status == ResidualCaseStatus.REMEDIATING
    assert "remediating-recent-fp" not in _fc._residual_closed_fingerprints
    event_mock.assert_not_awaited()


# ── _task_has_open_pr + reap-with-PR handoff to EVALUATING ────────────────


@pytest.mark.asyncio
async def test_task_has_open_pr_finds_artifact(reaper_db):
    """Helper returns (True, pr_number) when the task has a PULL_REQUEST artifact."""
    from app.reaper import _task_has_open_pr

    project = await _make_project(reaper_db)
    t = Task(
        project_id=project.id,
        payloads=[{"type": "create"}],
        status=TaskStatus.IN_PROGRESS,
    )
    reaper_db.add(t)
    await reaper_db.commit()
    await reaper_db.refresh(t)

    reaper_db.add(
        Artifact(
            task_id=t.id,
            artifact_type=ArtifactType.PULL_REQUEST,
            pr_url="https://github.com/HenryHamster/BlueprintAIStudio/pull/2903",
        )
    )
    await reaper_db.commit()

    has_pr, pr_num = await _task_has_open_pr(t, reaper_db)
    assert has_pr is True
    assert pr_num == 2903


@pytest.mark.asyncio
async def test_task_has_open_pr_no_artifact(reaper_db):
    """Helper returns (False, None) when no PULL_REQUEST artifact exists."""
    from app.reaper import _task_has_open_pr

    project = await _make_project(reaper_db)
    t = Task(
        project_id=project.id,
        payloads=[{"type": "create"}],
        status=TaskStatus.IN_PROGRESS,
    )
    reaper_db.add(t)
    await reaper_db.commit()
    await reaper_db.refresh(t)

    has_pr, pr_num = await _task_has_open_pr(t, reaper_db)
    assert has_pr is False
    assert pr_num is None


@pytest.mark.asyncio
async def test_task_has_open_pr_malformed_url(reaper_db):
    """Helper returns (False, None) when the PR URL can't be parsed."""
    from app.reaper import _task_has_open_pr

    project = await _make_project(reaper_db)
    t = Task(
        project_id=project.id,
        payloads=[{"type": "create"}],
        status=TaskStatus.IN_PROGRESS,
    )
    reaper_db.add(t)
    await reaper_db.commit()
    await reaper_db.refresh(t)

    reaper_db.add(
        Artifact(
            task_id=t.id,
            artifact_type=ArtifactType.PULL_REQUEST,
            pr_url="not-a-url",
        )
    )
    await reaper_db.commit()

    has_pr, pr_num = await _task_has_open_pr(t, reaper_db)
    assert has_pr is False
    assert pr_num is None


@pytest.mark.asyncio
async def test_reap_with_open_pr_goes_to_evaluating(
    reaper_db, stop_after_one_cycle, reaper_side_effects, mocker
):
    """Transient reap with a PR artifact → EVALUATING, not QUEUED.
    The retry-from-scratch path would collide with the already-pushed branch;
    the review pipeline should adjudicate the PR instead.
    """
    mocker.patch(
        "app.review_pipeline._spawn_pipeline_task",
        new_callable=AsyncMock,
        return_value=(None, True),
    )

    project = await _make_project(reaper_db)
    t = Task(
        project_id=project.id,
        payloads=[{"type": "create"}],
        vm_instance_id="vm-with-pr",
        status=TaskStatus.IN_PROGRESS,
        attempt_count=0,
        last_heartbeat=_utcnow() - timedelta(minutes=10),
        context={"scope": "small"},
    )
    reaper_db.add(t)
    await reaper_db.commit()
    await reaper_db.refresh(t)

    reaper_db.add(
        Artifact(
            task_id=t.id,
            artifact_type=ArtifactType.PULL_REQUEST,
            pr_url="https://github.com/HenryHamster/BlueprintAIStudio/pull/1234",
        )
    )
    await reaper_db.commit()

    await _run_reaper_once()
    await reaper_db.refresh(t)

    assert t.status == TaskStatus.EVALUATING
    assert t.vm_instance_id is None
    assert "#1234" in (t.latest_status_message or "")
    assert "handed off to review pipeline" in (t.latest_status_message or "")
    # C6: on confirmed spawn success the write-ahead recovery marker is dropped.
    assert not (t.context or {}).get("_pr_state_check_pending"), (
        "marker must be cleared once the review spawn is confirmed"
    )


@pytest.mark.asyncio
async def test_reap_with_open_pr_spawn_failure_keeps_recovery_marker(
    reaper_db, stop_after_one_cycle, reaper_side_effects, mocker
):
    """C6 seam: when the reaper's open-PR handoff review spawn FAILS, the parent
    must stay EVALUATING AND carry `_pr_state_check_pending=True` so the services
    `_check_evaluating_timeouts` consumer re-checks the PR and re-spawns review.

    Pre-fix the reaper flipped the parent to EVALUATING and spawned best-effort
    with NO recovery marker — a spawn failure left a childless EVALUATING parent
    invisible to reaper Step 3 (needs a COMPLETED child) and Step 4 (skips
    childless parents), stranded until the 6h/48h backstop.
    Would FAIL pre-fix: `_pr_state_check_pending` would be absent.
    """
    mocker.patch(
        "app.review_pipeline._spawn_pipeline_task",
        new_callable=AsyncMock,
        side_effect=RuntimeError("spawn boom"),
    )

    project = await _make_project(reaper_db)
    t = Task(
        project_id=project.id,
        payloads=[{"type": "create"}],
        vm_instance_id="vm-with-pr-spawnfail",
        status=TaskStatus.IN_PROGRESS,
        attempt_count=0,
        last_heartbeat=_utcnow() - timedelta(minutes=10),
        context={"scope": "small"},
    )
    reaper_db.add(t)
    await reaper_db.commit()
    await reaper_db.refresh(t)

    reaper_db.add(
        Artifact(
            task_id=t.id,
            artifact_type=ArtifactType.PULL_REQUEST,
            pr_url="https://github.com/HenryHamster/BlueprintAIStudio/pull/5678",
        )
    )
    await reaper_db.commit()

    await _run_reaper_once()
    await reaper_db.refresh(t)

    assert t.status == TaskStatus.EVALUATING, (
        f"Expected EVALUATING after reaper spawn failure, got {t.status}"
    )
    assert (t.context or {}).get("_pr_state_check_pending") is True, (
        "reaper spawn failure must leave _pr_state_check_pending set so the "
        "evaluating-sweep consumer can recover the childless parent"
    )


@pytest.mark.asyncio
async def test_reap_without_pr_still_retries(
    reaper_db, stop_after_one_cycle, reaper_side_effects, mocker
):
    """Transient reap on a task without a PR artifact → existing QUEUED behaviour preserved."""
    project = await _make_project(reaper_db)
    t = Task(
        project_id=project.id,
        payloads=[{"type": "create"}],
        vm_instance_id="vm-no-pr",
        status=TaskStatus.IN_PROGRESS,
        attempt_count=0,
        last_heartbeat=_utcnow() - timedelta(minutes=10),
        context={"scope": "small"},
    )
    reaper_db.add(t)
    await reaper_db.commit()
    await reaper_db.refresh(t)

    await _run_reaper_once()
    await reaper_db.refresh(t)

    # Without a PR artifact, the existing retry path fires.
    assert t.status == TaskStatus.QUEUED
    assert t.attempt_count >= 1


@pytest.mark.asyncio
async def test_terminal_reap_with_open_pr_goes_to_evaluating(
    reaper_db, stop_after_one_cycle, reaper_side_effects, mocker
):
    """Task at can_retry=False with a PR artifact → EVALUATING, not FAILED.
    The review pipeline gets a chance to adjudicate; the 6h EVALUATING timeout
    and 16-iter review-fix cap guarantee eventual termination.
    """
    mocker.patch(
        "app.review_pipeline._spawn_pipeline_task",
        new_callable=AsyncMock,
        return_value=(None, True),
    )

    project = await _make_project(reaper_db)
    t = Task(
        project_id=project.id,
        payloads=[{"type": "create"}],
        vm_instance_id="vm-terminal-pr",
        status=TaskStatus.IN_PROGRESS,
        attempt_count=3,  # already at max for most classes
        last_heartbeat=_utcnow() - timedelta(minutes=10),
        context={"scope": "small"},
    )
    reaper_db.add(t)
    await reaper_db.commit()
    await reaper_db.refresh(t)

    reaper_db.add(
        Artifact(
            task_id=t.id,
            artifact_type=ArtifactType.PULL_REQUEST,
            pr_url="https://github.com/HenryHamster/BlueprintAIStudio/pull/9999",
        )
    )
    await reaper_db.commit()

    await _run_reaper_once()
    await reaper_db.refresh(t)

    assert t.status == TaskStatus.EVALUATING
    assert "#9999" in (t.latest_status_message or "")


@pytest.mark.asyncio
async def test_terminal_reap_without_pr_still_fails(
    reaper_db, stop_after_one_cycle, reaper_side_effects, mocker
):
    """Terminal reap without a PR artifact → existing FAILED behaviour preserved."""
    project = await _make_project(reaper_db)
    t = Task(
        project_id=project.id,
        payloads=[{"type": "create"}],
        vm_instance_id="vm-terminal-no-pr",
        status=TaskStatus.IN_PROGRESS,
        attempt_count=5,  # definitely over any class budget
        last_heartbeat=_utcnow() - timedelta(minutes=10),
        context={"scope": "small"},
    )
    reaper_db.add(t)
    await reaper_db.commit()
    await reaper_db.refresh(t)

    await _run_reaper_once()
    await reaper_db.refresh(t)

    assert t.status == TaskStatus.FAILED


@pytest.mark.asyncio
async def test_reap_with_closed_unmerged_pr_marks_failed(
    reaper_db, stop_after_one_cycle, reaper_side_effects, mocker
):
    """Reap with PR artifact + GitHub reports state=closed, merged=False →
    FAILED with pr_closed_unmerged marker, no review-pipeline handoff. Without
    this, the task wedges in EVALUATING since review can't act on a closed PR."""
    mocker.patch(
        "app.routers.agent.get_github_token_for_task",
        new_callable=AsyncMock,
        return_value=("tok", "owner", "repo"),
    )
    mocker.patch(
        "app.github.get_pull_request",
        new_callable=AsyncMock,
        return_value={"state": "closed", "merged": False},
    )
    _spawn = mocker.patch(
        "app.review_pipeline._spawn_pipeline_task",
        new_callable=AsyncMock,
        return_value=(None, True),
    )

    project = await _make_project(reaper_db)
    t = Task(
        project_id=project.id,
        payloads=[{"type": "create"}],
        vm_instance_id="vm-closed-pr",
        status=TaskStatus.IN_PROGRESS,
        attempt_count=0,
        last_heartbeat=_utcnow() - timedelta(minutes=10),
        context={"scope": "small"},
    )
    reaper_db.add(t)
    await reaper_db.commit()
    await reaper_db.refresh(t)

    reaper_db.add(
        Artifact(
            task_id=t.id,
            artifact_type=ArtifactType.PULL_REQUEST,
            pr_url="https://github.com/HenryHamster/BlueprintAIStudio/pull/3444",
        )
    )
    await reaper_db.commit()

    await _run_reaper_once()
    await reaper_db.refresh(t)

    assert t.status == TaskStatus.FAILED
    assert (t.context or {}).get("pr_closed_unmerged") is True
    assert "#3444" in (t.latest_status_message or "")
    assert "closed without merging" in (t.latest_status_message or "")
    _spawn.assert_not_called()


@pytest.mark.asyncio
async def test_reap_with_merged_pr_marks_completed(
    reaper_db, stop_after_one_cycle, reaper_side_effects, mocker
):
    """Reap with PR artifact + GitHub reports state=closed, merged=True →
    COMPLETED with pr_merged marker. Covers the case where the PR merged
    out-of-band before completion-processing ran."""
    mocker.patch(
        "app.routers.agent.get_github_token_for_task",
        new_callable=AsyncMock,
        return_value=("tok", "owner", "repo"),
    )
    mocker.patch(
        "app.github.get_pull_request",
        new_callable=AsyncMock,
        return_value={"state": "closed", "merged": True, "base": {"ref": "main"}},
    )
    _spawn = mocker.patch(
        "app.review_pipeline._spawn_pipeline_task",
        new_callable=AsyncMock,
        return_value=(None, True),
    )

    project = await _make_project(reaper_db)
    t = Task(
        project_id=project.id,
        payloads=[{"type": "create", "base_branch": "automain"}],
        vm_instance_id="vm-merged-pr",
        status=TaskStatus.IN_PROGRESS,
        attempt_count=0,
        last_heartbeat=_utcnow() - timedelta(minutes=10),
        context={"scope": "small"},
    )
    reaper_db.add(t)
    await reaper_db.commit()
    await reaper_db.refresh(t)

    reaper_db.add(
        Artifact(
            task_id=t.id,
            artifact_type=ArtifactType.PULL_REQUEST,
            pr_url="https://github.com/HenryHamster/BlueprintAIStudio/pull/5555",
        )
    )
    await reaper_db.commit()

    await _run_reaper_once()
    await reaper_db.refresh(t)

    assert t.status == TaskStatus.COMPLETED
    assert (t.context or {}).get("pr_merged") is True
    assert "#5555" in (t.latest_status_message or "")
    alerts = (
        (await reaper_db.execute(select(Alert).where(Alert.task_id == t.id)))
        .scalars()
        .all()
    )
    assert alerts == []
    _spawn.assert_not_called()


@pytest.mark.asyncio
async def test_reap_with_merged_pr_on_distinct_promotion_target_alerts_without_pr_merged(
    reaper_db, stop_after_one_cycle, reaper_side_effects, mocker
):
    """A merged PR on promotion_target must complete the task without setting pr_merged."""
    dispatch_mock = mocker.patch(
        "app.notification_dispatch.dispatch_external_notification",
        new_callable=AsyncMock,
    )
    mocker.patch(
        "app.routers.agent.get_github_token_for_task",
        new_callable=AsyncMock,
        return_value=("tok", "owner", "repo"),
    )
    mocker.patch(
        "app.github.get_pull_request",
        new_callable=AsyncMock,
        return_value={"state": "closed", "merged": True, "base": {"ref": "main"}},
    )
    _spawn = mocker.patch(
        "app.review_pipeline._spawn_pipeline_task",
        new_callable=AsyncMock,
        return_value=(None, True),
    )

    project = Project(
        name="reaper-promotion-guard",
        user_id="user1",
        integration_branch="automain",
        promotion_target="main",
    )
    reaper_db.add(project)
    await reaper_db.commit()
    await reaper_db.refresh(project)

    t = Task(
        project_id=project.id,
        payloads=[{"type": "create", "base_branch": "automain"}],
        vm_instance_id="vm-merged-pr-promotion",
        status=TaskStatus.IN_PROGRESS,
        attempt_count=0,
        last_heartbeat=_utcnow() - timedelta(minutes=10),
        context={"scope": "small"},
    )
    reaper_db.add(t)
    await reaper_db.commit()
    await reaper_db.refresh(t)

    reaper_db.add(
        Artifact(
            task_id=t.id,
            artifact_type=ArtifactType.PULL_REQUEST,
            pr_url="https://github.com/HenryHamster/BlueprintAIStudio/pull/5556",
        )
    )
    await reaper_db.commit()

    await _run_reaper_once()
    loop = asyncio.get_running_loop()
    tick = loop.create_future()
    loop.call_soon(tick.set_result, None)
    await tick
    await reaper_db.refresh(t)

    assert t.status == TaskStatus.COMPLETED
    assert (t.context or {}).get("pr_merged") is None
    alerts = (
        (await reaper_db.execute(select(Alert).where(Alert.task_id == t.id)))
        .scalars()
        .all()
    )
    assert len(alerts) == 1
    assert alerts[0].severity == AlertSeverity.ERROR
    assert alerts[0].title == "Merged PR landed on promotion target before integration"
    approval_calls = [
        c for c in dispatch_mock.call_args_list if c.args[1] == "APPROVAL_NEEDED"
    ]
    assert len(approval_calls) == 1
    assert approval_calls[0].args[2]["pr_number"] == 5556
    _spawn.assert_not_called()


@pytest.mark.asyncio
async def test_reap_with_merged_pr_on_intermediate_staged_rollup_stays_evaluating(
    reaper_db, stop_after_one_cycle, reaper_side_effects, mocker
):
    dispatch_mock = mocker.patch(
        "app.notification_dispatch.dispatch_external_notification",
        new_callable=AsyncMock,
    )
    mocker.patch(
        "app.routers.agent.get_github_token_for_task",
        new_callable=AsyncMock,
        return_value=("tok", "owner", "repo"),
    )
    mocker.patch(
        "app.github.get_pull_request",
        new_callable=AsyncMock,
        return_value={
            "state": "closed",
            "merged": True,
            "base": {"ref": "release/candidate-1"},
        },
    )
    _spawn = mocker.patch(
        "app.review_pipeline._spawn_pipeline_task",
        new_callable=AsyncMock,
        return_value=(None, True),
    )

    project = Project(
        name="reaper-intermediate-rollup",
        user_id="user1",
        integration_branch="automain",
        promotion_target="release/main",
        build_config={"staging_branch_prefixes": ["release/"]},
    )
    reaper_db.add(project)
    await reaper_db.commit()
    await reaper_db.refresh(project)

    t = Task(
        project_id=project.id,
        payloads=[{"type": "research", "base_branch": "release/candidate-1"}],
        vm_instance_id="vm-merged-pr-intermediate",
        status=TaskStatus.IN_PROGRESS,
        attempt_count=0,
        last_heartbeat=_utcnow() - timedelta(minutes=10),
        context={"scope": "small", "rollup_pr_number": 5557},
    )
    reaper_db.add(t)
    await reaper_db.commit()
    await reaper_db.refresh(t)

    reaper_db.add(
        Artifact(
            task_id=t.id,
            artifact_type=ArtifactType.PULL_REQUEST,
            pr_url="https://github.com/HenryHamster/BlueprintAIStudio/pull/5557",
        )
    )
    await reaper_db.commit()

    await _run_reaper_once()
    await reaper_db.refresh(t)

    assert t.status == TaskStatus.EVALUATING
    assert (t.context or {}).get("pr_merged") is None
    assert "awaiting downstream landing" in (t.latest_status_message or "")
    approval_calls = [
        c for c in dispatch_mock.call_args_list if c.args[1] == "APPROVAL_NEEDED"
    ]
    assert approval_calls == []
    _spawn.assert_not_called()


@pytest.mark.asyncio
async def test_reap_with_open_pr_via_gh_check_goes_to_evaluating(
    reaper_db, stop_after_one_cycle, reaper_side_effects, mocker
):
    """Reap with PR artifact + GitHub reports state=open → existing handoff
    to EVALUATING with review-pipeline spawn. Confirms the GH-state check
    doesn't regress the happy path."""
    mocker.patch(
        "app.routers.agent.get_github_token_for_task",
        new_callable=AsyncMock,
        return_value=("tok", "owner", "repo"),
    )
    mocker.patch(
        "app.github.get_pull_request",
        new_callable=AsyncMock,
        return_value={"state": "open", "merged": False},
    )
    _spawn = mocker.patch(
        "app.review_pipeline._spawn_pipeline_task",
        new_callable=AsyncMock,
        return_value=(None, True),
    )

    project = await _make_project(reaper_db)
    t = Task(
        project_id=project.id,
        payloads=[{"type": "create"}],
        vm_instance_id="vm-open-pr",
        status=TaskStatus.IN_PROGRESS,
        attempt_count=0,
        last_heartbeat=_utcnow() - timedelta(minutes=10),
        context={"scope": "small"},
    )
    reaper_db.add(t)
    await reaper_db.commit()
    await reaper_db.refresh(t)

    reaper_db.add(
        Artifact(
            task_id=t.id,
            artifact_type=ArtifactType.PULL_REQUEST,
            pr_url="https://github.com/HenryHamster/BlueprintAIStudio/pull/7777",
        )
    )
    await reaper_db.commit()

    await _run_reaper_once()
    await reaper_db.refresh(t)

    assert t.status == TaskStatus.EVALUATING
    assert "#7777" in (t.latest_status_message or "")
    assert "handed off to review pipeline" in (t.latest_status_message or "")
    _spawn.assert_called_once()


@pytest.mark.asyncio
async def test_reap_with_pr_falls_back_to_open_when_gh_unreachable(
    reaper_db, stop_after_one_cycle, reaper_side_effects, mocker
):
    """GH API failure → fall back to current behavior (assume open and hand
    off). One transient API error shouldn't strand the task as FAILED."""
    mocker.patch(
        "app.routers.agent.get_github_token_for_task",
        new_callable=AsyncMock,
        side_effect=RuntimeError("gh down"),
    )
    _spawn = mocker.patch(
        "app.review_pipeline._spawn_pipeline_task",
        new_callable=AsyncMock,
        return_value=(None, True),
    )

    project = await _make_project(reaper_db)
    t = Task(
        project_id=project.id,
        payloads=[{"type": "create"}],
        vm_instance_id="vm-pr-gh-down",
        status=TaskStatus.IN_PROGRESS,
        attempt_count=0,
        last_heartbeat=_utcnow() - timedelta(minutes=10),
        context={"scope": "small"},
    )
    reaper_db.add(t)
    await reaper_db.commit()
    await reaper_db.refresh(t)

    reaper_db.add(
        Artifact(
            task_id=t.id,
            artifact_type=ArtifactType.PULL_REQUEST,
            pr_url="https://github.com/HenryHamster/BlueprintAIStudio/pull/8888",
        )
    )
    await reaper_db.commit()

    await _run_reaper_once()
    await reaper_db.refresh(t)

    assert t.status == TaskStatus.EVALUATING
    assert "#8888" in (t.latest_status_message or "")
    _spawn.assert_called_once()


# ── Serial console persistence across requeue ─────────────────────────────
#
# NOTE: PR #3609 originally added two more tests here that asserted serial
# capture fires on the heartbeat-timeout retry and rate-limit requeue paths.
# Those were removed when this branch merged origin/main: main's lock-during-IO
# refactor (production 2026-05-04 cascade fix) intentionally narrowed the
# unlocked serial-capture I/O block to is_provision_failure paths only —
# extending it back to the non-provision branches would re-introduce the
# held-lock-during-IO antipattern that the refactor was specifically
# designed to eliminate. The remaining test below covers the survivable
# slice of the original feature: a serial log captured on a prior cycle
# must persist across the requeue context copy.


@pytest.mark.asyncio
async def test_serial_log_survives_requeue_via_context_copy(
    reaper_db, stop_after_one_cycle, reaper_side_effects, mocker
):
    """last_serial_log set from a previous cycle must survive the requeue
    context copy — clear_task_llm_runtime_context must not strip it."""
    # Serial capture returns None this cycle (VM is gone), but previous cycle
    # wrote a log into task.context already. reaper_side_effects already patches
    # app.reaper_orphan_io._capture_serial_for_vm to return None; we only need to
    # suppress the zone lookup in reaper.py's provision-failure path.
    mocker.patch("app.reaper.get_vm_zone", return_value=None)

    project = await _make_project(reaper_db)
    t = Task(
        project_id=project.id,
        payloads=[{"type": "create"}],
        vm_instance_id="vm-sticky-serial",
        status=TaskStatus.IN_PROGRESS,
        attempt_count=0,
        last_heartbeat=_utcnow() - timedelta(minutes=7),
        context={
            "provision_zone": "us-east1-a",
            "last_serial_log": "previous cycle: agent hung at step 3",
        },
    )
    reaper_db.add(t)
    await reaper_db.commit()

    await _run_reaper_once()
    await reaper_db.refresh(t)

    assert t.status == TaskStatus.QUEUED
    ctx = t.context or {}
    assert "last_serial_log" in ctx, (
        "last_serial_log from a previous cycle must survive requeue context copy"
    )
    assert ctx["last_serial_log"] == "previous cycle: agent hung at step 3"


# ── Reaper liveness telemetry ─────────────────────────────────────────────


@pytest.mark.asyncio
async def test_get_reaper_stats_initial_state_reports_never_completed():
    """Before any cycle runs, stats report cycles=0 and age=-1 sentinel."""
    import app.reaper as _reaper_mod

    _reaper_mod._reaper_cycles_completed_count = 0
    _reaper_mod._reaper_last_cycle_completed_at = None
    stats = _reaper_mod.get_reaper_stats()
    assert stats["cycles_completed_total"] == 0
    assert stats["last_cycle_age_seconds"] == -1


@pytest.mark.asyncio
async def test_get_reaper_stats_reports_recent_age_after_cycle(
    reaper_db, stop_after_one_cycle, reaper_side_effects
):
    """After one successful cycle, counter increments and age is small.

    Production 2026-05-04: this counter would have caught the reaper
    crash-loop within ~5 cycle windows (the alert threshold) instead of
    requiring 80+ minutes of operator vigilance to notice via task
    heartbeat staleness.
    """
    import app.reaper as _reaper_mod

    _reaper_mod._reaper_cycles_completed_count = 0
    _reaper_mod._reaper_last_cycle_completed_at = None

    await _run_reaper_once()

    stats = _reaper_mod.get_reaper_stats()
    assert stats["cycles_completed_total"] == 1
    # Age should be tiny — well under the 300s alert threshold.
    assert 0 <= stats["last_cycle_age_seconds"] < 60


# ── Step 9: finalize release qualifications stuck "pending" ────────────────


def _empty_qualification_contract() -> dict:
    """Launch-time contract fields for synthetic pending qualification rows."""
    from app.release_qualifier import (
        _qualification_contract_fingerprint,
        _rubric_fingerprint,
    )

    rubric_fingerprint = _rubric_fingerprint([])
    return {
        "rubric_snapshot": [],
        "rubric_fingerprint": rubric_fingerprint,
        "contract_fingerprint": _qualification_contract_fingerprint(
            "", rubric_fingerprint
        ),
        "version_goal": "",
    }


@pytest.mark.asyncio
async def test_step9_sweep_finalizes_qualification_stuck_pending_after_reaper_kill(
    reaper_db, stop_after_one_cycle, reaper_side_effects, mocker
):
    """A ReleaseVersion stuck at last_qualification.status=='pending' whose
    audit + smoketest children are BOTH FAILED is finalized by the reaper's
    Step 9 sweep.

    This is the loose-end fix: the reaper can mark a release_audit /
    qualifier-smoketest child FAILED directly (infra-recovery / stale-task
    sweeps) without routing through the task-completion handler that normally
    calls maybe_finalize_qualification. Such a reaper-killed qualifier child
    would otherwise leave its ReleaseVersion wedged at status=='pending'
    forever. Step 9 wires release_qualifier.sweep_stuck_qualifications into
    the periodic loop so the next cycle catches it.

    After the cycle: last_qualification.status is no longer 'pending' (the
    finalize routed a FAILED qualifier child to the infra-failure path — a
    broken pipeline, not a quality verdict — yielding status 'infra_failure')
    and phase is back to 'stabilize'.
    """
    # Prevent the infra_failure redrive loop (runs after force-finalize in the
    # same sweep cycle) from overwriting the infra_failure status with error.
    mocker.patch(
        "app.release_qualifier_sweep._redrive_infra_failure_qualification",
        new=AsyncMock(return_value=False),
    )
    from app import shared_state

    _original_promotion = shared_state.runtime_config.get("promotion_enabled")
    shared_state.runtime_config["promotion_enabled"] = False
    try:
        project = await _make_project(reaper_db)

        # Open release version in the 'build' phase — finalize must drive it
        # back to 'stabilize', so starting at 'build' proves the transition ran.
        version = ReleaseVersion(
            project_id=project.id,
            version_number=1,
            phase="build",
            status="open",
        )
        reaper_db.add(version)
        await reaper_db.commit()
        await reaper_db.refresh(version)

        # Active audit plus a legacy smoke child are both FAILED — an upgraded
        # deployment must drain that historical shape without making smoke a
        # current qualification gate. vm_instance_id is None so the active-task
        # sweep ignores them.
        audit_task = Task(
            project_id=project.id,
            title="Release audit: main...automain diff",
            payloads=[{"type": "release_audit", "base_branch": "main"}],
            vm_instance_id=None,
            status=TaskStatus.FAILED,
            context={"release_version_id": str(version.id)},
        )
        smoke_task = Task(
            project_id=project.id,
            title="Qualifier smoketest: automain candidate",
            payloads=[{"type": "smoketest", "base_branch": "automain"}],
            vm_instance_id=None,
            status=TaskStatus.FAILED,
            context={
                "release_version_id": str(version.id),
                "qualifier_smoketest": True,
            },
        )
        reaper_db.add(audit_task)
        reaper_db.add(smoke_task)
        await reaper_db.commit()
        await reaper_db.refresh(audit_task)
        await reaper_db.refresh(smoke_task)

        # Version wedged 'pending' — its completion handler never ran because
        # the reaper killed the children directly. started_at is FRESH (now)
        # so this test exercises the both-children-terminal finalize path, not
        # the 6h qualification-timeout backstop (which would also finalize but
        # via a different code path and blocker).
        from app.time_utils import _utcnow as _now_for_seed

        version.last_qualification = {
            "status": "pending",
            "candidate_sha": "abcd" * 10,
            "ci_green": False,
            "started_at": _now_for_seed().isoformat(),
            "audit_task_id": str(audit_task.id),
            "smoketest_task_id": str(smoke_task.id),
            **_empty_qualification_contract(),
        }
        reaper_db.add(version)
        await reaper_db.commit()

        await _run_reaper_once()
        await reaper_db.refresh(version)

        # Step 9 drove the stuck version through maybe_finalize_qualification.
        assert version.last_qualification is not None
        assert version.last_qualification.get("status") != "pending", (
            "reaper Step 9 sweep must finalize a version stuck 'pending' with "
            "both qualifier children terminal"
        )
        # A FAILED audit child is a broken qualifier pipeline, not a quality
        # finding → status 'infra_failure'; the version returns to stabilize
        # for a clean re-qualification on the next strategic cycle.
        assert version.last_qualification.get("status") == "infra_failure"
        assert version.phase == "stabilize", (
            "finalize must send the infra-failed version back to 'stabilize'"
        )
        # The version stays open — a failed qualification only ever blocks.
        assert version.status == "open"
    finally:
        if _original_promotion is None:
            shared_state.runtime_config.pop("promotion_enabled", None)
        else:
            shared_state.runtime_config["promotion_enabled"] = _original_promotion


@pytest.mark.asyncio
async def test_step9_sweep_skips_qualification_with_running_child(
    reaper_db, stop_after_one_cycle, reaper_side_effects, mocker
):
    """Step 9 must NOT finalize a 'pending' version while a qualifier child is
    still non-terminal — the normal completion path owns that case.

    Guards against the sweep firing prematurely and finalizing on incomplete
    child results.

    **Positive control (Round 9).**  "Stays pending" is only meaningful if the
    sweep's finalize machinery is genuinely *running* against the test engine.
    The Round 7 dedicated-finalize-session change briefly made the sweep open
    its DB session against an unpatched engine: every finalize then errored and
    was swallowed by the per-version try/except, so EVERY version trivially
    "stayed pending" and this assertion passed for the wrong reason.  To make
    the test prove a *deliberate* skip, a second version (its own project, both
    children terminal) is seeded in the SAME cycle and MUST be finalized.  Now
    the test fails on either fault: a broken finalize path fails the positive
    control; a premature sweep wrongly finalizes the running-child version.
    """
    # Prevent the infra_failure redrive loop (runs after force-finalize in the
    # same sweep cycle) from overwriting the infra_failure status with error.
    mocker.patch(
        "app.release_qualifier_sweep._redrive_infra_failure_qualification",
        new=AsyncMock(return_value=False),
    )
    from app import shared_state
    from app.time_utils import _utcnow as _now_for_seed

    _original_promotion = shared_state.runtime_config.get("promotion_enabled")
    shared_state.runtime_config["promotion_enabled"] = False
    try:
        project = await _make_project(reaper_db)

        version = ReleaseVersion(
            project_id=project.id,
            version_number=1,
            phase="stabilize",
            status="open",
        )
        reaper_db.add(version)
        await reaper_db.commit()
        await reaper_db.refresh(version)

        # Audit is terminal, but an active required challenge is still running.
        # vm_instance_id is None so the active-task sweep leaves the challenge
        # unchanged and Step 9 must deliberately defer finalization.
        audit_task = Task(
            project_id=project.id,
            title="Release audit: main...automain diff",
            payloads=[{"type": "release_audit", "base_branch": "main"}],
            vm_instance_id=None,
            status=TaskStatus.FAILED,
            context={"release_version_id": str(version.id)},
        )
        challenge_task = Task(
            project_id=project.id,
            title="Release audit challenge",
            payloads=[
                {"type": "release_audit_challenge", "base_branch": "main"}
            ],
            vm_instance_id=None,
            status=TaskStatus.IN_PROGRESS,
            context={"release_version_id": str(version.id)},
        )
        reaper_db.add(audit_task)
        reaper_db.add(challenge_task)
        await reaper_db.commit()
        await reaper_db.refresh(audit_task)
        await reaper_db.refresh(challenge_task)

        # started_at is FRESH (now) so the timeout backstop does not fire. This
        # test exercises the active-required-child skip path directly.
        version.last_qualification = {
            "status": "pending",
            "candidate_sha": "abcd" * 10,
            "ci_green": False,
            "started_at": _now_for_seed().isoformat(),
            "audit_task_id": str(audit_task.id),
            "challenge_task_id": str(challenge_task.id),
            "audit_challenge_task_id": str(challenge_task.id),
            **_empty_qualification_contract(),
        }
        reaper_db.add(version)
        await reaper_db.commit()

        # Positive control: a SECOND version (own project — ReleaseVersion is
        # unique on (project_id, version_number)) whose required audit is
        # terminal. The same sweep cycle MUST finalize this one. If the sweep's
        # dedicated finalize session were opened against the wrong engine, this
        # finalize would silently fail and the control assertion below would
        # catch it — so "stays pending" cannot pass for the wrong reason.
        control_project = await _make_project(reaper_db)
        control_version = ReleaseVersion(
            project_id=control_project.id,
            version_number=1,
            phase="build",
            status="open",
        )
        reaper_db.add(control_version)
        await reaper_db.commit()
        await reaper_db.refresh(control_version)
        control_audit = Task(
            project_id=control_project.id,
            title="Release audit: main...automain diff",
            payloads=[{"type": "release_audit", "base_branch": "main"}],
            vm_instance_id=None,
            status=TaskStatus.FAILED,
            context={"release_version_id": str(control_version.id)},
        )
        reaper_db.add(control_audit)
        await reaper_db.commit()
        await reaper_db.refresh(control_audit)
        control_version.last_qualification = {
            "status": "pending",
            "candidate_sha": "ef01" * 10,
            "ci_green": False,
            "started_at": _now_for_seed().isoformat(),
            "audit_task_id": str(control_audit.id),
            **_empty_qualification_contract(),
        }
        reaper_db.add(control_version)
        await reaper_db.commit()

        await _run_reaper_once()
        await reaper_db.refresh(version)
        await reaper_db.refresh(control_version)

        # The required challenge is still non-terminal — version stays pending.
        assert version.last_qualification is not None
        assert version.last_qualification.get("status") == "pending", (
            "Step 9 must not finalize while a qualifier child is non-terminal"
        )
        # Positive control: the terminal-required-child version WAS finalized
        # in the same cycle — proves the skip above is a deliberate decision by a
        # working sweep, not the side effect of a silently-broken finalize.
        assert control_version.last_qualification is not None
        assert control_version.last_qualification.get("status") == "infra_failure", (
            "Step 9 sweep must finalize a both-children-terminal version — if "
            "this fails the sweep's finalize path is broken and the "
            "running-child 'stays pending' assertion proves nothing"
        )
        assert control_version.phase == "stabilize"
    finally:
        if _original_promotion is None:
            shared_state.runtime_config.pop("promotion_enabled", None)
        else:
            shared_state.runtime_config["promotion_enabled"] = _original_promotion


@pytest.mark.asyncio
async def test_step9_sweep_times_out_qualification_with_unparseable_started_at(
    reaper_db, stop_after_one_cycle, reaper_side_effects, mocker
):
    """Audit R4-H5: the reaper's Step 9 sweep must STILL force-finalize a
    qualification that is past the timeout when its started_at is corrupt /
    unparseable — the timeout backstop falls back to version.updated_at as the
    clock.  A bad started_at string must never wedge a qualification forever.

    Seeds a pending qualification with a hung (IN_PROGRESS) child, a corrupt
    started_at, and an updated_at forced far into the past so the fallback
    clock exceeds the qualification timeout.  After one reaper cycle the
    version is force-finalized as a qualification_timeout.
    """
    # Prevent the infra_failure redrive loop (runs after force-finalize in the
    # same sweep cycle) from overwriting the infra_failure status with error.
    mocker.patch(
        "app.release_qualifier_sweep._redrive_infra_failure_qualification",
        new=AsyncMock(return_value=False),
    )
    from datetime import timedelta

    from app import release_qualifier as _rq
    from app import shared_state
    from app.time_utils import _utcnow

    _original_promotion = shared_state.runtime_config.get("promotion_enabled")
    shared_state.runtime_config["promotion_enabled"] = False
    try:
        project = await _make_project(reaper_db)

        version = ReleaseVersion(
            project_id=project.id,
            version_number=1,
            phase="stabilize",
            status="open",
        )
        reaper_db.add(version)
        await reaper_db.commit()
        await reaper_db.refresh(version)

        # The required audit itself is hung IN_PROGRESS. Without the timeout,
        # this active qualification could never finalize.
        audit_task = Task(
            project_id=project.id,
            title="Release audit: main...automain diff",
            payloads=[{"type": "release_audit", "base_branch": "main"}],
            vm_instance_id=None,
            status=TaskStatus.IN_PROGRESS,  # hung — never reaches terminal
            context={"release_version_id": str(version.id)},
        )
        reaper_db.add(audit_task)
        await reaper_db.commit()
        await reaper_db.refresh(audit_task)

        # CORRUPT started_at — _parse_started_at returns None for this.
        version.last_qualification = {
            "status": "pending",
            "candidate_sha": "abcd" * 10,
            "ci_green": False,
            "started_at": "this-is-not-a-timestamp",
            "audit_task_id": str(audit_task.id),
        }
        reaper_db.add(version)
        await reaper_db.commit()

        # Force updated_at far into the past (bypassing the onupdate hook) so
        # the timeout's updated_at fallback clock exceeds the limit.
        from sqlalchemy import update as _sa_update

        old_ts = _utcnow() - timedelta(
            seconds=_rq._MAX_QUALIFICATION_DURATION_SECONDS + 3600
        )
        await reaper_db.execute(
            _sa_update(ReleaseVersion)
            .where(ReleaseVersion.id == version.id)
            .values(updated_at=old_ts)
        )
        await reaper_db.commit()

        await _run_reaper_once()
        await reaper_db.refresh(version)

        # The timeout backstop fired via the updated_at fallback, even though
        # started_at was unparseable.
        assert version.last_qualification is not None
        assert version.last_qualification.get("status") == "infra_failure", (
            "Step 9 timeout must force-finalize a pending qualification with "
            "an unparseable started_at — falling back to updated_at"
        )
        assert version.last_qualification.get("blocker") == "qualification_timeout"
        assert version.phase == "stabilize"
        assert version.status == "open"
    finally:
        if _original_promotion is None:
            shared_state.runtime_config.pop("promotion_enabled", None)
        else:
            shared_state.runtime_config["promotion_enabled"] = _original_promotion


# ── Step 9: calcification alerting ────────────────────────────────────────


@pytest.mark.asyncio
async def test_step9_calcification_alert_fires_for_repeated_hard_blockers(
    reaper_db, stop_after_one_cycle, reaper_side_effects
):
    """A version stuck in stabilize with >= _MIN_CALCIFICATION_ATTEMPTS hard-blocker
    failures gets a calcification alert posted by the reaper's Step 9 sweep."""
    from app import release_qualifier as _rq
    from app.models import Alert as _Alert

    project = await _make_project(reaper_db)
    version = ReleaseVersion(
        project_id=project.id,
        version_number=1,
        phase="stabilize",
        status="open",
        qualification_attempts=_rq._MIN_CALCIFICATION_ATTEMPTS,
        last_qualification={
            "status": "not_promoted",
            "blocker": "ci_not_green",
            "candidate_sha": "abc" * 13 + "a",
        },
    )
    reaper_db.add(version)
    await reaper_db.commit()

    await _run_reaper_once()
    await reaper_db.refresh(version)

    # Scoped to the calcification event type — the same Step 9 sweep runs
    # independent lifecycle checks (e.g. the standing full-suite drift sentinel)
    # that may emit their own alerts for this project.
    alerts = (
        (
            await reaper_db.execute(
                _select(_Alert).where(
                    _Alert.project_id == project.id,
                    _Alert.event_type == "release.calcifying",
                )
            )
        )
        .scalars()
        .all()
    )
    assert len(alerts) == 1
    assert "calcifying" in alerts[0].title
    assert "ci_not_green" in alerts[0].title


@pytest.mark.asyncio
async def test_step9_calcification_alert_not_fired_for_dry_run_hold(
    reaper_db, stop_after_one_cycle, reaper_side_effects
):
    """A version whose last qualification is a dry_run (passed but promotion
    disabled) must NOT trigger a calcification alert — that is healthy operation."""
    from app import release_qualifier as _rq
    from app.models import Alert as _Alert

    project = await _make_project(reaper_db)
    version = ReleaseVersion(
        project_id=project.id,
        version_number=1,
        phase="stabilize",
        status="open",
        qualification_attempts=_rq._MIN_CALCIFICATION_ATTEMPTS + 5,
        last_qualification={
            "status": "dry_run",
            "would_promote": True,
            "confidence": 0.95,
            "blocker": None,
        },
    )
    reaper_db.add(version)
    await reaper_db.commit()

    await _run_reaper_once()

    # Scoped to the calcification event type: the assertion is that a dry-run
    # hold raises no CALCIFICATION alert.  Unrelated sweep observations (e.g.
    # the standing full-suite drift sentinel) are not part of that contract.
    alerts = (
        (
            await reaper_db.execute(
                _select(_Alert).where(
                    _Alert.project_id == project.id,
                    _Alert.event_type == "release.calcifying",
                )
            )
        )
        .scalars()
        .all()
    )
    assert len(alerts) == 0


@pytest.mark.asyncio
async def test_step9_sweep_scan_error_blocks_liveness_counter(
    reaper_db, stop_after_one_cycle, reaper_side_effects, mocker
):
    """A scan-level failure in the Step 9 qualification sweep (e.g. DB error
    on the initial open-versions query) must NOT advance the reaper liveness
    counter.  The sweep re-raises scan-level errors so the outer
    consecutive_errors handler engages — unlike per-version finalize crashes,
    which are swallowed inside sweep_stuck_qualifications."""
    import app.reaper as _reaper_mod
    from app import release_qualifier as _rq

    _reaper_mod._reaper_cycles_completed_count = 0
    _reaper_mod._reaper_last_cycle_completed_at = None

    mocker.patch.object(
        _rq,
        "sweep_stuck_qualifications",
        new=AsyncMock(side_effect=RuntimeError("simulated scan-level DB failure")),
    )

    await _run_reaper_once()

    # Cycle FAILED — liveness counter must NOT have advanced.
    stats = _reaper_mod.get_reaper_stats()
    assert stats["cycles_completed_total"] == 0
    assert stats["last_cycle_age_seconds"] == -1


# ── Step 7: stalled-cycle reaper + linked task reconciliation ────────────────


def _make_cycle_id() -> str:
    return str(_uuid_mod.uuid4())


@pytest.mark.asyncio
async def test_step7_stalled_cycle_cancels_linked_phase1_task(
    reaper_db, stop_after_one_cycle, reaper_side_effects, mocker
):
    """Phase1 task IN_PROGRESS with stale heartbeat → task FAILED, cycle STALLED_PHASE1."""
    mocker.patch("app.broadcast.broadcast_to_project", new_callable=AsyncMock)

    project = await _make_project(reaper_db)
    task = Task(
        project_id=project.id,
        title="strategic-phase1",
        payloads=[{"type": "design_doc_update", "scope": "medium"}],
        status=TaskStatus.IN_PROGRESS,
        last_heartbeat=_utcnow() - timedelta(hours=1),
    )
    reaper_db.add(task)
    await reaper_db.commit()
    await reaper_db.refresh(task)

    cycle = StrategicCycle(
        id=_make_cycle_id(),
        project_id=project.id,
        outcome="phase1_running",
        phase1_task_id=task.id,
        phase1_started_at=_utcnow() - timedelta(hours=4),
    )
    reaper_db.add(cycle)
    await reaper_db.commit()

    await _run_reaper_once()

    await reaper_db.refresh(task)
    await reaper_db.refresh(cycle)

    assert task.status == TaskStatus.FAILED, (
        "stale linked task must be force-terminated"
    )
    assert "stalled-cycle reaper" in (task.latest_status_message or "")
    assert cycle.outcome == "stalled_phase1"


@pytest.mark.asyncio
async def test_step7_stalled_cycle_cancels_linked_phase2_task(
    reaper_db, stop_after_one_cycle, reaper_side_effects, mocker
):
    """Phase2 task IN_PROGRESS with stale heartbeat → task FAILED, cycle STALLED_PHASE2."""
    mocker.patch("app.broadcast.broadcast_to_project", new_callable=AsyncMock)

    project = await _make_project(reaper_db)
    task = Task(
        project_id=project.id,
        title="strategic-phase2",
        payloads=[{"type": "strategic_review", "scope": "medium"}],
        status=TaskStatus.IN_PROGRESS,
        last_heartbeat=_utcnow() - timedelta(hours=1),
    )
    reaper_db.add(task)
    await reaper_db.commit()
    await reaper_db.refresh(task)

    cycle = StrategicCycle(
        id=_make_cycle_id(),
        project_id=project.id,
        outcome="phase2_running",
        phase2_task_id=task.id,
        phase1_started_at=_utcnow() - timedelta(hours=4),
        phase2_started_at=_utcnow() - timedelta(hours=4),
    )
    reaper_db.add(cycle)
    await reaper_db.commit()

    await _run_reaper_once()

    await reaper_db.refresh(task)
    await reaper_db.refresh(cycle)

    assert task.status == TaskStatus.FAILED, (
        "stale linked task must be force-terminated"
    )
    assert "stalled-cycle reaper" in (task.latest_status_message or "")
    assert cycle.outcome == "stalled_phase2"


@pytest.mark.asyncio
async def test_stalled_cycle_force_terminate_records_infra_failure_category(
    reaper_db, stop_after_one_cycle, reaper_side_effects, mocker
):
    """Force-terminated stalled-cycle task must carry AGENT_STALLED failure_category in context."""
    mocker.patch("app.broadcast.broadcast_to_project", new_callable=AsyncMock)

    project = await _make_project(reaper_db)
    task = Task(
        project_id=project.id,
        title="strategic-phase1-stalled",
        payloads=[{"type": "design_doc_update", "scope": "medium"}],
        status=TaskStatus.IN_PROGRESS,
        last_heartbeat=_utcnow() - timedelta(hours=1),
    )
    reaper_db.add(task)
    await reaper_db.commit()
    await reaper_db.refresh(task)

    cycle = StrategicCycle(
        id=_make_cycle_id(),
        project_id=project.id,
        outcome="phase1_running",
        phase1_task_id=task.id,
        phase1_started_at=_utcnow() - timedelta(hours=4),
    )
    reaper_db.add(cycle)
    await reaper_db.commit()

    await _run_reaper_once()

    await reaper_db.refresh(task)
    assert task.status == TaskStatus.FAILED
    assert (task.context or {}).get("failure_category") == "agent_stalled", (
        "stalled-cycle force-terminate must record AGENT_STALLED infra class "
        "so direction-health metrics exclude it from code-failure counts"
    )


@pytest.mark.asyncio
async def test_step7_stalled_cycle_live_task_not_declared_stalled(
    reaper_db, stop_after_one_cycle, reaper_side_effects, mocker
):
    """Cycle >3h but linked task has a fresh heartbeat — cycle must NOT be declared stalled."""
    mocker.patch("app.broadcast.broadcast_to_project", new_callable=AsyncMock)

    project = await _make_project(reaper_db)
    task = Task(
        project_id=project.id,
        title="strategic-live",
        payloads=[{"type": "design_doc_update", "scope": "medium"}],
        status=TaskStatus.IN_PROGRESS,
        last_heartbeat=_utcnow() - timedelta(seconds=30),
    )
    reaper_db.add(task)
    await reaper_db.commit()
    await reaper_db.refresh(task)

    cycle = StrategicCycle(
        id=_make_cycle_id(),
        project_id=project.id,
        outcome="phase1_running",
        phase1_task_id=task.id,
        phase1_started_at=_utcnow() - timedelta(hours=4),
    )
    reaper_db.add(cycle)
    await reaper_db.commit()

    await _run_reaper_once()

    await reaper_db.refresh(task)
    await reaper_db.refresh(cycle)

    assert task.status == TaskStatus.IN_PROGRESS, "live task must not be terminated"
    assert cycle.outcome == "phase1_running", (
        "slow-but-live cycle must not be declared stalled"
    )


@pytest.mark.asyncio
async def test_step7_requeued_phase_task_with_recent_activity_not_reaped(
    reaper_db, mocker
):
    """A rate-limit-requeued phase task mid-recovery must not be force-killed.

    sc-93fd806a: the phase2 resident session hit a provider 429 session limit,
    self-requeued (FAILED -> PROVISIONING -> QUEUED -> PROVISIONING), and 60
    seconds later the stall sweep force-terminated it — the requeue cleared
    ``last_heartbeat`` and a QUEUED/PROVISIONING task has no agent to
    heartbeat, so the NULL-heartbeat path fell straight through to the kill.
    A recent ``updated_at`` (every lifecycle write advances it) is the
    liveness signal for that window.
    """
    mocker.patch("app.broadcast.broadcast_to_project", new_callable=AsyncMock)
    mocker.patch(
        "app.strategic_events.broadcast_to_project", new_callable=AsyncMock
    )

    project = await _make_project(reaper_db)
    task = Task(
        project_id=project.id,
        title="strategic-phase2-requeued",
        payloads=[{"type": "strategic_review", "scope": "medium"}],
        status=TaskStatus.PROVISIONING,
        last_heartbeat=None,
        latest_status_message="Rate Limit — requeued",
    )
    reaper_db.add(task)
    await reaper_db.commit()
    await reaper_db.refresh(task)

    cycle = StrategicCycle(
        id=_make_cycle_id(),
        project_id=project.id,
        outcome="phase2_running",
        phase2_task_id=task.id,
        phase1_started_at=_utcnow() - timedelta(hours=5),
        phase2_started_at=_utcnow() - timedelta(hours=4),
    )
    reaper_db.add(cycle)
    await reaper_db.commit()

    stalled = await _rs.sweep_stalled_strategic_cycles(
        reaper_db,
        _utcnow(),
        heartbeat_stale_cutoff_fn=lambda _task, now: now - timedelta(minutes=5),
    )

    await reaper_db.refresh(task)
    await reaper_db.refresh(cycle)
    assert stalled == 0
    assert task.status == TaskStatus.PROVISIONING, (
        "a requeued phase task with recent lifecycle activity is recovering, "
        "not stalled — the reaper must not force-terminate it"
    )
    assert cycle.outcome == "phase2_running"
    assert cycle.failure_reason is None


@pytest.mark.asyncio
async def test_step7_wedged_queued_phase_task_without_activity_still_reaped(
    reaper_db, mocker
):
    """The recent-activity skip must not shield a genuinely wedged QUEUED task.

    Mutation guard for the requeue exemption above: a phase task that has sat
    QUEUED with no heartbeat AND no lifecycle write for hours is the exact
    wedge the stall sweep exists to converge.
    """
    mocker.patch("app.broadcast.broadcast_to_project", new_callable=AsyncMock)
    mocker.patch(
        "app.strategic_events.broadcast_to_project", new_callable=AsyncMock
    )

    project = await _make_project(reaper_db)
    task = Task(
        project_id=project.id,
        title="strategic-phase2-wedged",
        payloads=[{"type": "strategic_review", "scope": "medium"}],
        status=TaskStatus.QUEUED,
        last_heartbeat=None,
    )
    reaper_db.add(task)
    await reaper_db.commit()
    await reaper_db.refresh(task)

    cycle = StrategicCycle(
        id=_make_cycle_id(),
        project_id=project.id,
        outcome="phase2_running",
        phase2_task_id=task.id,
        phase1_started_at=_utcnow() - timedelta(hours=5),
        phase2_started_at=_utcnow() - timedelta(hours=4),
    )
    reaper_db.add(cycle)
    await reaper_db.commit()

    # Force updated_at into the past (bypassing the onupdate hook): the task
    # row has not been touched since long before the liveness window.
    from sqlalchemy import update as _sa_update

    await reaper_db.execute(
        _sa_update(Task)
        .where(Task.id == task.id)
        .values(updated_at=_utcnow() - timedelta(hours=2))
    )
    await reaper_db.commit()

    stalled = await _rs.sweep_stalled_strategic_cycles(
        reaper_db,
        _utcnow(),
        heartbeat_stale_cutoff_fn=lambda _task, now: now - timedelta(minutes=5),
    )

    await reaper_db.refresh(task)
    await reaper_db.refresh(cycle)
    assert stalled == 1
    assert task.status == TaskStatus.FAILED
    assert cycle.outcome == "stalled_phase2"
    # The reason now names the CAUSE, not just the state: this task never left
    # QUEUED, so no agent ever ran and no completion handler could fire.
    assert cycle.failure_reason.startswith(
        "reaper_stall (attempt 1, cause phase2:queued_never_dispatched):"
    ), cycle.failure_reason
    assert "never left QUEUED" in cycle.failure_reason
    assert cycle.cycle_metadata["stall_signature"] == (
        "phase2:queued_never_dispatched"
    )


@pytest.mark.asyncio
async def test_step7_missing_phase2_start_is_repaired_not_borrowed_from_phase1(
    reaper_db, mocker
):
    """A NULL ``phase2_started_at`` must not be measured with phase 1's clock.

    Row shape produced by ``0047_add_strategic_cycle_table``: ``phase2_running``
    backfilled straight from the legacy JSONB, ``phase2_started_at`` left NULL
    because the entry had no parseable ``phase1_completed_at``, and
    ``phase1_started_at`` COALESCEd to a long-past timestamp. The old
    ``phase2_started_at or phase1_started_at`` fallback then reported phase 1's
    age as ">3h in phase2" and force-terminated a phase-2 task that had been
    running for minutes.
    """
    mocker.patch("app.broadcast.broadcast_to_project", new_callable=AsyncMock)
    mocker.patch(
        "app.strategic_events.broadcast_to_project", new_callable=AsyncMock
    )

    project = await _make_project(reaper_db)
    task = Task(
        project_id=project.id,
        title="strategic-phase2-just-started",
        payloads=[{"type": "strategic_review", "scope": "medium"}],
        status=TaskStatus.IN_PROGRESS,
        last_heartbeat=None,
    )
    reaper_db.add(task)
    await reaper_db.commit()
    await reaper_db.refresh(task)

    cycle = StrategicCycle(
        id=_make_cycle_id(),
        project_id=project.id,
        outcome="phase2_running",
        phase2_task_id=task.id,
        # Phase 1 started long ago; phase 2 started when the task was created.
        phase1_started_at=_utcnow() - timedelta(hours=9),
        phase2_started_at=None,
    )
    reaper_db.add(cycle)
    await reaper_db.commit()

    stalled = await _rs.sweep_stalled_strategic_cycles(
        reaper_db,
        _utcnow(),
        heartbeat_stale_cutoff_fn=lambda _task, now: now - timedelta(minutes=5),
    )

    await reaper_db.refresh(task)
    await reaper_db.refresh(cycle)
    assert stalled == 0, (
        "phase 2 started minutes ago; only phase 1's clock is >3h old and it is "
        "not evidence about phase 2"
    )
    assert task.status == TaskStatus.IN_PROGRESS, (
        "the phase-2 task must not be force-terminated on a duration nothing "
        "measured"
    )
    assert cycle.outcome == "phase2_running"
    # The missing fact is REPAIRED from evidence, not left for a human: the
    # phase-2 task is created at phase-2 start, so its created_at IS the start.
    assert cycle.phase2_started_at is not None
    assert cycle.phase2_started_at == task.created_at
    repair = cycle.cycle_metadata["phase_start_repair"]
    assert repair["phase"] == "phase2"
    assert repair["source"] == "linked_task_created_at"


@pytest.mark.asyncio
async def test_step7_repaired_phase2_start_still_stalls_a_wedged_cycle(
    reaper_db, mocker
):
    """Mutation guard: the repair must not become a shield.

    Same NULL ``phase2_started_at`` shape, but the phase-2 task really has been
    QUEUED for 9h with no heartbeat and no lifecycle write. Repairing the start
    from the task's ``created_at`` must produce an HONEST >3h measurement and
    the ordinary stall, with the reason naming both the cause and the repair.
    """
    mocker.patch("app.broadcast.broadcast_to_project", new_callable=AsyncMock)
    mocker.patch(
        "app.strategic_events.broadcast_to_project", new_callable=AsyncMock
    )

    project = await _make_project(reaper_db)
    task = Task(
        project_id=project.id,
        title="strategic-phase2-wedged-null-start",
        payloads=[{"type": "strategic_review", "scope": "medium"}],
        status=TaskStatus.QUEUED,
        last_heartbeat=None,
    )
    reaper_db.add(task)
    await reaper_db.commit()
    await reaper_db.refresh(task)

    from sqlalchemy import update as _sa_update

    await reaper_db.execute(
        _sa_update(Task)
        .where(Task.id == task.id)
        .values(
            created_at=_utcnow() - timedelta(hours=9),
            updated_at=_utcnow() - timedelta(hours=9),
        )
    )
    await reaper_db.commit()

    cycle = StrategicCycle(
        id=_make_cycle_id(),
        project_id=project.id,
        outcome="phase2_running",
        phase2_task_id=task.id,
        phase1_started_at=_utcnow() - timedelta(hours=10),
        phase2_started_at=None,
    )
    reaper_db.add(cycle)
    await reaper_db.commit()

    stalled = await _rs.sweep_stalled_strategic_cycles(
        reaper_db,
        _utcnow(),
        heartbeat_stale_cutoff_fn=lambda _task, now: now - timedelta(minutes=5),
    )

    await reaper_db.refresh(task)
    await reaper_db.refresh(cycle)
    assert stalled == 1
    assert task.status == TaskStatus.FAILED
    assert cycle.outcome == "stalled_phase2"
    assert cycle.failure_reason.startswith(
        "reaper_stall (attempt 1, cause phase2:queued_never_dispatched):"
    ), cycle.failure_reason
    # Surface the REASON, including where the clock came from — a cycle that
    # stalls on the first sweep that ever measured it otherwise reads as the
    # reaper inventing a deadline.
    assert "phase2 start timestamp was missing and was repaired" in (
        cycle.failure_reason
    ), cycle.failure_reason
    assert "linked_task_created_at" in cycle.failure_reason


@pytest.mark.asyncio
async def test_step7_phase2_start_repair_is_idempotent_and_bounded(reaper_db, mocker):
    """A repaired row is not repaired again, and repair alone is not a stall."""
    mocker.patch("app.broadcast.broadcast_to_project", new_callable=AsyncMock)

    from app import strategic_cycle as _sc

    project = await _make_project(reaper_db)
    cycle = StrategicCycle(
        id=_make_cycle_id(),
        project_id=project.id,
        outcome="phase2_running",
        # No phase-2 task and no phase-1 completion: the row carries no evidence
        # of phase 2 ever starting, so the clock starts at the observation.
        phase2_task_id=None,
        phase1_started_at=_utcnow() - timedelta(hours=9),
        phase1_completed_at=None,
        phase2_started_at=None,
    )
    reaper_db.add(cycle)
    await reaper_db.commit()

    first = await _sc.repair_missing_phase_starts(reaper_db)
    await reaper_db.commit()
    assert len(first) == 1
    assert first[0]["source"] == "observed_at_repair"

    await reaper_db.refresh(cycle)
    assert cycle.phase2_started_at is not None
    # Deciding costs at most one extra window; it never asserts an unmeasured age.
    assert cycle.phase2_started_at > _utcnow() - timedelta(minutes=5)

    second = await _sc.repair_missing_phase_starts(reaper_db)
    await reaper_db.commit()
    assert second == [], "a repaired row must not match the predicate again"


@pytest.mark.asyncio
async def test_step7_reconciles_already_terminal_task_while_pipeline_paused(
    reaper_db, stop_after_one_cycle, reaper_side_effects, mocker
):
    """A paused sweep reconciles terminal residue without declaring a time stall."""
    from app.models import DeploymentState, DeploymentStatus

    mocker.patch("app.broadcast.broadcast_to_project", new_callable=AsyncMock)

    project = await _make_project(reaper_db)
    now = _utcnow()
    reaper_db.add(
        DeploymentState(
            id=1,
            status=DeploymentStatus.PAUSED,
            paused_at=now - timedelta(hours=4),
            paused_kind="manual",
        )
    )
    task = Task(
        project_id=project.id,
        title="strategic-already-failed",
        payloads=[{"type": "design_doc_update", "scope": "medium"}],
        status=TaskStatus.FAILED,
        latest_status_message="crashed earlier",
    )
    reaper_db.add(task)
    await reaper_db.commit()
    await reaper_db.refresh(task)

    cycle = StrategicCycle(
        id=_make_cycle_id(),
        project_id=project.id,
        outcome="phase1_running",
        phase1_task_id=task.id,
        phase1_started_at=now - timedelta(hours=4),
    )
    reaper_db.add(cycle)
    await reaper_db.commit()

    await _run_reaper_once()

    await reaper_db.refresh(task)
    await reaper_db.refresh(cycle)

    assert task.status == TaskStatus.FAILED, "already-terminal task must not be mutated"
    assert task.latest_status_message == "crashed earlier", (
        "task message must not be overwritten"
    )
    assert cycle.outcome == "failed_phase1"
    assert "source=stalled_cycle_reaper" in (cycle.failure_reason or "")
    assert "reaper_stall" not in (cycle.failure_reason or "")


@pytest.mark.asyncio
async def test_step7_terminal_residue_broadcasts_completion_after_commit(
    reaper_db, mocker
):
    """The terminal-residue pass owns the completion broadcast it takes over.

    These cycles are removed from the stalled population, so without this emit a
    cycle whose phase task already FAILED would be durably terminal while the
    operator UI kept showing "Strategist working…" (strategistActive is cleared
    only by strategic_cycle_completed or a project switch).
    """
    project = await _make_project(reaper_db)
    task = Task(
        project_id=project.id,
        title="strategic-already-failed",
        payloads=[{"type": "design_doc_update", "scope": "medium"}],
        status=TaskStatus.FAILED,
        latest_status_message="crashed earlier",
    )
    reaper_db.add(task)
    await reaper_db.commit()
    await reaper_db.refresh(task)
    cycle = StrategicCycle(
        id=_make_cycle_id(),
        project_id=project.id,
        outcome="phase1_running",
        phase1_task_id=task.id,
        phase1_started_at=_utcnow() - timedelta(hours=4),
    )
    reaper_db.add(cycle)
    await reaper_db.commit()

    events: list = []
    real_commit = reaper_db.commit

    async def _tracking_commit(*args, **kwargs):
        events.append("commit")
        return await real_commit(*args, **kwargs)

    async def _tracking_broadcast(project_id, event, payload):
        events.append(("broadcast", event, payload))

    mocker.patch.object(reaper_db, "commit", side_effect=_tracking_commit)
    mocker.patch("app.broadcast.broadcast_to_project", side_effect=_tracking_broadcast)
    mocker.patch(
        "app.strategic_events.broadcast_to_project", side_effect=_tracking_broadcast
    )

    await _rs.sweep_stalled_strategic_cycles(
        reaper_db,
        _utcnow(),
        heartbeat_stale_cutoff_fn=lambda _task, now: now - timedelta(minutes=5),
    )

    await reaper_db.refresh(cycle)
    assert cycle.outcome == "failed_phase1"
    completion = next(
        event
        for event in events
        if isinstance(event, tuple) and event[1] == "strategic_cycle_completed"
    )
    payload = completion[2]
    assert payload["task_id"] == str(task.id)
    assert payload["cycle_id"] == str(cycle.id)
    assert payload["phase"] == payload["phase_number"] == 1
    assert payload["phase_state"] == "failed_phase1"
    assert payload["completion"]["status"] == "FAILED"
    assert payload["refetch"] == {"surfaces": ["strategic_session", "directions"]}
    assert events.index("commit") < events.index(completion), (
        "completion must be broadcast AFTER the reconciliation commit"
    )


@pytest.mark.asyncio
async def test_step7_terminal_residue_reloads_task_before_reconciliation(
    reaper_db, mocker
):
    """A stale terminal discovery row cannot terminalize a requeued task's cycle."""
    from app import strategic_cycle

    project = await _make_project(reaper_db)
    task = Task(
        project_id=project.id,
        title="strategic-requeued",
        payloads=[{"type": "design_doc_update", "scope": "medium"}],
        status=TaskStatus.QUEUED,
    )
    reaper_db.add(task)
    await reaper_db.commit()
    await reaper_db.refresh(task)
    cycle = StrategicCycle(
        id=_make_cycle_id(),
        project_id=project.id,
        outcome="phase1_running",
        phase1_task_id=task.id,
        phase1_started_at=_utcnow(),
    )
    reaper_db.add(cycle)
    await reaper_db.commit()

    stale_candidate = Task(
        id=task.id,
        project_id=project.id,
        status=TaskStatus.FAILED,
    )
    mocker.patch.object(
        strategic_cycle,
        "terminal_phase_tasks_requiring_reconciliation",
        return_value=[stale_candidate],
    )
    completion_events = mocker.patch.object(
        strategic_cycle,
        "emit_terminal_completion_events",
        new_callable=AsyncMock,
    )

    await _rs.sweep_stalled_strategic_cycles(
        reaper_db,
        _utcnow(),
        heartbeat_stale_cutoff_fn=lambda _task, now: now - timedelta(minutes=5),
    )

    await reaper_db.refresh(task)
    await reaper_db.refresh(cycle)
    assert task.status == TaskStatus.QUEUED
    assert cycle.outcome == "phase1_running"
    completion_events.assert_not_awaited()


@pytest.mark.asyncio
async def test_step7_terminal_residue_defers_contended_task(reaper_db, mocker):
    """A task held by another writer is deferred without a receipt."""
    from contextlib import asynccontextmanager
    from app import strategic_cycle

    project = await _make_project(reaper_db)
    task = Task(
        project_id=project.id,
        title="strategic-contended",
        payloads=[{"type": "design_doc_update", "scope": "medium"}],
        status=TaskStatus.FAILED,
    )
    reaper_db.add(task)
    await reaper_db.commit()
    await reaper_db.refresh(task)
    cycle = StrategicCycle(
        id=_make_cycle_id(),
        project_id=project.id,
        outcome="phase1_running",
        phase1_task_id=task.id,
        phase1_started_at=_utcnow(),
    )
    reaper_db.add(cycle)
    await reaper_db.commit()

    @asynccontextmanager
    async def _held_task(*_args, **_kwargs):
        yield None

    mocker.patch("app.task_locking.task_lock", _held_task)
    completion_events = mocker.patch.object(
        strategic_cycle,
        "emit_terminal_completion_events",
        new_callable=AsyncMock,
    )

    await _rs.sweep_stalled_strategic_cycles(
        reaper_db,
        _utcnow(),
        heartbeat_stale_cutoff_fn=lambda _task, now: now - timedelta(minutes=5),
    )

    await reaper_db.refresh(cycle)
    assert cycle.outcome == "phase1_running"
    completion_events.assert_not_awaited()


@pytest.mark.asyncio
async def test_step7_stalled_cycle_broadcasts_after_commit(reaper_db, mocker):
    """Phase-one stall broadcasts its alert then canonical completion after commit."""
    project = await _make_project(reaper_db)
    task = Task(
        project_id=project.id,
        title="strategic-phase1",
        payloads=[{"type": "design_doc_update", "scope": "medium"}],
        status=TaskStatus.IN_PROGRESS,
        last_heartbeat=_utcnow() - timedelta(hours=1),
    )
    reaper_db.add(task)
    await reaper_db.commit()
    await reaper_db.refresh(task)

    cycle = StrategicCycle(
        id=_make_cycle_id(),
        project_id=project.id,
        outcome="phase1_running",
        phase1_task_id=task.id,
        phase1_started_at=_utcnow() - timedelta(hours=4),
    )
    reaper_db.add(cycle)
    await reaper_db.commit()

    events: list = []
    real_commit = reaper_db.commit

    async def _tracking_commit(*a, **k):
        events.append("commit")
        return await real_commit(*a, **k)

    mocker.patch.object(reaper_db, "commit", side_effect=_tracking_commit)

    async def _tracking_broadcast(project_id, event, payload):
        events.append(("broadcast", event, payload))

    mocker.patch("app.broadcast.broadcast_to_project", side_effect=_tracking_broadcast)
    mocker.patch(
        "app.strategic_events.broadcast_to_project", side_effect=_tracking_broadcast
    )

    stalled = await _rs.sweep_stalled_strategic_cycles(
        reaper_db,
        _utcnow(),
        heartbeat_stale_cutoff_fn=lambda _task, now: now - timedelta(minutes=5),
    )

    assert stalled == 1
    assert "commit" in events
    alert = next(event for event in events if event[1] == "alert_created")
    completion = next(
        event for event in events if event[1] == "strategic_cycle_completed"
    )
    assert alert[2]["severity"] == "WARNING"
    payload = completion[2]
    assert payload["task_id"] == payload["strategic"]["task_id"] == str(task.id)
    assert payload["cycle_id"] == payload["strategic"]["cycle_id"] == str(cycle.id)
    assert payload["phase"] == payload["phase_number"] == payload["strategic"]["phase"] == 1
    assert payload["phase_state"] == "stalled_phase1"
    assert payload["refetch"] == {"surfaces": ["strategic_session", "directions"]}
    assert payload["completion"]["status"] == "failed"
    assert payload["completion"]["status_message"] == (
        "reaper declared strategic cycle stalled in phase1 after >3h"
    )
    assert payload["completion"]["failure_reason"].startswith(
        "reaper_stall (attempt 1, cause phase1:agent_heartbeat_lost):"
    ), payload["completion"]["failure_reason"]
    assert events.index("commit") < events.index(alert) < events.index(completion)


@pytest.mark.asyncio
async def test_step7_stalled_phase2_completion_broadcasts_after_commit(reaper_db, mocker):
    """Phase-two stall uses the same post-commit canonical completion contract."""
    project = await _make_project(reaper_db)
    task = Task(
        project_id=project.id,
        title="strategic-phase2",
        payloads=[{"type": "strategic_review", "scope": "medium"}],
        status=TaskStatus.IN_PROGRESS,
        last_heartbeat=_utcnow() - timedelta(hours=1),
    )
    reaper_db.add(task)
    await reaper_db.commit()
    cycle = StrategicCycle(
        id=_make_cycle_id(),
        project_id=project.id,
        outcome="phase2_running",
        phase2_task_id=task.id,
        phase1_started_at=_utcnow() - timedelta(hours=4),
        phase2_started_at=_utcnow() - timedelta(hours=4),
    )
    reaper_db.add(cycle)
    await reaper_db.commit()

    events: list = []
    real_commit = reaper_db.commit

    async def _tracking_commit(*args, **kwargs):
        events.append("commit")
        return await real_commit(*args, **kwargs)

    async def _tracking_broadcast(project_id, event, payload):
        events.append(("broadcast", event, payload))

    mocker.patch.object(reaper_db, "commit", side_effect=_tracking_commit)
    mocker.patch("app.broadcast.broadcast_to_project", side_effect=_tracking_broadcast)
    mocker.patch(
        "app.strategic_events.broadcast_to_project", side_effect=_tracking_broadcast
    )

    assert await _rs.sweep_stalled_strategic_cycles(
        reaper_db,
        _utcnow(),
        heartbeat_stale_cutoff_fn=lambda _task, now: now - timedelta(minutes=5),
    ) == 1

    alert = next(event for event in events if event[1] == "alert_created")
    completion = next(
        event for event in events if event[1] == "strategic_cycle_completed"
    )
    assert completion[2]["task_id"] == str(task.id)
    assert completion[2]["cycle_id"] == str(cycle.id)
    assert completion[2]["phase"] == completion[2]["phase_number"] == 2
    assert completion[2]["phase_state"] == "stalled_phase2"
    assert completion[2]["completion"]["status"] == "failed"
    assert completion[2]["completion"]["status_message"] == (
        "reaper declared strategic cycle stalled in phase2 after >3h"
    )
    assert completion[2]["completion"]["failure_reason"].startswith(
        "reaper_stall (attempt 1, cause phase2:"
    ), completion[2]["completion"]["failure_reason"]
    assert events.index("commit") < events.index(alert) < events.index(completion)


@pytest.mark.asyncio
async def test_step7_stalled_cycle_does_not_broadcast_on_commit_failure(
    reaper_db, mocker
):
    """If the batch commit fails, stalled-cycle alerts must not be broadcast."""
    project = await _make_project(reaper_db)
    task = Task(
        project_id=project.id,
        title="strategic-phase1",
        payloads=[{"type": "design_doc_update", "scope": "medium"}],
        status=TaskStatus.IN_PROGRESS,
        last_heartbeat=_utcnow() - timedelta(hours=1),
    )
    reaper_db.add(task)
    await reaper_db.commit()
    await reaper_db.refresh(task)

    cycle = StrategicCycle(
        id=_make_cycle_id(),
        project_id=project.id,
        outcome="phase1_running",
        phase1_task_id=task.id,
        phase1_started_at=_utcnow() - timedelta(hours=4),
    )
    reaper_db.add(cycle)
    await reaper_db.commit()

    broadcast = mocker.patch(
        "app.broadcast.broadcast_to_project", new_callable=AsyncMock
    )
    completion_broadcast = mocker.patch(
        "app.strategic_events.broadcast_to_project", new_callable=AsyncMock
    )
    mocker.patch.object(
        reaper_db, "commit", side_effect=RuntimeError("stalled-cycle commit boom")
    )

    with pytest.raises(RuntimeError, match="stalled-cycle commit boom"):
        await _rs.sweep_stalled_strategic_cycles(
            reaper_db,
            _utcnow(),
            heartbeat_stale_cutoff_fn=lambda _task, now: now - timedelta(minutes=5),
        )

    broadcast.assert_not_called()
    completion_broadcast.assert_not_called()


@pytest.mark.asyncio
async def test_step7_stalled_cycle_completion_broadcast_failure_is_nonfatal(
    reaper_db, mocker
):
    """A completion delivery error cannot undo a durably stalled cycle."""
    project = await _make_project(reaper_db)
    task = Task(
        project_id=project.id,
        title="strategic-phase1",
        payloads=[{"type": "design_doc_update", "scope": "medium"}],
        status=TaskStatus.IN_PROGRESS,
        last_heartbeat=_utcnow() - timedelta(hours=1),
    )
    reaper_db.add(task)
    await reaper_db.commit()
    cycle = StrategicCycle(
        id=_make_cycle_id(),
        project_id=project.id,
        outcome="phase1_running",
        phase1_task_id=task.id,
        phase1_started_at=_utcnow() - timedelta(hours=4),
    )
    reaper_db.add(cycle)
    await reaper_db.commit()

    mocker.patch("app.broadcast.broadcast_to_project", new_callable=AsyncMock)
    completion_broadcast = mocker.patch(
        "app.strategic_events.broadcast_to_project",
        new_callable=AsyncMock,
        side_effect=RuntimeError("completion websocket unavailable"),
    )

    assert await _rs.sweep_stalled_strategic_cycles(
        reaper_db,
        _utcnow(),
        heartbeat_stale_cutoff_fn=lambda _task, now: now - timedelta(minutes=5),
    ) == 1

    completion_broadcast.assert_awaited_once()
    await reaper_db.refresh(task)
    await reaper_db.refresh(cycle)
    assert task.status == TaskStatus.FAILED
    assert cycle.outcome == "stalled_phase1"


@pytest.mark.asyncio
async def test_step7_stalled_cycle_skips_strategic_local_runner_hold(reaper_db):
    project = await _make_project(reaper_db)
    task = Task(
        project_id=project.id,
        title="strategic-local-hold",
        payloads=[{"type": "strategic_review"}],
        status=TaskStatus.QUEUED,
        context={
            STRATEGIC_LOCAL_RUNNER_REQUIRED_CONTEXT_KEY: True,
            STRATEGIC_LOCAL_RUNNER_HOLD_CONTEXT_KEY: (
                _utcnow() - timedelta(hours=4)
            ).isoformat(),
        },
    )
    reaper_db.add(task)
    await reaper_db.commit()
    await reaper_db.refresh(task)

    cycle = StrategicCycle(
        id=_make_cycle_id(),
        project_id=project.id,
        outcome="phase2_running",
        phase1_started_at=_utcnow() - timedelta(hours=5),
        phase1_completed_at=_utcnow() - timedelta(hours=4),
        phase2_started_at=_utcnow() - timedelta(hours=4),
        phase2_task_id=task.id,
    )
    reaper_db.add(cycle)
    await reaper_db.commit()

    stalled = await _rs.sweep_stalled_strategic_cycles(
        reaper_db,
        _utcnow(),
        heartbeat_stale_cutoff_fn=lambda _task, now: now - timedelta(minutes=5),
    )

    await reaper_db.refresh(task)
    await reaper_db.refresh(cycle)
    assert stalled == 0
    assert task.status == TaskStatus.QUEUED
    assert cycle.outcome == "phase2_running"


# ── Orphan reap crash isolation & quarantine ─────────────────────────────


@pytest.fixture()
def clean_crash_streaks():
    """Reset the per-task reap-crash streak tracker before and after each test."""
    _orphan_reap_crash_streaks.clear()
    yield _orphan_reap_crash_streaks
    _orphan_reap_crash_streaks.clear()


@pytest.mark.asyncio
async def test_orphan_per_row_commit_idempotency_collision_isolated(
    reaper_db, stop_after_one_cycle, reaper_side_effects, clean_crash_streaks, mocker
):
    """FIX b: when ONE orphan's per-row commit raises the
    ux_task_active_idempotency_key IntegrityError (the narrow race the pre-query
    guard can't catch), only THAT row is rolled back and skipped — every other
    orphan in the batch stays reaped+committed. Before per-row commit, the single
    batched commit rolled back the WHOLE batch, discarding every sibling's
    recovery work. The colliding row is skipped cleanly (no crash-streak /
    quarantine) since it is a benign idempotency collision, and re-qualifies next
    cycle."""
    from sqlalchemy.exc import IntegrityError as _IntegrityError

    project = await _make_project(reaper_db)
    # Two stale orphans; the "collider" will trip a genuine IntegrityError on its
    # own per-row commit. The "good" sibling must survive committed regardless of
    # processing order.
    good = Task(
        project_id=project.id,
        payloads=[{"type": "create"}],
        vm_instance_id="vm-good-row",
        status=TaskStatus.IN_PROGRESS,
        attempt_count=0,
        last_heartbeat=_utcnow() - timedelta(minutes=10),
    )
    collider = Task(
        project_id=project.id,
        payloads=[{"type": "create"}],
        vm_instance_id="vm-collider-row",
        status=TaskStatus.IN_PROGRESS,
        attempt_count=0,
        last_heartbeat=_utcnow() - timedelta(minutes=10),
    )
    reaper_db.add(good)
    reaper_db.add(collider)
    await reaper_db.commit()
    collider_id = collider.id

    # Fail the per-row commit ONLY while the collider row is the dirty task. We
    # inject a GENUINE flush-level IntegrityError (duplicate PK) first so the
    # underlying DB transaction is really aborted — the way a racing
    # ux_task_active_idempotency_key insert would — then raise the
    # idempotency-shaped IntegrityError the reaper classifies. This makes the
    # subsequent session.rollback() actually discard the collider's pending
    # mutation (a no-op fake raise would leave the tx active and the trailing
    # post-loop commit would wrongly persist it). All other commits (pre-loop,
    # the good row, post-loop) pass through unchanged. Mirrors the real-poison
    # pattern in test_step3_4_rollup_poison_rolled_back_for_later_steps.
    from sqlalchemy import text as _text

    real_commit = AsyncSession.commit
    fired = {"n": 0}

    async def patched_commit(self):
        dirty_ids = {getattr(o, "id", None) for o in self.dirty}
        if collider_id in dirty_ids and fired["n"] == 0:
            fired["n"] += 1
            proj_id = (
                await self.execute(_text("SELECT id FROM project LIMIT 1"))
            ).scalar()
            dup = _uuid_mod.uuid4()
            for _ in range(2):
                self.add(
                    Task(
                        id=dup,
                        project_id=proj_id,
                        payloads=[{"type": "create"}],
                        status=TaskStatus.QUEUED,
                    )
                )
            with contextlib.suppress(_IntegrityError):
                await self.flush()  # real duplicate-PK abort
            raise _IntegrityError(
                "INSERT ...",
                {},
                Exception(
                    "duplicate key value violates unique constraint "
                    '"ux_task_active_idempotency_key"'
                ),
            )
        return await real_commit(self)

    mocker.patch.object(AsyncSession, "commit", patched_commit)

    import app.reaper as _reaper_mod

    _cycles_before = _reaper_mod._reaper_cycles_completed_count
    await _run_reaper_once()
    await reaper_db.refresh(good)
    await reaper_db.refresh(collider)

    assert _reaper_mod._reaper_cycles_completed_count == _cycles_before + 1, (
        "cycle must complete cleanly through the per-row collision"
    )
    # ── Core fix-(b) guarantee: one bad row does NOT discard the rest. ──
    # The good sibling's reap is durably committed even though the collider's
    # own per-row commit raised. Under the OLD batched commit, the single
    # post-loop commit hit the collision and rolled back the WHOLE batch — the
    # good sibling would still be IN_PROGRESS here.
    assert good.status == TaskStatus.QUEUED, (
        "the good orphan must stay reaped+committed despite the sibling's "
        "per-row idempotency collision (per-row commit isolation)"
    )
    assert good.attempt_count == 1
    assert fired["n"] == 1, (
        "the injected per-row collision must have fired exactly once"
    )
    # The collision is a benign skip, NOT a terminal failure: the collider must
    # not be FAILED/quarantined by the reaper (it re-qualifies next cycle).
    assert collider.status != TaskStatus.FAILED, (
        "a benign idempotency collision must not terminal-fail the orphan"
    )
    assert "Quarantined" not in (collider.latest_status_message or ""), (
        "a benign idempotency collision must not trigger quarantine"
    )
    # ...and it must not accrue a poison-pill crash streak.
    assert str(collider_id) not in clean_crash_streaks, (
        "a clean idempotency-collision skip must not accrue a crash streak"
    )


@pytest.mark.asyncio
async def test_orphan_crash_sibling_reaped_when_poison_throws(
    reaper_db, stop_after_one_cycle, reaper_side_effects, clean_crash_streaks, mocker
):
    """A poison task that raises mid-reap is skipped; a sibling orphan in the same
    batch is still reaped normally — per-task isolation prevents cascade failure."""
    project = await _make_project(reaper_db)
    poison = Task(
        project_id=project.id,
        payloads=[{"type": "create"}],
        vm_instance_id="vm-poison",
        status=TaskStatus.IN_PROGRESS,
        attempt_count=0,
        last_heartbeat=_utcnow() - timedelta(minutes=10),
    )
    healthy = Task(
        project_id=project.id,
        payloads=[{"type": "create"}],
        vm_instance_id="vm-healthy-sibling",
        status=TaskStatus.IN_PROGRESS,
        attempt_count=0,
        last_heartbeat=_utcnow() - timedelta(minutes=10),
    )
    reaper_db.add(poison)
    reaper_db.add(healthy)
    await reaper_db.commit()
    poison_id = poison.id

    # Inject a crash inside _run_orphan_io for the poison task.  This function
    # is called at line 1136 during orphan processing, which is inside the
    # per-orphan try/except block.  The crash triggers the except-block's
    # session.rollback() (line 2265), expiring ALL cached objects including
    # _projects_batch entries.  _run_orphan_io was chosen because (a) it is
    # guaranteed to be called for every orphan, (b) it runs AFTER the fresh_task
    # guard (so the task instance is valid), and (c) it runs BEFORE the three
    # _projects_batch call sites.
    import app.reaper as _reaper_mod

    _real_run_orphan_io = _reaper_mod._run_orphan_io

    async def patched_run_orphan_io(*args, **kwargs):
        task = kwargs.get("task")
        if task is not None and task.id == poison_id:
            raise RuntimeError("injected-poison-pill")
        return await _real_run_orphan_io(*args, **kwargs)

    mocker.patch.object(
        _reaper_mod, "_run_orphan_io", side_effect=patched_run_orphan_io
    )

    _cycles_before = _reaper_mod._reaper_cycles_completed_count
    await _run_reaper_once()
    await reaper_db.refresh(poison)
    await reaper_db.refresh(healthy)

    # The cycle must complete cleanly: the per-task crash + rollback expires the
    # batch's ORM objects, and the post-commit loops (rollup/dependent-unblock)
    # must not touch expired attributes (would raise MissingGreenlet and bubble
    # to the cycle handler, stalling the liveness counter). A bumped counter
    # proves the success path ran end-to-end, not the cycle-level except.
    assert _reaper_mod._reaper_cycles_completed_count == _cycles_before + 1, (
        "reaper cycle must complete cleanly despite the poison-task crash — "
        "a stalled counter means the post-loop hit MissingGreenlet on an "
        "expired ORM object and the cycle handler swallowed it"
    )

    assert poison.status == TaskStatus.IN_PROGRESS, (
        "poison task must not be reaped this cycle (crash was caught and skipped)"
    )
    assert healthy.status == TaskStatus.QUEUED, (
        "sibling healthy task must be reaped normally despite poison task crash"
    )
    assert healthy.attempt_count == 1
    assert clean_crash_streaks.get(str(poison_id)) == 1, (
        "crash streak must be incremented for the poison task"
    )


@pytest.mark.asyncio
async def test_orphan_projects_batch_expired_after_rollback_guard_re_fetches(
    reaper_db, stop_after_one_cycle, reaper_side_effects, clean_crash_streaks, mocker
):
    """After a per-row session.rollback() expires the batch-cached Project
    objects in _projects_batch, a later orphan in the same batch that reads
    _projects_batch must receive a FRESH Project via the expiry guard — not an
    expired instance whose attribute access would raise MissingGreenlet.

    Regression: the _orphan_task_map already has this guard (inspect+re-fetch at
    lines 888-890), but the three _projects_batch.get() call sites (lines 1589,
    1728, 2090) did not. After a prior orphan iteration's except-block
    session.rollback(), the cached Project instances became expired, and the
    guard-less sites would crash-quarantine an otherwise-healthy orphan task.

    This test targets the call site at the research retry path (line 2090):
    ``_res_proj = _projects_batch.get(task.project_id)`` inside
    ``if task_type == "research"``. Two research-type orphans share a project;
    the first triggers the rollback by crashing in _run_orphan_io, and the
    second reaches the research retry path where the guard must re-fetch the
    expired Project.
    """
    project = await _make_project(reaper_db)

    # Two research-type orphans sharing the same project.  Research/plan are
    # thinking-heavy task types, whose heartbeat-stale cutoff is floored at
    # _THINKING_HEAVY_PROGRESS_STALE_SECONDS (1500s / 25 min) — a live agent may
    # legitimately go silent for 20+ min mid-reasoning (b2410541 incident). The
    # heartbeat must therefore be older than that floor for these orphans to be
    # classified for reaping and reach the research-retry guard this test targets.
    poison = Task(
        project_id=project.id,
        payloads=[{"type": "research"}],
        vm_instance_id="vm-poison-research",
        status=TaskStatus.IN_PROGRESS,
        attempt_count=0,
        last_heartbeat=_utcnow() - timedelta(minutes=30),
    )
    healthy = Task(
        project_id=project.id,
        payloads=[{"type": "research"}],
        vm_instance_id="vm-healthy-research",
        status=TaskStatus.IN_PROGRESS,
        attempt_count=0,
        last_heartbeat=_utcnow() - timedelta(minutes=30),
    )
    reaper_db.add(poison)
    reaper_db.add(healthy)
    await reaper_db.commit()
    poison_id = poison.id

    # Inject a crash inside _run_orphan_io for the poison task.  This function
    # is called at line 1136 during orphan processing, which is inside the
    # per-orphan try/except block.  The crash triggers the except-block's
    # session.rollback() (line 2265), expiring ALL cached objects including
    # _projects_batch entries.  _run_orphan_io was chosen because (a) it is
    # guaranteed to be called for every orphan, (b) it runs AFTER the fresh_task
    # guard (so the task instance is valid), and (c) it runs BEFORE the three
    # _projects_batch call sites.
    import app.reaper as _reaper_mod

    _real_run_orphan_io = _reaper_mod._run_orphan_io

    async def patched_run_orphan_io(*args, **kwargs):
        task = kwargs.get("task")
        if task is not None and task.id == poison_id:
            raise RuntimeError("injected-poison-pill")
        return await _real_run_orphan_io(*args, **kwargs)

    mocker.patch.object(
        _reaper_mod, "_run_orphan_io", side_effect=patched_run_orphan_io
    )

    # Spy on refresh_research_task_verification to record whether the Project
    # instance passed in was expired.  If the guard works, the Project will be
    # fresh (re-fetched via session.get).  If the guard is missing, the Project
    # will be expired — accessing its attributes inside the real function would
    # raise MissingGreenlet.
    refresh_calls: list = []

    async def spy_refresh(task, proj, session):
        refresh_calls.append(
            {
                "project_expired": inspect(proj).expired,
                "task_id": task.id,
            }
        )

    mocker.patch(
        "app.autonomous_research.refresh_research_task_verification",
        side_effect=spy_refresh,
    )

    _cycles_before = _reaper_mod._reaper_cycles_completed_count
    await _run_reaper_once()
    await reaper_db.refresh(poison)
    await reaper_db.refresh(healthy)

    # The reaper cycle must complete cleanly — no MissingGreenlet from an
    # expired Project cascading through the per-orphan except block.
    assert _reaper_mod._reaper_cycles_completed_count == _cycles_before + 1, (
        "reaper cycle must complete cleanly despite expired _projects_batch"
    )

    # Poison task skipped this cycle (crash was caught, rolled back, skipped).
    assert poison.status == TaskStatus.IN_PROGRESS, (
        "poison task must not be reaped — crash caught and skipped"
    )

    # Healthy sibling MUST be reaped normally.  It reaches the research retry
    # path (line 2084+), hits _projects_batch.get(...) at line 2094, and the
    # expiry guard must have re-fetched the expired Project so the task is
    # retried without error.
    assert healthy.status == TaskStatus.QUEUED, (
        "healthy research task must be retried after sibling's rollback "
        "expired _projects_batch entries"
    )
    assert healthy.attempt_count == 1

    # refresh_research_task_verification must have been called for the healthy
    # task (it's a research type and the retry path reached line 2084-2096).
    assert len(refresh_calls) >= 1, (
        "refresh_research_task_verification must be called for the "
        "healthy research task's retry"
    )

    # CRITICAL: the Project passed to the verification function must NOT be
    # expired — this proves the expiry guard at line 2097 re-fetched it.
    expired = [c for c in refresh_calls if c["project_expired"]]
    assert not expired, (
        "refresh_research_task_verification received an expired Project — "
        "the _projects_batch expiry guard did not re-fetch"
    )

    # Crash streak must be incremented for the poison task (one crash).
    assert clean_crash_streaks.get(str(poison_id)) == 1, (
        "crash streak must be incremented for the poison task"
    )


@pytest.mark.asyncio
async def test_orphan_crash_quarantines_task_after_streak_threshold(
    reaper_db, stop_after_one_cycle, reaper_side_effects, clean_crash_streaks, mocker
):
    """A task that causes a reap crash on every cycle is quarantined (FAILED) after
    _ORPHAN_REAP_CRASH_STREAK_LIMIT consecutive crashes, with an operator-visible Alert.
    The streak counter is cleared on quarantine so it stops being re-selected."""
    project = await _make_project(reaper_db)
    poison = Task(
        project_id=project.id,
        payloads=[{"type": "create"}],
        vm_instance_id="vm-to-quarantine",
        status=TaskStatus.IN_PROGRESS,
        attempt_count=0,
        last_heartbeat=_utcnow() - timedelta(minutes=10),
    )
    reaper_db.add(poison)
    await reaper_db.commit()
    poison_id = poison.id

    # Pre-seed streak to one below the threshold so this cycle triggers quarantine.
    clean_crash_streaks[str(poison_id)] = _ORPHAN_REAP_CRASH_STREAK_LIMIT - 1

    # Inject a crash inside _run_orphan_io for the poison task.  This function
    # is called at line 1136 during orphan processing, which is inside the
    # per-orphan try/except block.  The crash triggers the except-block's
    # session.rollback() (line 2265), expiring ALL cached objects and
    # incrementing the crash streak.  The streak then reaches the threshold,
    # triggering the quarantine path (which uses its own fresh _qsession).
    import app.reaper as _reaper_mod

    _real_run_orphan_io = _reaper_mod._run_orphan_io

    async def patched_run_orphan_io(*args, **kwargs):
        task = kwargs.get("task")
        if task is not None and task.id == poison_id:
            raise RuntimeError("injected-poison-quarantine")
        return await _real_run_orphan_io(*args, **kwargs)

    mocker.patch.object(
        _reaper_mod, "_run_orphan_io", side_effect=patched_run_orphan_io
    )

    _cycles_before = _reaper_mod._reaper_cycles_completed_count
    await _run_reaper_once()
    await reaper_db.refresh(poison)

    # Quarantine path also rolls back + expires the batch; the cycle must still
    # complete cleanly (counter advances), proving the post-loop never reads an
    # expired ORM attribute.
    assert _reaper_mod._reaper_cycles_completed_count == _cycles_before + 1, (
        "reaper cycle must complete cleanly through the quarantine path"
    )

    assert poison.status == TaskStatus.FAILED, (
        "task must be quarantined (FAILED) after reaching the streak threshold"
    )
    assert "Quarantined" in (poison.latest_status_message or ""), (
        "status message must mention 'Quarantined'"
    )
    assert "injected-poison-quarantine" in (poison.latest_status_message or ""), (
        "status message must include the last error for operator triage"
    )

    alerts = (
        (await reaper_db.execute(_select(Alert).where(Alert.task_id == poison_id)))
        .scalars()
        .all()
    )
    assert len(alerts) >= 1, "quarantine must emit an operator-visible Alert"
    assert (
        "quarantine" in (alerts[0].title or "").lower()
        or "quarantine" in (alerts[0].message or "").lower()
    )

    assert str(poison_id) not in clean_crash_streaks, (
        "streak entry must be cleared after quarantine so the task stops being re-selected"
    )


# ── Cycle-level halt circuit breaker ─────────────────────────────────────────


class _FailingSessionCM:
    """Async context manager whose __aenter__ always raises (simulates cycle crash)."""

    def __call__(self):
        return self

    async def __aenter__(self):
        raise RuntimeError("injected-cycle-crash")

    async def __aexit__(self, *_):
        pass


_HALT_STATE_CLEAN = {
    "halted": False,
    "halt_timestamp": None,
    "last_error": None,
    "crash_count": 0,
    "hard_halted": False,
    "half_open_failures": 0,
    "_in_probe": False,
}


@pytest.fixture()
def clean_halt_state():
    """Reset _reaper_halt_state to the clean baseline before and after each test."""
    import app.reaper as _reaper_mod

    _reaper_mod._reaper_halt_state.update(_HALT_STATE_CLEAN)
    yield _reaper_mod._reaper_halt_state
    _reaper_mod._reaper_halt_state.update(_HALT_STATE_CLEAN)


@pytest.mark.asyncio
async def test_cycle_halt_state_recorded_after_n_consecutive_crashes(
    reaper_side_effects, clean_halt_state, mocker
):
    """After _REAPER_CYCLE_HALT_THRESHOLD consecutive whole-cycle exceptions the
    reaper records HALTED state and get_reaper_stats()/is_reaper_halted() reflect it."""
    mocker.patch("app.reaper.AsyncSessionLocal", _FailingSessionCM())
    mocker.patch("app.reaper._post_reaper_halt_alert", new_callable=AsyncMock)

    async def _sleep_until_halted(secs):
        if _reaper_halt_state["halted"]:
            raise _StopLoop()

    mocker.patch("asyncio.sleep", side_effect=_sleep_until_halted)

    await _run_reaper_once()

    assert _reaper_halt_state["halted"] is True, (
        "halt state must be set after N crashes"
    )
    assert _reaper_halt_state["crash_count"] == _REAPER_CYCLE_HALT_THRESHOLD
    assert "injected-cycle-crash" in (_reaper_halt_state["last_error"] or "")
    assert _reaper_halt_state["halt_timestamp"] is not None

    stats = get_reaper_stats()
    assert stats["halted"] is True, "get_reaper_stats must surface halt flag"
    assert stats["halt_crash_count"] == _REAPER_CYCLE_HALT_THRESHOLD
    assert is_reaper_halted() is True


@pytest.mark.asyncio
async def test_cycle_halt_skips_further_cycle_attempts(
    reaper_side_effects, clean_halt_state, mocker
):
    """Once the reaper is halted, subsequent loop iterations skip the cycle body
    entirely (the halt branch sleeps 300s and continues)."""
    import app.reaper as _reaper_mod

    _reaper_mod._reaper_halt_state.update(
        {
            "halted": True,
            "halt_timestamp": _utcnow(),
            "last_error": "pre-halted",
            "crash_count": _REAPER_CYCLE_HALT_THRESHOLD,
        }
    )

    session_factory_mock = mocker.patch("app.reaper.AsyncSessionLocal")
    mocker.patch("asyncio.sleep", side_effect=_StopLoop())

    await _run_reaper_once()

    assert session_factory_mock.call_count == 0, (
        "halted reaper must not enter the cycle body (AsyncSessionLocal not called)"
    )


@pytest.mark.asyncio
async def test_cycle_halt_alert_posted_exactly_once(
    reaper_side_effects, clean_halt_state, mocker
):
    """_post_reaper_halt_alert is called exactly once — right when the threshold is crossed."""
    mocker.patch("app.reaper.AsyncSessionLocal", _FailingSessionCM())
    halt_alert = mocker.patch(
        "app.reaper._post_reaper_halt_alert", new_callable=AsyncMock
    )

    # Allow error-backoff sleeps; stop once halted (halt-branch sleep fires)
    async def _sleep_until_halted(secs):
        if _reaper_halt_state["halted"]:
            raise _StopLoop()

    mocker.patch("asyncio.sleep", side_effect=_sleep_until_halted)

    await _run_reaper_once()

    # The alert task is created via asyncio.get_running_loop().create_task(...) so
    # it may not have awaited yet; we check the mock was called once.
    assert halt_alert.call_count == 1, (
        "_post_reaper_halt_alert must be called exactly once when halt is triggered"
    )
    call_args = halt_alert.call_args
    assert call_args[0][0] == _REAPER_CYCLE_HALT_THRESHOLD  # crash_count
    assert "injected-cycle-crash" in call_args[0][1]  # last_error


@pytest.mark.asyncio
async def test_post_reaper_halt_alert_bulk_inserts_one_per_project(reaper_db):
    """_post_reaper_halt_alert bulk-inserts exactly one ERROR alert per project
    in a single round-trip (no per-project ORM add())."""
    from app.reaper import _post_reaper_halt_alert

    for i in range(3):
        reaper_db.add(Project(name=f"halt-proj-{i}", user_id="u"))
    await reaper_db.commit()

    await _post_reaper_halt_alert(crash_count=4, last_error="boom")

    alerts = (await reaper_db.execute(select(Alert))).scalars().all()
    assert len(alerts) == 3
    assert all(a.severity == AlertSeverity.ERROR for a in alerts)
    assert all(a.title == "Reaper service HALTED after crash-loop" for a in alerts)
    assert all("crash-looped 4x" in a.message for a in alerts)
    # One alert per distinct project — no duplicates, none dropped.
    assert len({a.project_id for a in alerts}) == 3


@pytest.mark.asyncio
async def test_post_reaper_recovery_alert_bulk_inserts_one_per_project(reaper_db):
    """_post_reaper_recovery_alert bulk-inserts exactly one INFO alert per project."""
    from app.reaper import _post_reaper_recovery_alert

    for i in range(3):
        reaper_db.add(Project(name=f"recover-proj-{i}", user_id="u"))
    await reaper_db.commit()

    await _post_reaper_recovery_alert(probe_failures=2)

    alerts = (await reaper_db.execute(select(Alert))).scalars().all()
    assert len(alerts) == 3
    assert all(a.severity == AlertSeverity.INFO for a in alerts)
    assert all(a.title == "Reaper service recovered autonomously" for a in alerts)
    assert len({a.project_id for a in alerts}) == 3


@pytest.mark.asyncio
async def test_post_reaper_halt_alert_no_projects_is_noop(reaper_db):
    """With no projects the early return avoids issuing an empty bulk INSERT."""
    from app.reaper import _post_reaper_halt_alert

    await _post_reaper_halt_alert(crash_count=1, last_error="x")

    alerts = (await reaper_db.execute(select(Alert))).scalars().all()
    assert alerts == []


@pytest.mark.asyncio
async def test_per_task_crash_does_not_trigger_cycle_halt(
    reaper_db,
    stop_after_one_cycle,
    reaper_side_effects,
    clean_halt_state,
    clean_crash_streaks,
    mocker,
):
    """A crash inside the per-task isolation block completes the cycle cleanly
    (consecutive_errors stays 0) so the cycle-level halt is never triggered."""
    project = await _make_project(reaper_db)
    orphan = Task(
        project_id=project.id,
        payloads=[{"type": "create"}],
        vm_instance_id="vm-per-task-crash",
        status=TaskStatus.IN_PROGRESS,
        attempt_count=0,
        last_heartbeat=_utcnow() - timedelta(minutes=10),
    )
    reaper_db.add(orphan)
    await reaper_db.commit()
    orphan_id = orphan.id

    call_count: dict = {}
    real_get = AsyncSession.get

    async def _crash_on_first_get(self, model, pk, **kwargs):
        key = (model, pk)
        call_count[key] = call_count.get(key, 0) + 1
        if pk == orphan_id and call_count[key] == 1:
            raise RuntimeError("per-task-isolated-crash")
        return await real_get(self, model, pk, **kwargs)

    mocker.patch.object(AsyncSession, "get", _crash_on_first_get)

    import app.reaper as _reaper_mod

    cycles_before = _reaper_mod._reaper_cycles_completed_count
    await _run_reaper_once()

    assert _reaper_halt_state["halted"] is False, (
        "per-task crash must not trigger the cycle-level halt circuit breaker"
    )
    assert _reaper_mod._reaper_cycles_completed_count == cycles_before + 1, (
        "cycle must complete cleanly: per-task crash is isolated, not a cycle-level error"
    )


# ── Half-open circuit breaker (auto-recovery) ────────────────────────────────


@pytest.mark.asyncio
async def test_half_open_probe_recovers_after_cooldown(
    reaper_db, stop_after_one_cycle, reaper_side_effects, clean_halt_state, mocker
):
    """When the halt cooldown elapses the reaper runs a probe cycle; on success
    it clears halt state and posts a recovery alert."""
    import app.reaper as _reaper_mod

    recovery_alert = mocker.patch(
        "app.reaper._post_reaper_recovery_alert", new_callable=AsyncMock
    )
    # Pre-set soft halt with an expired cooldown (10 min ago).
    _reaper_mod._reaper_halt_state.update(
        {
            "halted": True,
            "hard_halted": False,
            "halt_timestamp": _utcnow() - timedelta(seconds=600),
            "last_error": "transient-error",
            "crash_count": 5,
            "half_open_failures": 0,
        }
    )

    await _run_reaper_once()

    assert _reaper_halt_state["halted"] is False, (
        "halt must be cleared after a successful probe cycle"
    )
    assert _reaper_halt_state.get("hard_halted") is False
    assert _reaper_halt_state.get("half_open_failures") == 0
    assert is_reaper_halted() is False
    stats = get_reaper_stats()
    assert stats["halted"] is False
    assert stats.get("hard_halted") is False
    assert recovery_alert.call_count == 1


@pytest.mark.asyncio
async def test_half_open_probe_failure_rehalts_with_incremented_counter(
    reaper_side_effects, clean_halt_state, mocker
):
    """A half-open probe that crashes re-halts (soft) with half_open_failures incremented."""
    import app.reaper as _reaper_mod

    mocker.patch("app.reaper.AsyncSessionLocal", _FailingSessionCM())
    mocker.patch("app.reaper._post_reaper_halt_alert", new_callable=AsyncMock)

    _reaper_mod._reaper_halt_state.update(
        {
            "halted": True,
            "hard_halted": False,
            "halt_timestamp": _utcnow() - timedelta(seconds=600),
            "last_error": "initial-error",
            "crash_count": 5,
            "half_open_failures": 0,
        }
    )

    async def _stop_after_rehalt(secs):
        # Stop once the probe has finished and re-halted (not during cooldown sleep).
        if _reaper_halt_state["halted"] and not _reaper_halt_state.get("_in_probe"):
            raise _StopLoop()

    mocker.patch("asyncio.sleep", side_effect=_stop_after_rehalt)

    await _run_reaper_once()

    assert _reaper_halt_state["halted"] is True, (
        "reaper must re-halt after probe failure"
    )
    assert _reaper_halt_state.get("hard_halted") is False, "must remain a soft halt"
    assert _reaper_halt_state.get("half_open_failures") == 1


@pytest.mark.asyncio
async def test_half_open_hard_halt_after_probe_limit(
    reaper_side_effects, clean_halt_state, mocker
):
    """After _REAPER_HALF_OPEN_PROBE_LIMIT consecutive failed probes the reaper
    escalates to a hard halt that requires operator reset."""
    import app.reaper as _reaper_mod

    mocker.patch("app.reaper.AsyncSessionLocal", _FailingSessionCM())
    mocker.patch("app.reaper._post_reaper_halt_alert", new_callable=AsyncMock)

    probe_limit = _REAPER_HALF_OPEN_PROBE_LIMIT
    # Use a timestamp far enough in the past that even the extended cooldown
    # (base * 2^(probe_limit-1)) has definitely elapsed.
    _reaper_mod._reaper_halt_state.update(
        {
            "halted": True,
            "hard_halted": False,
            "halt_timestamp": _utcnow() - timedelta(hours=2),
            "last_error": "recurring-error",
            "crash_count": 5,
            "half_open_failures": probe_limit - 1,
        }
    )

    async def _stop_on_hard_halt(secs):
        if _reaper_halt_state.get("hard_halted"):
            raise _StopLoop()

    mocker.patch("asyncio.sleep", side_effect=_stop_on_hard_halt)

    await _run_reaper_once()

    assert _reaper_halt_state.get("hard_halted") is True, (
        "must escalate to hard halt after probe_limit consecutive failures"
    )
    assert _reaper_halt_state["halted"] is True
    assert _reaper_halt_state.get("half_open_failures") == probe_limit
    assert is_reaper_halted() is True


# ── reaper review-respawn uses per-project task_branch_namespace ───────────


@pytest.mark.asyncio
async def test_reap_with_open_pr_uses_project_namespace(
    reaper_db, stop_after_one_cycle, reaper_side_effects, mocker
):
    """Reaper review-respawn derives branches under the project's task_branch_namespace.

    A project with namespace='autoproj' must produce 'autoproj/*' branch names
    instead of the Blueprint-hardcoded 'blueprint/*'.
    """
    mock_spawn = mocker.patch(
        "app.review_pipeline._spawn_pipeline_task",
        new_callable=AsyncMock,
        return_value=(None, True),
    )

    project = Project(
        name="AutoProj",
        user_id="user1",
        build_config={"task_branch_namespace": "autoproj"},
    )
    reaper_db.add(project)
    await reaper_db.commit()
    await reaper_db.refresh(project)

    t = Task(
        project_id=project.id,
        payloads=[{"type": "create"}],
        vm_instance_id="vm-with-pr-ns",
        status=TaskStatus.IN_PROGRESS,
        attempt_count=0,
        last_heartbeat=_utcnow() - timedelta(minutes=10),
        context={"scope": "small"},
    )
    reaper_db.add(t)
    await reaper_db.commit()
    await reaper_db.refresh(t)

    reaper_db.add(
        Artifact(
            task_id=t.id,
            artifact_type=ArtifactType.PULL_REQUEST,
            pr_url="https://github.com/owner/autoproj/pull/99",
        )
    )
    await reaper_db.commit()

    await _run_reaper_once()
    await reaper_db.refresh(t)

    assert t.status == TaskStatus.EVALUATING
    mock_spawn.assert_called_once()
    _spawned_branch = mock_spawn.call_args[0][4]
    assert _spawned_branch.startswith("autoproj/"), (
        f"expected autoproj/* branch, got {_spawned_branch!r}"
    )


# ── reaper_sweeps seam tests (direct helper invocations) ─────────────────────
# These tests call the extracted sweep helpers directly, proving the extraction
# boundary is well-formed and exercising behaviors that the full-cycle tests
# (which go through _run_reaper_once) do not cover.


@pytest.mark.asyncio
async def test_sweep_routine_message_retention_commits_when_purged(mocker):
    """sweep_routine_message_retention commits the session when messages are purged."""
    mocker.patch(
        "app.reaper_sweeps._purge_stale_routine_messages",
        new_callable=AsyncMock,
        return_value=3,
    )
    mock_session = AsyncMock()
    result = await _rs.sweep_routine_message_retention(mock_session)
    assert result == 3
    mock_session.commit.assert_awaited_once()


@pytest.mark.asyncio
async def test_sweep_routine_message_retention_skips_commit_when_nothing_purged(mocker):
    """sweep_routine_message_retention does NOT commit when there is nothing to purge."""
    mocker.patch(
        "app.reaper_sweeps._purge_stale_routine_messages",
        new_callable=AsyncMock,
        return_value=0,
    )
    mock_session = AsyncMock()
    result = await _rs.sweep_routine_message_retention(mock_session)
    assert result == 0
    mock_session.commit.assert_not_awaited()


@pytest.mark.asyncio
async def test_sweep_expired_message_attachment_uploads_is_global_and_bounded(
    db_session,
    mocker,
):
    from app.models import ProjectMessageAttachmentUpload

    project = Project(name="Global attachment sweep", user_id="sweep-user")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)
    expired = [
        ProjectMessageAttachmentUpload(
            attachment_id=_uuid_mod.uuid4(),
            project_id=project.id,
            actor_user_id=f"actor-{index}",
            storage_uri=f"gs://bucket/message-attachments/{project.id}/{index}/file.bin",
            filename="file.bin",
            expected_size_bytes=10,
            created_at=_utcnow() - timedelta(minutes=30),
            expires_at=_utcnow() - timedelta(minutes=15),
        )
        for index in range(3)
    ]
    db_session.add_all(expired)
    await db_session.commit()
    delete_object = mocker.patch(
        "app.reaper_sweeps.delete_storage_uri_best_effort",
        new_callable=AsyncMock,
        return_value=True,
    )

    removed = await _rs.sweep_expired_message_attachment_uploads(
        db_session,
        batch_size=2,
    )

    assert removed == 2
    assert delete_object.await_count == 2
    remaining = (
        await db_session.execute(select(ProjectMessageAttachmentUpload))
    ).scalars().all()
    assert len(remaining) == 1


@pytest.mark.asyncio
async def test_sweep_regression_scan_cadence_gate_suppresses_second_call(mocker):
    """sweep_regression_scan returns 0 immediately when called within the 15-min window."""
    _rs._last_regression_scan = None  # reset to force first call through
    scan_mock = mocker.patch(
        "app.reaper_sweeps._run_regression_scan_and_notify",
        new_callable=AsyncMock,
        return_value=1,
    )
    mock_session = AsyncMock()
    first = await _rs.sweep_regression_scan(mock_session)
    second = await _rs.sweep_regression_scan(mock_session)

    assert first == 1
    assert second == 0, "cadence gate must suppress second call within 15 min"
    scan_mock.assert_awaited_once()
    _rs._last_regression_scan = None  # restore for test isolation


# ── Intra-cycle heartbeat (PART A) ──────────────────────────────────────────


def test_mark_reaper_progress_updates_last_progress():
    """_mark_reaper_progress() writes a fresh _reaper_last_progress timestamp."""
    import app.reaper as reaper_mod

    saved = reaper_mod._reaper_last_progress
    try:
        reaper_mod._reaper_last_progress = None
        # _utcnow() is naive UTC (see app.time_utils), so bracket with naive UTC.
        before = _utcnow()
        _mark_reaper_progress()
        after = _utcnow()
        assert reaper_mod._reaper_last_progress is not None
        assert before <= reaper_mod._reaper_last_progress <= after
    finally:
        reaper_mod._reaper_last_progress = saved


def test_get_reaper_stats_reports_progress_age_after_heartbeat():
    """get_reaper_stats() exposes last_progress_age_seconds (>=0 after a beat)."""
    import app.reaper as reaper_mod

    saved = reaper_mod._reaper_last_progress
    try:
        _mark_reaper_progress()
        stats = get_reaper_stats()
        assert "last_progress_age_seconds" in stats
        assert stats["last_progress_age_seconds"] >= 0
        assert stats["last_progress_age_seconds"] < 60
        # Existing keys must remain present (no regression).
        assert "last_cycle_age_seconds" in stats
        assert "cycles_completed_total" in stats
    finally:
        reaper_mod._reaper_last_progress = saved


def test_get_reaper_stats_progress_age_minus_one_when_never_marked():
    """last_progress_age_seconds is -1 before any heartbeat is recorded."""
    import app.reaper as reaper_mod

    saved = reaper_mod._reaper_last_progress
    try:
        reaper_mod._reaper_last_progress = None
        stats = get_reaper_stats()
        assert stats["last_progress_age_seconds"] == -1
    finally:
        reaper_mod._reaper_last_progress = saved


# ── Orphan priority + per-cycle cap (PART B) ────────────────────────────────


class _FakeOrphan:
    """Minimal stand-in for a Task exposing only the columns the priority key
    reads (status, latest_status_message, context, vm_spot). Lets the pure
    helpers be unit-tested without a DB session or ORM objects."""

    def __init__(
        self,
        *,
        tag,
        status=TaskStatus.IN_PROGRESS,
        latest_status_message=None,
        context=None,
        vm_spot=None,
    ):
        self.tag = tag  # test-only marker for asserting order
        self.status = status
        self.latest_status_message = latest_status_message
        self.context = context
        self.vm_spot = vm_spot


def test_orphan_priority_key_rate_limited_is_highest_priority():
    """Rate-limit signals (message text OR context failure_category) -> bucket 0."""
    from app.failure_classification import RATE_LIMIT

    msg_rate = _FakeOrphan(tag="m", latest_status_message="hit rate limit, retrying")
    msg_429 = _FakeOrphan(tag="m429", latest_status_message="HTTP 429 Too Many")
    msg_quota = _FakeOrphan(tag="mq", latest_status_message="quota exceeded")
    ctx_rate = _FakeOrphan(tag="c", context={"failure_category": RATE_LIMIT})
    for o in (msg_rate, msg_429, msg_quota, ctx_rate):
        assert _orphan_priority_key(o) == _ORPHAN_PRIORITY_RATE_LIMITED


def test_orphan_priority_key_spot_then_provision_then_default():
    """Bucket order: spot (1) < provision (2) < default (3)."""
    spot = _FakeOrphan(tag="s", vm_spot=True)
    provision = _FakeOrphan(tag="p", status=TaskStatus.PROVISIONING)
    plain = _FakeOrphan(tag="d")
    assert _orphan_priority_key(spot) == _ORPHAN_PRIORITY_SPOT
    assert _orphan_priority_key(provision) == _ORPHAN_PRIORITY_PROVISION
    assert _orphan_priority_key(plain) == _ORPHAN_PRIORITY_DEFAULT


def test_orphan_priority_key_rate_limit_beats_spot_and_provision():
    """A rate-limited spot/provision orphan still sorts into bucket 0."""
    rl_spot = _FakeOrphan(tag="rs", vm_spot=True, latest_status_message="rate limited")
    rl_provision = _FakeOrphan(
        tag="rp",
        status=TaskStatus.PROVISIONING,
        latest_status_message="429 quota",
    )
    assert _orphan_priority_key(rl_spot) == _ORPHAN_PRIORITY_RATE_LIMITED
    assert _orphan_priority_key(rl_provision) == _ORPHAN_PRIORITY_RATE_LIMITED


def test_select_orphans_sorts_by_priority_and_is_stable():
    """_select_orphans_for_cycle sorts ascending by bucket; ties keep input order."""
    rate = _FakeOrphan(tag="rate", latest_status_message="rate limit")
    spot = _FakeOrphan(tag="spot", vm_spot=True)
    provision = _FakeOrphan(tag="provision", status=TaskStatus.PROVISIONING)
    plain_a = _FakeOrphan(tag="plain_a")
    plain_b = _FakeOrphan(tag="plain_b")
    # Deliberately scrambled input; plain_a precedes plain_b for stability check.
    tasks = [plain_a, provision, plain_b, spot, rate]
    selected, deferred = _select_orphans_for_cycle(tasks, cap=10)
    assert deferred == 0
    assert [t.tag for t in selected] == [
        "rate",
        "spot",
        "provision",
        "plain_a",
        "plain_b",
    ]


def test_select_orphans_cap_truncates_keeping_highest_priority():
    """When over cap, the top-N by priority are kept and the rest deferred."""
    rate = _FakeOrphan(tag="rate", latest_status_message="rate limit")
    spot = _FakeOrphan(tag="spot", vm_spot=True)
    provision = _FakeOrphan(tag="provision", status=TaskStatus.PROVISIONING)
    plain = _FakeOrphan(tag="plain")
    tasks = [plain, provision, spot, rate]
    selected, deferred = _select_orphans_for_cycle(tasks, cap=2)
    # Cap=2 keeps the two highest-priority (rate, spot); defers provision+plain.
    assert deferred == 2
    assert [t.tag for t in selected] == ["rate", "spot"]


def test_select_orphans_non_positive_cap_means_no_cap():
    """A non-positive cap fails safe: process everything, defer nothing."""
    tasks = [_FakeOrphan(tag=str(i)) for i in range(5)]
    for cap in (0, -1):
        selected, deferred = _select_orphans_for_cycle(tasks, cap=cap)
        assert deferred == 0
        assert len(selected) == 5


def test_select_orphans_under_cap_returns_all():
    """List shorter than the cap is returned in full, zero deferred."""
    tasks = [_FakeOrphan(tag=str(i)) for i in range(3)]
    selected, deferred = _select_orphans_for_cycle(tasks, cap=60)
    assert deferred == 0
    assert len(selected) == 3


# ── _classify_orphan (single-source-of-truth predicate, FIX a) ──────────────


class _FakeClassifyTask:
    """Minimal stand-in for a Task exposing only the columns _classify_orphan and
    the cutoff helpers read. Lets the predicate be unit-tested without a DB."""

    def __init__(
        self,
        *,
        status,
        started_at=None,
        last_heartbeat=None,
        vm_provisioned_at=None,
        last_tool_use_at=None,
        created_at=None,
        context=None,
        first_payload=None,
        vm_instance_id=None,
        id=None,
    ):
        self.status = status
        self.started_at = started_at
        self.last_heartbeat = last_heartbeat
        self.vm_provisioned_at = vm_provisioned_at
        self.last_tool_use_at = last_tool_use_at
        # created_at defaults to "fresh" so the >10min catch-alls don't fire
        # unless a test sets it explicitly.
        self.created_at = created_at if created_at is not None else _utcnow()
        self.context = context or {}
        self.first_payload = first_payload or {"type": "create"}
        self.vm_instance_id = vm_instance_id
        # Stable id for the runner self-report membership check
        # (_runner_report_excludes_task compares str(task.id)).
        self.id = id if id is not None else _uuid_mod.uuid4()


def test_classify_orphan_provisioning_absolute_ceiling():
    now = _utcnow()
    t = _FakeClassifyTask(
        status=TaskStatus.PROVISIONING, started_at=now - timedelta(minutes=16)
    )
    assert _classify_orphan(t, now, has_open_pr=False) is True


def test_classify_orphan_provisioning_just_inside_ceiling_not_orphan():
    now = _utcnow()
    # 14min: under the 15min absolute ceiling, no heartbeat, no vm_provisioned_at.
    t = _FakeClassifyTask(
        status=TaskStatus.PROVISIONING, started_at=now - timedelta(minutes=14)
    )
    assert _classify_orphan(t, now, has_open_pr=False) is False


def test_classify_orphan_provisioning_silent_preclaim_death():
    now = _utcnow()
    # vm dispatched 7min ago (past small-scope 6min preclaim cutoff), never claimed.
    t = _FakeClassifyTask(
        status=TaskStatus.PROVISIONING,
        vm_provisioned_at=now - timedelta(minutes=7),
        last_heartbeat=None,
    )
    assert _classify_orphan(t, now, has_open_pr=False) is True


def test_classify_orphan_runner_pin_awaiting_pull_not_preclaim_reaped():
    now = _utcnow()
    # Pinned to a self-hosted runner (vm_instance_id='runner:{id}') and not yet
    # pulled (started_at IS NULL). vm_provisioned_at is well past the small-scope
    # 6min preclaim cutoff and there is no heartbeat — the EXACT shape the
    # pre-claim VM-death branch would otherwise reap. But no GCE VM ever existed;
    # the task is waiting for the operator's runner to poll, so it must NOT be
    # classified as a pre-claim orphan (the M2 over-pin churn fix).
    t = _FakeClassifyTask(
        status=TaskStatus.PROVISIONING,
        vm_provisioned_at=now - timedelta(minutes=30),
        last_heartbeat=None,
        started_at=None,
        vm_instance_id="runner:11111111-1111-1111-1111-111111111111",
    )
    assert _classify_orphan(t, now, has_open_pr=False) is False


def test_classify_orphan_runner_pin_after_pull_still_reaped_at_ceiling():
    now = _utcnow()
    # Once the runner pulls a pin it stamps started_at (runner.py next-task). A
    # pulled runner task that then blows the 15min absolute provisioning ceiling
    # is genuinely stuck and must still be reaped — the exclusion only covers the
    # awaiting-pull window (started_at IS NULL), not pulled work.
    t = _FakeClassifyTask(
        status=TaskStatus.PROVISIONING,
        started_at=now - timedelta(minutes=16),
        vm_instance_id="runner:11111111-1111-1111-1111-111111111111",
    )
    assert _classify_orphan(t, now, has_open_pr=False) is True


def test_classify_orphan_non_runner_preclaim_still_reaped_with_null_started_at():
    now = _utcnow()
    # A cloud/GCE pin (non-'runner:' vm id) with the same started_at IS NULL +
    # stale vm_provisioned_at + no heartbeat shape IS a silent pre-claim death and
    # must still be reaped — the runner exclusion is scoped strictly to the
    # 'runner:' prefix and changes nothing for cloud VMs.
    t = _FakeClassifyTask(
        status=TaskStatus.PROVISIONING,
        vm_provisioned_at=now - timedelta(minutes=7),
        last_heartbeat=None,
        started_at=None,
        vm_instance_id="agent-vm-abc123",
    )
    assert _classify_orphan(t, now, has_open_pr=False) is True


def test_classify_orphan_in_progress_heartbeat_stale():
    now = _utcnow()
    t = _FakeClassifyTask(
        status=TaskStatus.IN_PROGRESS,
        last_heartbeat=now - timedelta(minutes=10),  # past 5min small cutoff
        started_at=now - timedelta(minutes=12),
    )
    assert _classify_orphan(t, now, has_open_pr=False) is True


def test_classify_orphan_in_progress_fresh_heartbeat_not_orphan():
    now = _utcnow()
    t = _FakeClassifyTask(
        status=TaskStatus.IN_PROGRESS,
        last_heartbeat=now - timedelta(seconds=30),  # fresh
        started_at=now - timedelta(minutes=2),
        last_tool_use_at=now - timedelta(seconds=30),  # fresh tool use
    )
    assert _classify_orphan(t, now, has_open_pr=False) is False


def test_classify_orphan_open_pr_uses_90s_cutoff():
    now = _utcnow()
    # 2min-stale heartbeat: NOT stale under the 5min default, but IS stale under
    # the 90s open-PR cutoff. The has_open_pr flag must flip the verdict.
    t = _FakeClassifyTask(
        status=TaskStatus.IN_PROGRESS,
        last_heartbeat=now - timedelta(minutes=2),
        started_at=now - timedelta(minutes=5),
    )
    assert _classify_orphan(t, now, has_open_pr=False) is False
    assert _classify_orphan(t, now, has_open_pr=True) is True


def test_classify_orphan_stalled_but_heartbeating_no_tool_use_all_providers():
    now = _utcnow()
    # Fresh heartbeat, zero tool calls ever, started 10min ago (past 8min small
    # progress cutoff). All providers now emit recordToolUse on progress, so
    # last_tool_use_at=None means genuinely zero-progress for all → reap.
    for provider in ("anthropic", "openai", "gemini", "zai", "deepseek", None):
        t = _FakeClassifyTask(
            status=TaskStatus.IN_PROGRESS,
            last_heartbeat=now - timedelta(seconds=30),
            last_tool_use_at=None,
            started_at=now - timedelta(minutes=10),
            context={"llm_provider": provider} if provider else {},
        )
        assert _classify_orphan(t, now, has_open_pr=False) is True, (
            f"Expected zero-progress reap for provider={provider!r}"
        )


def test_classify_orphan_cross_attempt_stale_tool_guard():
    now = _utcnow()
    # Fresh heartbeat, last_tool_use_at is 11min stale (carried across a retry),
    # but the CURRENT attempt only started 1min ago — must NOT be reaped (the
    # started_at clause guards the 8c8cf973 cross-attempt false-positive).
    t = _FakeClassifyTask(
        status=TaskStatus.IN_PROGRESS,
        last_heartbeat=now - timedelta(seconds=30),
        last_tool_use_at=now - timedelta(minutes=11),
        started_at=now - timedelta(minutes=1),
    )
    assert _classify_orphan(t, now, has_open_pr=False) is False


def test_classify_orphan_degenerate_in_progress_catch_all():
    now = _utcnow()
    # No heartbeat, no started_at, created >10min ago → degenerate catch-all.
    t = _FakeClassifyTask(
        status=TaskStatus.IN_PROGRESS,
        last_heartbeat=None,
        started_at=None,
        created_at=now - timedelta(minutes=11),
    )
    assert _classify_orphan(t, now, has_open_pr=False) is True


def test_classify_orphan_non_resident_strategic_review_still_reaped():
    """A strategic_review that blows the 1500s thinking-heavy progress budget
    (fresh heartbeat, zero tool calls) is reaped as a stalled orphan."""
    now = _utcnow()
    t = _FakeClassifyTask(
        status=TaskStatus.IN_PROGRESS,
        last_heartbeat=now - timedelta(seconds=30),
        last_tool_use_at=None,
        started_at=now - timedelta(minutes=30),
        first_payload={"type": "strategic_review"},
        context={"llm_provider": "anthropic"},
    )
    assert _classify_orphan(t, now, has_open_pr=False) is True


def test_classify_orphan_resident_session_exempt_from_progress_staleness():
    """A warm-idle resident session with a fresh heartbeat is NOT reaped even when
    last_tool_use_at is stale well past the 1500s thinking-heavy budget.

    The carve-out in _classify_orphan delegates to _classify_resident_session for
    any IN_PROGRESS task with context["_resident_session"]==True.  Being idle
    between operator turns is normal for a resident; progress-staleness must not
    trigger a false reap."""
    now = _utcnow()
    t = _FakeClassifyTask(
        status=TaskStatus.IN_PROGRESS,
        last_heartbeat=now - timedelta(seconds=30),
        last_tool_use_at=now - timedelta(minutes=30),  # far past 1500s budget
        started_at=now - timedelta(hours=2),  # long-running resident session
        context={"_resident_session": True},
        first_payload={"type": "strategic_review"},
    )
    # Fresh heartbeat → resident session is live, must NOT be reaped.
    assert _classify_orphan(t, now, has_open_pr=False) is False


def test_classify_resident_session_warm_idle_not_reaped():
    """Direct positive test: _classify_resident_session is exempt from
    progress-staleness reaping.

    A warm-idle resident session has a fresh heartbeat but a stale
    last_tool_use_at (no tool calls for much longer than the 1500s
    thinking-heavy budget).  _classify_resident_session must return False
    (NOT reaped) because being idle between operator turns is normal for a
    Workday session.  The function must never consult last_tool_use_at —
    only last_heartbeat vs the resident heartbeat window matters."""
    now = _utcnow()
    t = _FakeClassifyTask(
        status=TaskStatus.IN_PROGRESS,
        last_heartbeat=now - timedelta(seconds=30),   # fresh
        last_tool_use_at=now - timedelta(minutes=60), # far past 1500s budget
        started_at=now - timedelta(hours=3),
        context={"_resident_session": True},
        first_payload={"type": "strategic_review"},
    )
    # Warm-idle: fresh heartbeat overrides stale tool-use → must NOT be reaped.
    assert _classify_resident_session(t, now, None) is False


def test_classify_orphan_step1_and_step2_predicate_agree():
    """FIX a regression guard: the helper IS the predicate both Step 1 and Step 2
    call, so by construction they cannot drift. This asserts the helper returns a
    plain bool across a representative matrix (the property the dedup relies on)."""
    now = _utcnow()
    cases = [
        _FakeClassifyTask(
            status=TaskStatus.PROVISIONING, started_at=now - timedelta(minutes=20)
        ),
        _FakeClassifyTask(
            status=TaskStatus.PROVISIONING, started_at=now - timedelta(minutes=3)
        ),
        _FakeClassifyTask(
            status=TaskStatus.IN_PROGRESS,
            last_heartbeat=now - timedelta(minutes=9),
            started_at=now - timedelta(minutes=12),
        ),
        _FakeClassifyTask(
            status=TaskStatus.IN_PROGRESS,
            last_heartbeat=now - timedelta(seconds=10),
            last_tool_use_at=now - timedelta(seconds=10),
            started_at=now - timedelta(minutes=1),
        ),
    ]
    for t in cases:
        v1 = _classify_orphan(t, now, has_open_pr=False)
        v2 = _classify_orphan(t, now, has_open_pr=False)
        assert isinstance(v1, bool)
        assert v1 == v2  # deterministic / pure


# ── vm_contact liveness override (heartbeat false-reap fix, option B) ────────


def test_classify_orphan_fresh_vm_contact_suppresses_stale_heartbeat_reap():
    """(a) An otherwise-stale IN_PROGRESS task is NOT reaped when the orchestrator
    heard from its VM within the same heartbeat-stale cutoff — the agent is alive
    and its last_heartbeat write was lock-starved by the rollup's task-row lock."""
    now = _utcnow()
    t = _FakeClassifyTask(
        status=TaskStatus.IN_PROGRESS,
        last_heartbeat=now - timedelta(minutes=10),  # past 5min small cutoff
        started_at=now - timedelta(minutes=12),
        vm_instance_id="vm-alive-1",
    )
    # Without the lookup it reaps (back-compat sanity).
    assert _classify_orphan(t, now, has_open_pr=False) is True
    # Fresh contact (30s ago, well within the 5min small cutoff) suppresses it.
    lookup = {"vm-alive-1": now - timedelta(seconds=30)}
    assert (
        _classify_orphan(t, now, has_open_pr=False, vm_contact_lookup=lookup) is False
    )


def test_classify_orphan_stale_vm_contact_still_reaps_genuine_death():
    """(b) A stale heartbeat AND a stale/absent vm_contact still reap — genuine
    death (crashed/preempted VM that sends no requests) is preserved. Gated on the
    SAME cutoff so an old contact row never suppresses a real reap."""
    now = _utcnow()
    t = _FakeClassifyTask(
        status=TaskStatus.IN_PROGRESS,
        last_heartbeat=now - timedelta(minutes=10),
        started_at=now - timedelta(minutes=12),
        vm_instance_id="vm-dead-1",
    )
    # Contact is 9min old — past the 5min small heartbeat-stale cutoff → no
    # suppression, the dead VM is still reaped exactly as today.
    stale_lookup = {"vm-dead-1": now - timedelta(minutes=9)}
    assert (
        _classify_orphan(t, now, has_open_pr=False, vm_contact_lookup=stale_lookup)
        is True
    )
    # A contact for a DIFFERENT vm (not this task's) must also not suppress.
    other_lookup = {"vm-someone-else": now - timedelta(seconds=10)}
    assert (
        _classify_orphan(t, now, has_open_pr=False, vm_contact_lookup=other_lookup)
        is True
    )


def test_classify_orphan_no_vm_contact_row_is_backcompat():
    """(c) No vm_contact lookup (or an empty one) → unchanged behaviour: the stale
    heartbeat reaps exactly as it did before the fix."""
    now = _utcnow()
    t = _FakeClassifyTask(
        status=TaskStatus.IN_PROGRESS,
        last_heartbeat=now - timedelta(minutes=10),
        started_at=now - timedelta(minutes=12),
        vm_instance_id="vm-x",
    )
    # None lookup (default), empty lookup, and lookup without this vm all reap.
    assert _classify_orphan(t, now, has_open_pr=False) is True
    assert _classify_orphan(t, now, has_open_pr=False, vm_contact_lookup={}) is True
    assert (
        _classify_orphan(
            t, now, has_open_pr=False, vm_contact_lookup={"unrelated": now}
        )
        is True
    )
    # A fresh contact override does NOT fire when the task has no vm_instance_id
    # (defensive: nothing to key the liveness signal on).
    t_no_vm = _FakeClassifyTask(
        status=TaskStatus.IN_PROGRESS,
        last_heartbeat=now - timedelta(minutes=10),
        started_at=now - timedelta(minutes=12),
        vm_instance_id=None,
    )
    assert (
        _classify_orphan(t_no_vm, now, has_open_pr=False, vm_contact_lookup={None: now})
        is True
    )


def test_classify_orphan_fresh_contact_does_not_suppress_progress_stall():
    """A fresh vm_contact must NOT suppress the PROGRESS-stale reap (stalled-but-
    heartbeating): there the heartbeat is fresh and the reap is on tool-use
    staleness, not heartbeat staleness. The override only covers the
    last_heartbeat-staleness false-reap, never a genuine hung-agent stall."""
    now = _utcnow()
    t = _FakeClassifyTask(
        status=TaskStatus.IN_PROGRESS,
        last_heartbeat=now - timedelta(seconds=30),  # FRESH heartbeat
        last_tool_use_at=None,
        started_at=now - timedelta(minutes=10),  # past progress cutoff
        context={"llm_provider": "anthropic"},
        vm_instance_id="vm-stalled",
    )
    lookup = {"vm-stalled": now - timedelta(seconds=5)}  # very fresh contact
    # Still reaped — fresh contact is expected (it IS heartbeating); the stall is
    # genuine (no tool use past the progress cutoff).
    assert _classify_orphan(t, now, has_open_pr=False, vm_contact_lookup=lookup) is True


def test_classify_orphan_fresh_contact_suppresses_provisioning_preclaim_death():
    """The override also covers the PROVISIONING silent-pre-claim-death branch: a
    VM that is demonstrably contacting the orchestrator is not a silent death."""
    now = _utcnow()
    t = _FakeClassifyTask(
        status=TaskStatus.PROVISIONING,
        vm_provisioned_at=now - timedelta(minutes=7),  # past 6min preclaim cutoff
        last_heartbeat=None,
        vm_instance_id="vm-provisioning",
    )
    assert _classify_orphan(t, now, has_open_pr=False) is True
    lookup = {"vm-provisioning": now - timedelta(seconds=20)}
    assert (
        _classify_orphan(t, now, has_open_pr=False, vm_contact_lookup=lookup) is False
    )


# ── Runner self-report defeats shared-VmContact zombie masking ───────────────
# All tasks on one fleet runner share vm_instance_id='runner:<id>', so a live
# sibling agent keeps the shared vmcontact row fresh and a per-task agent death
# is suppressed forever (2026-07-12 incident: FATAL'd tasks sat IN_PROGRESS for
# hours). A FRESH runner self-report that omits the task defeats the override.


def _zombie_on_runner(now, *, started_minutes_ago=30):
    """The incident shape: stale heartbeat on a runner-hosted task whose shared
    contact row is kept fresh by a live sibling agent."""
    return _FakeClassifyTask(
        status=TaskStatus.IN_PROGRESS,
        last_heartbeat=now - timedelta(minutes=10),  # past 5min small cutoff
        started_at=now - timedelta(minutes=started_minutes_ago),
        vm_instance_id="runner:aaaa-bbbb",
    )


def test_classify_orphan_runner_report_excluding_task_defeats_contact_mask():
    """A fresh runner self-report that does NOT list the task proves its agent
    is gone: the reap proceeds even though a sibling keeps the shared
    vmcontact row fresh (the exact zombie shape)."""
    now = _utcnow()
    t = _zombie_on_runner(now)
    contact = {"runner:aaaa-bbbb": now - timedelta(seconds=30)}  # sibling alive
    # Without the runner report the shared contact masks the death (the bug).
    assert (
        _classify_orphan(t, now, has_open_pr=False, vm_contact_lookup=contact)
        is False
    )
    # Fresh report omitting the task → reaped despite the fresh shared contact.
    report = {
        "runner:aaaa-bbbb": (now - timedelta(seconds=30), frozenset({"other-task"}))
    }
    assert (
        _classify_orphan(
            t,
            now,
            has_open_pr=False,
            vm_contact_lookup=contact,
            runner_report_lookup=report,
        )
        is True
    )


def test_classify_orphan_runner_report_including_task_still_suppresses():
    """A fresh report that DOES list the task keeps the suppression: the agent
    is alive and its heartbeat write was merely lock-starved."""
    now = _utcnow()
    t = _zombie_on_runner(now)
    contact = {"runner:aaaa-bbbb": now - timedelta(seconds=30)}
    report = {
        "runner:aaaa-bbbb": (
            now - timedelta(seconds=30),
            frozenset({str(t.id), "other-task"}),
        )
    }
    assert (
        _classify_orphan(
            t,
            now,
            has_open_pr=False,
            vm_contact_lookup=contact,
            runner_report_lookup=report,
        )
        is False
    )


def test_classify_orphan_stale_runner_report_is_not_evidence():
    """A report older than the heartbeat-stale cutoff (runner itself gone
    quiet) must not be trusted to exclude — previous suppress behaviour holds."""
    now = _utcnow()
    t = _zombie_on_runner(now)
    contact = {"runner:aaaa-bbbb": now - timedelta(seconds=30)}
    report = {
        "runner:aaaa-bbbb": (now - timedelta(minutes=9), frozenset({"other-task"}))
    }
    assert (
        _classify_orphan(
            t,
            now,
            has_open_pr=False,
            vm_contact_lookup=contact,
            runner_report_lookup=report,
        )
        is False
    )


def test_classify_orphan_runner_report_within_claim_grace_not_trusted():
    """A report inside the post-claim grace window may simply predate the
    runner learning about the claim — never distrust it into a reap-churn."""
    now = _utcnow()
    t = _zombie_on_runner(now, started_minutes_ago=2)  # claimed 2min ago
    contact = {"runner:aaaa-bbbb": now - timedelta(seconds=30)}
    report = {
        "runner:aaaa-bbbb": (now - timedelta(seconds=30), frozenset({"other-task"}))
    }
    # started_at + 180s grace is still in the future relative to the report.
    assert (
        _classify_orphan(
            t,
            now,
            has_open_pr=False,
            vm_contact_lookup=contact,
            runner_report_lookup=report,
        )
        is False
    )


def test_classify_orphan_runner_report_absent_is_backcompat():
    """No lookup / no row for this runner (older runner version without the
    active_work_reported_at stamp) → previous suppress behaviour holds."""
    now = _utcnow()
    t = _zombie_on_runner(now)
    contact = {"runner:aaaa-bbbb": now - timedelta(seconds=30)}
    for report in (None, {}, {"runner:someone-else": (now, frozenset())}):
        assert (
            _classify_orphan(
                t,
                now,
                has_open_pr=False,
                vm_contact_lookup=contact,
                runner_report_lookup=report,
            )
            is False
        )


def test_classify_orphan_runner_report_ignored_for_non_runner_vm():
    """GCE-hosted tasks (non-'runner:' vm ids) are never affected by runner
    self-reports — the contact override applies exactly as before."""
    now = _utcnow()
    t = _FakeClassifyTask(
        status=TaskStatus.IN_PROGRESS,
        last_heartbeat=now - timedelta(minutes=10),
        started_at=now - timedelta(minutes=30),
        vm_instance_id="agent-vm-123",
    )
    contact = {"agent-vm-123": now - timedelta(seconds=30)}
    report = {"agent-vm-123": (now - timedelta(seconds=30), frozenset())}
    assert (
        _classify_orphan(
            t,
            now,
            has_open_pr=False,
            vm_contact_lookup=contact,
            runner_report_lookup=report,
        )
        is False
    )


def test_classify_orphan_runner_report_preclaim_death_on_pulled_pin():
    """The exclusion also unmasks a claimed-then-died-pre-heartbeat task on a
    busy runner (PROVISIONING, started_at set, no heartbeat), while a pin the
    runner has not pulled yet (started_at IS NULL) is never distrusted."""
    now = _utcnow()
    pulled = _FakeClassifyTask(
        status=TaskStatus.PROVISIONING,
        vm_provisioned_at=now - timedelta(minutes=8),
        last_heartbeat=None,
        started_at=now
        - timedelta(seconds=_RUNNER_REPORT_CLAIM_GRACE_SECONDS + 300),
        vm_instance_id="runner:aaaa-bbbb",
    )
    contact = {"runner:aaaa-bbbb": now - timedelta(seconds=30)}
    report = {
        "runner:aaaa-bbbb": (now - timedelta(seconds=30), frozenset({"other-task"}))
    }
    assert (
        _classify_orphan(
            pulled,
            now,
            has_open_pr=False,
            vm_contact_lookup=contact,
            runner_report_lookup=report,
        )
        is True
    )
    awaiting_pull = _FakeClassifyTask(
        status=TaskStatus.PROVISIONING,
        vm_provisioned_at=now - timedelta(minutes=30),
        last_heartbeat=None,
        started_at=None,
        vm_instance_id="runner:aaaa-bbbb",
    )
    assert (
        _classify_orphan(
            awaiting_pull,
            now,
            has_open_pr=False,
            vm_contact_lookup=contact,
            runner_report_lookup=report,
        )
        is False
    )
    # Direct helper check: an unpulled pin is never "excluded".
    assert (
        _runner_report_excludes_task(
            awaiting_pull, now - timedelta(minutes=5), report
        )
        is False
    )


# ── Fix 3b: off-row vmcontact.last_tool_use_at progress override ─────────────


def test_classify_orphan_fresh_offrow_tool_use_suppresses_progress_stall():
    """Fix 3b: a stalled-but-heartbeating task (fresh heartbeat, stale ON-ROW
    last_tool_use_at) is NOT reaped when the OFF-ROW vmcontact.last_tool_use_at is
    within the progress cutoff — the agent IS calling tools; its on-row tool stamp
    was lock-starved together with the heartbeat by the rollup's task-row lock."""
    now = _utcnow()
    t = _FakeClassifyTask(
        status=TaskStatus.IN_PROGRESS,
        last_heartbeat=now - timedelta(seconds=30),  # FRESH heartbeat
        # on-row tool use is 10min stale (past small 8min progress cutoff) and the
        # attempt started 12min ago (so the cross-attempt guard doesn't spare it).
        last_tool_use_at=now - timedelta(minutes=10),
        started_at=now - timedelta(minutes=12),
        vm_instance_id="vm-progressing",
    )
    # Without the off-row tool-use lookup this is a genuine-looking stall → reap.
    assert _classify_orphan(t, now, has_open_pr=False) is True
    # A mere contact lookup must NOT suppress it (regression guard for the
    # contact-vs-tool-use distinction — a hung agent still heartbeats).
    contact_lookup = {"vm-progressing": now - timedelta(seconds=5)}
    assert (
        _classify_orphan(t, now, has_open_pr=False, vm_contact_lookup=contact_lookup)
        is True
    )
    # A FRESH off-row tool use (1min ago, within the 8min small progress cutoff)
    # proves real progress → suppress the reap.
    tool_lookup = {"vm-progressing": now - timedelta(minutes=1)}
    assert (
        _classify_orphan(t, now, has_open_pr=False, vm_tool_use_lookup=tool_lookup)
        is False
    )


def test_classify_orphan_stale_offrow_tool_use_still_reaps_genuine_stall():
    """A genuinely stalled agent (no recent tool use anywhere — on-row stale AND
    off-row stale/absent) is still reaped. Gated on the SAME progress cutoff so an
    old off-row tool stamp can never mask a real stall."""
    now = _utcnow()
    t = _FakeClassifyTask(
        status=TaskStatus.IN_PROGRESS,
        last_heartbeat=now - timedelta(seconds=30),
        last_tool_use_at=now - timedelta(minutes=10),
        started_at=now - timedelta(minutes=12),
        vm_instance_id="vm-stalled",
    )
    # Off-row tool use is 9min old — past the 8min small progress cutoff → no
    # suppression, the genuine stall is reaped exactly as today.
    stale_tool = {"vm-stalled": now - timedelta(minutes=9)}
    assert (
        _classify_orphan(t, now, has_open_pr=False, vm_tool_use_lookup=stale_tool)
        is True
    )
    # An off-row tool use for a DIFFERENT vm must also not suppress.
    other_tool = {"vm-someone-else": now - timedelta(seconds=10)}
    assert (
        _classify_orphan(t, now, has_open_pr=False, vm_tool_use_lookup=other_tool)
        is True
    )
    # No lookup / empty lookup → unchanged (reap).
    assert _classify_orphan(t, now, has_open_pr=False, vm_tool_use_lookup={}) is True


def test_classify_orphan_fresh_offrow_tool_use_suppresses_zero_toolcall_stall():
    """Fix 3b: on-row last_tool_use_at is None and started_at is past the
    progress cutoff, but a fresh OFF-ROW tool use means the agent DID record a
    tool call the lock-starved heartbeat failed to land on the task row — it is
    progressing, don't reap. Applies to all providers."""
    now = _utcnow()
    t = _FakeClassifyTask(
        status=TaskStatus.IN_PROGRESS,
        last_heartbeat=now - timedelta(seconds=30),
        last_tool_use_at=None,  # on-row: zero tool calls ever
        started_at=now - timedelta(minutes=10),  # past 8min small progress cutoff
        context={"llm_provider": "gemini"},
        vm_instance_id="vm-zero-tool",
    )
    # Without the off-row signal → reap (existing zero-tool anthropic stall).
    assert _classify_orphan(t, now, has_open_pr=False) is True
    # Fresh off-row tool use → suppress.
    tool_lookup = {"vm-zero-tool": now - timedelta(minutes=1)}
    assert (
        _classify_orphan(t, now, has_open_pr=False, vm_tool_use_lookup=tool_lookup)
        is False
    )
    # Stale off-row tool use → still reaped (genuine zero-progress stall).
    stale_lookup = {"vm-zero-tool": now - timedelta(minutes=9)}
    assert (
        _classify_orphan(t, now, has_open_pr=False, vm_tool_use_lookup=stale_lookup)
        is True
    )


# ── Task-scoped progress/contact on MULTIPLEXED runners ─────────────────────
# vmcontact is primary-keyed on vm_instance_id ("one row per VM"), but a fleet
# runner multiplexes MANY tasks onto one vm id (runner:<id>) — so every task on
# that runner shares ONE contact/progress row, advanced by ANY agent's heartbeat
# (func.greatest). A hung-but-heartbeating agent (wedged LLM call / spin loop)
# next to ANY actively-working sibling therefore read as "progressing" forever
# and was NEVER stall-reaped. The runner self-report (#14413) cannot rescue this
# by construction: a hung-but-ALIVE process is still in active_task_ids. With no
# absolute IN_PROGRESS ceiling anywhere, nothing else ever fired → permanent
# zombie holding a runner slot, an account slot, and pinning its parent lineage.
#
# Fix: for runner: vm ids the overrides read the TASK-scoped taskcontact row.
# GCE (1 task per VM) keeps reading vmcontact — its genuine lock-starvation
# false-reap protection must survive untouched.

_RUNNER_VM = "runner:cafe-f00d"


def _hung_task_on_busy_runner(now, *, last_tool_use_at=None):
    """The P0 shape: fresh heartbeat (process alive, still calling /heartbeat),
    stale ON-ROW last_tool_use_at (it is hung — no tool calls), hosted on a runner
    that ALSO runs a healthy, actively-working sibling."""
    return _FakeClassifyTask(
        status=TaskStatus.IN_PROGRESS,
        last_heartbeat=now - timedelta(seconds=30),  # hung, but still heartbeating
        last_tool_use_at=(
            last_tool_use_at
            if last_tool_use_at is not None
            else now - timedelta(minutes=10)  # past the 8min small progress cutoff
        ),
        started_at=now - timedelta(minutes=12),
        vm_instance_id=_RUNNER_VM,
    )


def test_hung_task_masked_by_working_sibling_is_now_stall_reaped():
    """THE P0. A hung agent on a runner whose SHARED vmcontact progress stamp is
    kept fresh by an actively-working sibling is now reaped: the task-scoped row
    (its own, stale) is authoritative, so the sibling's tool calls no longer count
    as this task's progress."""
    now = _utcnow()
    t = _hung_task_on_busy_runner(now)
    # The sibling is working hard → shared vm-keyed progress stamp is fresh.
    sibling_fed_vm_tool_use = {_RUNNER_VM: now - timedelta(seconds=15)}

    # BEFORE (vm-keyed only): the sibling's freshness masks the stall → NOT reaped.
    assert (
        _classify_orphan(
            t, now, has_open_pr=False, vm_tool_use_lookup=sibling_fed_vm_tool_use
        )
        is False
    ), "regression guard: this is the masking bug being fixed"

    # AFTER: the task's OWN row exists (it heartbeats) but its own tool stamp is
    # stale → genuinely hung → REAPED, despite the fresh sibling-fed shared row.
    task_contact = {str(t.id): now - timedelta(seconds=30)}  # it IS heartbeating
    task_tool_use = {}  # ...but has made NO tool calls (hung)
    assert (
        _classify_orphan(
            t,
            now,
            has_open_pr=False,
            vm_tool_use_lookup=sibling_fed_vm_tool_use,
            task_contact_lookup=task_contact,
            task_tool_use_lookup=task_tool_use,
        )
        is True
    ), (
        "a hung agent whose own task-scoped progress stamp is stale must be "
        "stall-reaped even though a working sibling keeps the SHARED vmcontact "
        "row fresh (the 2026-07-12 permanent-zombie class)"
    )


def test_runner_task_making_its_own_tool_calls_is_still_not_reaped():
    """The other side: a runner task that IS progressing (its OWN task-scoped tool
    stamp is fresh) keeps the lock-starvation suppression. The fix must not turn
    every runner task into a reap candidate."""
    now = _utcnow()
    t = _hung_task_on_busy_runner(now)  # on-row stamp stale (lock-starved write)
    task_contact = {str(t.id): now - timedelta(seconds=20)}
    task_tool_use = {str(t.id): now - timedelta(seconds=45)}  # ITS OWN fresh tools
    assert (
        _classify_orphan(
            t,
            now,
            has_open_pr=False,
            task_contact_lookup=task_contact,
            task_tool_use_lookup=task_tool_use,
        )
        is False
    ), (
        "a runner task with a FRESH task-scoped tool-use stamp is progressing — its "
        "on-row stamp was merely lock-starved; it must not be reaped"
    )


def test_gce_single_task_vm_lock_starvation_override_is_unchanged():
    """MUST NOT REGRESS: for a 1-task-per-VM GCE task the vmcontact row IS the
    task's row, so the vm-keyed progress override stays fully in force. This is the
    genuine lock-starvation false-reap protection (Fix 3b) — a lock-starved but
    actively-PROGRESSING GCE agent must still be spared, with NO task-scoped row
    present (and even when unrelated task-scoped rows are in the lookup)."""
    now = _utcnow()
    t = _FakeClassifyTask(
        status=TaskStatus.IN_PROGRESS,
        last_heartbeat=now - timedelta(seconds=30),
        last_tool_use_at=now - timedelta(minutes=10),  # on-row stamp lock-starved
        started_at=now - timedelta(minutes=12),
        vm_instance_id="vm-gce-single",  # NOT a runner: id
    )
    vm_tool_use = {"vm-gce-single": now - timedelta(seconds=20)}  # off-row: alive
    assert (
        _classify_orphan(
            t,
            now,
            has_open_pr=False,
            vm_tool_use_lookup=vm_tool_use,
            # Task-scoped lookups populated for OTHER tasks must not disturb GCE.
            task_contact_lookup={"some-other-task": now},
            task_tool_use_lookup={"some-other-task": now},
        )
        is False
    ), (
        "GCE 1-task-per-VM tasks must keep the vm-keyed progress override — "
        "deleting it there re-exposes them to lock-starved false stall-reaps"
    )
    # And a GCE task with a genuinely stale off-row stamp is still reaped.
    assert (
        _classify_orphan(
            t,
            now,
            has_open_pr=False,
            vm_tool_use_lookup={"vm-gce-single": now - timedelta(minutes=11)},
        )
        is True
    )


def test_heartbeat_stale_hung_process_still_in_active_task_ids_is_reaped():
    """The second half of the gap #14413 left open: a hung process that STOPS
    heartbeating but stays in the runner's active_task_ids (the thread is alive, so
    _runner_report_excludes_task returns False) was saved forever by the shared
    contact row. Its task-scoped contact is stale → now reaped."""
    now = _utcnow()
    t = _FakeClassifyTask(
        status=TaskStatus.IN_PROGRESS,
        last_heartbeat=now - timedelta(minutes=10),  # past 5min small cutoff
        started_at=now - timedelta(minutes=30),
        vm_instance_id=_RUNNER_VM,
    )
    sibling_fed_contact = {_RUNNER_VM: now - timedelta(seconds=20)}  # sibling alive
    # The runner still lists the task (the hung thread is alive) → no exclusion.
    report = {
        _RUNNER_VM: (now - timedelta(seconds=20), frozenset({str(t.id), "other"}))
    }
    assert (
        _runner_report_excludes_task(t, now - timedelta(minutes=5), report) is False
    ), "precondition: a hung-but-alive process is still in active_task_ids"

    # BEFORE: shared contact + no runner exclusion → suppressed forever.
    assert (
        _classify_orphan(
            t,
            now,
            has_open_pr=False,
            vm_contact_lookup=sibling_fed_contact,
            runner_report_lookup=report,
        )
        is False
    ), "regression guard: the heartbeat-stale half of the shared-row mask"

    # AFTER: its OWN contact row is stale (it stopped heartbeating) → reaped.
    task_contact = {str(t.id): now - timedelta(minutes=10)}
    assert (
        _classify_orphan(
            t,
            now,
            has_open_pr=False,
            vm_contact_lookup=sibling_fed_contact,
            runner_report_lookup=report,
            task_contact_lookup=task_contact,
        )
        is True
    ), (
        "a hung process that stopped heartbeating but is still in active_task_ids "
        "must be reaped — its own task-scoped contact is stale; the sibling-fed "
        "shared row must not shield it"
    )


def test_runner_task_with_fresh_task_scoped_contact_still_suppressed():
    """A runner task whose OWN contact is fresh (alive; last_heartbeat write merely
    lock-starved) keeps the heartbeat-stale suppression."""
    now = _utcnow()
    t = _FakeClassifyTask(
        status=TaskStatus.IN_PROGRESS,
        last_heartbeat=now - timedelta(minutes=10),
        started_at=now - timedelta(minutes=30),
        vm_instance_id=_RUNNER_VM,
    )
    assert (
        _classify_orphan(
            t,
            now,
            has_open_pr=False,
            vm_contact_lookup={_RUNNER_VM: now - timedelta(seconds=20)},
            task_contact_lookup={str(t.id): now - timedelta(seconds=20)},
        )
        is False
    )


def test_absent_task_scoped_row_falls_back_to_shared_row():
    """ROLLOUT SAFETY: absence of a task-scoped row is NEVER evidence of death. A
    runner task that has not yet heartbeated under the new handler (no taskcontact
    row) must degrade to the previous shared-row behaviour, not become reapable."""
    now = _utcnow()
    hung = _hung_task_on_busy_runner(now)
    sibling_fed_vm_tool_use = {_RUNNER_VM: now - timedelta(seconds=15)}
    # No task-scoped row at all (empty lookups) → old behaviour: suppressed.
    assert (
        _classify_orphan(
            hung,
            now,
            has_open_pr=False,
            vm_tool_use_lookup=sibling_fed_vm_tool_use,
            task_contact_lookup={},
            task_tool_use_lookup={},
        )
        is False
    ), "no task-scoped row → fall back to today's behaviour (no false-reap)"

    # Rows exist for OTHER tasks on the same runner, but not this one → still
    # falls back for THIS task (presence is checked per-task, not per-runner).
    assert (
        _classify_orphan(
            hung,
            now,
            has_open_pr=False,
            vm_tool_use_lookup=sibling_fed_vm_tool_use,
            task_contact_lookup={"sibling-task": now},
            task_tool_use_lookup={"sibling-task": now},
        )
        is False
    )


def test_task_scoped_row_present_with_null_tool_stamp_does_not_fall_back():
    """The subtle one: a task-scoped row EXISTS but its last_tool_use_at is NULL
    (the agent heartbeats but has never recorded a tool call). This must NOT be
    treated like an absent row — falling back to the sibling-fed shared row there
    would silently restore the whole masking bug. Presence is decided by
    taskcontact.last_contact_at (NOT NULL), never by the nullable tool stamp."""
    now = _utcnow()
    t = _hung_task_on_busy_runner(now)
    sibling_fed_vm_tool_use = {_RUNNER_VM: now - timedelta(seconds=15)}
    assert (
        _classify_orphan(
            t,
            now,
            has_open_pr=False,
            vm_tool_use_lookup=sibling_fed_vm_tool_use,
            task_contact_lookup={str(t.id): now - timedelta(seconds=30)},  # row EXISTS
            task_tool_use_lookup={},  # ...with a NULL tool stamp
        )
        is True
    ), (
        "row-exists-but-tool-stamp-NULL must reap, not fall back to the shared row "
        "(falling back would restore the sibling mask)"
    )


def test_zero_tool_stall_branch_is_also_task_scoped():
    """The zero-tool-use stall branch (fresh heartbeat, NO tool use ever) must
    consult the task-scoped row too — otherwise a sibling's tool calls still mask a
    never-progressed hung agent on a shared runner."""
    now = _utcnow()
    t = _FakeClassifyTask(
        status=TaskStatus.IN_PROGRESS,
        last_heartbeat=now - timedelta(seconds=30),
        last_tool_use_at=None,  # never called a tool
        started_at=now - timedelta(minutes=12),
        vm_instance_id=_RUNNER_VM,
        context={"llm_provider": "anthropic"},
    )
    sibling_fed_vm_tool_use = {_RUNNER_VM: now - timedelta(seconds=15)}
    # BEFORE: sibling's tool calls suppress the zero-tool stall.
    assert (
        _classify_orphan(
            t, now, has_open_pr=False, vm_tool_use_lookup=sibling_fed_vm_tool_use
        )
        is False
    )
    # AFTER: its own row has no tool use → reaped.
    assert (
        _classify_orphan(
            t,
            now,
            has_open_pr=False,
            vm_tool_use_lookup=sibling_fed_vm_tool_use,
            task_contact_lookup={str(t.id): now - timedelta(seconds=30)},
            task_tool_use_lookup={},
        )
        is True
    )


# ── Fix 3a: _relock_orphan_task vm_liveness override is reason-gated ─────────


@pytest.mark.asyncio
async def test_relock_orphan_vm_liveness_cutoff_suppresses_heartbeat_stale_reap(
    reaper_db,
):
    """A heartbeat-stale orphan with a fresh vmcontact landing during the unlocked
    I/O window is spared: passing vm_liveness_cutoff causes _relock_orphan_task to
    consult vmcontact.last_contact_at and abort the reap. (Heartbeat-stale path.)"""
    from app.models import VmContact

    project = await _make_project(reaper_db)
    now = _utcnow()
    vm_id = "vm-relock-alive"
    t = Task(
        project_id=project.id,
        payloads=[{"type": "create"}],
        vm_instance_id=vm_id,
        status=TaskStatus.IN_PROGRESS,
        started_at=now - timedelta(minutes=12),
        last_heartbeat=now - timedelta(minutes=10),
    )
    reaper_db.add(t)
    reaper_db.add(
        VmContact(vm_instance_id=vm_id, last_contact_at=now - timedelta(seconds=20))
    )
    await reaper_db.commit()
    await reaper_db.refresh(t)

    # cutoff = 5min ago (small default). Fresh contact (20s) >= cutoff → suppress.
    cutoff = now - timedelta(minutes=5)
    relocked = await _relock_orphan_task(
        reaper_db,
        t.id,
        t,
        TaskStatus.IN_PROGRESS,
        t.last_heartbeat,
        vm_liveness_cutoff=cutoff,
    )
    assert relocked is None, "fresh vmcontact within cutoff must abort the reap"


@pytest.mark.asyncio
async def test_relock_orphan_no_cutoff_does_not_consult_vm_contact(reaper_db):
    """Fix 3a: for a PROGRESS-stall reap (agent_stalled) the caller passes
    vm_liveness_cutoff=None, so _relock_orphan_task must NOT consult vmcontact —
    a hung-but-heartbeating agent always has a fresh contact, and consulting it
    would defeat genuine stall detection. With None the relock proceeds (returns
    the locked task) so the stall reap goes through."""
    from app.models import VmContact

    project = await _make_project(reaper_db)
    now = _utcnow()
    vm_id = "vm-relock-stalled"
    t = Task(
        project_id=project.id,
        payloads=[{"type": "fix", "scope": "small"}],
        vm_instance_id=vm_id,
        status=TaskStatus.IN_PROGRESS,
        started_at=now - timedelta(minutes=12),
        # Fresh heartbeat (a stalled agent is still heartbeating).
        last_heartbeat=now - timedelta(seconds=30),
    )
    reaper_db.add(t)
    # A very fresh contact exists — but with cutoff=None it must be IGNORED.
    reaper_db.add(
        VmContact(vm_instance_id=vm_id, last_contact_at=now - timedelta(seconds=5))
    )
    await reaper_db.commit()
    await reaper_db.refresh(t)

    relocked = await _relock_orphan_task(
        reaper_db,
        t.id,
        t,
        TaskStatus.IN_PROGRESS,
        t.last_heartbeat,
        vm_liveness_cutoff=None,
    )
    assert relocked is not None, (
        "agent_stalled reap (vm_liveness_cutoff=None) must NOT be suppressed by a "
        "fresh vmcontact — a hung agent still heartbeats; genuine stall detection "
        "must be preserved (Fix 3a)"
    )
    assert relocked.id == t.id


# ── Z1: the relock stage un-reaped runner zombies every cycle ───────────────
# _classify_orphan learned to see through the shared-VmContact mask (#14413),
# but the RELOCK still re-queried vmcontact.last_contact_at on the shared
# 'runner:<id>' row and aborted the reap when a sibling kept it fresh — so a
# correctly-classified zombie was silently un-reaped, forever. The relock now
# consults the same runner self-report.


async def _runner_zombie_task(reaper_db, *, vm_id, now, started_minutes_ago=30):
    """A heartbeat-stale task on a fleet runner whose SHARED vmcontact row is
    kept fresh by a live sibling agent (the zombie shape)."""
    from app.models import VmContact

    project = await _make_project(reaper_db)
    t = Task(
        project_id=project.id,
        payloads=[{"type": "create"}],
        vm_instance_id=vm_id,
        status=TaskStatus.IN_PROGRESS,
        started_at=now - timedelta(minutes=started_minutes_ago),
        last_heartbeat=now - timedelta(minutes=10),  # past the 5min small cutoff
    )
    reaper_db.add(t)
    # Sibling agent on the SAME runner keeps the one-row-per-VM contact fresh.
    reaper_db.add(
        VmContact(vm_instance_id=vm_id, last_contact_at=now - timedelta(seconds=20))
    )
    await reaper_db.commit()
    await reaper_db.refresh(t)
    return t


@pytest.mark.asyncio
async def test_relock_orphan_runner_report_excluding_task_proceeds_with_reap(
    reaper_db,
):
    """Z1: with a fresh runner self-report that OMITS the task, the relock must NOT
    abort on the shared vmcontact row — that contact is a sibling's liveness. The
    reap proceeds (relock returns the locked task)."""
    now = _utcnow()
    vm_id = "runner:11111111-1111-1111-1111-111111111111"
    t = await _runner_zombie_task(reaper_db, vm_id=vm_id, now=now)
    cutoff = now - timedelta(minutes=5)

    # Baseline (no report): the shared contact un-reaps it — the bug.
    assert (
        await _relock_orphan_task(
            reaper_db,
            t.id,
            t,
            TaskStatus.IN_PROGRESS,
            t.last_heartbeat,
            vm_liveness_cutoff=cutoff,
        )
        is None
    )

    report = {vm_id: (now - timedelta(seconds=20), frozenset({"sibling-task"}))}
    relocked = await _relock_orphan_task(
        reaper_db,
        t.id,
        t,
        TaskStatus.IN_PROGRESS,
        t.last_heartbeat,
        vm_liveness_cutoff=cutoff,
        runner_report_lookup=report,
    )
    assert relocked is not None, (
        "a fresh runner report that omits the task proves its agent is gone — the "
        "shared-vmcontact abort must not un-reap it"
    )
    assert relocked.id == t.id


@pytest.mark.asyncio
async def test_relock_orphan_runner_report_including_task_still_aborts(reaper_db):
    """The abort MUST still fire for a task the runner DOES list — that agent is
    alive and its last_heartbeat write was merely lock-starved."""
    now = _utcnow()
    vm_id = "runner:22222222-2222-2222-2222-222222222222"
    t = await _runner_zombie_task(reaper_db, vm_id=vm_id, now=now)
    report = {
        vm_id: (now - timedelta(seconds=20), frozenset({str(t.id), "sibling-task"}))
    }
    assert (
        await _relock_orphan_task(
            reaper_db,
            t.id,
            t,
            TaskStatus.IN_PROGRESS,
            t.last_heartbeat,
            vm_liveness_cutoff=now - timedelta(minutes=5),
            runner_report_lookup=report,
        )
        is None
    ), "a task the runner still reports as active must keep the contact suppression"


@pytest.mark.asyncio
async def test_relock_orphan_stale_runner_report_still_aborts(reaper_db):
    """Fail-safe: a STALE report (the runner itself went quiet) is not evidence of
    a per-task death — the vm_contact abort applies exactly as before."""
    now = _utcnow()
    vm_id = "runner:33333333-3333-3333-3333-333333333333"
    t = await _runner_zombie_task(reaper_db, vm_id=vm_id, now=now)
    # reported_at is older than the liveness cutoff → not trusted.
    stale_report = {vm_id: (now - timedelta(minutes=30), frozenset({"sibling-task"}))}
    assert (
        await _relock_orphan_task(
            reaper_db,
            t.id,
            t,
            TaskStatus.IN_PROGRESS,
            t.last_heartbeat,
            vm_liveness_cutoff=now - timedelta(minutes=5),
            runner_report_lookup=stale_report,
        )
        is None
    )
    # Absent report (older runner build, no active_work_reported_at stamp) → same.
    assert (
        await _relock_orphan_task(
            reaper_db,
            t.id,
            t,
            TaskStatus.IN_PROGRESS,
            t.last_heartbeat,
            vm_liveness_cutoff=now - timedelta(minutes=5),
            runner_report_lookup={},
        )
        is None
    )


@pytest.mark.asyncio
async def test_relock_orphan_runner_report_within_claim_grace_still_aborts(reaper_db):
    """Fail-safe: a report that predates this task's claim + grace may simply not
    know about the claim yet — it must not authorize the reap."""
    now = _utcnow()
    vm_id = "runner:44444444-4444-4444-4444-444444444444"
    # started_at only 30s ago → any report is inside the 180s claim grace.
    t = await _runner_zombie_task(
        reaper_db, vm_id=vm_id, now=now, started_minutes_ago=0
    )
    t.started_at = now - timedelta(seconds=30)
    reaper_db.add(t)
    await reaper_db.commit()
    await reaper_db.refresh(t)
    assert _RUNNER_REPORT_CLAIM_GRACE_SECONDS > 30
    report = {vm_id: (now - timedelta(seconds=10), frozenset({"sibling-task"}))}
    assert (
        await _relock_orphan_task(
            reaper_db,
            t.id,
            t,
            TaskStatus.IN_PROGRESS,
            t.last_heartbeat,
            vm_liveness_cutoff=now - timedelta(minutes=5),
            runner_report_lookup=report,
        )
        is None
    )


@pytest.mark.asyncio
async def test_relock_orphan_runner_report_ignored_for_non_runner_vm(reaper_db):
    """A cloud/GCE vm id owns its vmcontact row alone — there is no mask to defeat,
    so the report is irrelevant and the abort still fires."""
    now = _utcnow()
    vm_id = "vm-cloud-abc"  # NOT a 'runner:' id
    t = await _runner_zombie_task(reaper_db, vm_id=vm_id, now=now)
    report = {vm_id: (now - timedelta(seconds=20), frozenset({"sibling-task"}))}
    assert (
        await _relock_orphan_task(
            reaper_db,
            t.id,
            t,
            TaskStatus.IN_PROGRESS,
            t.last_heartbeat,
            vm_liveness_cutoff=now - timedelta(minutes=5),
            runner_report_lookup=report,
        )
        is None
    ), "the runner-report override must be scoped to 'runner:' vm ids"


async def _runner_zombie_e2e_fixture(reaper_db, *, now, reported_task_ids):
    """The full runner-zombie shape as reaper_service sees it: a registered
    LocalRunner carrying a FRESH self-report, a heartbeat-stale IN_PROGRESS task
    pinned to it, and the ONE shared 'runner:<id>' vmcontact row kept fresh by a
    live sibling agent. ``reported_task_ids`` is a callable taking the task so a
    caller can have the report include or omit it."""
    from app.models import LocalRunner, VmContact

    project = await _make_project(reaper_db)
    runner_id = _uuid_mod.uuid4()
    vm_id = f"runner:{runner_id}"
    t = Task(
        project_id=project.id,
        payloads=[{"type": "create"}],
        vm_instance_id=vm_id,
        status=TaskStatus.IN_PROGRESS,
        attempt_count=0,
        # Started long ago → well past _RUNNER_REPORT_CLAIM_GRACE_SECONDS.
        started_at=now - timedelta(minutes=30),
        # Past the 5min small-scope heartbeat cutoff → orphan candidate.
        last_heartbeat=now - timedelta(minutes=10),
    )
    reaper_db.add(t)
    await reaper_db.commit()
    await reaper_db.refresh(t)

    reaper_db.add(
        LocalRunner(
            id=runner_id,
            project_id=project.id,
            token_hash="0" * 64,
            capabilities={
                "active_task_ids": reported_task_ids(t),
                "active_work_reported_at": (now - timedelta(seconds=20)).isoformat(),
            },
        )
    )
    # The single per-runner contact row — a live sibling agent keeps it fresh.
    reaper_db.add(
        VmContact(vm_instance_id=vm_id, last_contact_at=now - timedelta(seconds=20))
    )
    await reaper_db.commit()
    await reaper_db.refresh(t)
    return t, vm_id


@pytest.mark.asyncio
async def test_reaper_service_wires_runner_report_into_relock_and_reaps_zombie(
    reaper_db, stop_after_one_cycle, reaper_side_effects, mocker
):
    """Z1 END-TO-END — covers the reaper-side WIRING itself.

    The five tests above call ``_relock_orphan_task`` directly with a hand-built
    dict, so they stay green even if the reaper drops the
    ``runner_report_lookup=`` kwarg at the call site — i.e. they cover the helper
    the fix feeds, not the fix. This test drives the real ``reaper_service`` cycle
    over a genuine runner zombie (LocalRunner row + fresh self-report + the shared
    vmcontact row a sibling keeps alive) and asserts BOTH that the relock received
    a non-empty report AND that the task is actually reaped. Remove
    ``runner_report_lookup=`` (or the ``_build_runner_report_lookup`` call feeding
    it) and this FAILS — the shared contact aborts the relock and the task stays
    IN_PROGRESS forever, which is the exact bug Z1 fixes.
    """
    now = _utcnow()
    t, vm_id = await _runner_zombie_e2e_fixture(
        reaper_db,
        now=now,
        # The runner is running a SIBLING, not this task → this agent is gone.
        reported_task_ids=lambda _t: [str(_uuid_mod.uuid4())],
    )

    seen_kwargs: list[dict] = []
    _real_relock = _relock_orphan_task

    async def _spy_relock(*args, **kwargs):
        seen_kwargs.append(dict(kwargs))
        return await _real_relock(*args, **kwargs)

    mocker.patch("app.reaper._relock_orphan_task", new=_spy_relock)

    await _run_reaper_once()
    await reaper_db.refresh(t)

    assert seen_kwargs, "the classified orphan must have reached the relock stage"
    lookup = seen_kwargs[-1].get("runner_report_lookup")
    assert lookup, (
        "reaper_service must BUILD the runner self-report at the relock stage and "
        "PASS it as runner_report_lookup= — without it the shared-vmcontact abort "
        "silently un-reaps the zombie on every cycle (Z1)"
    )
    assert vm_id in lookup, "the report must be keyed by the task's own runner vm id"

    assert t.status == TaskStatus.QUEUED, (
        "the zombie must be reaped END-TO-END: the shared 'runner:<id>' vmcontact "
        "row carries the SIBLING's liveness, and the runner's own fresh report "
        "omits this task — so the relock must not abort on that contact"
    )
    assert t.attempt_count == 1


@pytest.mark.asyncio
async def test_reaper_service_spares_task_the_runner_still_reports_active(
    reaper_db, stop_after_one_cycle, reaper_side_effects
):
    """Negative control for the wiring above: identical shape, except the runner's
    fresh report DOES list the task. Its agent is alive and its last_heartbeat
    write was merely lock-starved (exactly the case the vm_contact override exists
    for), so the full cycle must leave the task IN_PROGRESS. Guards the Z1 fix
    against degrading into "reap every task on a runner"."""
    now = _utcnow()
    t, _vm_id = await _runner_zombie_e2e_fixture(
        reaper_db,
        now=now,
        reported_task_ids=lambda _t: [str(_t.id), str(_uuid_mod.uuid4())],
    )

    await _run_reaper_once()
    await reaper_db.refresh(t)

    assert t.status == TaskStatus.IN_PROGRESS, (
        "a task the runner still self-reports as active must keep the vm_contact "
        "suppression — its agent is alive, its heartbeat write was lock-starved"
    )
    assert t.attempt_count == 0


@pytest.mark.asyncio
async def test_reaper_startup_grace_defers_false_reap_until_runner_reconnect_window(
    reaper_db,
    stop_after_one_cycle,
    reaper_side_effects,
    monkeypatch,
):
    """A worker restart cannot charge its own downtime to an in-flight task."""
    import app.reaper as reaper_module

    now = _utcnow()
    task, _vm_id = await _runner_zombie_e2e_fixture(
        reaper_db,
        now=now,
        reported_task_ids=lambda _task: [str(_uuid_mod.uuid4())],
    )
    _runtime_config["reaper_startup_orphan_grace_seconds"] = 120
    monkeypatch.setattr(reaper_module, "_REAPER_PROCESS_STARTED_AT", now)

    assert _startup_orphan_grace_remaining(now + timedelta(seconds=6)) == 114
    await _run_reaper_once()
    await reaper_db.refresh(task)
    assert task.status == TaskStatus.IN_PROGRESS
    assert task.attempt_count == 0

    # Once the reconnect window has elapsed, the unchanged stale task follows
    # the normal classifier and retry path.
    monkeypatch.setattr(
        reaper_module,
        "_REAPER_PROCESS_STARTED_AT",
        now - timedelta(seconds=121),
    )
    await _run_reaper_once()
    await reaper_db.refresh(task)
    assert task.status == TaskStatus.QUEUED
    assert task.attempt_count == 1


# ── RELOCK stage reads the TASK-scoped contact row on multiplexed runners ────
# Without these, the heartbeat-stale half of the multiplexed-runner fix is INERT
# end-to-end: _classify_orphan correctly classifies the hung task as an orphan and
# then _relock_orphan_task silently un-reaps it, every cycle, because it re-read
# the SHARED vmcontact row that a working sibling keeps fresh.


@pytest.mark.asyncio
async def test_relock_multiplexed_runner_reads_task_scoped_contact_not_shared(
    reaper_db,
):
    """THE Blocker-2 case. A hung agent on a fleet runner, co-resident with a
    working sibling: the SHARED vmcontact row is fresh (the sibling's heartbeats
    advance it), but this task's OWN taskcontact row is stale. The relock must read
    the TASK-scoped row and let the heartbeat-stale reap proceed.

    On the shared-row code this returns None (reap aborted) forever → permanent
    zombie. The runner self-report gate cannot close this: a hung-but-alive process
    is still listed in active_task_ids."""
    from app.models import TaskContact, VmContact

    project = await _make_project(reaper_db)
    now = _utcnow()
    runner_vm = "runner:relock-zombie"
    t = Task(
        project_id=project.id,
        payloads=[{"type": "create"}],
        vm_instance_id=runner_vm,
        status=TaskStatus.IN_PROGRESS,
        started_at=now - timedelta(minutes=40),
        last_heartbeat=now - timedelta(minutes=30),  # hung: stopped heartbeating
    )
    reaper_db.add(t)
    # The SHARED row is FRESH — a live sibling on the same runner keeps it warm.
    reaper_db.add(
        VmContact(
            vm_instance_id=runner_vm, last_contact_at=now - timedelta(seconds=10)
        )
    )
    await reaper_db.commit()
    await reaper_db.refresh(t)
    # This task's OWN row is STALE (its agent died 30min ago).
    reaper_db.add(
        TaskContact(task_id=t.id, last_contact_at=now - timedelta(minutes=30))
    )
    await reaper_db.commit()

    cutoff = now - timedelta(minutes=5)
    relocked = await _relock_orphan_task(
        reaper_db,
        t.id,
        t,
        TaskStatus.IN_PROGRESS,
        t.last_heartbeat,
        vm_liveness_cutoff=cutoff,
    )
    assert relocked is not None, (
        "relock must consult the TASK-scoped taskcontact row for a runner: vm id. "
        "The sibling-fed SHARED vmcontact row is fresh, but THIS task's agent is "
        "dead — reading the shared row here is exactly what made the "
        "heartbeat-stale reap inert (permanent zombie)."
    )
    assert relocked.id == t.id


@pytest.mark.asyncio
async def test_relock_multiplexed_runner_fresh_task_contact_still_suppresses(
    reaper_db,
):
    """The false-reap guard must survive on runners: a task whose OWN taskcontact
    row is fresh is alive-but-lock-starved (its contended last_heartbeat write was
    skipped). The relock must still abort the reap. This is the case that made
    Option B unacceptable — the fix must not reap lock-starved-but-progressing
    runner tasks."""
    from app.models import TaskContact, VmContact

    project = await _make_project(reaper_db)
    now = _utcnow()
    runner_vm = "runner:relock-lockstarved"
    t = Task(
        project_id=project.id,
        payloads=[{"type": "create"}],
        vm_instance_id=runner_vm,
        status=TaskStatus.IN_PROGRESS,
        started_at=now - timedelta(minutes=20),
        last_heartbeat=now - timedelta(minutes=10),  # lock-starved: stale on-row
    )
    reaper_db.add(t)
    reaper_db.add(
        VmContact(
            vm_instance_id=runner_vm, last_contact_at=now - timedelta(seconds=10)
        )
    )
    await reaper_db.commit()
    await reaper_db.refresh(t)
    # THIS task's own row is FRESH — it is alive and heartbeating off-row.
    reaper_db.add(
        TaskContact(task_id=t.id, last_contact_at=now - timedelta(seconds=15))
    )
    await reaper_db.commit()

    cutoff = now - timedelta(minutes=5)
    relocked = await _relock_orphan_task(
        reaper_db,
        t.id,
        t,
        TaskStatus.IN_PROGRESS,
        t.last_heartbeat,
        vm_liveness_cutoff=cutoff,
    )
    assert relocked is None, (
        "a fresh TASK-scoped contact proves THIS agent is alive (lock-starved "
        "heartbeat) — the reap must still be aborted on a multiplexed runner"
    )


@pytest.mark.asyncio
async def test_relock_multiplexed_runner_absent_task_contact_falls_back_to_shared(
    reaper_db,
):
    """Rollout safety at the relock stage: a runner task with NO taskcontact row
    (pre-deploy, or not yet heartbeated under the new handler) must fall back to the
    shared vmcontact row — today's behaviour. An ABSENT row is never evidence of
    death, so such a task cannot be false-reaped by shipping this."""
    from app.models import VmContact

    project = await _make_project(reaper_db)
    now = _utcnow()
    runner_vm = "runner:relock-prerollout"
    t = Task(
        project_id=project.id,
        payloads=[{"type": "create"}],
        vm_instance_id=runner_vm,
        status=TaskStatus.IN_PROGRESS,
        started_at=now - timedelta(minutes=20),
        last_heartbeat=now - timedelta(minutes=10),
    )
    reaper_db.add(t)
    reaper_db.add(
        VmContact(
            vm_instance_id=runner_vm, last_contact_at=now - timedelta(seconds=10)
        )
    )
    await reaper_db.commit()
    await reaper_db.refresh(t)
    # NO TaskContact row is created for this task.

    cutoff = now - timedelta(minutes=5)
    relocked = await _relock_orphan_task(
        reaper_db,
        t.id,
        t,
        TaskStatus.IN_PROGRESS,
        t.last_heartbeat,
        vm_liveness_cutoff=cutoff,
    )
    assert relocked is None, (
        "absent taskcontact row must fall back to the shared vmcontact row "
        "(fresh → abort), preserving today's behaviour for pre-rollout tasks"
    )


def test_relock_orphan_task_re_export_identity():
    """app.reaper._relock_orphan_task must be the same object as
    app.reaper_relock._relock_orphan_task (re-export points at the moved definition)."""
    import app.reaper as _reaper_mod
    import app.reaper_relock as _reaper_relock_mod

    assert _reaper_mod._relock_orphan_task is _reaper_relock_mod._relock_orphan_task


# ── Per-cycle orphan-loop wall-time budget (FIX c) ──────────────────────────


def test_orphan_loop_time_budget_default_under_watchdog():
    """The default wall-time budget must be comfortably below REAPER_HANG_SECONDS
    so one cycle's orphan loop cannot, on its own, consume the watchdog window."""
    import app.reaper as _reaper_mod

    assert _REAPER_ORPHAN_LOOP_TIME_BUDGET_DEFAULT > 0
    assert _REAPER_ORPHAN_LOOP_TIME_BUDGET_DEFAULT < _reaper_mod.REAPER_HANG_SECONDS, (
        "orphan-loop budget must be under the watchdog hang threshold"
    )


@pytest.mark.asyncio
async def test_orphan_loop_time_budget_defers_remaining_orphans(
    reaper_db, stop_after_one_cycle, reaper_side_effects, clean_crash_streaks, mocker
):
    """FIX c: once the orphan loop's wall-time budget is spent, the remaining
    orphans are DEFERRED (left untouched, re-qualify next cycle) rather than
    blowing the cycle past the watchdog. We force monotonic() to jump past the
    budget after the first orphan is processed and assert exactly one orphan is
    reaped this cycle while the rest stay IN_PROGRESS."""
    project = await _make_project(reaper_db)
    # Three identical stale orphans — all qualify; only the first should be
    # processed before the (simulated) budget is exhausted.
    orphans = []
    for i in range(3):
        o = Task(
            project_id=project.id,
            payloads=[{"type": "create"}],
            vm_instance_id=f"vm-budget-{i}",
            status=TaskStatus.IN_PROGRESS,
            attempt_count=0,
            last_heartbeat=_utcnow() - timedelta(minutes=10),
        )
        orphans.append(o)
        reaper_db.add(o)
    await reaper_db.commit()

    # Tighten the budget so the simulated clock trips it after the first orphan.
    mocker.patch.dict(
        _runtime_config, {"reaper_orphan_loop_time_budget_seconds": 5.0}, clear=False
    )

    # Monotonic clock that jumps +1000s on EVERY reading. The orphan loop reads
    # it once at loop start and once per iteration (only when processed>0). With
    # a 5s budget, the diff between loop-start and the first per-iteration check
    # is always >= 1000 >> 5, so exactly the FIRST orphan is processed (its check
    # is short-circuited by the processed>0 guard) and the rest are deferred.
    # Stepping by a huge constant makes the test robust to any incidental
    # monotonic() calls between loop start and the first budget check.
    import app.reaper as _reaper_mod

    clock = {"t": 0.0}

    def fake_monotonic():
        clock["t"] += 1000.0
        return clock["t"]

    mocker.patch.object(_reaper_mod.time, "monotonic", side_effect=fake_monotonic)

    await _run_reaper_once()
    for o in orphans:
        await reaper_db.refresh(o)

    reaped = [o for o in orphans if o.status == TaskStatus.QUEUED]
    deferred = [o for o in orphans if o.status == TaskStatus.IN_PROGRESS]
    assert len(reaped) == 1, (
        "exactly one orphan should be reaped before the wall-time budget trips"
    )
    assert len(deferred) == 2, (
        "the remaining orphans must be DEFERRED (untouched) to next cycle, not "
        "reaped past the budget"
    )
    # Deferred orphans keep their attempt budget intact (no premature burn).
    for o in deferred:
        assert o.attempt_count == 0


@pytest.mark.asyncio
async def test_orphan_loop_no_time_budget_when_disabled_processes_all(
    reaper_db, stop_after_one_cycle, reaper_side_effects, clean_crash_streaks, mocker
):
    """A non-positive budget disables the wall-time gate (fail-safe): every
    selected orphan is processed even though the simulated clock is far past any
    positive budget."""
    project = await _make_project(reaper_db)
    orphans = []
    for i in range(3):
        o = Task(
            project_id=project.id,
            payloads=[{"type": "create"}],
            vm_instance_id=f"vm-nobudget-{i}",
            status=TaskStatus.IN_PROGRESS,
            attempt_count=0,
            last_heartbeat=_utcnow() - timedelta(minutes=10),
        )
        orphans.append(o)
        reaper_db.add(o)
    await reaper_db.commit()

    mocker.patch.dict(
        _runtime_config, {"reaper_orphan_loop_time_budget_seconds": 0}, clear=False
    )

    import app.reaper as _reaper_mod

    # Clock is always far past any budget — but the budget is disabled, so the
    # gate must never trip.
    mocker.patch.object(_reaper_mod.time, "monotonic", return_value=10_000.0)

    await _run_reaper_once()
    for o in orphans:
        await reaper_db.refresh(o)

    assert all(o.status == TaskStatus.QUEUED for o in orphans), (
        "with the budget disabled, all orphans must be reaped in one cycle"
    )


# ══════════════════════════════════════════════════════════════════════════════
# _mark_landed_parent_dependents — completion-side warm-reuse trigger
# ══════════════════════════════════════════════════════════════════════════════


class _FakeParent:
    """Minimal stand-in exposing only the ``status`` attribute the helper reads."""

    def __init__(self, status):
        self.status = status


class _FakeGetSession:
    """Session stub exposing only the async ``get`` the helper uses."""

    def __init__(self, result):
        self._result = result
        self.got = []

    async def get(self, _model, ident):
        self.got.append(ident)
        return self._result


class TestMarkLandedParentDependents:
    """The reaper's two rollup-trigger sites stamp the sticky ``_deps_satisfied``
    marker on a landed parent's just-unblocked dependents — but ONLY when the
    parent actually landed (status COMPLETED), and a failure never aborts the
    reaper cycle."""

    @pytest.mark.asyncio
    async def test_marks_dependents_when_parent_completed(self, mocker):
        from app.reaper import _mark_landed_parent_dependents

        pid = _uuid_mod.uuid4()
        parent = _FakeParent(TaskStatus.COMPLETED)
        spy = mocker.patch(
            "app.task_lifecycle._mark_dependents_warm_claimable",
            new_callable=AsyncMock,
            return_value=None,
        )
        session = _FakeGetSession(parent)

        await _mark_landed_parent_dependents(pid, session)

        # Re-fetched the parent on the shared session, then delegated with it.
        assert session.got == [pid]
        spy.assert_awaited_once()
        assert spy.await_args.args[0] is parent

    @pytest.mark.asyncio
    async def test_skips_when_parent_not_completed(self, mocker):
        """A parent still EVALUATING (rollup did not land) must NOT be stamped —
        the readiness signal is never loosened."""
        from app.reaper import _mark_landed_parent_dependents

        spy = mocker.patch(
            "app.task_lifecycle._mark_dependents_warm_claimable",
            new_callable=AsyncMock,
        )
        await _mark_landed_parent_dependents(
            _uuid_mod.uuid4(), _FakeGetSession(_FakeParent(TaskStatus.EVALUATING))
        )
        spy.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_skips_when_parent_id_none_or_missing(self, mocker):
        from app.reaper import _mark_landed_parent_dependents

        spy = mocker.patch(
            "app.task_lifecycle._mark_dependents_warm_claimable",
            new_callable=AsyncMock,
        )
        # No parent id at all (e.g. a parentless child).
        await _mark_landed_parent_dependents(None, _FakeGetSession(None))
        # Parent row gone between trigger and re-fetch.
        await _mark_landed_parent_dependents(_uuid_mod.uuid4(), _FakeGetSession(None))
        spy.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_swallows_delegate_failure(self, mocker):
        """A failure in the marking pass must never abort the reaper cycle."""
        from app.reaper import _mark_landed_parent_dependents

        mocker.patch(
            "app.task_lifecycle._mark_dependents_warm_claimable",
            new_callable=AsyncMock,
            side_effect=RuntimeError("boom"),
        )
        # Must not raise.
        assert (
            await _mark_landed_parent_dependents(
                _uuid_mod.uuid4(), _FakeGetSession(_FakeParent(TaskStatus.COMPLETED))
            )
            is None
        )


# ── Step 12: BLOCKED-task auto-unblock / escalation sweep ─────────────────────

from app.reaper_sweeps import (  # noqa: E402
    sweep_unblock_blocked_tasks,
    _BLOCKED_ESCALATE_AFTER,
    _BLOCKED_ESCALATED_PARK,
    _BLOCKED_OPTIMISTIC_RETRY_CAP,
    _BLOCKED_PARKED_REEVAL_CAP,
)
async def _blocked_task(db, project, **kwargs):
    defaults = {
        "project_id": project.id,
        "title": "blocked-unit",
        "description": "x",
        "status": TaskStatus.BLOCKED,
        "created_by": "test-user",
        "requires_approval": False,
        "payloads": [{"type": "fix"}],
        "context": {},
        "status_log": [],
        "blocker_kind": "file_conflict",
        "blocked_reason": "Waiting: file conflict on a.py",
        "blocked_at": _utcnow(),
        "recheck_after": _utcnow() - timedelta(minutes=1),  # due
    }
    defaults.update(kwargs)
    t = Task(**defaults)
    db.add(t)
    await db.commit()
    await db.refresh(t)
    return t


def _verified_runner_snapshot(project_id, *, runner_id=None):
    from app.runner_eligibility import RunnerDispatchSnapshot

    return RunnerDispatchSnapshot(
        id=runner_id or _uuid_mod.uuid4(),
        project_id=project_id,
        capabilities={
            "task_types": "all",
            "agent_available": True,
            "max_concurrent": 2,
            "project_verification": {
                "version": 1,
                "ready": True,
                "boundary": {"class": "agent", "backend": "bwrap"},
                "probes": {
                    "ruff": {"ok": True, "exit_code": 0},
                    "docker_postgres": {"ok": True, "exit_code": 0},
                },
            },
        },
        hostname="verified-runner",
    )


def _learned_backend_verification_context(**extra):
    from app.execution_capability_requirements import (
        CAPABILITY_BACKEND_VERIFICATION,
        persist_learned_execution_capabilities,
    )

    context, _ = persist_learned_execution_capabilities(
        extra,
        {CAPABILITY_BACKEND_VERIFICATION},
        observed_at=_utcnow(),
    )
    return context


@pytest.mark.asyncio
async def test_unblock_sweep_requeues_conflict_blocker(reaper_db, mocker):
    """A named file conflict requeues only after the named writer is gone."""
    mocker.patch("app.broadcast.broadcast_to_project", new_callable=AsyncMock)
    project = await _make_project(reaper_db)
    t = await _blocked_task(
        reaper_db,
        project,
        depends_on=[],
        context={
            "blocker": {"source": "agent", "waiting_on": "file conflict"},
            "failure_class": "BLOCKED_PRECONDITION",
            "failure_category": "BLOCKED_PRECONDITION",
        },
    )

    requeued = await sweep_unblock_blocked_tasks(reaper_db, _utcnow())

    assert requeued == 1
    await reaper_db.refresh(t)
    assert t.status == TaskStatus.QUEUED
    assert t.blocker_kind is None
    assert t.blocked_at is None
    assert t.recheck_after is None
    # Recovery must leave NO residual blocker metadata — neither the structured
    # record nor the BLOCKED_PRECONDITION compat tags.
    ctx = t.context or {}
    assert "blocker" not in ctx
    assert "failure_class" not in ctx
    assert "failure_category" not in ctx
    # BLOCKED -> QUEUED is a real execution boundary: the old attempt is immutable
    # and the fresh ordinal advances even when the old row had no token telemetry.
    assert len(t.attempt_history or []) == 1
    assert t.attempt_history[-1]["status"] == "BLOCKED"
    from app.models import get_task_lifetime_attempt_ordinal

    assert get_task_lifetime_attempt_ordinal(t) == 2


@pytest.mark.asyncio
async def test_unblock_sweep_holds_unchanged_named_file_conflict(reaper_db, mocker):
    """Elapsed time cannot requeue while the exact named file is still owned."""

    mocker.patch("app.broadcast.broadcast_to_project", new_callable=AsyncMock)
    project = await _make_project(reaper_db)
    writer = Task(
        project_id=project.id,
        title="active writer",
        description="x",
        status=TaskStatus.IN_PROGRESS,
        created_by="test-user",
        requires_approval=False,
        payloads=[{"type": "fix"}],
        context={"related_files": ["a.py"]},
        status_log=[],
    )
    reaper_db.add(writer)
    await reaper_db.commit()
    blocked = await _blocked_task(
        reaper_db,
        project,
        depends_on=[],
        context={
            "related_files": ["a.py"],
            "_precondition_defer": {
                "key": "file conflict on a.py",
                "count": 3,
                "files": ["a.py"],
            },
        },
    )

    requeued = await sweep_unblock_blocked_tasks(reaper_db, _utcnow())

    assert requeued == 0
    await reaper_db.refresh(blocked)
    assert blocked.status == TaskStatus.BLOCKED
    assert not (blocked.attempt_history or [])


@pytest.mark.asyncio
async def test_unblock_sweep_respects_project_pause_control(reaper_db, mocker):
    """Project pause fences BLOCKED resurrection and attempt burn."""

    mocker.patch("app.broadcast.broadcast_to_project", new_callable=AsyncMock)
    project = await _make_project(reaper_db)
    project.paused = True
    reaper_db.add(project)
    await reaper_db.commit()
    blocked = await _blocked_task(
        reaper_db,
        project,
        depends_on=[],
        context={
            "related_files": ["a.py"],
            "_precondition_defer": {
                "key": "file conflict on a.py",
                "count": 3,
                "files": ["a.py"],
            },
        },
    )

    requeued = await sweep_unblock_blocked_tasks(reaper_db, _utcnow())

    assert requeued == 0
    await reaper_db.refresh(blocked)
    assert blocked.status == TaskStatus.BLOCKED
    assert not (blocked.attempt_history or [])


@pytest.mark.asyncio
async def test_unblock_sweep_revalidates_repo_gate_only_after_head_advances(
    reaper_db, mocker
):
    """A green integration HEAD requeues an inherited-gate task on a fresh base."""
    mocker.patch("app.broadcast.broadcast_to_project", new_callable=AsyncMock)
    mocker.patch(
        "app.gate_revalidation.resolve_gate_state",
        new_callable=AsyncMock,
        return_value={
            "head_sha": "b" * 40,
            "green_at_head": True,
            "checked_at_utc": _utcnow().isoformat(),
        },
    )
    project = await _make_project(reaper_db)
    task = await _blocked_task(
        reaper_db,
        project,
        blocker_kind="precondition",
        blocked_reason=(
            "BLOCKER: ./pr open failed on pre-existing repo-wide standalone "
            "smoke failures in untouched tests"
        ),
        context={
            "generated_head_sha": "a" * 40,
            "blocker": {
                "source": "agent",
                "waiting_on": "precondition",
            },
        },
    )

    requeued = await sweep_unblock_blocked_tasks(reaper_db, _utcnow())

    assert requeued == 1
    await reaper_db.refresh(task)
    assert task.status == TaskStatus.QUEUED
    assert task.context["_gate_revalidated_head"] == "b" * 40
    assert "integration head" in (task.latest_status_message or "").lower()


@pytest.mark.asyncio
async def test_unblock_sweep_prioritizes_gate_blocker_before_backoff_is_due(
    reaper_db, mocker
):
    """A HEAD advance bypasses stale backoff without bypassing SHA/CI guards."""
    mocker.patch("app.broadcast.broadcast_to_project", new_callable=AsyncMock)
    mocker.patch(
        "app.gate_revalidation.resolve_gate_state",
        new_callable=AsyncMock,
        return_value={
            "head_sha": "b" * 40,
            "green_at_head": True,
            "checked_at_utc": _utcnow().isoformat(),
        },
    )
    project = await _make_project(reaper_db)
    now = _utcnow()
    task = await _blocked_task(
        reaper_db,
        project,
        blocker_kind="precondition",
        blocked_reason=(
            "BLOCKER: ./pr open failed on pre-existing repo-wide standalone "
            "smoke failures in untouched tests"
        ),
        recheck_after=now + timedelta(hours=1),
        context={
            "generated_head_sha": "a" * 40,
            "blocker": {
                "source": "agent",
                "waiting_on": "precondition",
            },
        },
    )

    requeued = await sweep_unblock_blocked_tasks(reaper_db, now)

    assert requeued == 1
    await reaper_db.refresh(task)
    assert task.status == TaskStatus.QUEUED
    assert task.context["_gate_revalidated_head"] == "b" * 40


@pytest.mark.asyncio
async def test_unblock_sweep_retries_tests_gated_legacy_task_after_new_commit(
    reaper_db, mocker
):
    """No-checks integration branches recover legacy rows without spawn SHAs."""
    mocker.patch("app.broadcast.broadcast_to_project", new_callable=AsyncMock)
    now = _utcnow()
    mocker.patch(
        "app.gate_revalidation.resolve_gate_state",
        new_callable=AsyncMock,
        return_value={
            "head_sha": "b" * 40,
            "green_at_head": False,
            "no_checks_allowed": True,
            "revalidation_ready": True,
            "head_committed_at": now + timedelta(minutes=1),
            "checked_at_utc": now.isoformat(),
        },
    )
    project = await _make_project(reaper_db)
    task = await _blocked_task(
        reaper_db,
        project,
        blocker_kind="precondition",
        blocked_reason=(
            "BLOCKER: ./pr open is blocked by standalone smoke failures in "
            "untouched backend/testing/test_invariants.py"
        ),
        recheck_after=now + timedelta(hours=1),
        context={
            "blocker": {
                "source": "agent",
                "waiting_on": "precondition",
            },
        },
    )
    task.blocked_at = now
    reaper_db.add(task)
    await reaper_db.commit()

    requeued = await sweep_unblock_blocked_tasks(reaper_db, now)

    assert requeued == 1
    await reaper_db.refresh(task)
    assert task.status == TaskStatus.QUEUED
    assert task.context["_gate_revalidated_head"] == "b" * 40
    assert "rerun the repository gate" in (task.latest_status_message or "")


@pytest.mark.asyncio
async def test_unblock_sweep_does_not_revalidate_same_green_head(
    reaper_db, mocker
):
    """The same green HEAD cannot repeatedly unblock a task that blocks again."""
    mocker.patch("app.broadcast.broadcast_to_project", new_callable=AsyncMock)
    mocker.patch(
        "app.gate_revalidation.resolve_gate_state",
        new_callable=AsyncMock,
        return_value={
            "head_sha": "b" * 40,
            "green_at_head": True,
            "checked_at_utc": _utcnow().isoformat(),
        },
    )
    project = await _make_project(reaper_db)
    task = await _blocked_task(
        reaper_db,
        project,
        blocker_kind="precondition",
        blocked_reason=(
            "BLOCKER: ./check test has pre-existing repo-wide failures "
            "outside this diff"
        ),
        context={
            "generated_head_sha": "a" * 40,
            "_gate_revalidated_head": "b" * 40,
            "_blocked_optimistic_retries": _BLOCKED_OPTIMISTIC_RETRY_CAP,
            "blocker": {
                "source": "agent",
                "waiting_on": "precondition",
            },
        },
    )

    requeued = await sweep_unblock_blocked_tasks(reaper_db, _utcnow())

    assert requeued == 0
    await reaper_db.refresh(task)
    assert task.status == TaskStatus.BLOCKED
    assert task.context["_gate_revalidated_head"] == "b" * 40


@pytest.mark.asyncio
async def test_unblock_sweep_ignores_unrelated_active_sibling_for_gate_recovery(
    reaper_db, mocker
):
    """Independent decomposed siblings do not masquerade as a live respawn."""
    mocker.patch("app.broadcast.broadcast_to_project", new_callable=AsyncMock)
    mocker.patch(
        "app.gate_revalidation.resolve_gate_state",
        new_callable=AsyncMock,
        return_value={
            "head_sha": "b" * 40,
            "green_at_head": True,
            "checked_at_utc": _utcnow().isoformat(),
        },
    )
    project = await _make_project(reaper_db)
    parent_id = _uuid_mod.uuid4()
    task = await _blocked_task(
        reaper_db,
        project,
        parent_task_id=parent_id,
        blocker_kind="precondition",
        blocked_reason=(
            "BLOCKER: mandatory impacted backend gate fails outside this diff"
        ),
        context={
            "generated_head_sha": "a" * 40,
            "finding_hash": "blocked-finding",
            "blocker": {
                "source": "agent",
                "waiting_on": "precondition",
            },
        },
    )
    sibling = Task(
        project_id=project.id,
        parent_task_id=parent_id,
        title="independent sibling",
        status=TaskStatus.IN_PROGRESS,
        created_by="test-user",
        payloads=[{"type": "create"}],
        context={"finding_hash": "other-finding"},
        status_log=[],
    )
    reaper_db.add(sibling)
    await reaper_db.commit()

    requeued = await sweep_unblock_blocked_tasks(reaper_db, _utcnow())

    assert requeued == 1
    await reaper_db.refresh(task)
    assert task.status == TaskStatus.QUEUED


@pytest.mark.asyncio
async def test_unblock_sweep_broadcasts_after_commit(reaper_db, mocker):
    """The unblock-to-QUEUED task_updated broadcast must fire only after the
    batch commit succeeds, so the UI never refetches a still-BLOCKED row."""
    project = await _make_project(reaper_db)
    t = await _blocked_task(
        reaper_db,
        project,
        depends_on=[],
        context={
            "blocker": {"source": "agent", "waiting_on": "file conflict"},
            "failure_class": "BLOCKED_PRECONDITION",
            "failure_category": "BLOCKED_PRECONDITION",
        },
    )

    # Track commit/broadcast ordering. Install AFTER setup so the setup's own
    # commits aren't recorded.
    events: list = []
    real_commit = reaper_db.commit

    async def _tracking_commit(*a, **k):
        events.append("commit")
        return await real_commit(*a, **k)

    mocker.patch.object(reaper_db, "commit", side_effect=_tracking_commit)

    async def _tracking_broadcast(project_id, event, payload):
        events.append(("broadcast", event, payload.get("status")))

    mocker.patch("app.broadcast.broadcast_to_project", side_effect=_tracking_broadcast)

    requeued = await sweep_unblock_blocked_tasks(reaper_db, _utcnow())

    assert requeued == 1
    await reaper_db.refresh(t)
    assert t.status == TaskStatus.QUEUED
    # The QUEUED broadcast fired exactly once and strictly after the commit.
    assert "commit" in events
    assert ("broadcast", "task_updated", "QUEUED") in events
    assert events.index("commit") < events.index(
        ("broadcast", "task_updated", "QUEUED")
    )


@pytest.mark.asyncio
async def test_unblock_sweep_does_not_broadcast_on_commit_failure(reaper_db, mocker):
    """If the batch commit raises, no task_updated event may be published —
    otherwise the UI would act on a transition that never persisted."""
    project = await _make_project(reaper_db)
    await _blocked_task(
        reaper_db,
        project,
        depends_on=[],
        context={
            "blocker": {"source": "agent", "waiting_on": "file conflict"},
            "failure_class": "BLOCKED_PRECONDITION",
            "failure_category": "BLOCKED_PRECONDITION",
        },
    )

    broadcast = mocker.patch(
        "app.broadcast.broadcast_to_project", new_callable=AsyncMock
    )
    mocker.patch.object(reaper_db, "commit", side_effect=RuntimeError("commit boom"))

    with pytest.raises(RuntimeError, match="commit boom"):
        await sweep_unblock_blocked_tasks(reaper_db, _utcnow())

    # The status transition was never committed, so no broadcast may fire.
    broadcast.assert_not_called()


@pytest.mark.asyncio
async def test_unblock_sweep_does_not_retry_unnamed_agent_precondition_without_dep_edge(
    reaper_db, mocker
):
    """Free text and elapsed time are not retry authority."""
    mocker.patch("app.broadcast.broadcast_to_project", new_callable=AsyncMock)
    project = await _make_project(reaper_db)
    now = _utcnow()
    t = await _blocked_task(
        reaper_db,
        project,
        blocker_kind="precondition",
        blocked_reason="BLOCKER: orchestrator ./pr open returned HTTP 500",
        depends_on=[],
        context={
            "blocker": {
                "source": "agent",
                "waiting_on": "precondition",
                "reason": "BLOCKER: orchestrator ./pr open returned HTTP 500",
            }
        },
        recheck_after=now - timedelta(minutes=1),
    )

    requeued = await sweep_unblock_blocked_tasks(reaper_db, now)

    assert requeued == 0
    await reaper_db.refresh(t)
    assert t.status == TaskStatus.BLOCKED
    assert t.context.get("_blocked_optimistic_retries") is None
    assert t.recheck_after is not None and t.recheck_after > now


@pytest.mark.asyncio
async def test_unblock_sweep_requeues_legacy_viewport_capability_sentinel(
    reaper_db, mocker
):
    """The advisory migration clears exactly once per server policy receipt."""
    mocker.patch("app.broadcast.broadcast_to_project", new_callable=AsyncMock)
    project = await _make_project(reaper_db)
    now = _utcnow()
    t = await _blocked_task(
        reaper_db,
        project,
        blocker_kind="precondition",
        blocked_reason=(
            "BLOCKER: VIEWPORT_GATE_INFRA_UNAVAILABLE: Chromium bootstrap failed"
        ),
        depends_on=[],
        context={
            "blocker": {
                "source": "agent",
                "waiting_on": "precondition",
                "reason": "VIEWPORT_GATE_INFRA_UNAVAILABLE",
            }
        },
        recheck_after=now - timedelta(minutes=1),
    )

    requeued = await sweep_unblock_blocked_tasks(reaper_db, now)

    assert requeued == 1
    await reaper_db.refresh(t)
    assert t.status == TaskStatus.QUEUED
    assert t.context.get("_blocked_optimistic_retries") is None
    assert len(t.attempt_history or []) == 1
    assert t.context.get("_blocked_retry_precondition_receipt") == {
        "authority": "server_advisory_policy_v1",
        "kind": "legacy_viewport_advisory",
        "policy": "viewport_capture_ci_advisory_v1",
    }

    # An old runner emits the same retired sentinel again. The stable policy
    # receipt survives the fresh-attempt transition, so no second attempt burns.
    from app.task_blocked import enter_blocked_state

    enter_blocked_state(
        t,
        waiting_on="precondition",
        reason="VIEWPORT_GATE_INFRA_UNAVAILABLE: Chromium bootstrap failed",
        source="agent",
        blocker_kind="precondition",
        recheck_in_seconds=None,
        now=now + timedelta(seconds=1),
    )
    reaper_db.add(t)
    await reaper_db.commit()

    second_requeue = await sweep_unblock_blocked_tasks(
        reaper_db, now + timedelta(seconds=2)
    )

    assert second_requeue == 0
    await reaper_db.refresh(t)
    assert t.status == TaskStatus.BLOCKED
    assert len(t.attempt_history or []) == 1


@pytest.mark.asyncio
async def test_unblock_sweep_parks_agent_precondition_after_retry_budget_spent(
    reaper_db, mocker
):
    """Once the optimistic-retry budget is exhausted, a durable no-dep agent
    precondition blocker stops thrashing: it stays BLOCKED and backs off (the 6h
    deadline escalation then surfaces it for the operator)."""
    mocker.patch("app.broadcast.broadcast_to_project", new_callable=AsyncMock)
    project = await _make_project(reaper_db)
    now = _utcnow()
    t = await _blocked_task(
        reaper_db,
        project,
        blocker_kind="precondition",
        blocked_reason="BLOCKER: backend/app/foo.py does not exist on main",
        depends_on=[],
        blocked_at=now - timedelta(minutes=20),  # well short of the 6h deadline
        context={
            "blocker": {
                "source": "agent",
                "waiting_on": "precondition",
                "reason": "BLOCKER: backend/app/foo.py does not exist on main",
            },
            "_blocked_optimistic_retries": _BLOCKED_OPTIMISTIC_RETRY_CAP,
        },
        recheck_after=now - timedelta(minutes=1),
    )

    requeued = await sweep_unblock_blocked_tasks(reaper_db, now)

    assert requeued == 0
    await reaper_db.refresh(t)
    assert t.status == TaskStatus.BLOCKED
    assert t.recheck_after is not None and t.recheck_after > now
    assert t.context.get("_blocked_recheck_attempts") == 1


@pytest.mark.asyncio
async def test_blocked_escalation_due_excludes_pause_time(reaper_db):
    """_blocked_escalation_due subtracts pause windows from the 6h escalate-and-
    park deadline: a task blocked well past the deadline is NOT due while the
    excess is pipeline-pause time, IS due with only a brief pause (no over-
    protection), and falls back to raw wall-clock when no DeploymentState row
    exists (unchanged legacy behavior)."""
    from app.blocked_task_unblock_sweeps import (
        _blocked_escalation_due,
        _BLOCKED_ESCALATE_AFTER,
    )
    from app.models import DeploymentState, DeploymentStatus

    project = await _make_project(reaper_db)
    t = await _blocked_task(
        reaper_db,
        project,
        depends_on=[],
        blocked_at=_utcnow() - (_BLOCKED_ESCALATE_AFTER + timedelta(hours=1)),
    )

    # No DeploymentState row → raw wall-clock (7h) → past the 6h deadline.
    assert await _blocked_escalation_due(t, reaper_db) is True

    # PAUSED for 6h of the 7h → ~1h of ACTIVE age → NOT due.
    ds = DeploymentState(
        id=1,
        status=DeploymentStatus.PAUSED,
        paused_at=_utcnow() - _BLOCKED_ESCALATE_AFTER,
        paused_kind="manual",
    )
    reaper_db.add(ds)
    await reaper_db.commit()
    assert await _blocked_escalation_due(t, reaper_db) is False

    # Only a 30-min open pause → ~6.5h of ACTIVE age → still due.
    ds.paused_at = _utcnow() - timedelta(minutes=30)
    reaper_db.add(ds)
    await reaper_db.commit()
    assert await _blocked_escalation_due(t, reaper_db) is True


@pytest.mark.asyncio
async def test_unblock_sweep_defers_deadline_escalation_while_paused(reaper_db, mocker):
    """Integration: the sweep's 6h deadline escalation is pause-aware. A no-dep
    precondition blocker 7h past onset with its optimistic-retry budget already
    spent is NOT escalated-and-parked while the pipeline has been PAUSED for 6h of
    that (active age ~1h) — it stays BLOCKED and backs off, exactly as a task
    still short of the deadline would."""
    mocker.patch("app.broadcast.broadcast_to_project", new_callable=AsyncMock)
    from app.models import DeploymentState, DeploymentStatus

    project = await _make_project(reaper_db)
    now = _utcnow()
    reaper_db.add(
        DeploymentState(
            id=1,
            status=DeploymentStatus.PAUSED,
            paused_at=now - _BLOCKED_ESCALATE_AFTER,
            paused_kind="manual",
        )
    )
    await reaper_db.commit()
    t = await _blocked_task(
        reaper_db,
        project,
        blocker_kind="precondition",
        blocked_reason="BLOCKER: backend/app/foo.py does not exist on main",
        depends_on=[],
        blocked_at=now - (_BLOCKED_ESCALATE_AFTER + timedelta(hours=1)),
        context={
            "blocker": {
                "source": "agent",
                "waiting_on": "precondition",
                "reason": "BLOCKER: backend/app/foo.py does not exist on main",
            },
            "_blocked_optimistic_retries": _BLOCKED_OPTIMISTIC_RETRY_CAP,
        },
        recheck_after=now - timedelta(minutes=1),
    )

    requeued = await sweep_unblock_blocked_tasks(reaper_db, now)

    assert requeued == 0
    await reaper_db.refresh(t)
    assert t.status == TaskStatus.BLOCKED
    # NOT escalated/parked — the pause spared it.
    assert t.context.get("_blocked_escalated") is not True
    # Backed off at the recheck cadence, not the multi-year escalation park.
    assert t.recheck_after is not None
    assert t.recheck_after < now + (_BLOCKED_ESCALATED_PARK / 2)


@pytest.mark.asyncio
async def test_unblock_sweep_escalates_past_deadline_despite_brief_pause(
    reaper_db, mocker
):
    """MUTATION-PROOF companion: the pause-aware deadline must not become a blanket
    disable. The same 7h-past-onset precondition blocker with its retry budget
    spent and only a 30-min pause has ~6.5h of ACTIVE age > the 6h deadline and IS
    escalated-and-parked."""
    mocker.patch("app.broadcast.broadcast_to_project", new_callable=AsyncMock)
    from app.models import DeploymentState, DeploymentStatus

    project = await _make_project(reaper_db)
    now = _utcnow()
    reaper_db.add(
        DeploymentState(
            id=1,
            status=DeploymentStatus.PAUSED,
            paused_at=now - timedelta(minutes=30),
            paused_kind="manual",
        )
    )
    await reaper_db.commit()
    t = await _blocked_task(
        reaper_db,
        project,
        blocker_kind="precondition",
        blocked_reason="BLOCKER: backend/app/foo.py does not exist on main",
        depends_on=[],
        blocked_at=now - (_BLOCKED_ESCALATE_AFTER + timedelta(hours=1)),
        context={
            "blocker": {
                "source": "agent",
                "waiting_on": "precondition",
                "reason": "BLOCKER: backend/app/foo.py does not exist on main",
            },
            "_blocked_optimistic_retries": _BLOCKED_OPTIMISTIC_RETRY_CAP,
        },
        recheck_after=now - timedelta(minutes=1),
    )

    requeued = await sweep_unblock_blocked_tasks(reaper_db, now)

    assert requeued == 0
    await reaper_db.refresh(t)
    assert t.status == TaskStatus.BLOCKED
    assert t.context.get("_blocked_escalated") is True


@pytest.mark.asyncio
async def test_unblock_sweep_first_capability_epoch_banks_and_advances_attempt(
    reaper_db, mocker
):
    """A trusted changed capability authority admits exactly one fresh attempt."""
    mocker.patch("app.broadcast.broadcast_to_project", new_callable=AsyncMock)
    project = await _make_project(reaper_db)
    now = _utcnow()
    runner = _verified_runner_snapshot(project.id)
    mocker.patch(
        "app.orchestrator_runner_dispatch._load_active_runners_cache",
        new_callable=AsyncMock,
        return_value={project.id: [runner]},
    )
    t = await _blocked_task(
        reaper_db,
        project,
        blocker_kind="precondition",
        blocked_reason="BLOCKER: ./pr open returned HTTP 500",
        depends_on=[],
        blocked_at=now - timedelta(minutes=5),
        context=_learned_backend_verification_context(
            blocker={
                "source": "agent",
                "waiting_on": "precondition",
                "reason": "runtime unavailable",
            }
        ),
        recheck_after=now - timedelta(minutes=1),
        vm_provisioned_at=now - timedelta(minutes=3),
        vm_stopped_at=now - timedelta(minutes=1),
        api_call_summary={"total_input_tokens": 100, "total_output_tokens": 20},
    )

    requeued = await sweep_unblock_blocked_tasks(reaper_db, now)

    assert requeued == 1
    await reaper_db.refresh(t)
    assert t.status == TaskStatus.QUEUED
    assert t.context.get("_blocked_optimistic_retries") == 1
    assert t.context.get("_blocked_last_retry_at") is not None
    receipt = t.context.get("_blocked_retry_precondition_receipt")
    assert receipt["runner_id"] == str(runner.id)
    assert receipt["kind"] == "execution_capability"
    assert len(t.attempt_history or []) == 1
    assert t.attempt_history[-1]["attempt_ordinal"] == 1
    assert t.attempt_history[-1]["llm_input_tokens"] == 100
    from app.models import get_task_lifetime_attempt_ordinal

    assert get_task_lifetime_attempt_ordinal(t) == 2
    assert "optimistic" in (t.latest_status_message or "").lower()


@pytest.mark.asyncio
async def test_unblock_sweep_retry_gate_holds_when_no_evidence(reaper_db, mocker):
    """Retry 2+ is withheld when blocked_at has not advanced past the last retry
    timestamp — no VM run happened since the previous retry was dispatched."""
    mocker.patch("app.broadcast.broadcast_to_project", new_callable=AsyncMock)
    project = await _make_project(reaper_db)
    now = _utcnow()
    runner = _verified_runner_snapshot(project.id)
    mocker.patch(
        "app.orchestrator_runner_dispatch._load_active_runners_cache",
        new_callable=AsyncMock,
        return_value={project.id: [runner]},
    )
    last_retry_at = now - timedelta(seconds=30)
    t = await _blocked_task(
        reaper_db,
        project,
        blocker_kind="precondition",
        blocked_reason="BLOCKER: backend/app/foo.py does not exist on main",
        depends_on=[],
        # blocked_at is BEFORE last_retry_at: no new VM run has happened since retry 1.
        blocked_at=now - timedelta(seconds=60),
        context=_learned_backend_verification_context(
            blocker={
                "source": "agent",
                "waiting_on": "precondition",
                "reason": "BLOCKER: backend/app/foo.py does not exist on main",
            },
            _blocked_optimistic_retries=1,
            _blocked_last_retry_at=last_retry_at.isoformat(),
        ),
        recheck_after=now - timedelta(minutes=1),
    )

    requeued = await sweep_unblock_blocked_tasks(reaper_db, now)

    assert requeued == 0
    await reaper_db.refresh(t)
    assert t.status == TaskStatus.BLOCKED
    # Retry budget unchanged — the slot was NOT consumed.
    assert t.context.get("_blocked_optimistic_retries") == 1
    # Backoff was re-armed instead.
    assert t.recheck_after is not None and t.recheck_after > now
    assert t.context.get("_blocked_recheck_attempts") == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("last_retry_at", [None, "not-a-timestamp"])
async def test_unblock_sweep_retry_gate_holds_for_missing_or_malformed_evidence(
    reaper_db, mocker, last_retry_at
):
    """Retry 2+ fails closed when its prior-run timestamp is absent or invalid."""
    mocker.patch("app.broadcast.broadcast_to_project", new_callable=AsyncMock)
    project = await _make_project(reaper_db)
    now = _utcnow()
    runner = _verified_runner_snapshot(project.id)
    mocker.patch(
        "app.orchestrator_runner_dispatch._load_active_runners_cache",
        new_callable=AsyncMock,
        return_value={project.id: [runner]},
    )
    context = _learned_backend_verification_context(**{
        "blocker": {
            "source": "agent",
            "waiting_on": "precondition",
            "reason": "BLOCKER: backend/app/foo.py does not exist on main",
        },
        "_blocked_optimistic_retries": 1,
    })
    if last_retry_at is not None:
        context["_blocked_last_retry_at"] = last_retry_at
    t = await _blocked_task(
        reaper_db,
        project,
        blocker_kind="precondition",
        blocked_reason="BLOCKER: backend/app/foo.py does not exist on main",
        depends_on=[],
        blocked_at=now - timedelta(seconds=30),
        context=context,
        recheck_after=now - timedelta(minutes=1),
    )

    requeued = await sweep_unblock_blocked_tasks(reaper_db, now)

    assert requeued == 0
    await reaper_db.refresh(t)
    assert t.status == TaskStatus.BLOCKED
    assert t.context.get("_blocked_optimistic_retries") == 1
    assert t.context.get("_blocked_recheck_attempts") == 1
    assert t.recheck_after is not None and t.recheck_after > now


@pytest.mark.asyncio
async def test_unblock_sweep_holds_after_reblock_on_same_capability_epoch(
    reaper_db, mocker
):
    """A real prior run does not permit another retry on the same runner proof."""

    mocker.patch("app.broadcast.broadcast_to_project", new_callable=AsyncMock)
    project = await _make_project(reaper_db)
    now = _utcnow()
    runner = _verified_runner_snapshot(project.id)
    mocker.patch(
        "app.orchestrator_runner_dispatch._load_active_runners_cache",
        new_callable=AsyncMock,
        return_value={project.id: [runner]},
    )
    last_retry_at = now - timedelta(minutes=2)
    t = await _blocked_task(
        reaper_db,
        project,
        blocker_kind="precondition",
        blocked_reason="BLOCKER: runtime still unavailable",
        depends_on=[],
        blocked_at=now - timedelta(minutes=1),
        context=_learned_backend_verification_context(
            blocker={"source": "agent", "waiting_on": "precondition"},
            _blocked_optimistic_retries=1,
            _blocked_last_retry_at=last_retry_at.isoformat(),
        ),
        recheck_after=now - timedelta(seconds=1),
    )
    from app.blocked_task_unblock_sweeps import (
        _BLOCKED_RETRY_PRECONDITION_RECEIPT_KEY,
        _execution_capability_retry_receipt,
    )

    receipt = await _execution_capability_retry_receipt(t, reaper_db)
    context = dict(t.context or {})
    context[_BLOCKED_RETRY_PRECONDITION_RECEIPT_KEY] = receipt
    t.context = context
    reaper_db.add(t)
    await reaper_db.commit()

    requeued = await sweep_unblock_blocked_tasks(reaper_db, now)

    assert requeued == 0
    await reaper_db.refresh(t)
    assert t.status == TaskStatus.BLOCKED
    assert t.context.get("_blocked_optimistic_retries") == 1
    assert not (t.attempt_history or [])


@pytest.mark.asyncio
async def test_unblock_sweep_retry_gate_allows_changed_capability_after_reblock(
    reaper_db, mocker
):
    """Retry 2 needs both a completed prior run and a distinct capability epoch."""
    mocker.patch("app.broadcast.broadcast_to_project", new_callable=AsyncMock)
    project = await _make_project(reaper_db)
    now = _utcnow()
    last_retry_at = now - timedelta(seconds=60)
    previous_runner = _verified_runner_snapshot(project.id)
    replacement_runner = _verified_runner_snapshot(project.id)
    fleet = [previous_runner]

    async def _active_runners(_session):
        return {project.id: list(fleet)}

    mocker.patch(
        "app.orchestrator_runner_dispatch._load_active_runners_cache",
        side_effect=_active_runners,
    )
    t = await _blocked_task(
        reaper_db,
        project,
        blocker_kind="precondition",
        blocked_reason="BLOCKER: backend/app/foo.py does not exist on main",
        depends_on=[],
        # blocked_at is AFTER last_retry_at: the previous retry actually ran a VM.
        blocked_at=now - timedelta(seconds=30),
        context=_learned_backend_verification_context(
            blocker={
                "source": "agent",
                "waiting_on": "precondition",
                "reason": "BLOCKER: backend/app/foo.py does not exist on main",
            },
            _blocked_optimistic_retries=1,
            _blocked_last_retry_at=last_retry_at.isoformat(),
        ),
        recheck_after=now - timedelta(minutes=1),
        vm_provisioned_at=now - timedelta(seconds=50),
        vm_stopped_at=now - timedelta(seconds=30),
    )

    from app.blocked_task_unblock_sweeps import (
        _BLOCKED_RETRY_PRECONDITION_RECEIPT_KEY,
        _execution_capability_retry_receipt,
    )

    prior_receipt = await _execution_capability_retry_receipt(t, reaper_db)
    assert prior_receipt is not None
    context = dict(t.context or {})
    context[_BLOCKED_RETRY_PRECONDITION_RECEIPT_KEY] = prior_receipt
    t.context = context
    reaper_db.add(t)
    await reaper_db.commit()
    fleet[:] = [replacement_runner]

    requeued = await sweep_unblock_blocked_tasks(reaper_db, now)

    assert requeued == 1
    await reaper_db.refresh(t)
    assert t.status == TaskStatus.QUEUED
    assert t.context.get("_blocked_optimistic_retries") == 2
    assert t.context.get("_blocked_last_retry_at") is not None
    assert t.context.get("_blocked_last_retry_at") != last_retry_at.isoformat()
    assert (
        t.context["_blocked_retry_precondition_receipt"]["runner_id"]
        == str(replacement_runner.id)
    )
    assert len(t.attempt_history or []) == 1
    assert t.attempt_history[-1]["attempt_ordinal"] == 1
    assert "optimistic" in (t.latest_status_message or "").lower()


@pytest.mark.asyncio
async def test_unblock_sweep_resolves_task_need_into_dep_edge_and_requeues(
    reaper_db, mocker
):
    """An agent BLOCKER carrying a machine-resolvable {kind:'task'} need is
    resolved into a real depends_on edge onto the in-flight foundation and
    requeued — SHORT-CIRCUITING the optimistic-retry path (no wasted re-dispatch
    loop; the dispatch-time dependency gate now holds it)."""
    mocker.patch("app.broadcast.broadcast_to_project", new_callable=AsyncMock)
    project = await _make_project(reaper_db)
    now = _utcnow()
    foundation = Task(
        project_id=project.id,
        title="foundation",
        description="x",
        status=TaskStatus.IN_PROGRESS,
        created_by="u",
        requires_approval=False,
        payloads=[{"type": "create"}],
        context={},
        status_log=[],
    )
    reaper_db.add(foundation)
    await reaper_db.commit()
    await reaper_db.refresh(foundation)
    t = await _blocked_task(
        reaper_db,
        project,
        blocker_kind="precondition",
        blocked_reason="BLOCKER: needs the foundation task first",
        depends_on=[],
        context={
            "blocker": {
                "source": "agent",
                "waiting_on": "foundation",
                "reason": "BLOCKER: needs the foundation task first",
                "need": {"kind": "task", "ref": str(foundation.id)},
            }
        },
        recheck_after=now - timedelta(minutes=1),
    )

    requeued = await sweep_unblock_blocked_tasks(reaper_db, now)

    assert requeued == 1
    await reaper_db.refresh(t)
    assert t.status == TaskStatus.QUEUED
    # The need became a real dependency edge onto the foundation.
    assert str(foundation.id) in (t.depends_on or [])
    # Resolution short-circuited the optimistic retry: no retry budget consumed.
    assert t.context.get("_blocked_optimistic_retries") is None
    # Blocker metadata cleared on requeue.
    assert "blocker" not in (t.context or {})


@pytest.mark.asyncio
async def test_unblock_sweep_stamps_resume_marker_on_edge_resolution(reaper_db, mocker):
    """A blocked dependent resumed by wiring an in-flight dependency edge gets a
    durable RESUME_MARKER_CONTEXT_KEY marker stamped (resolved_by='edge',
    foundation=[<foundation id>], direction_id, at), symmetric to the give-up
    marker. Observability only — the resume decision is unchanged."""
    from app.reaper_sweeps import RESUME_MARKER_CONTEXT_KEY

    mocker.patch("app.broadcast.broadcast_to_project", new_callable=AsyncMock)
    project = await _make_project(reaper_db)
    now = _utcnow()
    foundation = Task(
        project_id=project.id,
        title="foundation",
        description="x",
        status=TaskStatus.IN_PROGRESS,
        created_by="u",
        requires_approval=False,
        payloads=[{"type": "create"}],
        context={},
        status_log=[],
    )
    reaper_db.add(foundation)
    await reaper_db.commit()
    await reaper_db.refresh(foundation)
    t = await _blocked_task(
        reaper_db,
        project,
        blocker_kind="precondition",
        blocked_reason="BLOCKER: needs the foundation task first",
        depends_on=[],
        context={
            "direction_id": "d-resume",
            "blocker": {
                "source": "agent",
                "waiting_on": "foundation",
                "reason": "BLOCKER: needs the foundation task first",
                "need": {"kind": "task", "ref": str(foundation.id)},
            },
        },
        recheck_after=now - timedelta(minutes=1),
    )

    requeued = await sweep_unblock_blocked_tasks(reaper_db, now)

    assert requeued == 1
    await reaper_db.refresh(t)
    assert t.status == TaskStatus.QUEUED
    marker = (t.context or {}).get(RESUME_MARKER_CONTEXT_KEY)
    assert isinstance(marker, dict)
    assert marker["resolved_by"] == "edge"
    assert str(foundation.id) in marker["foundation"]
    assert marker["direction_id"] == "d-resume"
    assert isinstance(marker["at"], str) and marker["at"]


@pytest.mark.asyncio
async def test_unblock_sweep_stamps_resume_marker_on_completed_work(reaper_db, mocker):
    """A blocked dependent resumed by already-completed/landed work gets a resume
    marker with resolved_by='completed' (no edge wired)."""
    from app.reaper_sweeps import RESUME_MARKER_CONTEXT_KEY

    mocker.patch("app.broadcast.broadcast_to_project", new_callable=AsyncMock)
    project = await _make_project(reaper_db)
    now = _utcnow()
    foundation = Task(
        project_id=project.id,
        title="foundation",
        description="x",
        status=TaskStatus.COMPLETED,
        created_by="u",
        requires_approval=False,
        payloads=[{"type": "create"}],
        context={"pr_merged": True},
        status_log=[],
    )
    reaper_db.add(foundation)
    await reaper_db.commit()
    await reaper_db.refresh(foundation)
    t = await _blocked_task(
        reaper_db,
        project,
        blocker_kind="upstream_dependency",
        blocked_reason="BLOCKER: waiting on foundation",
        depends_on=[],
        context={
            "direction_id": "d-resume",
            "blocker": {
                "source": "agent",
                "waiting_on": "foundation",
                "reason": "BLOCKER: waiting on foundation",
                "need": {"kind": "task", "ref": str(foundation.id)},
            },
        },
        recheck_after=now - timedelta(minutes=1),
    )

    requeued = await sweep_unblock_blocked_tasks(reaper_db, now)

    assert requeued == 1
    await reaper_db.refresh(t)
    assert t.status == TaskStatus.QUEUED
    assert not (t.depends_on or [])
    marker = (t.context or {}).get(RESUME_MARKER_CONTEXT_KEY)
    assert isinstance(marker, dict)
    assert marker["resolved_by"] == "completed"
    assert marker["direction_id"] == "d-resume"
    assert isinstance(marker["at"], str) and marker["at"]


@pytest.mark.asyncio
async def test_unblock_sweep_resolves_finding_hash_need_into_dep_edge(
    reaper_db, mocker
):
    """A {kind:'task'} need whose ref is the foundation's non-UUID finding-hash
    resolves into a real depends_on edge — the resume matcher now sees the same
    finding-hash signal the producer recorded (previously this parked forever)."""
    mocker.patch("app.broadcast.broadcast_to_project", new_callable=AsyncMock)
    project = await _make_project(reaper_db)
    now = _utcnow()
    foundation = Task(
        project_id=project.id,
        title="foundation",
        description="x",
        status=TaskStatus.IN_PROGRESS,
        created_by="u",
        requires_approval=False,
        payloads=[{"type": "create"}],
        context={"finding_hash": "abc123def4560789"},
        status_log=[],
    )
    reaper_db.add(foundation)
    await reaper_db.commit()
    await reaper_db.refresh(foundation)
    t = await _blocked_task(
        reaper_db,
        project,
        blocker_kind="precondition",
        blocked_reason="BLOCKER: needs the foundation finding first",
        depends_on=[],
        context={
            "blocker": {
                "source": "agent",
                "waiting_on": "foundation",
                "reason": "BLOCKER: needs the foundation finding first",
                "need": {"kind": "task", "ref": "abc123def4560789"},
            }
        },
        recheck_after=now - timedelta(minutes=1),
    )

    requeued = await sweep_unblock_blocked_tasks(reaper_db, now)

    assert requeued == 1
    await reaper_db.refresh(t)
    assert t.status == TaskStatus.QUEUED
    assert str(foundation.id) in (t.depends_on or [])
    assert t.context.get("_blocked_optimistic_retries") is None


@pytest.mark.asyncio
async def test_unblock_sweep_resolves_pr_need_to_owning_task(reaper_db, mocker):
    """A {kind:'pr'} need resolves onto the in-flight task that owns that PR."""
    mocker.patch("app.broadcast.broadcast_to_project", new_callable=AsyncMock)
    project = await _make_project(reaper_db)
    now = _utcnow()
    owner = Task(
        project_id=project.id,
        title="pr-owner",
        description="x",
        status=TaskStatus.IN_PROGRESS,
        created_by="u",
        requires_approval=False,
        payloads=[{"type": "fix", "pr_number": 7919}],
        context={},
        status_log=[],
    )
    reaper_db.add(owner)
    await reaper_db.commit()
    await reaper_db.refresh(owner)
    t = await _blocked_task(
        reaper_db,
        project,
        blocker_kind="precondition",
        depends_on=[],
        context={
            "blocker": {
                "source": "agent",
                "waiting_on": "PR 7919",
                "need": {"kind": "pr", "ref": "7919"},
            }
        },
        recheck_after=now - timedelta(minutes=1),
    )

    requeued = await sweep_unblock_blocked_tasks(reaper_db, now)

    assert requeued == 1
    await reaper_db.refresh(t)
    assert t.status == TaskStatus.QUEUED
    assert str(owner.id) in (t.depends_on or [])


@pytest.mark.asyncio
async def test_unblock_sweep_unresolved_capability_need_stays_blocked_without_provider(
    reaper_db, mocker
):
    """A {kind:'capability'} need with NO conservative candidate match does NOT
    wire an edge or authorize a blind retry."""
    mocker.patch("app.broadcast.broadcast_to_project", new_callable=AsyncMock)
    project = await _make_project(reaper_db)
    now = _utcnow()
    # An in-flight task that does NOT match the capability ref by any exact signal.
    other = Task(
        project_id=project.id,
        title="unrelated work",
        description="x",
        status=TaskStatus.IN_PROGRESS,
        created_by="u",
        requires_approval=False,
        payloads=[{"type": "create"}],
        context={},
        status_log=[],
    )
    reaper_db.add(other)
    await reaper_db.commit()
    t = await _blocked_task(
        reaper_db,
        project,
        blocker_kind="precondition",
        blocked_reason="BLOCKER: needs an auth subsystem",
        depends_on=[],
        context={
            "blocker": {
                "source": "agent",
                "waiting_on": "auth",
                "reason": "BLOCKER: needs an auth subsystem",
                "need": {"kind": "capability", "ref": "auth"},
            }
        },
        recheck_after=now - timedelta(minutes=1),
    )

    requeued = await sweep_unblock_blocked_tasks(reaper_db, now)

    assert requeued == 0
    await reaper_db.refresh(t)
    assert t.status == TaskStatus.BLOCKED
    assert not (t.depends_on or [])
    assert t.context.get("_blocked_optimistic_retries") is None
    assert t.recheck_after is not None and t.recheck_after > now


@pytest.mark.asyncio
async def test_unblock_sweep_resumes_when_need_satisfied_by_completed_work(
    reaper_db, mocker
):
    """A blocked task whose need is already satisfied by completed/merged work
    resumes promptly on the routine sweep with no dependency edge — reusing the
    same conservative exact-match check as the parked-resume path."""
    mocker.patch("app.broadcast.broadcast_to_project", new_callable=AsyncMock)
    project = await _make_project(reaper_db)
    now = _utcnow()
    # The foundation has already completed (landed) before the recheck fires.
    foundation = Task(
        project_id=project.id,
        title="foundation",
        description="x",
        status=TaskStatus.COMPLETED,
        created_by="u",
        requires_approval=False,
        payloads=[{"type": "create"}],
        context={"pr_merged": True},
        status_log=[],
    )
    reaper_db.add(foundation)
    await reaper_db.commit()
    await reaper_db.refresh(foundation)
    # Task blocked on that foundation, but the need hasn't resolved to an edge yet
    # because the foundation completed before the sweep ran.
    t = await _blocked_task(
        reaper_db,
        project,
        blocker_kind="upstream_dependency",
        blocked_reason="BLOCKER: waiting on foundation",
        depends_on=[],  # no edge yet
        context={
            "blocker": {
                "source": "agent",
                "waiting_on": "foundation",
                "reason": "BLOCKER: waiting on foundation",
                "need": {"kind": "task", "ref": str(foundation.id)},
            }
        },
        recheck_after=now - timedelta(minutes=1),  # due now
    )

    requeued = await sweep_unblock_blocked_tasks(reaper_db, now)

    assert requeued == 1
    await reaper_db.refresh(t)
    assert t.status == TaskStatus.QUEUED
    # No edge wired (the work is already done, not in-flight).
    assert not (t.depends_on or [])
    # Blocker metadata cleared on requeue.
    assert "blocker" not in (t.context or {})
    assert "completed work" in (t.latest_status_message or "").lower()


@pytest.mark.asyncio
async def test_unblock_sweep_resumes_when_need_satisfied_by_knowledge_document(
    reaper_db, mocker
):
    """Incident shape (2026-06-07): a blocked dependent's kind='task' need whose ref
    is a finding_hash is satisfied by a durable KnowledgeDocument even when the
    originating task is CANCELLED — the dependent must resume (QUEUED), never be
    routed to the dead-foundation-cancel path."""
    from app.models import KnowledgeDocument

    mocker.patch("app.broadcast.broadcast_to_project", new_callable=AsyncMock)
    project = await _make_project(reaper_db)
    now = _utcnow()
    # Source task: CANCELLED (non-death — scope-empty so it is excluded from
    # the dead-foundation path), but its finding_hash IS the need's ref.
    source_task = Task(
        project_id=project.id,
        title="source",
        description="x",
        status=TaskStatus.CANCELLED,
        created_by="u",
        requires_approval=False,
        payloads=[{"type": "web_research"}],
        context={"finding_hash": "bfb447f23b6d44f6", "failure_category": "SCOPE_EMPTY"},
        status_log=[],
    )
    reaper_db.add(source_task)
    await reaper_db.commit()
    await reaper_db.refresh(source_task)
    # A KnowledgeDocument was written by that source task — durable substance.
    doc = KnowledgeDocument(
        project_id=project.id,
        source_task_id=source_task.id,
        source_kind="web_research",
        title="Better runs blueprint",
        summary="Analysis of better-run patterns.",
        body="Full analysis body here.",
        topic="better-runs-blueprint",
    )
    reaper_db.add(doc)
    await reaper_db.commit()
    # Blocked dependent: kind='task' need referencing the source task's finding_hash.
    t = await _blocked_task(
        reaper_db,
        project,
        blocker_kind="upstream_dependency",
        blocked_reason="BLOCKER: needs the research finding",
        depends_on=[],
        context={
            "blocker": {
                "source": "agent",
                "waiting_on": "bfb447f23b6d44f6",
                "reason": "BLOCKER: needs the research finding",
                "need": {"kind": "task", "ref": "bfb447f23b6d44f6"},
            }
        },
        recheck_after=now - timedelta(minutes=1),
    )

    requeued = await sweep_unblock_blocked_tasks(reaper_db, now)

    assert requeued == 1
    await reaper_db.refresh(t)
    # Resumed via knowledge-document satisfaction — not cancelled.
    assert t.status == TaskStatus.QUEUED
    # No edge wired (knowledge satisfaction has no live foundation).
    assert not (t.depends_on or [])
    # Blocker metadata cleared.
    assert "blocker" not in (t.context or {})


@pytest.mark.asyncio
async def test_unblock_sweep_does_not_treat_ghost_merge_as_completed_foundation(
    reaper_db, mocker
):
    """A ghost merge — a child PR that merged into a rollup staging branch whose
    rollup parent later FAILED — never reached the integration branch, so it must
    NOT be treated as the completed/shipped foundation that satisfies a dependent
    (the dedup layer would re-spawn it). Instead of resuming edge-free against the
    unshipped code, the unmatched need falls through to the bounded optimistic
    retry exactly like any unsatisfied need — the blocker is retained, not cleared."""
    mocker.patch("app.broadcast.broadcast_to_project", new_callable=AsyncMock)
    project = await _make_project(reaper_db)
    now = _utcnow()
    # Rollup parent FAILED — its children's staging merges never reached main.
    parent = Task(
        project_id=project.id,
        title="rollup-parent",
        description="x",
        status=TaskStatus.FAILED,
        created_by="u",
        requires_approval=False,
        payloads=[{"type": "create"}],
        context={},
        status_log=[],
    )
    reaper_db.add(parent)
    await reaper_db.commit()
    await reaper_db.refresh(parent)
    # Child foundation: merged onto the staging branch (pr_merged=True) but its
    # parent rollup failed → ghost merge.
    ghost = Task(
        project_id=project.id,
        title="ghost-foundation",
        description="x",
        status=TaskStatus.COMPLETED,
        created_by="u",
        requires_approval=False,
        payloads=[{"type": "create"}],
        context={"pr_merged": True},
        parent_task_id=parent.id,
        status_log=[],
    )
    reaper_db.add(ghost)
    await reaper_db.commit()
    await reaper_db.refresh(ghost)
    t = await _blocked_task(
        reaper_db,
        project,
        blocker_kind="upstream_dependency",
        blocked_reason="BLOCKER: waiting on foundation",
        depends_on=[],
        context={
            "blocker": {
                "source": "agent",
                "waiting_on": "foundation",
                "reason": "BLOCKER: waiting on foundation",
                "need": {"kind": "task", "ref": str(ghost.id)},
            }
        },
        recheck_after=now - timedelta(minutes=1),
    )

    await sweep_unblock_blocked_tasks(reaper_db, now)

    await reaper_db.refresh(t)
    # NOT resolved as a satisfied completed foundation: no edge wired and no
    # blind retry is admitted for the unchanged missing provider.
    assert not (t.depends_on or [])
    assert "completed work" not in (t.latest_status_message or "").lower()
    assert t.status == TaskStatus.BLOCKED
    assert t.context.get("_blocked_optimistic_retries") is None


@pytest.mark.asyncio
async def test_unblock_sweep_resumes_when_provider_landed_in_base_lineage_before_rollup(
    reaper_db, mocker
):
    """LANDED-IN-BASE-LINEAGE (audit case 3): a COMPLETED, exact-title provider
    whose PR merged into the plan branch (merge-unblock signal) must resume its
    dependent even though the parent rollup has NOT yet integrated. A parent that
    has merely not-yet-rolled-up — here COMPLETED while its rollup PR to
    integration is still pending, so it carries no merge-unblock signal — must NOT
    strand the provider as a ghost merge: the provider's code is already on the
    plan branch in the dependent's base lineage. (Before the fix the
    completed-without-merge parent was treated as stranded and the dependent sat
    blocked indefinitely.)"""
    mocker.patch("app.broadcast.broadcast_to_project", new_callable=AsyncMock)
    project = await _make_project(reaper_db)
    now = _utcnow()
    # Plan/rollup parent: COMPLETED but its rollup PR to integration is still
    # pending, so it carries NO merge-unblock signal. It is ALIVE, not dead.
    parent = Task(
        project_id=project.id,
        title="plan-rollup",
        description="x",
        status=TaskStatus.COMPLETED,
        created_by="u",
        requires_approval=False,
        payloads=[{"type": "create"}],
        context={},
        status_log=[],
    )
    reaper_db.add(parent)
    await reaper_db.commit()
    await reaper_db.refresh(parent)
    # Provider: merged onto the plan branch (merge-unblock signal, not yet landed
    # on integration), parented under the not-yet-rolled-up plan.
    provider = Task(
        project_id=project.id,
        title="feat: foundation",
        description="x",
        status=TaskStatus.COMPLETED,
        created_by="u",
        requires_approval=False,
        payloads=[{"type": "create"}],
        context={
            "pr_merged": True,
            "landed_on_integration": False,
            "_non_merge_unblock_reason": "merged_pr_non_integration_branch",
        },
        parent_task_id=parent.id,
        status_log=[],
    )
    reaper_db.add(provider)
    await reaper_db.commit()
    await reaper_db.refresh(provider)
    t = await _blocked_task(
        reaper_db,
        project,
        blocker_kind="upstream_dependency",
        blocked_reason="BLOCKER: waiting on foundation",
        depends_on=[],
        context={
            "blocker": {
                "source": "agent",
                "waiting_on": "foundation",
                "reason": "BLOCKER: waiting on foundation",
                "need": {"kind": "task", "ref": str(provider.id)},
            }
        },
        recheck_after=now - timedelta(minutes=1),
    )

    requeued = await sweep_unblock_blocked_tasks(reaper_db, now)

    assert requeued == 1
    await reaper_db.refresh(t)
    assert t.status == TaskStatus.QUEUED
    # Resumed via completed (base-lineage-landed) work — no edge wired.
    assert not (t.depends_on or [])
    assert "blocker" not in (t.context or {})
    assert "completed work" in (t.latest_status_message or "").lower()


@pytest.mark.asyncio
async def test_unblock_sweep_escalates_structurally_unobservable_need_without_rearm(
    reaper_db, mocker
):
    """BOUNDED-OUTCOME LIVENESS: a residual need the sweep can prove is
    structurally unobservable — here an observation-class ("obs:") finding ref no
    task can ever provide — must reach a terminal disposition IMMEDIATELY rather
    than re-arm the recheck for another indefinite loop. The dependent is
    escalate-and-cancelled on the FIRST sweep with a durable never-satisfiable
    marker; it never sits re-armed."""
    from app.reaper_sweeps import (
        UNRESUMABLE_CANCEL_CONTEXT_KEY,
        GIVE_UP_NEVER_SATISFIABLE,
    )

    mocker.patch("app.broadcast.broadcast_to_project", new_callable=AsyncMock)
    project = await _make_project(reaper_db)
    now = _utcnow()
    # Observation-class need: a valid finding-hash ref paired with an "obs:" label.
    # Observation findings never spawn a provider task, so no foundation can ever
    # exist — re-arming the recheck would loop forever.
    obs_need = {
        "kind": "task",
        "ref": "deadbeefcafe0001",
        "label": "obs: lane substrate layers a new system over pool/park-wake",
    }
    t = await _blocked_task(
        reaper_db,
        project,
        blocker_kind="upstream_dependency",
        blocked_reason="BLOCKER: waiting on an observation",
        depends_on=[],
        context={
            "direction_id": "d-obs1",
            "blocker": {
                "source": "agent",
                "waiting_on": "observation",
                "reason": "BLOCKER: waiting on an observation",
                "need": obs_need,
            },
        },
        recheck_after=now - timedelta(minutes=1),
    )

    requeued = await sweep_unblock_blocked_tasks(reaper_db, now)

    assert requeued == 0
    await reaper_db.refresh(t)
    # Terminal disposition on the FIRST sweep — never re-armed.
    assert t.status == TaskStatus.CANCELLED
    assert "structurally unobservable" in (t.latest_status_message or "").lower()
    marker = (t.context or {}).get(UNRESUMABLE_CANCEL_CONTEXT_KEY)
    assert isinstance(marker, dict)
    assert marker["give_up_class"] == GIVE_UP_NEVER_SATISFIABLE
    # No recheck re-armed into the future: the loop is closed by construction.
    assert not (t.context or {}).get("_blocked_recheck_attempts")


async def _cross_base_completed_foundation(db, project, *, landed=True):
    """A COMPLETED code-producing foundation on the integration trunk whose
    related files overlap a dependent's, so it is a genuine cross-base code dep."""
    ctx = {"related_files": ["app/foo.py"]}
    if landed:
        ctx["landed_on_integration"] = True
        ctx["pr_merged"] = True
    foundation = Task(
        project_id=project.id,
        title="foundation",
        description="x",
        status=TaskStatus.COMPLETED,
        created_by="u",
        requires_approval=False,
        payloads=[{"type": "create", "base_branch": "main"}],
        context=ctx,
        status_log=[],
    )
    db.add(foundation)
    await db.commit()
    await db.refresh(foundation)
    return foundation


async def _cross_base_blocked_dependent(db, project, foundation, now):
    """A blocked dependent on its OWN rollup base (≠ integration) whose need names
    the completed foundation, with no dependency edge wired yet."""
    return await _blocked_task(
        db,
        project,
        blocker_kind="upstream_dependency",
        blocked_reason="BLOCKER: waiting on foundation",
        depends_on=[],
        payloads=[{"type": "fix", "base_branch": "rollup/dep"}],
        context={
            "related_files": ["app/foo.py"],
            "blocker": {
                "source": "agent",
                "waiting_on": "foundation",
                "reason": "BLOCKER: waiting on foundation",
                "need": {"kind": "task", "ref": str(foundation.id)},
            },
        },
        recheck_after=now - timedelta(minutes=1),
    )


@pytest.mark.asyncio
async def test_unblock_sweep_recuts_base_when_cross_base_foundation_landed(
    reaper_db, mocker
):
    """A blocked dependent resuming because its CROSS-BASE genuine code foundation
    already landed must have its base re-cut from integration BEFORE it leaves
    BLOCKED — otherwise it dispatches against a stale base missing the landed code."""
    mocker.patch("app.broadcast.broadcast_to_project", new_callable=AsyncMock)
    token = mocker.patch(
        "app.routers.agent.get_github_token_for_task",
        new_callable=AsyncMock,
        return_value=("tok", "owner", "repo"),
    )
    merge = mocker.patch(
        "app.github.merge_branch",
        new_callable=AsyncMock,
        return_value={"merged": True},
    )
    project = await _make_project(reaper_db)
    now = _utcnow()
    foundation = await _cross_base_completed_foundation(reaper_db, project)
    t = await _cross_base_blocked_dependent(reaper_db, project, foundation, now)

    requeued = await sweep_unblock_blocked_tasks(reaper_db, now)

    assert requeued == 1
    # The shared re-cut helper merged integration into the dependent's base.
    assert token.await_count == 1
    assert merge.await_count == 1
    await reaper_db.refresh(t)
    assert t.status == TaskStatus.QUEUED
    assert not (t.depends_on or [])


@pytest.mark.asyncio
async def test_unblock_sweep_same_base_completed_foundation_skips_recut(
    reaper_db, mocker
):
    """A SAME-BASE completed foundation needs no re-cut — the dependent resumes
    exactly as before and no merge is attempted (no false serialization)."""
    mocker.patch("app.broadcast.broadcast_to_project", new_callable=AsyncMock)
    merge = mocker.patch(
        "app.github.merge_branch",
        new_callable=AsyncMock,
        return_value={"merged": True},
    )
    project = await _make_project(reaper_db)
    now = _utcnow()
    # Foundation and dependent share the same (default integration) base branch.
    foundation = Task(
        project_id=project.id,
        title="foundation",
        description="x",
        status=TaskStatus.COMPLETED,
        created_by="u",
        requires_approval=False,
        payloads=[{"type": "create"}],
        context={
            "related_files": ["app/foo.py"],
            "landed_on_integration": True,
            "pr_merged": True,
        },
        status_log=[],
    )
    reaper_db.add(foundation)
    await reaper_db.commit()
    await reaper_db.refresh(foundation)
    t = await _blocked_task(
        reaper_db,
        project,
        blocker_kind="upstream_dependency",
        blocked_reason="BLOCKER: waiting on foundation",
        depends_on=[],
        context={
            "related_files": ["app/foo.py"],
            "blocker": {
                "source": "agent",
                "waiting_on": "foundation",
                "reason": "BLOCKER: waiting on foundation",
                "need": {"kind": "task", "ref": str(foundation.id)},
            },
        },
        recheck_after=now - timedelta(minutes=1),
    )

    requeued = await sweep_unblock_blocked_tasks(reaper_db, now)

    assert requeued == 1
    merge.assert_not_awaited()
    await reaper_db.refresh(t)
    assert t.status == TaskStatus.QUEUED


@pytest.mark.asyncio
async def test_unblock_sweep_holds_dependent_when_recut_conflicts(reaper_db, mocker):
    """When the cross-base re-cut CONFLICTS, the dependent is HELD (kept BLOCKED,
    re-armed for next cycle) rather than resumed against an un-recut base."""
    mocker.patch("app.broadcast.broadcast_to_project", new_callable=AsyncMock)
    mocker.patch(
        "app.routers.agent.get_github_token_for_task",
        new_callable=AsyncMock,
        return_value=("tok", "owner", "repo"),
    )
    mocker.patch(
        "app.github.merge_branch",
        new_callable=AsyncMock,
        return_value={"conflict": True},
    )
    project = await _make_project(reaper_db)
    now = _utcnow()
    foundation = await _cross_base_completed_foundation(reaper_db, project)
    t = await _cross_base_blocked_dependent(reaper_db, project, foundation, now)

    requeued = await sweep_unblock_blocked_tasks(reaper_db, now)

    assert requeued == 0
    await reaper_db.refresh(t)
    # Held: still BLOCKED, never resumed, and re-armed for a later recheck.
    assert t.status == TaskStatus.BLOCKED
    assert "blocker" in (t.context or {})
    assert t.recheck_after is not None and t.recheck_after > now


async def _elapse_recut_backoff(session, task):
    """Simulate the cross-base re-cut's backed-off recheck window having ELAPSED.

    ``ensure_dependent_base_recut`` now short-circuits (issuing NO GitHub merge
    call) while ``_recut_recheck_after`` is still in the future, for EVERY failing
    outcome — that is the amplifier fix: the merge POST used to be re-issued on
    every dispatch/reaper cycle for the transient class. These sweeps simulate the
    passage of time by advancing the ``now`` they are called with, but the re-cut
    backoff reads wall-clock, so a test that wants the NEXT cycle to make a real
    attempt must lapse the window explicitly.
    """
    ctx = dict(task.context or {})
    if not ctx.get("_recut_recheck_after"):
        return
    ctx["_recut_recheck_after"] = (_utcnow() - timedelta(hours=1)).isoformat()
    task.context = ctx
    session.add(task)
    await session.commit()


@pytest.mark.asyncio
async def test_unblock_sweep_persistent_recut_conflict_accumulates_and_escalates(
    reaper_db, mocker
):
    """Regression: the blocked-resume HOLD must PRESERVE the ``_recut_*`` escalation
    accounting that ``ensure_dependent_base_recut`` committed, so a persistently-
    conflicting cross-base dependent eventually escalates instead of resetting its
    consecutive-conflict counter every cycle and parking invisibly forever.

    Previously the caller reassigned ``task.context`` to a stale loop-local dict
    captured before the re-cut helper replaced+committed the context, wiping the
    ``_recut_conflict_attempts`` counter so it never reached the escalation bound.
    """
    from app.orchestrator_health import _RECUT_CONFLICT_ESCALATION_THRESHOLD

    mocker.patch("app.broadcast.broadcast_to_project", new_callable=AsyncMock)
    mocker.patch(
        "app.routers.agent.get_github_token_for_task",
        new_callable=AsyncMock,
        return_value=("tok", "owner", "repo"),
    )
    mocker.patch(
        "app.github.merge_branch",
        new_callable=AsyncMock,
        return_value={"conflict": True},
    )
    project = await _make_project(reaper_db)
    base_now = _utcnow()
    foundation = await _cross_base_completed_foundation(reaper_db, project)
    t = await _cross_base_blocked_dependent(reaper_db, project, foundation, base_now)

    # Run enough consecutive conflicting cycles to cross the escalation bound. Each
    # cycle advances `now` past the backed-off recheck_after so the task re-selects.
    for i in range(_RECUT_CONFLICT_ESCALATION_THRESHOLD):
        await reaper_db.refresh(t)
        cycle_now = (t.recheck_after or base_now) + timedelta(minutes=1)
        # The re-cut's own backed-off window reads wall-clock, so lapse it to match
        # the simulated cycle_now — otherwise the re-cut is (correctly) suppressed.
        await _elapse_recut_backoff(reaper_db, t)
        await sweep_unblock_blocked_tasks(reaper_db, cycle_now)
        await reaper_db.refresh(t)
        # The consecutive-conflict counter ACCUMULATES across cycles (it is not
        # wiped by the stale-ctx clobber the regression guards against).
        assert t.context.get("_recut_conflict_attempts") == i + 1
        assert t.status == TaskStatus.BLOCKED

    # On reaching the bound the dependent is escalated exactly once with an
    # operator-facing marker — it no longer parks invisibly forever.
    assert t.context.get("_recut_escalated") is True
    assert (
        t.context.get("_recut_conflict_attempts")
        >= _RECUT_CONFLICT_ESCALATION_THRESHOLD
    )


@pytest.mark.asyncio
async def test_unblock_sweep_base_safe_sibling_need_does_not_wipe_conflict_accounting(
    reaper_db, mocker
):
    """Multi-need regression: a held dependent with TWO completed-foundation needs
    whose re-cut outcomes differ — one base-safe no-op, one persistently
    conflicting cross-base code dep — must still accumulate and escalate the
    conflicting need's ``_recut_*`` accounting.

    Previously each completed need was re-cut one foundation at a time, so the
    base-safe sibling's ``_clear_recut_accounting`` wiped the conflicting need's
    consecutive-conflict counter every tick — it never reached the escalation
    bound and the dependent parked invisibly forever.
    """
    from app.orchestrator_health import _RECUT_CONFLICT_ESCALATION_THRESHOLD

    mocker.patch("app.broadcast.broadcast_to_project", new_callable=AsyncMock)
    mocker.patch(
        "app.routers.agent.get_github_token_for_task",
        new_callable=AsyncMock,
        return_value=("tok", "owner", "repo"),
    )
    mocker.patch(
        "app.github.merge_branch",
        new_callable=AsyncMock,
        return_value={"conflict": True},
    )
    project = await _make_project(reaper_db)
    base_now = _utcnow()
    # Conflicting cross-base landed foundation (base=main, dependent is rollup/dep).
    conflicting = await _cross_base_completed_foundation(reaper_db, project)
    # Base-safe sibling: SAME base as the dependent → no re-cut needed → its
    # resolution is a clean no-op that must NOT clear the conflicting accounting.
    base_safe = Task(
        project_id=project.id,
        title="base-safe-foundation",
        description="x",
        status=TaskStatus.COMPLETED,
        created_by="u",
        requires_approval=False,
        payloads=[{"type": "create", "base_branch": "rollup/dep"}],
        context={
            "related_files": ["app/bar.py"],
            "landed_on_integration": True,
            "pr_merged": True,
        },
        status_log=[],
    )
    reaper_db.add(base_safe)
    await reaper_db.commit()
    await reaper_db.refresh(base_safe)

    t = await _blocked_task(
        reaper_db,
        project,
        blocker_kind="upstream_dependency",
        blocked_reason="BLOCKER: waiting on foundations",
        depends_on=[],
        payloads=[{"type": "fix", "base_branch": "rollup/dep"}],
        context={
            "related_files": ["app/foo.py"],
            "blocker": {
                "source": "agent",
                "waiting_on": "foundations",
                "reason": "BLOCKER: waiting on foundations",
                "needs": [
                    {"kind": "task", "ref": str(conflicting.id)},
                    {"kind": "task", "ref": str(base_safe.id)},
                ],
            },
        },
        recheck_after=base_now - timedelta(minutes=1),
    )

    # First tick: the base-safe sibling resolves (dropped), but it must NOT wipe
    # the conflicting need's freshly-recorded conflict counter.
    await sweep_unblock_blocked_tasks(reaper_db, base_now)
    await reaper_db.refresh(t)
    assert t.status == TaskStatus.BLOCKED
    assert t.context.get("_recut_conflict_attempts") == 1

    # Remaining ticks: the conflicting need keeps accumulating and crosses the
    # escalation bound — the dependent is no longer parked invisibly forever.
    for i in range(1, _RECUT_CONFLICT_ESCALATION_THRESHOLD):
        await reaper_db.refresh(t)
        cycle_now = (t.recheck_after or base_now) + timedelta(minutes=1)
        # Lapse the wall-clock re-cut backoff to match the simulated cycle_now.
        await _elapse_recut_backoff(reaper_db, t)
        await sweep_unblock_blocked_tasks(reaper_db, cycle_now)
        await reaper_db.refresh(t)
        assert t.context.get("_recut_conflict_attempts") == i + 1
        assert t.status == TaskStatus.BLOCKED

    assert t.context.get("_recut_escalated") is True
    assert (
        t.context.get("_recut_conflict_attempts")
        >= _RECUT_CONFLICT_ESCALATION_THRESHOLD
    )


@pytest.mark.asyncio
async def test_parked_blocker_holds_when_cross_base_recut_conflicts(reaper_db, mocker):
    """The parked-resume path applies the SAME cross-base re-cut guard: a conflict
    keeps the parked blocker BLOCKED instead of resuming it against a stale base."""
    mocker.patch("app.broadcast.broadcast_to_project", new_callable=AsyncMock)
    mocker.patch(
        "app.routers.agent.get_github_token_for_task",
        new_callable=AsyncMock,
        return_value=("tok", "owner", "repo"),
    )
    mocker.patch(
        "app.github.merge_branch",
        new_callable=AsyncMock,
        return_value={"conflict": True},
    )
    project = await _make_project(reaper_db)
    now = _utcnow()
    foundation = await _cross_base_completed_foundation(reaper_db, project)
    t = await _parked_blocker(
        reaper_db,
        project,
        need={"kind": "task", "ref": str(foundation.id)},
        now=now,
    )
    # Give the parked dependent its own rollup base + overlapping files so the
    # completed foundation is a genuine cross-base code dependency.
    t.payloads = [{"type": "fix", "base_branch": "rollup/dep"}]
    t.context = {**(t.context or {}), "related_files": ["app/foo.py"]}
    reaper_db.add(t)
    await reaper_db.commit()

    requeued = await sweep_unblock_blocked_tasks(reaper_db, now)

    assert requeued == 0
    await reaper_db.refresh(t)
    assert t.status == TaskStatus.BLOCKED
    assert t.context.get("_blocked_escalated") is True


@pytest.mark.asyncio
async def test_unblock_sweep_cancels_when_needed_foundation_terminally_dead(
    reaper_db, mocker
):
    """A blocked dependent whose need names a foundation that has reached a
    terminal FAILED state — with no edge ever wired — can never resume. It is
    cancelled the same way the dispatch path cascade-cancels an edged dead
    dependency, rather than parking to re-arm forever."""
    mocker.patch("app.broadcast.broadcast_to_project", new_callable=AsyncMock)
    project = await _make_project(reaper_db)
    now = _utcnow()
    # The named foundation died before any depends_on edge was wired.
    dead = Task(
        project_id=project.id,
        title="dead-foundation",
        description="x",
        status=TaskStatus.FAILED,
        created_by="u",
        requires_approval=False,
        payloads=[{"type": "create"}],
        context={},
        status_log=[],
    )
    reaper_db.add(dead)
    await reaper_db.commit()
    await reaper_db.refresh(dead)
    t = await _blocked_task(
        reaper_db,
        project,
        blocker_kind="upstream_dependency",
        blocked_reason="BLOCKER: waiting on foundation",
        depends_on=[],
        context={
            "blocker": {
                "source": "agent",
                "waiting_on": "foundation",
                "reason": "BLOCKER: waiting on foundation",
                "need": {"kind": "task", "ref": str(dead.id)},
            }
        },
        recheck_after=now - timedelta(minutes=1),
    )

    requeued = await sweep_unblock_blocked_tasks(reaper_db, now)

    assert requeued == 0
    await reaper_db.refresh(t)
    # Bounded terminal outcome — not parked forever.
    assert t.status == TaskStatus.CANCELLED
    assert "terminally failed" in (t.latest_status_message or "").lower()
    # Blocker metadata cleared on the terminal transition.
    assert "blocker" not in (t.context or {})


@pytest.mark.asyncio
async def test_unblock_sweep_resolves_finding_hash_task_need_into_dep_edge(
    reaper_db, mocker
):
    """A {kind:'task'} need referencing a foundation's context['finding_hash']
    (not its UUID) resolves into a real depends_on edge — the resume matcher now
    honors the same finding-hash signal the research spawn path uses."""
    mocker.patch("app.broadcast.broadcast_to_project", new_callable=AsyncMock)
    project = await _make_project(reaper_db)
    now = _utcnow()
    foundation = Task(
        project_id=project.id,
        title="foundation",
        description="x",
        status=TaskStatus.IN_PROGRESS,
        created_by="u",
        requires_approval=False,
        payloads=[{"type": "create"}],
        context={"finding_hash": "abc123def4560000"},
        status_log=[],
    )
    reaper_db.add(foundation)
    await reaper_db.commit()
    await reaper_db.refresh(foundation)
    t = await _blocked_task(
        reaper_db,
        project,
        blocker_kind="precondition",
        blocked_reason="BLOCKER: needs the foundation finding first",
        depends_on=[],
        context={
            "blocker": {
                "source": "agent",
                "waiting_on": "foundation finding",
                "reason": "BLOCKER: needs the foundation finding first",
                "need": {"kind": "task", "ref": "abc123def4560000"},
            }
        },
        recheck_after=now - timedelta(minutes=1),
    )

    requeued = await sweep_unblock_blocked_tasks(reaper_db, now)

    assert requeued == 1
    await reaper_db.refresh(t)
    assert t.status == TaskStatus.QUEUED
    assert str(foundation.id) in (t.depends_on or [])
    assert "blocker" not in (t.context or {})


@pytest.mark.asyncio
async def test_unblock_sweep_resolves_capability_marker_need_into_dep_edge(
    reaper_db, mocker
):
    """A {kind:'capability'} need resolves onto the in-flight task that advertises
    the matching explicit provides_capability marker — and wires a dep edge."""
    mocker.patch("app.broadcast.broadcast_to_project", new_callable=AsyncMock)
    project = await _make_project(reaper_db)
    now = _utcnow()
    foundation = Task(
        project_id=project.id,
        title="schema work",
        description="x",
        status=TaskStatus.IN_PROGRESS,
        created_by="u",
        requires_approval=False,
        payloads=[{"type": "create"}],
        context={"provides_capability": "schema-models"},
        status_log=[],
    )
    reaper_db.add(foundation)
    await reaper_db.commit()
    await reaper_db.refresh(foundation)
    t = await _blocked_task(
        reaper_db,
        project,
        blocker_kind="precondition",
        blocked_reason="BLOCKER: needs the schema models",
        depends_on=[],
        context={
            "blocker": {
                "source": "agent",
                "waiting_on": "schema-models",
                "reason": "BLOCKER: needs the schema models",
                "need": {"kind": "capability", "ref": "schema-models"},
            }
        },
        recheck_after=now - timedelta(minutes=1),
    )

    requeued = await sweep_unblock_blocked_tasks(reaper_db, now)

    assert requeued == 1
    await reaper_db.refresh(t)
    assert t.status == TaskStatus.QUEUED
    assert str(foundation.id) in (t.depends_on or [])
    assert "blocker" not in (t.context or {})


@pytest.mark.asyncio
async def test_unblock_sweep_resolves_floor_stamped_capability_need_into_dep_edge(
    reaper_db, mocker
):
    """A non-research task's persisted floor marker resolves a blocked need.

    The insert listener must stamp the derived namespaced provider marker into
    context so future exact-match consumers can depend on any task type without
    per-path wiring.
    """
    mocker.patch("app.broadcast.broadcast_to_project", new_callable=AsyncMock)
    project = await _make_project(reaper_db)
    now = _utcnow()
    foundation = Task(
        project_id=project.id,
        title="build schema models foundation",
        description="x",
        status=TaskStatus.IN_PROGRESS,
        created_by="u",
        requires_approval=False,
        payloads=[{"type": "plan"}],
        context={},
        status_log=[],
    )
    floor_marker = derive_provider_marker(foundation)
    assert floor_marker is not None
    reaper_db.add(foundation)
    await reaper_db.commit()
    await reaper_db.refresh(foundation)
    assert floor_marker in (
        [foundation.context["provides_capability"]]
        if isinstance(foundation.context.get("provides_capability"), str)
        else foundation.context.get("provides_capability", [])
    )

    t = await _blocked_task(
        reaper_db,
        project,
        blocker_kind="precondition",
        blocked_reason="BLOCKER: needs the exact foundation task",
        depends_on=[],
        context={
            "blocker": {
                "source": "agent",
                "waiting_on": floor_marker,
                "reason": "BLOCKER: needs the exact foundation task",
                "need": {"kind": "capability", "ref": floor_marker},
            }
        },
        recheck_after=now - timedelta(minutes=1),
    )

    requeued = await sweep_unblock_blocked_tasks(reaper_db, now)

    assert requeued == 1
    await reaper_db.refresh(t)
    assert t.status == TaskStatus.QUEUED
    assert str(foundation.id) in (t.depends_on or [])
    assert "blocker" not in (t.context or {})


@pytest.mark.asyncio
async def test_unblock_sweep_resolves_legacy_capability_description_need_into_dep_edge(
    reaper_db, mocker
):
    """A legacy free-text blocked capability need still resolves when it exactly
    matches the foundation title advertised by ``./blocked --needs-capability``."""
    mocker.patch("app.broadcast.broadcast_to_project", new_callable=AsyncMock)
    project = await _make_project(reaper_db)
    now = _utcnow()
    foundation = Task(
        project_id=project.id,
        title="shared auth layer",
        description="x",
        status=TaskStatus.IN_PROGRESS,
        created_by="u",
        requires_approval=False,
        payloads=[{"type": "create"}],
        context={},
        status_log=[],
    )
    reaper_db.add(foundation)
    await reaper_db.commit()
    await reaper_db.refresh(foundation)
    t = await _blocked_task(
        reaper_db,
        project,
        blocker_kind="precondition",
        blocked_reason="BLOCKER: waiting on shared auth layer",
        depends_on=[],
        context={
            "blocker": {
                "source": "agent",
                "waiting_on": "shared auth layer",
                "reason": "BLOCKER: waiting on shared auth layer",
                "need": {"kind": "capability", "ref": "shared auth layer"},
            }
        },
        recheck_after=now - timedelta(minutes=1),
    )

    requeued = await sweep_unblock_blocked_tasks(reaper_db, now)

    assert requeued == 1
    await reaper_db.refresh(t)
    assert t.status == TaskStatus.QUEUED
    assert str(foundation.id) in (t.depends_on or [])
    assert "blocker" not in (t.context or {})


@pytest.mark.asyncio
async def test_unblock_sweep_resumes_when_finding_hash_need_satisfied_by_completed(
    reaper_db, mocker
):
    """A finding-hash {kind:'task'} need satisfied by already-COMPLETED work
    resumes edge-free on the routine sweep."""
    mocker.patch("app.broadcast.broadcast_to_project", new_callable=AsyncMock)
    project = await _make_project(reaper_db)
    now = _utcnow()
    foundation = Task(
        project_id=project.id,
        title="foundation",
        description="x",
        status=TaskStatus.COMPLETED,
        created_by="u",
        requires_approval=False,
        payloads=[{"type": "create"}],
        context={"finding_hash": "0011223344556677", "pr_merged": True},
        status_log=[],
    )
    reaper_db.add(foundation)
    await reaper_db.commit()
    await reaper_db.refresh(foundation)
    t = await _blocked_task(
        reaper_db,
        project,
        blocker_kind="upstream_dependency",
        blocked_reason="BLOCKER: waiting on foundation finding",
        depends_on=[],
        context={
            "blocker": {
                "source": "agent",
                "waiting_on": "foundation finding",
                "reason": "BLOCKER: waiting on foundation finding",
                "need": {"kind": "task", "ref": "0011223344556677"},
            }
        },
        recheck_after=now - timedelta(minutes=1),
    )

    requeued = await sweep_unblock_blocked_tasks(reaper_db, now)

    assert requeued == 1
    await reaper_db.refresh(t)
    assert t.status == TaskStatus.QUEUED
    assert not (t.depends_on or [])
    assert "blocker" not in (t.context or {})
    assert "completed work" in (t.latest_status_message or "").lower()


@pytest.mark.asyncio
async def test_unblock_sweep_cancels_when_capability_foundation_terminally_dead(
    reaper_db, mocker
):
    """A {kind:'capability'} need matched ONLY to a terminally FAILED foundation
    (by explicit marker) is cancelled rather than parked to re-arm forever."""
    mocker.patch("app.broadcast.broadcast_to_project", new_callable=AsyncMock)
    project = await _make_project(reaper_db)
    now = _utcnow()
    dead = Task(
        project_id=project.id,
        title="dead-foundation",
        description="x",
        status=TaskStatus.FAILED,
        created_by="u",
        requires_approval=False,
        payloads=[{"type": "create"}],
        context={"capability": "schema-models"},
        status_log=[],
    )
    reaper_db.add(dead)
    await reaper_db.commit()
    await reaper_db.refresh(dead)
    t = await _blocked_task(
        reaper_db,
        project,
        blocker_kind="upstream_dependency",
        blocked_reason="BLOCKER: waiting on schema models",
        depends_on=[],
        context={
            "blocker": {
                "source": "agent",
                "waiting_on": "schema-models",
                "reason": "BLOCKER: waiting on schema models",
                "need": {"kind": "capability", "ref": "schema-models"},
            }
        },
        recheck_after=now - timedelta(minutes=1),
    )

    requeued = await sweep_unblock_blocked_tasks(reaper_db, now)

    assert requeued == 0
    await reaper_db.refresh(t)
    assert t.status == TaskStatus.CANCELLED
    assert "terminally failed" in (t.latest_status_message or "").lower()
    assert "blocker" not in (t.context or {})


@pytest.mark.asyncio
async def test_unblock_sweep_cancels_never_provided_capability_after_reeval_cap(
    reaper_db, mocker
):
    """A parked blocker whose capability need is never advertised by any task is
    given a bounded number of parked re-evaluations, then terminated (CANCELLED)
    instead of re-arming the low-cadence re-evaluation indefinitely."""
    mocker.patch("app.broadcast.broadcast_to_project", new_callable=AsyncMock)
    project = await _make_project(reaper_db)
    now = _utcnow()
    # Already parked (escalated), one re-eval short of the cap, and the capability
    # has never been (and is not now) advertised by any task.
    t = await _blocked_task(
        reaper_db,
        project,
        blocker_kind="upstream_dependency",
        blocked_reason="BLOCKER: needs an auth subsystem",
        depends_on=[],
        blocked_at=now - timedelta(hours=30),
        context={
            "blocker": {
                "source": "agent",
                "waiting_on": "auth subsystem",
                "reason": "BLOCKER: needs an auth subsystem",
                "need": {"kind": "capability", "ref": "nonexistent-capability"},
            },
            "_blocked_escalated": True,
            "_blocked_parked_reevals": _BLOCKED_PARKED_REEVAL_CAP - 1,
        },
        recheck_after=now - timedelta(minutes=1),
    )

    requeued = await sweep_unblock_blocked_tasks(reaper_db, now)

    assert requeued == 0
    await reaper_db.refresh(t)
    assert t.status == TaskStatus.CANCELLED
    assert "never became satisfiable" in (t.latest_status_message or "").lower()


@pytest.mark.asyncio
async def test_dead_foundation_cancel_stamps_unresumable_marker(reaper_db, mocker):
    """The foundation-died terminal cancel stamps a durable, queryable marker
    recording the give-up class, the unresolved need, and the originating
    direction — so the give-up is countable fleet-wide after the structured
    blocker metadata is cleared on the same transition."""
    from app.reaper_sweeps import (
        UNRESUMABLE_CANCEL_CONTEXT_KEY,
        GIVE_UP_FOUNDATION_DIED,
    )

    mocker.patch("app.broadcast.broadcast_to_project", new_callable=AsyncMock)
    project = await _make_project(reaper_db)
    now = _utcnow()
    dead = Task(
        project_id=project.id,
        title="dead-foundation",
        description="x",
        status=TaskStatus.FAILED,
        created_by="u",
        requires_approval=False,
        payloads=[{"type": "create"}],
        context={},
        status_log=[],
    )
    reaper_db.add(dead)
    await reaper_db.commit()
    await reaper_db.refresh(dead)
    need = {"kind": "task", "ref": str(dead.id)}
    t = await _blocked_task(
        reaper_db,
        project,
        blocker_kind="upstream_dependency",
        blocked_reason="BLOCKER: waiting on foundation",
        depends_on=[],
        context={
            "direction_id": "d-abc123",
            "blocker": {
                "source": "agent",
                "waiting_on": "foundation",
                "reason": "BLOCKER: waiting on foundation",
                "need": need,
            },
        },
        recheck_after=now - timedelta(minutes=1),
    )

    await sweep_unblock_blocked_tasks(reaper_db, now)

    await reaper_db.refresh(t)
    assert t.status == TaskStatus.CANCELLED
    marker = (t.context or {}).get(UNRESUMABLE_CANCEL_CONTEXT_KEY)
    assert isinstance(marker, dict)
    assert marker["give_up_class"] == GIVE_UP_FOUNDATION_DIED
    assert marker["need"] == need
    assert marker["direction_id"] == "d-abc123"
    assert isinstance(marker["at"], str) and marker["at"]


@pytest.mark.asyncio
async def test_co_deferred_shelved_foundation_not_dead_dependent_stays_blocked(
    reaper_db, mocker
):
    """A co-deferred (shelved) foundation is recoverable, NOT dead: a blocked
    dependent whose need matches it must stay BLOCKED-and-waiting (it re-surfaces
    with its prerequisite), it must NOT be cascade-cancelled as foundation-died,
    and the foundation_died give-up count must NOT be polluted by the shelving."""
    from app.failure_classification import CO_DEFERRED_SHELVED
    from app.reaper_sweeps import UNRESUMABLE_CANCEL_CONTEXT_KEY

    mocker.patch("app.broadcast.broadcast_to_project", new_callable=AsyncMock)
    project = await _make_project(reaper_db)
    now = _utcnow()
    # The dependent's foundation was co-deferred this cycle: CANCELLED + the
    # durable non-death marker, advertising a capability the dependent needs.
    shelved = Task(
        project_id=project.id,
        title="co-deferred-foundation",
        description="x",
        status=TaskStatus.CANCELLED,
        created_by="u",
        requires_approval=False,
        payloads=[{"type": "create"}],
        context={"provides_capability": "auth-tokens", CO_DEFERRED_SHELVED: True},
        status_log=[],
    )
    reaper_db.add(shelved)
    await reaper_db.commit()
    need = {"kind": "capability", "ref": "auth-tokens"}
    t = await _blocked_task(
        reaper_db,
        project,
        blocker_kind="upstream_dependency",
        blocked_reason="BLOCKER: waiting on foundation",
        depends_on=[],
        blocked_at=now - timedelta(hours=8),
        context={
            "direction_id": "d-abc123",
            "blocker": {
                "source": "agent",
                "waiting_on": "foundation",
                "reason": "BLOCKER: waiting on foundation",
                "need": need,
            },
            "_blocked_escalated": True,
            "_blocked_parked_reevals": 0,
        },
        recheck_after=now - timedelta(minutes=1),
    )

    await sweep_unblock_blocked_tasks(reaper_db, now)

    await reaper_db.refresh(t)
    # Stays blocked-and-waiting — not cascade-cancelled as a dead foundation.
    assert t.status == TaskStatus.BLOCKED
    # No foundation_died give-up was stamped, so the health summary is unpolluted.
    assert (t.context or {}).get(UNRESUMABLE_CANCEL_CONTEXT_KEY) is None


@pytest.mark.asyncio
async def test_never_satisfiable_cancel_stamps_unresumable_marker(reaper_db, mocker):
    """The never-satisfiable terminal cancel (parked re-eval cap reached) stamps
    the durable marker with the never_satisfiable give-up class and the need."""
    from app.reaper_sweeps import (
        UNRESUMABLE_CANCEL_CONTEXT_KEY,
        GIVE_UP_NEVER_SATISFIABLE,
    )

    mocker.patch("app.broadcast.broadcast_to_project", new_callable=AsyncMock)
    project = await _make_project(reaper_db)
    now = _utcnow()
    need = {"kind": "capability", "ref": "nonexistent-capability"}
    t = await _blocked_task(
        reaper_db,
        project,
        blocker_kind="upstream_dependency",
        blocked_reason="BLOCKER: needs an auth subsystem",
        depends_on=[],
        blocked_at=now - timedelta(hours=30),
        context={
            "direction_id": "d-deadbeef",
            "blocker": {
                "source": "agent",
                "waiting_on": "auth subsystem",
                "reason": "BLOCKER: needs an auth subsystem",
                "need": need,
            },
            "_blocked_escalated": True,
            "_blocked_parked_reevals": _BLOCKED_PARKED_REEVAL_CAP - 1,
        },
        recheck_after=now - timedelta(minutes=1),
    )

    await sweep_unblock_blocked_tasks(reaper_db, now)

    await reaper_db.refresh(t)
    assert t.status == TaskStatus.CANCELLED
    marker = (t.context or {}).get(UNRESUMABLE_CANCEL_CONTEXT_KEY)
    assert isinstance(marker, dict)
    assert marker["give_up_class"] == GIVE_UP_NEVER_SATISFIABLE
    assert marker["need"] == need
    assert marker["direction_id"] == "d-deadbeef"


async def _completed_provider(db, project, **kwargs):
    """A COMPLETED foundation task, unshipped unless the caller says otherwise."""
    defaults = {
        "project_id": project.id,
        "title": "auth-subsystem-provider",
        "description": "x",
        "status": TaskStatus.COMPLETED,
        "created_by": "test-user",
        "requires_approval": False,
        "payloads": [{"type": "create"}],
        "context": {},
        "status_log": [],
    }
    defaults.update(kwargs)
    t = Task(**defaults)
    db.add(t)
    await db.commit()
    await db.refresh(t)
    return t


def test_parked_reeval_cull_budget_pinned_at_24h():
    """LITERAL pin on the give-up budget. The attribution split must never be
    able to retime the cull: 6h x 4 parked re-evaluations = ~24h, unchanged."""
    from app.reaper_sweeps import (
        _BLOCKED_PARKED_REEVAL_CAP as _cap,
        _BLOCKED_PARKED_REEVAL_INTERVAL as _interval,
    )

    assert _interval == timedelta(hours=6)
    assert _cap == 4
    assert _interval * _cap == timedelta(hours=24)


@pytest.mark.asyncio
async def test_parked_giveup_names_unlanded_provider_not_missing_foundation(
    reaper_db, mocker
):
    """The give-up must NOT say "no task ever provided it" when a task DID.

    Incident shape: the prerequisite was built - a task ran, COMPLETED and opened
    a PR - but the PR never merged (its review loop is parked for operator
    disposition), so the shipped gate correctly refuses to satisfy the need and
    the dependent is culled at the cap. The old reason sent an operator hunting
    for a task that does not exist. The reason must instead name the providing
    task and its PR, and must not be stamped as missing-foundation demand.
    """
    from app.reaper_sweeps import (
        GIVE_UP_PRODUCED_BUT_UNLANDED,
        MISSING_FOUNDATION_MARKER_KEY,
        UNRESUMABLE_CANCEL_CONTEXT_KEY,
    )

    mocker.patch("app.broadcast.broadcast_to_project", new_callable=AsyncMock)
    project = await _make_project(reaper_db)
    now = _utcnow()
    provider = await _completed_provider(
        reaper_db,
        project,
        title="fix(size): attribute merge cap breaches",
        payloads=[{"type": "fix", "pr_number": 17707}],
        context={},  # no pr_merged -> the PR never merged
    )
    need = {"kind": "task", "ref": str(provider.id)}
    t = await _blocked_task(
        reaper_db,
        project,
        blocker_kind="upstream_dependency",
        blocked_reason="BLOCKER: waiting on the size-cap fix",
        blocked_at=now - timedelta(hours=30),
        context={
            "direction_id": "d-deadbeef",
            "blocker": {
                "source": "agent",
                "waiting_on": "size-cap fix",
                "reason": "BLOCKER: waiting on the size-cap fix",
                "need": need,
            },
            "_blocked_escalated": True,
            "_blocked_parked_reevals": _BLOCKED_PARKED_REEVAL_CAP - 1,
        },
        recheck_after=now - timedelta(minutes=1),
    )

    requeued = await sweep_unblock_blocked_tasks(reaper_db, now)

    assert requeued == 0
    await reaper_db.refresh(t)
    # The cull itself is unchanged - only the attribution.
    assert t.status == TaskStatus.CANCELLED
    message = t.latest_status_message or ""
    assert "PR #17707" in message
    assert "never merged" in message.lower()
    assert "fix(size): attribute merge cap breaches" in message
    # The false claim must be gone from every operator-facing surface.
    assert "no task ever provided it" not in message.lower()
    assert "never became satisfiable" not in message.lower()

    marker = (t.context or {}).get(UNRESUMABLE_CANCEL_CONTEXT_KEY)
    assert isinstance(marker, dict)
    assert marker["give_up_class"] == GIVE_UP_PRODUCED_BUT_UNLANDED
    assert marker["provider_task_id"] == str(provider.id)
    assert marker["unshipped_reason"] == "pr_never_merged"
    assert marker["need"] == need
    assert marker["direction_id"] == "d-deadbeef"
    # A foundation that WAS built is not demand to commission it again.
    assert MISSING_FOUNDATION_MARKER_KEY not in marker


@pytest.mark.asyncio
async def test_parked_giveup_names_ghost_merge_rollup(reaper_db, mocker):
    """Second unshipped sub-class: the provider's PR DID merge, but only into a
    rollup staging branch whose parent then died, so the code never reached
    integration. The reason must say that - not "no task ever provided it", and
    not "the PR never merged" (it did)."""
    from app.reaper_sweeps import (
        GIVE_UP_PRODUCED_BUT_UNLANDED,
        UNRESUMABLE_CANCEL_CONTEXT_KEY,
    )

    mocker.patch("app.broadcast.broadcast_to_project", new_callable=AsyncMock)
    project = await _make_project(reaper_db)
    now = _utcnow()
    parent = Task(
        project_id=project.id,
        title="abandoned-rollup-parent",
        description="x",
        status=TaskStatus.CANCELLED,
        created_by="test-user",
        requires_approval=False,
        payloads=[{"type": "rollup"}],
        context={},
        status_log=[],
    )
    reaper_db.add(parent)
    await reaper_db.commit()
    await reaper_db.refresh(parent)
    provider = await _completed_provider(
        reaper_db,
        project,
        title="feat: staging-only child",
        payloads=[{"type": "create", "pr_number": 4242}],
        context={"pr_merged": True},
        parent_task_id=parent.id,
    )
    need = {"kind": "task", "ref": str(provider.id)}
    t = await _blocked_task(
        reaper_db,
        project,
        blocker_kind="upstream_dependency",
        blocked_reason="BLOCKER: waiting on the staging-only child",
        blocked_at=now - timedelta(hours=30),
        context={
            "blocker": {
                "source": "agent",
                "waiting_on": "staging-only child",
                "reason": "BLOCKER: waiting on the staging-only child",
                "need": need,
            },
            "_blocked_escalated": True,
            "_blocked_parked_reevals": _BLOCKED_PARKED_REEVAL_CAP - 1,
        },
        recheck_after=now - timedelta(minutes=1),
    )

    await sweep_unblock_blocked_tasks(reaper_db, now)

    await reaper_db.refresh(t)
    assert t.status == TaskStatus.CANCELLED
    message = (t.latest_status_message or "").lower()
    assert "pr #4242" in message
    assert "ghost-merged" in message or "ghost merge" in message
    assert "no task ever provided it" not in message
    marker = (t.context or {}).get(UNRESUMABLE_CANCEL_CONTEXT_KEY)
    assert marker["give_up_class"] == GIVE_UP_PRODUCED_BUT_UNLANDED
    assert marker["unshipped_reason"] == "ghost_merge_abandoned_rollup"
    assert marker["provider_task_id"] == str(provider.id)


@pytest.mark.asyncio
async def test_parked_giveup_keeps_unprovided_reason_when_no_provider_matches(
    reaper_db, mocker
):
    """Over-attribution guard. An unshipped COMPLETED task that does NOT match
    the need must never be credited as the provider: with nothing matching, the
    give-up keeps the (correct) never-satisfiable / nothing-provided reason."""
    from app.reaper_sweeps import (
        GIVE_UP_NEVER_SATISFIABLE,
        MISSING_FOUNDATION_MARKER_KEY,
        UNRESUMABLE_CANCEL_CONTEXT_KEY,
    )

    mocker.patch("app.broadcast.broadcast_to_project", new_callable=AsyncMock)
    project = await _make_project(reaper_db)
    now = _utcnow()
    # Present, COMPLETED, unmerged - but unrelated to the need.
    await _completed_provider(
        reaper_db,
        project,
        title="totally-unrelated-work",
        payloads=[{"type": "create", "pr_number": 999}],
        context={},
    )
    need = {"kind": "capability", "ref": "nonexistent-capability"}
    t = await _blocked_task(
        reaper_db,
        project,
        blocker_kind="upstream_dependency",
        blocked_reason="BLOCKER: needs an auth subsystem",
        blocked_at=now - timedelta(hours=30),
        context={
            "blocker": {
                "source": "agent",
                "waiting_on": "auth subsystem",
                "reason": "BLOCKER: needs an auth subsystem",
                "need": need,
            },
            "_blocked_escalated": True,
            "_blocked_parked_reevals": _BLOCKED_PARKED_REEVAL_CAP - 1,
        },
        recheck_after=now - timedelta(minutes=1),
    )

    await sweep_unblock_blocked_tasks(reaper_db, now)

    await reaper_db.refresh(t)
    assert t.status == TaskStatus.CANCELLED
    message = (t.latest_status_message or "").lower()
    assert "never became satisfiable" in message
    assert "999" not in message
    assert "totally-unrelated-work" not in message
    marker = (t.context or {}).get(UNRESUMABLE_CANCEL_CONTEXT_KEY)
    assert marker["give_up_class"] == GIVE_UP_NEVER_SATISFIABLE
    assert "provider_task_id" not in marker
    # A genuinely-unprovided foundation need IS still missing-foundation demand.
    assert marker.get(MISSING_FOUNDATION_MARKER_KEY) is True


@pytest.mark.asyncio
async def test_blocked_escalation_detail_names_unlanded_provider(reaper_db, mocker):
    """The escalation fires ~18h BEFORE the cull, so it is the first reason an
    operator reads. It must not claim the prerequisite "never appeared" when a
    task produced it and only the PR failed to land."""
    mocker.patch("app.broadcast.broadcast_to_project", new_callable=AsyncMock)
    project = await _make_project(reaper_db)
    now = _utcnow()
    provider = await _completed_provider(
        reaper_db,
        project,
        title="fix(size): attribute merge cap breaches",
        payloads=[{"type": "fix", "pr_number": 17707}],
        context={},
    )
    inflight = Task(
        project_id=project.id,
        title="unrelated-inflight",
        description="x",
        status=TaskStatus.IN_PROGRESS,
        created_by="test-user",
        requires_approval=False,
        payloads=[{"type": "create"}],
        context={},
        status_log=[],
    )
    reaper_db.add(inflight)
    await reaper_db.commit()
    await reaper_db.refresh(inflight)
    t = await _blocked_task(
        reaper_db,
        project,
        blocker_kind="upstream_dependency",
        blocked_reason="BLOCKER: waiting on the size-cap fix",
        # An already-wired edge to an unrelated in-flight task keeps the dependent
        # on the escalate path rather than the legacy precondition machine.
        depends_on=[str(inflight.id)],
        blocked_at=now - _BLOCKED_ESCALATE_AFTER - timedelta(hours=1),
        context={
            "blocker": {
                "source": "agent",
                "waiting_on": "size-cap fix",
                "reason": "BLOCKER: waiting on the size-cap fix",
                "need": {"kind": "task", "ref": str(provider.id)},
            }
        },
        recheck_after=now - timedelta(minutes=1),
    )

    await sweep_unblock_blocked_tasks(reaper_db, now)

    await reaper_db.refresh(t)
    assert t.status == TaskStatus.BLOCKED
    assert (t.context or {}).get("_blocked_escalated") is True
    detail = ((t.context or {}).get("blocker") or {}).get("escalation_detail") or ""
    assert "PR #17707" in detail
    assert "WAS produced" in detail
    assert "never appeared" not in detail.lower()
    assert "PR #17707" in (t.latest_status_message or "")


@pytest.mark.asyncio
async def test_unblock_sweep_parked_capability_below_cap_stays_blocked(
    reaper_db, mocker
):
    """The legitimate foundation-might-still-appear case is untouched: a parked
    blocker with a not-yet-satisfiable need below the re-eval cap stays BLOCKED
    (re-parked, re-eval counter bumped), it does NOT terminate early."""
    mocker.patch("app.broadcast.broadcast_to_project", new_callable=AsyncMock)
    project = await _make_project(reaper_db)
    now = _utcnow()
    t = await _blocked_task(
        reaper_db,
        project,
        blocker_kind="upstream_dependency",
        blocked_reason="BLOCKER: needs an auth subsystem",
        depends_on=[],
        blocked_at=now - timedelta(hours=8),
        context={
            "blocker": {
                "source": "agent",
                "waiting_on": "auth subsystem",
                "reason": "BLOCKER: needs an auth subsystem",
                "need": {"kind": "capability", "ref": "nonexistent-capability"},
            },
            "_blocked_escalated": True,
            "_blocked_parked_reevals": 0,
        },
        recheck_after=now - timedelta(minutes=1),
    )

    requeued = await sweep_unblock_blocked_tasks(reaper_db, now)

    assert requeued == 0
    await reaper_db.refresh(t)
    assert t.status == TaskStatus.BLOCKED
    # Re-eval counter advanced toward the cap; task re-parked, not terminated.
    assert t.context.get("_blocked_parked_reevals") == 1
    assert t.recheck_after is not None and t.recheck_after > now


@pytest.mark.asyncio
async def test_unblock_sweep_wires_edge_for_research_prerequisite_blocker(
    reaper_db, mocker
):
    """A research-spawned dependent BLOCKED on an unresolved finding prerequisite
    (source='research_prerequisite', carrying the prereq as its need) is wired
    into a real depends_on edge onto the matching foundation the moment it
    appears in flight, then requeued — closing the spawn-time seam through the
    existing blocked-resume sweep."""
    from app.task_blocked import enter_blocked_state

    mocker.patch("app.broadcast.broadcast_to_project", new_callable=AsyncMock)
    project = await _make_project(reaper_db)
    now = _utcnow()
    foundation = Task(
        project_id=project.id,
        title="shared foundation",
        description="x",
        status=TaskStatus.IN_PROGRESS,
        created_by="u",
        requires_approval=False,
        payloads=[{"type": "create"}],
        context={},
        status_log=[],
    )
    reaper_db.add(foundation)
    await reaper_db.commit()
    await reaper_db.refresh(foundation)
    # Build the dependent exactly the way research_completion does at spawn time.
    t = await _blocked_task(
        reaper_db,
        project,
        blocker_kind="upstream_dependency",
        depends_on=[],
        context={},
    )
    enter_blocked_state(
        t,
        waiting_on=f"foundation task:{foundation.id}",
        reason="Finding declared prerequisite task that did not resolve at spawn",
        source="research_prerequisite",
        blocker_kind="upstream_dependency",
        need={"kind": "task", "ref": str(foundation.id)},
    )
    t.recheck_after = now - timedelta(minutes=1)
    reaper_db.add(t)
    await reaper_db.commit()

    requeued = await sweep_unblock_blocked_tasks(reaper_db, now)

    assert requeued == 1
    await reaper_db.refresh(t)
    assert t.status == TaskStatus.QUEUED
    assert str(foundation.id) in (t.depends_on or [])
    assert "blocker" not in (t.context or {})


@pytest.mark.asyncio
async def test_unblock_sweep_resumes_research_prerequisite_when_foundation_landed(
    reaper_db, mocker
):
    """A research-spawned dependent BLOCKED on a prerequisite that has ALREADY
    landed as completed/merged work resumes with no edge — the foundation is
    done, so it requeues directly instead of waiting on an in-flight task."""
    from app.task_blocked import enter_blocked_state

    mocker.patch("app.broadcast.broadcast_to_project", new_callable=AsyncMock)
    project = await _make_project(reaper_db)
    now = _utcnow()
    foundation = Task(
        project_id=project.id,
        title="landed foundation",
        description="x",
        status=TaskStatus.COMPLETED,
        created_by="u",
        requires_approval=False,
        payloads=[{"type": "create"}],
        context={"pr_merged": True},
        status_log=[],
    )
    reaper_db.add(foundation)
    await reaper_db.commit()
    await reaper_db.refresh(foundation)
    t = await _blocked_task(
        reaper_db,
        project,
        blocker_kind="upstream_dependency",
        depends_on=[],
        context={},
    )
    enter_blocked_state(
        t,
        waiting_on=f"foundation task:{foundation.id}",
        reason="Finding declared prerequisite task that did not resolve at spawn",
        source="research_prerequisite",
        blocker_kind="upstream_dependency",
        need={"kind": "task", "ref": str(foundation.id)},
    )
    t.recheck_after = now - timedelta(minutes=1)
    reaper_db.add(t)
    await reaper_db.commit()

    requeued = await sweep_unblock_blocked_tasks(reaper_db, now)

    assert requeued == 1
    await reaper_db.refresh(t)
    assert t.status == TaskStatus.QUEUED
    assert not (t.depends_on or [])
    assert "blocker" not in (t.context or {})


@pytest.mark.asyncio
async def test_unblock_sweep_resumes_capability_need_registry_marks_working(
    reaper_db, mocker
):
    """A dependent BLOCKED on a capability need that the project's problem_model
    registry already declares ``working`` resumes with no edge: the capability
    exists in the codebase itself, so no provider task will ever advertise it —
    without the registry step the need sits residual until the escalation
    deadline for work that was runnable all along."""
    from app.task_blocked import enter_blocked_state

    mocker.patch("app.broadcast.broadcast_to_project", new_callable=AsyncMock)
    project = await _make_project(reaper_db)
    project.problem_model = {
        "capabilities": [
            {"name": "Budgeted compute job substrate", "status": "working"},
        ]
    }
    reaper_db.add(project)
    await reaper_db.commit()
    now = _utcnow()
    t = await _blocked_task(
        reaper_db,
        project,
        blocker_kind="upstream_dependency",
        depends_on=[],
        context={},
    )
    enter_blocked_state(
        t,
        waiting_on="foundation capability:budgeted-compute-job-substrate",
        reason="Finding declared 1 unresolved prerequisite(s)",
        source="research_prerequisite",
        blocker_kind="upstream_dependency",
        need={"kind": "capability", "ref": "budgeted-compute-job-substrate"},
    )
    t.recheck_after = now - timedelta(minutes=1)
    reaper_db.add(t)
    await reaper_db.commit()

    requeued = await sweep_unblock_blocked_tasks(reaper_db, now)

    assert requeued == 1
    await reaper_db.refresh(t)
    assert t.status == TaskStatus.QUEUED
    assert not (t.depends_on or [])
    assert "blocker" not in (t.context or {})


@pytest.mark.asyncio
async def test_unblock_sweep_working_capability_trumps_dead_provider(reaper_db, mocker):
    """The registry step runs BEFORE the dead-foundation check: a stale terminal
    task that once advertised a capability the registry now declares ``working``
    must not cancel the dependent — the capability survives the provider."""
    from app.task_blocked import enter_blocked_state

    mocker.patch("app.broadcast.broadcast_to_project", new_callable=AsyncMock)
    project = await _make_project(reaper_db)
    project.problem_model = {
        "capabilities": [
            {"name": "budgeted-compute-job-substrate", "status": "working"},
        ]
    }
    reaper_db.add(project)
    await reaper_db.commit()
    now = _utcnow()
    dead_provider = Task(
        project_id=project.id,
        title="old substrate build",
        description="x",
        status=TaskStatus.FAILED,
        created_by="u",
        requires_approval=False,
        payloads=[{"type": "create"}],
        context={"provides_capability": "budgeted-compute-job-substrate"},
        status_log=[],
    )
    reaper_db.add(dead_provider)
    await reaper_db.commit()
    t = await _blocked_task(
        reaper_db,
        project,
        blocker_kind="upstream_dependency",
        depends_on=[],
        context={},
    )
    enter_blocked_state(
        t,
        waiting_on="foundation capability:budgeted-compute-job-substrate",
        reason="Finding declared 1 unresolved prerequisite(s)",
        source="research_prerequisite",
        blocker_kind="upstream_dependency",
        need={"kind": "capability", "ref": "budgeted-compute-job-substrate"},
    )
    t.recheck_after = now - timedelta(minutes=1)
    reaper_db.add(t)
    await reaper_db.commit()

    requeued = await sweep_unblock_blocked_tasks(reaper_db, now)

    assert requeued == 1
    await reaper_db.refresh(t)
    assert t.status == TaskStatus.QUEUED, (
        "a working registry capability must rescue the need before the "
        "dead-foundation path can cancel the dependent"
    )


@pytest.mark.asyncio
async def test_unblock_sweep_capability_need_without_registry_stays_blocked(
    reaper_db, mocker
):
    """Control: a capability need with NO registry entry (and no provider task)
    keeps gating exactly as before — the registry step is a bypass only for
    declared-working capabilities."""
    from app.task_blocked import enter_blocked_state

    mocker.patch("app.broadcast.broadcast_to_project", new_callable=AsyncMock)
    project = await _make_project(reaper_db)
    now = _utcnow()
    t = await _blocked_task(
        reaper_db,
        project,
        blocker_kind="upstream_dependency",
        depends_on=[],
        context={},
    )
    enter_blocked_state(
        t,
        waiting_on="foundation capability:not-yet-built-substrate",
        reason="Finding declared 1 unresolved prerequisite(s)",
        source="research_prerequisite",
        blocker_kind="upstream_dependency",
        need={"kind": "capability", "ref": "not-yet-built-substrate"},
    )
    t.recheck_after = now - timedelta(minutes=1)
    reaper_db.add(t)
    await reaper_db.commit()

    requeued = await sweep_unblock_blocked_tasks(reaper_db, now)

    assert requeued == 0
    await reaper_db.refresh(t)
    assert t.status == TaskStatus.BLOCKED


@pytest.mark.asyncio
async def test_unblock_sweep_wires_recorded_need_alongside_existing_edge(
    reaper_db, mocker
):
    """A blocked dependent that already carries an UNRELATED depends_on edge AND
    records a machine-resolvable need must still have that need wired: a dependent
    with a partly-resolved prerequisite set keeps converging on the rest. Once
    every recorded need is accounted for, it resumes to QUEUED with BOTH edges (the
    dispatch gate holds it until both in-flight foundations land)."""
    mocker.patch("app.broadcast.broadcast_to_project", new_callable=AsyncMock)
    project = await _make_project(reaper_db)
    now = _utcnow()
    dep = Task(
        project_id=project.id,
        title="existing-dep",
        description="x",
        status=TaskStatus.IN_PROGRESS,
        created_by="u",
        requires_approval=False,
        payloads=[{"type": "fix"}],
        context={},
        status_log=[],
    )
    foundation = Task(
        project_id=project.id,
        title="foundation",
        description="x",
        status=TaskStatus.IN_PROGRESS,
        created_by="u",
        requires_approval=False,
        payloads=[{"type": "create"}],
        context={},
        status_log=[],
    )
    reaper_db.add_all([dep, foundation])
    await reaper_db.commit()
    await reaper_db.refresh(dep)
    await reaper_db.refresh(foundation)
    t = await _blocked_task(
        reaper_db,
        project,
        blocker_kind="upstream_dependency",
        depends_on=[str(dep.id)],
        context={
            "blocker": {
                "source": "agent",
                "waiting_on": "foundation",
                "need": {"kind": "task", "ref": str(foundation.id)},
            }
        },
        recheck_after=now - timedelta(minutes=1),
    )
    verify = AsyncMock(return_value=False)

    requeued = await sweep_unblock_blocked_tasks(
        reaper_db, now, verify_pr_merged=verify
    )

    # The recorded need resolves to the in-flight foundation → edge wired; that was
    # the only need, so the task resumes with BOTH edges present.
    assert requeued == 1
    await reaper_db.refresh(t)
    assert t.status == TaskStatus.QUEUED
    assert str(dep.id) in (t.depends_on or [])
    assert str(foundation.id) in (t.depends_on or [])
    assert "blocker" not in (t.context or {})


@pytest.mark.asyncio
async def test_unblock_sweep_partial_needs_resolution_stays_blocked(reaper_db, mocker):
    """A dependent BLOCKED on TWO needs where only ONE resolves to an in-flight
    foundation stays BLOCKED: the edge is wired for the resolved need, the residual
    still-unsatisfied need is re-persisted, and the task is NOT requeued until every
    need is accounted for."""
    from app.task_blocked import enter_blocked_state, get_blocker_needs

    mocker.patch("app.broadcast.broadcast_to_project", new_callable=AsyncMock)
    project = await _make_project(reaper_db)
    now = _utcnow()
    found1 = Task(
        project_id=project.id,
        title="foundation-1",
        description="x",
        status=TaskStatus.IN_PROGRESS,
        created_by="u",
        requires_approval=False,
        payloads=[{"type": "create"}],
        context={},
        status_log=[],
    )
    reaper_db.add(found1)
    await reaper_db.commit()
    await reaper_db.refresh(found1)
    missing_id = str(_uuid_mod.uuid4())  # names a foundation that does not exist yet
    t = await _blocked_task(
        reaper_db,
        project,
        blocker_kind="upstream_dependency",
        depends_on=[],
        context={},
    )
    enter_blocked_state(
        t,
        waiting_on="two foundations",
        reason="Finding declared two prerequisites",
        source="research_prerequisite",
        blocker_kind="upstream_dependency",
        needs=[
            {"kind": "task", "ref": str(found1.id)},
            {"kind": "task", "ref": missing_id},
        ],
    )
    t.recheck_after = now - timedelta(minutes=1)
    reaper_db.add(t)
    await reaper_db.commit()

    requeued = await sweep_unblock_blocked_tasks(reaper_db, now)

    assert requeued == 0
    await reaper_db.refresh(t)
    # Edge wired for the resolved foundation, but the task stays BLOCKED on the
    # residual missing need.
    assert t.status == TaskStatus.BLOCKED
    assert str(found1.id) in (t.depends_on or [])
    residual = get_blocker_needs(t)
    assert [n["ref"] for n in residual] == [missing_id], (
        "only the still-unsatisfied need must remain; the resolved one is dropped"
    )
    # The legacy single 'need' mirror is recomputed to residual[0].
    assert (t.context or {})["blocker"]["need"]["ref"] == missing_id
    assert t.recheck_after is not None and t.recheck_after > now


@pytest.mark.asyncio
async def test_unblock_sweep_full_needs_resolution_requeues(reaper_db, mocker):
    """A dependent BLOCKED on TWO needs unblocks to QUEUED once ALL are accounted
    for: an edge for the in-flight foundation, and no edge for the need already
    satisfied by completed work."""
    from app.task_blocked import enter_blocked_state

    mocker.patch("app.broadcast.broadcast_to_project", new_callable=AsyncMock)
    project = await _make_project(reaper_db)
    now = _utcnow()
    inflight = Task(
        project_id=project.id,
        title="inflight-foundation",
        description="x",
        status=TaskStatus.IN_PROGRESS,
        created_by="u",
        requires_approval=False,
        payloads=[{"type": "create"}],
        context={},
        status_log=[],
    )
    landed = Task(
        project_id=project.id,
        title="landed-foundation",
        description="x",
        status=TaskStatus.COMPLETED,
        created_by="u",
        requires_approval=False,
        payloads=[{"type": "create"}],
        context={"pr_merged": True},
        status_log=[],
    )
    reaper_db.add_all([inflight, landed])
    await reaper_db.commit()
    await reaper_db.refresh(inflight)
    await reaper_db.refresh(landed)
    t = await _blocked_task(
        reaper_db,
        project,
        blocker_kind="upstream_dependency",
        depends_on=[],
        context={},
    )
    enter_blocked_state(
        t,
        waiting_on="two foundations",
        reason="Finding declared two prerequisites",
        source="research_prerequisite",
        blocker_kind="upstream_dependency",
        needs=[
            {"kind": "task", "ref": str(inflight.id)},
            {"kind": "task", "ref": str(landed.id)},
        ],
    )
    t.recheck_after = now - timedelta(minutes=1)
    reaper_db.add(t)
    await reaper_db.commit()

    requeued = await sweep_unblock_blocked_tasks(reaper_db, now)

    assert requeued == 1
    await reaper_db.refresh(t)
    assert t.status == TaskStatus.QUEUED
    # Edge wired only for the in-flight foundation; the completed one needs none.
    assert str(inflight.id) in (t.depends_on or [])
    assert str(landed.id) not in (t.depends_on or [])
    assert "blocker" not in (t.context or {})


@pytest.mark.asyncio
async def test_unblock_sweep_partial_resolution_runs_base_safe_for_completed_need(
    reaper_db, mocker
):
    """Regression: during PARTIAL multi-need resolution, a need satisfied by an
    ALREADY-COMPLETED CROSS-BASE foundation must be run through the base re-cut
    BEFORE it is dropped from the persisted set — it must not be silently discarded
    while another need is still missing.

    needs = [A (completed cross-base, re-cut succeeds), B (missing)]. The dependent
    stays BLOCKED on B, but A's re-cut (the integration merge) MUST have fired this
    tick so A's landed code is in the base; only then is A dropped from the residual.
    Previously the partial branch persisted only the genuine residual and never ran
    base-safe, so A vanished unverified and the task later resumed against a stale base.
    """
    from app.task_blocked import enter_blocked_state, get_blocker_needs

    mocker.patch("app.broadcast.broadcast_to_project", new_callable=AsyncMock)
    mocker.patch(
        "app.routers.agent.get_github_token_for_task",
        new_callable=AsyncMock,
        return_value=("tok", "owner", "repo"),
    )
    merge = mocker.patch(
        "app.github.merge_branch",
        new_callable=AsyncMock,
        return_value={"merged": True},
    )
    project = await _make_project(reaper_db)
    now = _utcnow()
    foundation = await _cross_base_completed_foundation(reaper_db, project)
    missing_id = str(_uuid_mod.uuid4())  # a foundation that does not exist yet
    t = await _blocked_task(
        reaper_db,
        project,
        blocker_kind="upstream_dependency",
        blocked_reason="BLOCKER: waiting on foundations",
        depends_on=[],
        payloads=[{"type": "fix", "base_branch": "rollup/dep"}],
        context={"related_files": ["app/foo.py"]},
    )
    enter_blocked_state(
        t,
        waiting_on="two foundations",
        reason="Finding declared two prerequisites",
        source="research_prerequisite",
        blocker_kind="upstream_dependency",
        needs=[
            {"kind": "task", "ref": str(foundation.id)},
            {"kind": "task", "ref": missing_id},
        ],
    )
    t.recheck_after = now - timedelta(minutes=1)
    reaper_db.add(t)
    await reaper_db.commit()

    requeued = await sweep_unblock_blocked_tasks(reaper_db, now)

    assert requeued == 0
    # The cross-base re-cut for the completed need DID fire this partial tick.
    assert merge.await_count == 1
    await reaper_db.refresh(t)
    assert t.status == TaskStatus.BLOCKED
    # The completed (now base-safe) need is dropped; only the missing one remains.
    residual = get_blocker_needs(t)
    assert [n["ref"] for n in residual] == [missing_id]
    assert t.recheck_after is not None and t.recheck_after > now


@pytest.mark.asyncio
async def test_unblock_sweep_partial_resolution_retains_completed_need_on_conflict(
    reaper_db, mocker
):
    """Regression (counterpart): during PARTIAL resolution, a completed cross-base
    need whose re-cut CONFLICTS must be RETAINED as a residual need — not dropped —
    so it is re-checked next tick rather than letting the dependent later resume
    against a base missing its landed code.

    needs = [A (completed cross-base, re-cut conflicts), B (missing)]. The dependent
    stays BLOCKED and BOTH A and B remain in the persisted needs set.
    """
    from app.task_blocked import enter_blocked_state, get_blocker_needs

    mocker.patch("app.broadcast.broadcast_to_project", new_callable=AsyncMock)
    mocker.patch(
        "app.routers.agent.get_github_token_for_task",
        new_callable=AsyncMock,
        return_value=("tok", "owner", "repo"),
    )
    merge = mocker.patch(
        "app.github.merge_branch",
        new_callable=AsyncMock,
        return_value={"conflict": True},
    )
    project = await _make_project(reaper_db)
    now = _utcnow()
    foundation = await _cross_base_completed_foundation(reaper_db, project)
    missing_id = str(_uuid_mod.uuid4())
    t = await _blocked_task(
        reaper_db,
        project,
        blocker_kind="upstream_dependency",
        blocked_reason="BLOCKER: waiting on foundations",
        depends_on=[],
        payloads=[{"type": "fix", "base_branch": "rollup/dep"}],
        context={"related_files": ["app/foo.py"]},
    )
    enter_blocked_state(
        t,
        waiting_on="two foundations",
        reason="Finding declared two prerequisites",
        source="research_prerequisite",
        blocker_kind="upstream_dependency",
        needs=[
            {"kind": "task", "ref": str(foundation.id)},
            {"kind": "task", "ref": missing_id},
        ],
    )
    t.recheck_after = now - timedelta(minutes=1)
    reaper_db.add(t)
    await reaper_db.commit()

    requeued = await sweep_unblock_blocked_tasks(reaper_db, now)

    assert requeued == 0
    assert merge.await_count == 1  # base-safe attempted the re-cut and it conflicted
    await reaper_db.refresh(t)
    assert t.status == TaskStatus.BLOCKED
    # The conflicting completed need is NOT dropped — both needs are retained.
    residual_refs = {n["ref"] for n in get_blocker_needs(t)}
    assert residual_refs == {str(foundation.id), missing_id}
    assert t.recheck_after is not None and t.recheck_after > now


@pytest.mark.asyncio
async def test_unblock_sweep_legacy_single_need_row_resolves_unchanged(
    reaper_db, mocker
):
    """Back-compat: a blocker row carrying ONLY the legacy single 'need' key (no
    'needs' list, exactly as rows persisted before multi-need support) resolves
    exactly as before — the need is wired into an edge and the task requeues."""
    mocker.patch("app.broadcast.broadcast_to_project", new_callable=AsyncMock)
    project = await _make_project(reaper_db)
    now = _utcnow()
    foundation = Task(
        project_id=project.id,
        title="legacy-foundation",
        description="x",
        status=TaskStatus.IN_PROGRESS,
        created_by="u",
        requires_approval=False,
        payloads=[{"type": "create"}],
        context={},
        status_log=[],
    )
    reaper_db.add(foundation)
    await reaper_db.commit()
    await reaper_db.refresh(foundation)
    # Hand-build the legacy blocker shape: only context['blocker']['need'], no
    # 'needs' list — the row a pre-multi-need deployment would have persisted.
    t = await _blocked_task(
        reaper_db,
        project,
        blocker_kind="upstream_dependency",
        depends_on=[],
        context={
            "blocker": {
                "source": "research_prerequisite",
                "waiting_on": "foundation",
                "reason": "BLOCKER: waiting on foundation",
                "need": {"kind": "task", "ref": str(foundation.id)},
            }
        },
        recheck_after=now - timedelta(minutes=1),
    )

    requeued = await sweep_unblock_blocked_tasks(reaper_db, now)

    assert requeued == 1
    await reaper_db.refresh(t)
    assert t.status == TaskStatus.QUEUED
    assert str(foundation.id) in (t.depends_on or [])
    assert "blocker" not in (t.context or {})


@pytest.mark.asyncio
async def test_unblock_sweep_skips_not_yet_due(reaper_db, mocker):
    """A BLOCKED task whose recheck_after is in the future is left untouched."""
    mocker.patch("app.broadcast.broadcast_to_project", new_callable=AsyncMock)
    project = await _make_project(reaper_db)
    t = await _blocked_task(
        reaper_db, project, depends_on=[], recheck_after=_utcnow() + timedelta(hours=1)
    )

    requeued = await sweep_unblock_blocked_tasks(reaper_db, _utcnow())

    assert requeued == 0
    await reaper_db.refresh(t)
    assert t.status == TaskStatus.BLOCKED


@pytest.mark.asyncio
async def test_unblock_sweep_keeps_parent_blocker_waiting_while_child_blocked(
    reaper_db, mocker
):
    """A parent parked in BLOCKED must not auto-promote while one of its
    children is still BLOCKED on the same precondition."""
    mocker.patch("app.broadcast.broadcast_to_project", new_callable=AsyncMock)
    project = await _make_project(reaper_db)
    now = _utcnow()
    parent = await _blocked_task(
        reaper_db,
        project,
        blocker_kind="upstream_dependency",
        blocked_reason="All 1 subtask(s) blocked on upstream precondition — awaiting recheck",
        depends_on=[],
        context={
            "blocker": {
                "source": "parent",
                "waiting_on": "subtask precondition",
                "reason": "All 1 subtask(s) blocked on upstream precondition — awaiting recheck",
            }
        },
        recheck_after=now - timedelta(minutes=1),
    )
    child = Task(
        project_id=project.id,
        title="child",
        description="x",
        status=TaskStatus.BLOCKED,
        created_by="u",
        requires_approval=False,
        payloads=[{"type": "fix"}],
        context={"failure_category": "BLOCKED_PRECONDITION"},
        status_log=[],
        parent_task_id=parent.id,
    )
    reaper_db.add(child)
    await reaper_db.commit()

    requeued = await sweep_unblock_blocked_tasks(reaper_db, now)

    assert requeued == 0
    await reaper_db.refresh(parent)
    assert parent.status == TaskStatus.BLOCKED
    assert parent.recheck_after is not None and parent.recheck_after > now
    assert parent.context.get("_blocked_recheck_attempts") == 1


@pytest.mark.asyncio
async def test_unblock_sweep_moves_parent_blocker_to_evaluating_past_deadline(
    reaper_db, mocker
):
    """Once a blocked parent's child blockers clear, the parent returns to the
    rollup re-evaluation path, not the normal QUEUED dispatch path."""
    broadcast = mocker.patch(
        "app.broadcast.broadcast_to_project", new_callable=AsyncMock
    )
    project = await _make_project(reaper_db)
    # Record commit/broadcast ordering to prove the EVALUATING event fires
    # only after the batch commit. Installed below, after setup commits.
    events: list = []
    now = _utcnow()
    parent = await _blocked_task(
        reaper_db,
        project,
        blocker_kind="upstream_dependency",
        blocked_reason="All 1 subtask(s) blocked on upstream precondition — awaiting recheck",
        depends_on=[],
        context={
            "blocker": {
                "source": "parent",
                "waiting_on": "subtask precondition",
                "reason": "All 1 subtask(s) blocked on upstream precondition — awaiting recheck",
            },
            "failure_class": "BLOCKED_PRECONDITION",
            "failure_category": "BLOCKED_PRECONDITION",
        },
        blocked_at=now - _BLOCKED_ESCALATE_AFTER - timedelta(minutes=5),
        recheck_after=now - timedelta(minutes=1),
    )
    child = Task(
        project_id=project.id,
        title="child",
        description="x",
        status=TaskStatus.COMPLETED,
        created_by="u",
        requires_approval=False,
        payloads=[{"type": "fix"}],
        context={},
        status_log=[],
        parent_task_id=parent.id,
    )
    reaper_db.add(child)
    await reaper_db.commit()

    real_commit = reaper_db.commit

    async def _tracking_commit(*a, **k):
        events.append("commit")
        return await real_commit(*a, **k)

    mocker.patch.object(reaper_db, "commit", side_effect=_tracking_commit)
    broadcast.side_effect = lambda *a, **k: events.append("broadcast")

    requeued = await sweep_unblock_blocked_tasks(reaper_db, now)

    assert requeued == 0
    await reaper_db.refresh(parent)
    assert parent.status == TaskStatus.EVALUATING
    assert parent.blocker_kind is None
    assert parent.blocked_at is None
    assert parent.recheck_after is None
    assert parent.context.get("_blocked_escalated") is None
    # Parent re-evaluation must also strip residual blocker metadata.
    assert "blocker" not in (parent.context or {})
    assert "failure_class" not in (parent.context or {})
    assert "failure_category" not in (parent.context or {})
    alerts = (
        (
            await reaper_db.execute(
                _select(Alert).where(
                    Alert.task_id == parent.id,
                    Alert.severity == AlertSeverity.ERROR,
                )
            )
        )
        .scalars()
        .all()
    )
    assert len(alerts) == 0
    broadcast.assert_any_call(
        str(project.id),
        "task_updated",
        {
            "task_id": str(parent.id),
            "status": "EVALUATING",
            "message": "Unblocked — awaiting rollup re-evaluation",
        },
    )
    # The EVALUATING broadcast must follow the batch commit, not precede it.
    assert "commit" in events
    assert "broadcast" in events
    assert events.index("commit") < events.index("broadcast")


@pytest.mark.asyncio
async def test_unblock_sweep_requeues_when_dependency_resolves_past_deadline(
    reaper_db, mocker
):
    """A dependency blocker is requeued once its dep resolves as eligible."""
    mocker.patch("app.broadcast.broadcast_to_project", new_callable=AsyncMock)
    project = await _make_project(reaper_db)
    dep = Task(
        project_id=project.id,
        title="dep",
        description="x",
        status=TaskStatus.COMPLETED,
        created_by="u",
        requires_approval=False,
        payloads=[{"type": "fix"}],
        context={},
        status_log=[],
    )
    reaper_db.add(dep)
    await reaper_db.commit()
    await reaper_db.refresh(dep)
    now = _utcnow()
    t = await _blocked_task(
        reaper_db,
        project,
        blocker_kind="upstream_dependency",
        depends_on=[str(dep.id)],
        blocked_at=now - _BLOCKED_ESCALATE_AFTER - timedelta(minutes=5),
        recheck_after=now - timedelta(minutes=1),
    )
    verify = AsyncMock(return_value=True)

    requeued = await sweep_unblock_blocked_tasks(
        reaper_db, now, verify_pr_merged=verify
    )

    assert requeued == 1
    await reaper_db.refresh(t)
    assert t.status == TaskStatus.QUEUED
    assert t.context.get("_blocked_escalated") is None
    alerts = (
        (
            await reaper_db.execute(
                _select(Alert).where(
                    Alert.task_id == t.id,
                    Alert.severity == AlertSeverity.ERROR,
                )
            )
        )
        .scalars()
        .all()
    )
    assert len(alerts) == 0


@pytest.mark.asyncio
async def test_unblock_sweep_keeps_waiting_dependency_blocked(reaper_db, mocker):
    """An in-flight dependency keeps the task BLOCKED and re-arms recheck_after."""
    mocker.patch("app.broadcast.broadcast_to_project", new_callable=AsyncMock)
    project = await _make_project(reaper_db)
    dep = Task(
        project_id=project.id,
        title="dep",
        description="x",
        status=TaskStatus.IN_PROGRESS,
        created_by="u",
        requires_approval=False,
        payloads=[{"type": "fix"}],
        context={},
        status_log=[],
    )
    reaper_db.add(dep)
    await reaper_db.commit()
    await reaper_db.refresh(dep)
    now = _utcnow()
    t = await _blocked_task(
        reaper_db,
        project,
        blocker_kind="upstream_dependency",
        depends_on=[str(dep.id)],
        recheck_after=now - timedelta(minutes=1),
    )
    verify = AsyncMock(return_value=False)

    requeued = await sweep_unblock_blocked_tasks(
        reaper_db, now, verify_pr_merged=verify
    )

    assert requeued == 0
    await reaper_db.refresh(t)
    assert t.status == TaskStatus.BLOCKED
    # recheck_after pushed into the future with backoff.
    assert t.recheck_after > now
    assert t.context.get("_blocked_recheck_attempts") == 1


@pytest.mark.asyncio
async def test_unblock_sweep_escalates_dead_dependency(reaper_db, mocker):
    """A failed dependency escalates terminally with one ERROR alert; task parked."""
    mocker.patch("app.broadcast.broadcast_to_project", new_callable=AsyncMock)
    project = await _make_project(reaper_db)
    dep = Task(
        project_id=project.id,
        title="dep",
        description="x",
        status=TaskStatus.FAILED,
        created_by="u",
        requires_approval=False,
        payloads=[{"type": "fix"}],
        context={},
        status_log=[],
    )
    reaper_db.add(dep)
    await reaper_db.commit()
    await reaper_db.refresh(dep)
    t = await _blocked_task(
        reaper_db,
        project,
        blocker_kind="upstream_dependency",
        depends_on=[str(dep.id)],
    )
    verify = AsyncMock(return_value=False)

    requeued = await sweep_unblock_blocked_tasks(
        reaper_db, _utcnow(), verify_pr_merged=verify
    )

    assert requeued == 0
    await reaper_db.refresh(t)
    assert t.status == TaskStatus.BLOCKED
    assert t.context.get("_blocked_escalated") is True
    alerts = (
        (
            await reaper_db.execute(
                _select(Alert).where(
                    Alert.task_id == t.id, Alert.severity == AlertSeverity.ERROR
                )
            )
        )
        .scalars()
        .all()
    )
    assert len(alerts) == 1
    assert "permanently blocked" in alerts[0].title


@pytest.mark.asyncio
async def test_unblock_sweep_requeues_completed_measured_negative_compute_prereq(
    reaper_db, mocker
):
    """A measured-negative compute prerequisite is still eligible once completed."""
    mocker.patch("app.broadcast.broadcast_to_project", new_callable=AsyncMock)
    project = await _make_project(reaper_db)
    dep = Task(
        project_id=project.id,
        title="measure baseline",
        description="x",
        status=TaskStatus.COMPLETED,
        created_by="u",
        requires_approval=False,
        payloads=[
            {
                "type": "compute_experiment",
                "success_criterion": {
                    "metric": "accuracy",
                    "operator": ">=",
                    "threshold": 0.9,
                },
            }
        ],
        context={
            "compute_success_criterion": {
                "metric": "accuracy",
                "operator": ">=",
                "threshold": 0.9,
                "value": 0.42,
                "met": False,
                "measurable": True,
            }
        },
        status_log=[],
        plan_output={
            "compute_result": {
                "outcome": "SUCCESS",
                "final_cost_usd": 0.37,
                "criterion": {
                    "metric": "accuracy",
                    "operator": ">=",
                    "threshold": 0.9,
                    "value": 0.42,
                    "met": False,
                    "measurable": True,
                },
            }
        },
    )
    reaper_db.add(dep)
    await reaper_db.commit()
    await reaper_db.refresh(dep)
    now = _utcnow()
    t = await _blocked_task(
        reaper_db,
        project,
        blocker_kind="upstream_dependency",
        depends_on=[str(dep.id)],
        blocked_at=now - _BLOCKED_ESCALATE_AFTER - timedelta(minutes=5),
        recheck_after=now - timedelta(minutes=1),
    )

    requeued = await sweep_unblock_blocked_tasks(reaper_db, now)

    assert requeued == 1
    await reaper_db.refresh(t)
    assert t.status == TaskStatus.QUEUED
    assert t.context.get("_blocked_escalated") is None


@pytest.mark.asyncio
async def test_unblock_sweep_escalates_broken_compute_prereq(reaper_db, mocker):
    """A broken compute prerequisite still cascade-blocks its dependent."""
    mocker.patch("app.broadcast.broadcast_to_project", new_callable=AsyncMock)
    project = await _make_project(reaper_db)
    dep = Task(
        project_id=project.id,
        title="measure baseline",
        description="x",
        status=TaskStatus.FAILED,
        created_by="u",
        requires_approval=False,
        payloads=[
            {
                "type": "compute_experiment",
                "success_criterion": {
                    "metric": "accuracy",
                    "operator": ">=",
                    "threshold": 0.9,
                },
            }
        ],
        context={"evaluation_gate": {"passed": False, "strategy": "metric"}},
        status_log=[],
    )
    reaper_db.add(dep)
    await reaper_db.commit()
    await reaper_db.refresh(dep)
    t = await _blocked_task(
        reaper_db,
        project,
        blocker_kind="upstream_dependency",
        depends_on=[str(dep.id)],
    )

    requeued = await sweep_unblock_blocked_tasks(reaper_db, _utcnow())

    assert requeued == 0
    await reaper_db.refresh(t)
    assert t.status == TaskStatus.BLOCKED
    assert t.context.get("_blocked_escalated") is True


@pytest.mark.asyncio
async def test_unblock_sweep_deadline_escalation(reaper_db, mocker):
    """A blocker older than the escalation deadline gets one ERROR alert, stays parked."""
    mocker.patch("app.broadcast.broadcast_to_project", new_callable=AsyncMock)
    project = await _make_project(reaper_db)
    now = _utcnow()
    t = await _blocked_task(
        reaper_db,
        project,
        depends_on=[],
        blocked_at=now - _BLOCKED_ESCALATE_AFTER - timedelta(minutes=5),
        recheck_after=now - timedelta(minutes=1),
    )

    requeued = await sweep_unblock_blocked_tasks(reaper_db, now)

    assert requeued == 0
    await reaper_db.refresh(t)
    assert t.status == TaskStatus.BLOCKED
    assert t.context.get("_blocked_escalated") is True
    # Parked far in the future (not NULL) so it drops out of the due set and is
    # never re-SELECTed / FOR UPDATE-locked on the reaper hot path again.
    assert t.recheck_after is not None and t.recheck_after > now
    alerts = (
        (
            await reaper_db.execute(
                _select(Alert).where(
                    Alert.task_id == t.id, Alert.severity == AlertSeverity.ERROR
                )
            )
        )
        .scalars()
        .all()
    )
    assert len(alerts) == 1

    # Idempotent: a second sweep does not create a duplicate alert.
    await sweep_unblock_blocked_tasks(reaper_db, now)
    alerts2 = (
        (
            await reaper_db.execute(
                _select(Alert).where(
                    Alert.task_id == t.id, Alert.severity == AlertSeverity.ERROR
                )
            )
        )
        .scalars()
        .all()
    )
    assert len(alerts2) == 1


@pytest.mark.asyncio
async def test_park_horizon_is_never_silent_invariant(reaper_db, mocker):
    """INVARIANT: a BLOCKED task only reaches escalated (operator-tier) status AFTER
    a tracked operator escalation alert has been emitted — escalation is never
    silent, AND it never expresses itself as a fake far-future ``recheck_after``
    (the old ``_BLOCKED_ESCALATED_PARK`` sentinel is legacy-only now — a live
    escalation always lands on the bounded, exponentially-backed-off cadence
    capped at ``_BLOCKED_ESCALATED_RECHECK_MAX``, see the 2026-08-18 incident:
    5 tasks tombstoned at a 2036 recheck_after until an operator manually
    requeued them).

    Runs the unblock sweep over a mixed population (one past-deadline blocker that
    must escalate, one free-form agent blocker that must remain held until a
    named precondition changes) and asserts that EVERY newly-escalated task
    carries the ``_blocked_escalated`` flag, a matching ERROR escalation alert,
    AND a bounded (never multi-year) ``recheck_after``. This guards the
    observability of parked blockers (compute_parked_blocker_summary keys off
    the same flag): an escalation without an alert would be an uncounted,
    invisible permanently-stuck task, and an escalation without a bounded
    recheck would be a silent tombstone.
    """
    from app.blocked_task_unblock_sweeps import _BLOCKED_ESCALATED_RECHECK_MAX

    mocker.patch("app.broadcast.broadcast_to_project", new_callable=AsyncMock)
    project = await _make_project(reaper_db)
    now = _utcnow()

    # (a) Past-deadline upstream-dependency blocker → escalates. Non-transient
    # blocker_kind (upstream_dependency): still escalates like any other kind,
    # just on the bounded cadence rather than the transient re-evaluation path.
    escalated = await _blocked_task(
        reaper_db,
        project,
        depends_on=[],
        blocker_kind="upstream_dependency",
        blocked_at=now - _BLOCKED_ESCALATE_AFTER - timedelta(minutes=5),
        recheck_after=now - timedelta(minutes=1),
    )
    # (b) A free-form agent blocker has no server-verifiable changed precondition,
    # so it remains blocked without consuming another execution episode.
    held = await _blocked_task(
        reaper_db,
        project,
        depends_on=[],
        blocker_kind="precondition",
        blocked_reason="BLOCKER: orchestrator ./pr open returned HTTP 500",
        blocked_at=now,
        context={"blocker": {"source": "agent"}},
    )

    await sweep_unblock_blocked_tasks(
        reaper_db, now, verify_pr_merged=AsyncMock(return_value=False)
    )

    # Distinguish "escalated" from "merely held" by the durable, queryable
    # ``_blocked_escalated`` flag (the actual operator-tier marker) rather than
    # a recheck_after window — both an escalated and a held row now use bounded,
    # not-far-future cadences, so a recheck_after range alone can't tell them
    # apart the way the old +10y-vs-everything-else split could.
    await reaper_db.refresh(escalated)
    await reaper_db.refresh(held)

    far_future = now + timedelta(days=3000)

    # The core invariant: the escalated task is escalation-flagged, alerted,
    # AND bounded (never a multi-year tombstone).
    assert escalated.status == TaskStatus.BLOCKED
    assert escalated.context.get("_blocked_escalated") is True
    assert escalated.recheck_after is not None
    assert escalated.recheck_after < far_future
    assert escalated.recheck_after <= now + _BLOCKED_ESCALATED_RECHECK_MAX + timedelta(
        minutes=1
    )
    alerts = (
        (
            await reaper_db.execute(
                _select(Alert).where(
                    Alert.task_id == escalated.id,
                    Alert.severity == AlertSeverity.ERROR,
                )
            )
        )
        .scalars()
        .all()
    )
    assert len(alerts) == 1, "an escalation must be preceded by an alert"
    assert "permanently blocked" in alerts[0].title

    # The receipt-less blocker never escalated, neither retries nor silently
    # parks, and emits no alert.
    assert held.status == TaskStatus.BLOCKED
    assert held.context.get("_blocked_escalated") is not True
    no_alerts = (
        (await reaper_db.execute(_select(Alert).where(Alert.task_id == held.id)))
        .scalars()
        .all()
    )
    assert no_alerts == []


@pytest.mark.asyncio
async def test_unblock_sweep_evicts_legacy_escalated_none_recheck(reaper_db, mocker):
    """A legacy escalated row with recheck_after=NULL is parked out of the due
    set (far-future recheck_after) instead of being re-locked every cycle."""
    mocker.patch("app.broadcast.broadcast_to_project", new_callable=AsyncMock)
    project = await _make_project(reaper_db)
    now = _utcnow()
    # Use a non-transient blocker_kind: transient ones (file_conflict, pending_rollup)
    # now re-evaluate and requeue when their conflict has cleared; the eviction
    # behavior (stay BLOCKED, push recheck_after forward) applies only to durable
    # non-transient escalated blockers with no machine-resolvable need.
    t = await _blocked_task(
        reaper_db,
        project,
        depends_on=[],
        blocker_kind="upstream_dependency",
        context={"_blocked_escalated": True},
        recheck_after=None,  # legacy: escalated rows used to be left NULL
    )

    requeued = await sweep_unblock_blocked_tasks(reaper_db, now)

    assert requeued == 0
    await reaper_db.refresh(t)
    # Still escalated/BLOCKED but evicted from the due set so it stops being
    # re-SELECTed and FOR UPDATE-locked on the reaper hot path.
    assert t.status == TaskStatus.BLOCKED
    assert t.recheck_after is not None and t.recheck_after > now


async def _parked_blocker(db, project, *, need, now, alert_kind="precondition"):
    """A BLOCKED task already escalated and parked, carrying ``need`` and a
    matching open 'permanently blocked' ERROR alert (so resume can reconcile it).
    Parked due at the low re-eval cadence so the sweep re-examines it."""
    t = await _blocked_task(
        db,
        project,
        blocker_kind="precondition",
        blocked_reason="BLOCKER: needs a foundation that never arrived in time",
        depends_on=[],
        blocked_at=now - timedelta(hours=7),
        context={
            "_blocked_escalated": True,
            "blocker": {
                "source": "agent",
                "waiting_on": "foundation",
                "escalated": True,
                "need": need,
            },
        },
        recheck_after=now - timedelta(minutes=1),  # due
    )
    alert = Alert(
        project_id=project.id,
        severity=AlertSeverity.ERROR,
        title=f"Task permanently blocked: {alert_kind}",
        message="automatic recovery has given up",
        task_id=t.id,
    )
    db.add(alert)
    await db.commit()
    return t


@pytest.mark.asyncio
async def test_parked_blocker_resumes_when_inflight_need_becomes_satisfiable(
    reaper_db, mocker
):
    """A parked blocker whose need now names an in-flight foundation resumes:
    a depends_on edge is wired, the parked/escalated marker is cleared, and the
    stale 'permanently blocked' alert is reconciled."""
    mocker.patch("app.broadcast.broadcast_to_project", new_callable=AsyncMock)
    project = await _make_project(reaper_db)
    now = _utcnow()
    foundation = Task(
        project_id=project.id,
        title="foundation",
        description="x",
        status=TaskStatus.IN_PROGRESS,
        created_by="u",
        requires_approval=False,
        payloads=[{"type": "create"}],
        context={},
        status_log=[],
    )
    reaper_db.add(foundation)
    await reaper_db.commit()
    await reaper_db.refresh(foundation)
    t = await _parked_blocker(
        reaper_db,
        project,
        need={"kind": "task", "ref": str(foundation.id)},
        now=now,
    )

    requeued = await sweep_unblock_blocked_tasks(reaper_db, now)

    assert requeued == 1
    await reaper_db.refresh(t)
    assert t.status == TaskStatus.QUEUED
    # Edge wired onto the in-flight foundation; parked/escalated state cleared.
    assert str(foundation.id) in (t.depends_on or [])
    assert t.context.get("_blocked_escalated") is None
    assert "blocker" not in (t.context or {})
    assert t.recheck_after is None
    # The stale 'permanently blocked' ERROR alert was reconciled (dismissed).
    alerts = (
        (await reaper_db.execute(_select(Alert).where(Alert.task_id == t.id)))
        .scalars()
        .all()
    )
    assert len(alerts) == 1 and alerts[0].dismissed_at is not None


@pytest.mark.asyncio
async def test_parked_blocker_resumes_when_need_satisfied_by_completed_work(
    reaper_db, mocker
):
    """A parked blocker whose need is already satisfied by completed/merged work
    proceeds with NO dangling edge (the foundation is already done)."""
    mocker.patch("app.broadcast.broadcast_to_project", new_callable=AsyncMock)
    project = await _make_project(reaper_db)
    now = _utcnow()
    merged = Task(
        project_id=project.id,
        title="merged-foundation",
        description="x",
        status=TaskStatus.COMPLETED,
        created_by="u",
        requires_approval=False,
        payloads=[{"type": "fix", "pr_number": 8123}],
        context={"pr_merged": True},
        status_log=[],
    )
    reaper_db.add(merged)
    await reaper_db.commit()
    await reaper_db.refresh(merged)
    t = await _parked_blocker(
        reaper_db,
        project,
        need={"kind": "pr", "ref": "8123"},
        now=now,
    )

    requeued = await sweep_unblock_blocked_tasks(reaper_db, now)

    assert requeued == 1
    await reaper_db.refresh(t)
    assert t.status == TaskStatus.QUEUED
    # Satisfied by completed work — no edge wired, just proceeds.
    assert not (t.depends_on or [])
    assert t.context.get("_blocked_escalated") is None
    assert "blocker" not in (t.context or {})


@pytest.mark.asyncio
async def test_parked_blocker_stays_parked_when_need_unsatisfiable(reaper_db, mocker):
    """A parked blocker whose need matches nothing live stays BLOCKED+escalated,
    re-parked at the low cadence without re-firing an alert; and a parked blocker
    with NO structured need is re-armed on the bounded, exponentially-backed-off
    escalated cadence (never a multi-year tombstone — see the 2026-08-18
    incident: 5 tasks stranded until an operator manually requeued them)."""
    mocker.patch("app.broadcast.broadcast_to_project", new_callable=AsyncMock)
    project = await _make_project(reaper_db)
    now = _utcnow()
    from app.reaper_sweeps import _BLOCKED_PARKED_REEVAL_INTERVAL
    from app.blocked_task_unblock_sweeps import _BLOCKED_ESCALATED_RECHECK_MAX

    # (a) Unsatisfiable capability need — no candidate matches.
    needful = await _parked_blocker(
        reaper_db,
        project,
        need={"kind": "capability", "ref": "auth-subsystem-that-does-not-exist"},
        now=now,
    )
    # (b) No structured need at all. Use a non-transient blocker_kind (transient
    # ones — file_conflict, pending_rollup — take the separate precondition-
    # reevaluation branch just above this one).
    needless = await _blocked_task(
        reaper_db,
        project,
        depends_on=[],
        blocker_kind="upstream_dependency",
        context={"_blocked_escalated": True, "blocker": {"source": "agent"}},
        recheck_after=now - timedelta(minutes=1),
    )

    requeued = await sweep_unblock_blocked_tasks(reaper_db, now)

    assert requeued == 0
    await reaper_db.refresh(needful)
    await reaper_db.refresh(needless)
    # Needful-but-unsatisfiable: still parked, re-armed at the LOW cadence so it
    # is re-examined when its foundation eventually lands.
    assert needful.status == TaskStatus.BLOCKED
    assert needful.context.get("_blocked_escalated") is True
    assert needful.recheck_after is not None
    assert needful.recheck_after <= now + _BLOCKED_PARKED_REEVAL_INTERVAL + timedelta(
        minutes=1
    )
    # No-need blocker: stays BLOCKED+escalated (still an operator-tier marker —
    # queryable via context['_blocked_escalated']) but re-armed on the bounded
    # backoff cadence, NEVER a fake far-future timestamp — a fix landing outside
    # the dependency graph (runner gains a capability, a gate bug is fixed) must
    # still get picked back up automatically.
    assert needless.status == TaskStatus.BLOCKED
    assert needless.context.get("_blocked_escalated") is True
    assert needless.recheck_after is not None
    assert needless.recheck_after <= now + _BLOCKED_ESCALATED_RECHECK_MAX + timedelta(
        minutes=1
    )
    far_future = now + timedelta(days=3000)
    assert needless.recheck_after < far_future, (
        "a needless non-transient blocker must never be tombstoned at a fake "
        "far-future recheck_after"
    )

    # Repeated sweeps neither resume, loop, nor create a duplicate alert.
    await sweep_unblock_blocked_tasks(reaper_db, now + timedelta(hours=7))
    alerts = (
        (await reaper_db.execute(_select(Alert).where(Alert.task_id == needful.id)))
        .scalars()
        .all()
    )
    assert len(alerts) == 1  # the original escalation alert; no re-fire


@pytest.mark.asyncio
async def test_unblock_sweep_cleared_conflict_starts_fresh_block_episode(
    reaper_db, mocker
):
    """A conflict with no remaining writer is a genuine clear, not a retry."""
    mocker.patch("app.broadcast.broadcast_to_project", new_callable=AsyncMock)
    project = await _make_project(reaper_db)
    onset = (_utcnow() - timedelta(hours=5)).isoformat()
    t = await _blocked_task(
        reaper_db,
        project,
        depends_on=[],
        context={"_blocked_first_at": {"key": "file conflict on a.py", "at": onset}},
    )

    requeued = await sweep_unblock_blocked_tasks(reaper_db, _utcnow())

    assert requeued == 1
    await reaper_db.refresh(t)
    assert t.status == TaskStatus.QUEUED
    assert "_blocked_first_at" not in (t.context or {})
    assert len(t.attempt_history or []) == 1


@pytest.mark.asyncio
async def test_unblock_sweep_wipes_first_blocked_on_genuine_dep_clear(
    reaper_db, mocker
):
    """A genuine dependency clear (had dep edges) fully resets blocker
    bookkeeping, including the _blocked_first_at onset stamp."""
    mocker.patch("app.broadcast.broadcast_to_project", new_callable=AsyncMock)
    project = await _make_project(reaper_db)
    dep = Task(
        project_id=project.id,
        title="dep",
        description="x",
        status=TaskStatus.COMPLETED,
        created_by="u",
        requires_approval=False,
        payloads=[{"type": "fix"}],
        context={},
        status_log=[],
    )
    reaper_db.add(dep)
    await reaper_db.commit()
    await reaper_db.refresh(dep)
    onset = (_utcnow() - timedelta(hours=5)).isoformat()
    t = await _blocked_task(
        reaper_db,
        project,
        blocker_kind="upstream_dependency",
        depends_on=[str(dep.id)],
        context={"_blocked_first_at": {"key": "upstream", "at": onset}},
    )
    verify = AsyncMock(return_value=True)

    requeued = await sweep_unblock_blocked_tasks(
        reaper_db, _utcnow(), verify_pr_merged=verify
    )

    assert requeued == 1
    await reaper_db.refresh(t)
    assert t.status == TaskStatus.QUEUED
    assert "_blocked_first_at" not in (t.context or {})


@pytest.mark.asyncio
async def test_unblock_sweep_conflict_blocker_with_stale_satisfied_dep_escalates_past_deadline(
    reaper_db, mocker
):
    """Regression: a conflict blocker that ALSO carries an already-satisfied
    dependency edge must still escalate past the deadline.

    A successfully-completed dependency id is never stripped from depends_on, so
    a file_conflict/pending_rollup blocker routinely lists a now-completed dep.
    The escalation discriminator must key off the blocker IDENTITY, not the mere
    presence of dep edges — otherwise the conflict loops QUEUED<->BLOCKED forever
    and the operator ERROR alert never fires.
    """
    mocker.patch("app.broadcast.broadcast_to_project", new_callable=AsyncMock)
    project = await _make_project(reaper_db)
    dep = Task(
        project_id=project.id,
        title="dep",
        description="x",
        status=TaskStatus.COMPLETED,
        created_by="u",
        requires_approval=False,
        payloads=[{"type": "fix"}],
        context={},
        status_log=[],
    )
    writer = Task(
        project_id=project.id,
        title="active writer",
        description="x",
        status=TaskStatus.IN_PROGRESS,
        created_by="u",
        requires_approval=False,
        payloads=[{"type": "fix"}],
        context={"related_files": ["a.py"]},
        status_log=[],
    )
    reaper_db.add(dep)
    reaper_db.add(writer)
    await reaper_db.commit()
    await reaper_db.refresh(dep)
    now = _utcnow()
    t = await _blocked_task(
        reaper_db,
        project,
        blocker_kind="file_conflict",
        depends_on=[str(dep.id)],
        blocked_at=now - _BLOCKED_ESCALATE_AFTER - timedelta(minutes=5),
        recheck_after=now - timedelta(minutes=1),
    )
    verify = AsyncMock(return_value=True)

    requeued = await sweep_unblock_blocked_tasks(
        reaper_db, now, verify_pr_merged=verify
    )

    # Escalated terminally, NOT requeued — the conflict never cleared.
    assert requeued == 0
    await reaper_db.refresh(t)
    assert t.status == TaskStatus.BLOCKED
    assert t.context.get("_blocked_escalated") is True
    alerts = (
        (
            await reaper_db.execute(
                _select(Alert).where(
                    Alert.task_id == t.id, Alert.severity == AlertSeverity.ERROR
                )
            )
        )
        .scalars()
        .all()
    )
    assert len(alerts) == 1


@pytest.mark.asyncio
async def test_unblock_sweep_unchanged_conflict_with_stale_dep_stays_blocked(
    reaper_db, mocker
):
    """A satisfied stale edge cannot override an unchanged named conflict."""
    mocker.patch("app.broadcast.broadcast_to_project", new_callable=AsyncMock)
    project = await _make_project(reaper_db)
    dep = Task(
        project_id=project.id,
        title="dep",
        description="x",
        status=TaskStatus.COMPLETED,
        created_by="u",
        requires_approval=False,
        payloads=[{"type": "fix"}],
        context={},
        status_log=[],
    )
    writer = Task(
        project_id=project.id,
        title="active writer",
        description="x",
        status=TaskStatus.IN_PROGRESS,
        created_by="u",
        requires_approval=False,
        payloads=[{"type": "fix"}],
        context={"related_files": ["a.py"]},
        status_log=[],
    )
    reaper_db.add(dep)
    reaper_db.add(writer)
    await reaper_db.commit()
    await reaper_db.refresh(dep)
    now = _utcnow()
    onset = (now - timedelta(hours=1)).isoformat()
    t = await _blocked_task(
        reaper_db,
        project,
        blocker_kind="file_conflict",
        depends_on=[str(dep.id)],
        blocked_at=now - timedelta(hours=1),
        recheck_after=now - timedelta(minutes=1),
        context={"_blocked_first_at": {"key": "file conflict on a.py", "at": onset}},
    )
    verify = AsyncMock(return_value=True)

    requeued = await sweep_unblock_blocked_tasks(
        reaper_db, now, verify_pr_merged=verify
    )

    assert requeued == 0
    await reaper_db.refresh(t)
    assert t.status == TaskStatus.BLOCKED
    assert t.context.get("_blocked_first_at", {}).get("at") == onset
    assert t.context.get("_blocked_escalated") is None
    assert not (t.attempt_history or [])


@pytest.mark.asyncio
async def test_unblock_sweep_cleared_conflict_resets_defer_counter(
    reaper_db, mocker
):
    """A named conflict that disappeared starts a clean block episode."""
    mocker.patch("app.broadcast.broadcast_to_project", new_callable=AsyncMock)
    project = await _make_project(reaper_db)
    blocker_key = "file conflict on src/foo.py"
    t = await _blocked_task(
        reaper_db,
        project,
        blocker_kind="file_conflict",
        depends_on=[],
        context={
            "_precondition_defer": {"key": blocker_key, "count": 3},
            "_precondition_escalated_key": blocker_key,
            "_blocked_first_at": {"key": blocker_key, "at": "2026-01-01T00:00:00"},
        },
    )

    requeued = await sweep_unblock_blocked_tasks(reaper_db, _utcnow())

    assert requeued == 1
    await reaper_db.refresh(t)
    assert t.status == TaskStatus.QUEUED
    ctx = t.context or {}
    assert "_precondition_defer" not in ctx
    assert "_precondition_escalated_key" not in ctx
    assert len(t.attempt_history or []) == 1


@pytest.mark.asyncio
async def test_genuine_dep_clear_wipes_defer_counter(reaper_db, mocker):
    """A genuine dependency clear must wipe _precondition_defer and _precondition_escalated_key.

    When a real blocking dependency completes and _clear_blocked_fields is called
    with preserve_first_blocked=False, the defer counter must be erased so a fresh
    block starts counting from zero.
    """
    from app.blocked_task_unblock_sweeps import _clear_blocked_fields

    project = await _make_project(reaper_db)
    blocker_key = "file conflict on src/foo.py"
    t = await _blocked_task(
        reaper_db,
        project,
        blocker_kind="file_conflict",
        depends_on=[],
        context={
            "_precondition_defer": {"key": blocker_key, "count": 3},
            "_precondition_escalated_key": blocker_key,
            "_blocked_first_at": {"key": blocker_key, "at": "2026-01-01T00:00:00"},
        },
    )

    _clear_blocked_fields(t, preserve_first_blocked=False)

    ctx = t.context or {}
    assert "_precondition_defer" not in ctx, (
        "_precondition_defer must be wiped on genuine clear"
    )
    assert "_precondition_escalated_key" not in ctx, (
        "_precondition_escalated_key must be wiped on genuine clear"
    )
    assert "_blocked_first_at" not in ctx


@pytest.mark.asyncio
async def test_clear_blocked_fields_resets_optimistic_budget_on_genuine_clear(
    reaper_db,
):
    """_clear_blocked_fields wipes _blocked_optimistic_retries on genuine clear and
    preserves it on optimistic requeue.

    A genuine dependency clear (preserve_first_blocked=False) must reset the counter
    so a later, unrelated block starts with a fresh budget.  An optimistic requeue
    (preserve_first_blocked=True) must keep the counter so the cap is correctly
    enforced across requeue episodes.
    """
    from app.blocked_task_unblock_sweeps import _clear_blocked_fields

    project = await _make_project(reaper_db)

    # --- genuine clear path ---
    t_genuine = await _blocked_task(
        reaper_db,
        project,
        blocker_kind="file_conflict",
        depends_on=[],
        context={
            "_blocked_optimistic_retries": 2,
            "_blocked_first_at": {"key": "k", "at": "2026-01-01T00:00:00"},
        },
    )
    _clear_blocked_fields(t_genuine, preserve_first_blocked=False)
    ctx_genuine = t_genuine.context or {}
    assert "_blocked_optimistic_retries" not in ctx_genuine, (
        "_blocked_optimistic_retries must be wiped on genuine clear so a fresh block"
        " starts with a full budget"
    )

    # --- optimistic requeue path ---
    t_optimistic = await _blocked_task(
        reaper_db,
        project,
        blocker_kind="file_conflict",
        depends_on=[],
        context={
            "_blocked_optimistic_retries": 1,
            "_blocked_first_at": {"key": "k", "at": "2026-01-01T00:00:00"},
        },
    )
    _clear_blocked_fields(t_optimistic, preserve_first_blocked=True)
    ctx_optimistic = t_optimistic.context or {}
    assert ctx_optimistic.get("_blocked_optimistic_retries") == 1, (
        "_blocked_optimistic_retries must be preserved on optimistic requeue so the"
        " cap accumulates across episodes"
    )


@pytest.mark.asyncio
async def test_unblock_sweep_clears_failure_class_so_later_genuine_failure_reclassifies(
    reaper_db, mocker
):
    """A recovered task must not carry the stale BLOCKED_PRECONDITION tag: a
    later, genuinely different failure must classify on its own merits instead
    of being short-circuited as the budget-exempt blocked class."""
    mocker.patch("app.broadcast.broadcast_to_project", new_callable=AsyncMock)
    from app.failure_classification import classify_failure

    project = await _make_project(reaper_db)
    t = await _blocked_task(
        reaper_db,
        project,
        depends_on=[],
        context={
            "blocker": {"source": "agent", "waiting_on": "file conflict"},
            "failure_class": "BLOCKED_PRECONDITION",
            "failure_category": "BLOCKED_PRECONDITION",
        },
    )

    # Pre-fix sanity: while the stale tag is present the classifier
    # short-circuits to the blocked class regardless of the real reason.
    assert classify_failure(t) == "BLOCKED_PRECONDITION"

    requeued = await sweep_unblock_blocked_tasks(reaper_db, _utcnow())
    assert requeued == 1
    await reaper_db.refresh(t)
    assert t.status == TaskStatus.QUEUED

    # Simulate a subsequent, genuinely different failure on the recovered task.
    ctx = dict(t.context or {})
    ctx["failure_category"] = "test_failed"
    t.context = ctx
    assert classify_failure(t) == "CODE_ERROR", (
        "recovered task must reclassify a real failure, not report the stale "
        "BLOCKED_PRECONDITION class"
    )


@pytest.mark.asyncio
async def test_unblock_sweep_escalation_marks_structured_record_escalated(
    reaper_db, mocker
):
    """Terminal deadline escalation must mark the structured blocker record as
    escalated so its state reflects reality (not a stale escalated=False)."""
    mocker.patch("app.broadcast.broadcast_to_project", new_callable=AsyncMock)
    project = await _make_project(reaper_db)
    now = _utcnow()
    t = await _blocked_task(
        reaper_db,
        project,
        depends_on=[],
        context={
            "blocker": {
                "source": "agent",
                "waiting_on": "file conflict",
                "escalated": False,
            },
            "failure_class": "BLOCKED_PRECONDITION",
            "failure_category": "BLOCKED_PRECONDITION",
        },
        blocked_at=now - _BLOCKED_ESCALATE_AFTER - timedelta(minutes=5),
        recheck_after=now - timedelta(minutes=1),
    )

    requeued = await sweep_unblock_blocked_tasks(reaper_db, now)

    assert requeued == 0
    await reaper_db.refresh(t)
    assert t.status == TaskStatus.BLOCKED
    assert t.context.get("_blocked_escalated") is True
    blocker = (t.context or {}).get("blocker") or {}
    assert blocker.get("escalated") is True
    assert blocker.get("escalated_at")


@pytest.mark.asyncio
async def test_full_reaper_cycle_does_not_reap_blocked_task(
    reaper_db, stop_after_one_cycle, reaper_side_effects
):
    """A BLOCKED task (no VM, recheck in future) survives a full reaper cycle —
    the stale/orphan sweeps must never reap it to FAILED."""
    project = Project(
        name="reaper-blocked",
        user_id="user1",
        integration_branch="main",
        promotion_target="main",
    )
    reaper_db.add(project)
    await reaper_db.commit()
    await reaper_db.refresh(project)
    t = await _blocked_task(
        reaper_db,
        project,
        depends_on=[],
        started_at=_utcnow() - timedelta(hours=2),
        recheck_after=_utcnow() + timedelta(hours=1),  # not due → sweep skips
    )

    await _run_reaper_once()

    await reaper_db.refresh(t)
    assert t.status == TaskStatus.BLOCKED


# ── Step 15: linked bug-class message convergence / resurfacing sweep ─────────

from app.reaper_sweeps import sweep_converge_linked_bug_messages  # noqa: E402
from app.models import MessageType, ProjectMessage  # noqa: E402
from app.models import Direction  # noqa: E402
from app.message_convergence import link_message_to_outcome  # noqa: E402


async def _make_bug_message(
    db,
    project,
    *,
    msg_type=MessageType.BUG_REPORT,
    status="open",
    context=None,
    from_type="system",
):
    """Create a ProjectMessage of bug-class type and flush it."""
    msg = ProjectMessage(
        project_id=project.id,
        message_type=msg_type,
        from_type=from_type,
        content="Test bug report",
        status=status,
        context=context or {},
    )
    db.add(msg)
    await db.commit()
    await db.refresh(msg)
    return msg


def _assert_message_update_event_payload(
    payload,
    *,
    project,
    msg,
    status: str,
    action: str,
) -> None:
    message_type = getattr(msg.message_type, "value", str(msg.message_type))
    assert payload["schema_version"] == 1
    assert payload["project_id"] == str(project.id)
    assert payload["resource"] == "project_message"
    assert payload["action"] == action
    assert payload["message_id"] == str(msg.id)
    assert payload["type"] == message_type
    assert payload["from"] == msg.from_type
    assert payload["status"] == status
    msg_payload = payload["message"]
    assert msg_payload["id"] == str(msg.id)
    assert msg_payload["type"] == message_type
    assert msg_payload["from"] == msg.from_type
    assert msg_payload["status"] == status
    assert msg_payload["excerpt"] == msg.content
    assert payload["refetch"] == {"surfaces": ["messages", "strategic_session"]}


async def _make_direction_row(
    db, project, *, direction_key="d-abc123", status="active"
):
    """Create a Direction row for convergence tests."""
    row = Direction(
        project_id=project.id,
        direction_key=direction_key,
        status=status,
        data={"id": direction_key, "status": status},
    )
    db.add(row)
    await db.commit()
    await db.refresh(row)
    return row


@pytest.mark.asyncio
async def test_linked_message_converges_when_task_lands_with_merged_pr(reaper_db):
    """A 'linked' bug message is resolved when its owning task is COMPLETED + pr_merged."""
    project = await _make_project(reaper_db)
    msg = await _make_bug_message(reaper_db, project)

    task = Task(
        project_id=project.id,
        payloads=[{"type": "fix"}],
        status=TaskStatus.COMPLETED,
        context={
            "pr_url": "https://github.com/example/repo/pull/42",
            "pr_merged": True,
        },
    )
    reaper_db.add(task)
    await reaper_db.commit()
    await reaper_db.refresh(task)

    link_message_to_outcome(
        msg,
        "task",
        str(task.id),
        linked_at=_utcnow() - timedelta(hours=1),
    )
    reaper_db.add(msg)
    await reaper_db.commit()

    acted = await sweep_converge_linked_bug_messages(reaper_db, _utcnow())

    assert acted == 1
    await reaper_db.refresh(msg)
    assert msg.status == "resolved"
    assert msg.resolution is not None
    assert "landed" in msg.resolution
    assert msg.resolved_at is not None
    assert msg.context.get("_convergence_converged_at") is not None


@pytest.mark.asyncio
async def test_linked_message_left_linked_when_task_completed_but_not_landed(reaper_db):
    """COMPLETED != merged: a task that completed WITHOUT the landed signal has not
    actually landed the fix (its PR may be open/unmerged, tracked only in Artifact),
    so within the time bound the message stays 'linked' rather than being silently
    converged — the exact false-convergence this feature exists to prevent."""
    project = await _make_project(reaper_db)
    msg = await _make_bug_message(reaper_db, project)

    task = Task(
        project_id=project.id,
        payloads=[{"type": "fix"}],
        status=TaskStatus.COMPLETED,
        context={},  # no landed signal → COMPLETED but not merged
    )
    reaper_db.add(task)
    await reaper_db.commit()
    await reaper_db.refresh(task)

    link_message_to_outcome(
        msg,
        "task",
        str(task.id),
        linked_at=_utcnow() - timedelta(hours=1),
    )
    reaper_db.add(msg)
    await reaper_db.commit()

    acted = await sweep_converge_linked_bug_messages(reaper_db, _utcnow())

    assert acted == 0
    await reaper_db.refresh(msg)
    assert msg.status == "linked"


@pytest.mark.asyncio
async def test_linked_message_resurfaces_when_task_fails(reaper_db):
    """A 'linked' bug message resurfaces to 'open' when its owning task FAILED."""
    project = await _make_project(reaper_db)
    msg = await _make_bug_message(reaper_db, project)

    task = Task(
        project_id=project.id,
        payloads=[{"type": "fix"}],
        status=TaskStatus.FAILED,
        context={},
    )
    reaper_db.add(task)
    await reaper_db.commit()
    await reaper_db.refresh(task)

    link_message_to_outcome(msg, "task", str(task.id))
    reaper_db.add(msg)
    await reaper_db.commit()

    acted = await sweep_converge_linked_bug_messages(reaper_db, _utcnow())

    assert acted == 1
    await reaper_db.refresh(msg)
    assert msg.status == "open"
    ctx = msg.context or {}
    assert ctx.get("_convergence_link_stale") is not None
    assert "_convergence_link" not in ctx
    assert "FAILED" in (ctx.get("_convergence_resurfaced_reason") or "")


@pytest.mark.asyncio
async def test_linked_message_resurface_broadcasts_workbench_envelope(
    reaper_db, mocker
):
    """Linked-message updates publish the shared workbench message envelope."""
    project = await _make_project(reaper_db)
    msg = await _make_bug_message(reaper_db, project)
    task = Task(
        project_id=project.id,
        payloads=[{"type": "fix"}],
        status=TaskStatus.FAILED,
        context={},
    )
    reaper_db.add(task)
    await reaper_db.commit()
    await reaper_db.refresh(task)
    link_message_to_outcome(msg, "task", str(task.id))
    reaper_db.add(msg)
    await reaper_db.commit()
    broadcast = mocker.patch(
        "app.broadcast.broadcast_to_project", new_callable=AsyncMock
    )

    acted = await sweep_converge_linked_bug_messages(reaper_db, _utcnow())

    assert acted == 1
    await reaper_db.refresh(msg)
    broadcast.assert_awaited_once()
    project_id, event, payload = broadcast.await_args.args
    assert project_id == str(project.id)
    assert event == "message_updated"
    _assert_message_update_event_payload(
        payload,
        project=project,
        msg=msg,
        status="open",
        action="updated",
    )


@pytest.mark.asyncio
async def test_linked_message_resurfaces_when_task_cancelled(reaper_db):
    """A 'linked' bug message resurfaces to 'open' when its owning task is CANCELLED."""
    project = await _make_project(reaper_db)
    msg = await _make_bug_message(reaper_db, project, msg_type=MessageType.BLOCKER)

    task = Task(
        project_id=project.id,
        payloads=[{"type": "fix"}],
        status=TaskStatus.CANCELLED,
        context={},
    )
    reaper_db.add(task)
    await reaper_db.commit()
    await reaper_db.refresh(task)

    link_message_to_outcome(msg, "task", str(task.id))
    reaper_db.add(msg)
    await reaper_db.commit()

    acted = await sweep_converge_linked_bug_messages(reaper_db, _utcnow())

    assert acted == 1
    await reaper_db.refresh(msg)
    assert msg.status == "open"
    assert "CANCELLED" in (msg.context or {}).get("_convergence_resurfaced_reason", "")


@pytest.mark.asyncio
async def test_linked_message_converges_when_direction_completed(reaper_db):
    """A 'linked' bug message is resolved when its owning direction is 'completed'."""
    project = await _make_project(reaper_db)
    msg = await _make_bug_message(reaper_db, project)
    dir_row = await _make_direction_row(
        reaper_db, project, direction_key="d-comp1", status="completed"
    )

    link_message_to_outcome(msg, "direction", dir_row.direction_key)
    reaper_db.add(msg)
    await reaper_db.commit()

    acted = await sweep_converge_linked_bug_messages(reaper_db, _utcnow())

    assert acted == 1
    await reaper_db.refresh(msg)
    assert msg.status == "resolved"
    assert "completed" in (msg.resolution or "")
    assert msg.resolved_at is not None


@pytest.mark.asyncio
@pytest.mark.parametrize("dir_status", ["retired", "rejected", "expired", "superseded"])
async def test_linked_message_resurfaces_when_direction_ended_without_landing(
    reaper_db, dir_status
):
    """A 'linked' bug message resurfaces when its owning direction ends non-completed."""
    project = await _make_project(reaper_db)
    msg = await _make_bug_message(reaper_db, project)
    dir_row = await _make_direction_row(
        reaper_db, project, direction_key=f"d-{dir_status}", status=dir_status
    )

    link_message_to_outcome(msg, "direction", dir_row.direction_key)
    reaper_db.add(msg)
    await reaper_db.commit()

    acted = await sweep_converge_linked_bug_messages(reaper_db, _utcnow())

    assert acted == 1
    await reaper_db.refresh(msg)
    assert msg.status == "open"
    ctx = msg.context or {}
    assert ctx.get("_convergence_link_stale") is not None
    assert dir_status in (ctx.get("_convergence_resurfaced_reason") or "")


@pytest.mark.asyncio
async def test_linked_message_resurfaces_when_owning_task_missing(reaper_db):
    """A 'linked' bug message resurfaces when its owning task no longer exists."""
    project = await _make_project(reaper_db)
    msg = await _make_bug_message(reaper_db, project)
    import uuid as _uuid

    nonexistent_id = str(_uuid.uuid4())
    link_message_to_outcome(msg, "task", nonexistent_id)
    reaper_db.add(msg)
    await reaper_db.commit()

    acted = await sweep_converge_linked_bug_messages(reaper_db, _utcnow())

    assert acted == 1
    await reaper_db.refresh(msg)
    assert msg.status == "open"
    assert "no longer exists" in (msg.context or {}).get(
        "_convergence_resurfaced_reason", ""
    )


@pytest.mark.asyncio
async def test_linked_message_left_linked_when_outcome_still_in_flight(reaper_db):
    """A 'linked' message is untouched when its owning task is still actively in flight."""
    project = await _make_project(reaper_db)
    msg = await _make_bug_message(reaper_db, project)

    task = Task(
        project_id=project.id,
        payloads=[{"type": "fix"}],
        status=TaskStatus.IN_PROGRESS,
        context={},
    )
    reaper_db.add(task)
    await reaper_db.commit()
    await reaper_db.refresh(task)

    # Link very recently — well within the 7-day resurface window.
    link_message_to_outcome(
        msg, "task", str(task.id), linked_at=_utcnow() - timedelta(hours=2)
    )
    reaper_db.add(msg)
    await reaper_db.commit()

    acted = await sweep_converge_linked_bug_messages(reaper_db, _utcnow())

    assert acted == 0
    await reaper_db.refresh(msg)
    assert msg.status == "linked"


@pytest.mark.asyncio
async def test_linked_message_resurfaces_when_in_flight_exceeds_time_bound(reaper_db):
    """A 'linked' message resurfaces when its task is still in flight past the time bound."""
    project = await _make_project(reaper_db)
    msg = await _make_bug_message(reaper_db, project)

    task = Task(
        project_id=project.id,
        payloads=[{"type": "fix"}],
        status=TaskStatus.QUEUED,
        context={},
    )
    reaper_db.add(task)
    await reaper_db.commit()
    await reaper_db.refresh(task)

    # Link older than the default 7-day resurface window.
    link_message_to_outcome(
        msg, "task", str(task.id), linked_at=_utcnow() - timedelta(days=8)
    )
    reaper_db.add(msg)
    await reaper_db.commit()

    acted = await sweep_converge_linked_bug_messages(reaper_db, _utcnow())

    assert acted == 1
    await reaper_db.refresh(msg)
    assert msg.status == "open"
    assert "QUEUED" in (msg.context or {}).get("_convergence_resurfaced_reason", "")


@pytest.mark.asyncio
async def test_linked_message_resurfaces_when_task_completed_but_never_landed(
    reaper_db,
):
    """A task COMPLETED but never landed (no merge signal) past the time bound
    resurfaces — the fix never actually merged, so the bug returns to the strategist."""
    project = await _make_project(reaper_db)
    msg = await _make_bug_message(reaper_db, project)

    task = Task(
        project_id=project.id,
        payloads=[{"type": "fix"}],
        status=TaskStatus.COMPLETED,
        context={},  # COMPLETED but no landed signal → never merged
    )
    reaper_db.add(task)
    await reaper_db.commit()
    await reaper_db.refresh(task)

    # Linked longer ago than the default 7-day resurface window.
    link_message_to_outcome(
        msg, "task", str(task.id), linked_at=_utcnow() - timedelta(days=8)
    )
    reaper_db.add(msg)
    await reaper_db.commit()

    acted = await sweep_converge_linked_bug_messages(reaper_db, _utcnow())

    assert acted == 1
    await reaper_db.refresh(msg)
    assert msg.status == "open"
    ctx = msg.context or {}
    assert ctx.get("_convergence_link_stale") is not None
    assert "did not land" in (ctx.get("_convergence_resurfaced_reason") or "")


# ── Step 18b/18c: generalized linked-resolve + stale-open decay sweeps ─────────

from app.reaper_sweeps import (  # noqa: E402
    sweep_resolve_linked_messages,
    sweep_decay_stale_open_messages,
    STALE_OPEN_DECAY_DAYS,
    STALE_QUESTION_DECAY_DAYS,
)


async def _make_conversational_message(
    db, project, *, msg_type=MessageType.NOTE, status="open", context=None,
    created_at=None,
):
    kwargs = dict(
        project_id=project.id,
        message_type=msg_type,
        from_type="strategic",
        content="conversational item",
        status=status,
        context=context or {},
    )
    if created_at is not None:
        kwargs["created_at"] = created_at
    msg = ProjectMessage(**kwargs)
    db.add(msg)
    await db.commit()
    await db.refresh(msg)
    return msg


@pytest.mark.asyncio
async def test_resolve_linked_messages_resolves_non_bug_on_landing(reaper_db):
    """A non-bug 'linked' NOTE auto-resolves once its owning task lands."""
    project = await _make_project(reaper_db)
    task = Task(
        project_id=project.id,
        payloads=[{"type": "fix"}],
        status=TaskStatus.COMPLETED,
        context={"pr_merged": True},
    )
    reaper_db.add(task)
    await reaper_db.commit()
    await reaper_db.refresh(task)

    msg = await _make_conversational_message(reaper_db, project)
    link_message_to_outcome(msg, "task", str(task.id))
    reaper_db.add(msg)
    await reaper_db.commit()

    acted = await sweep_resolve_linked_messages(reaper_db, _utcnow())

    assert acted == 1
    await reaper_db.refresh(msg)
    assert msg.status == "resolved"
    assert "landed" in (msg.resolution or "")
    assert msg.resolved_at is not None


@pytest.mark.asyncio
async def test_resolve_linked_messages_skips_bug_class(reaper_db):
    """Bug-class 'linked' messages stay owned by the Step-15 sweep (disjoint)."""
    project = await _make_project(reaper_db)
    task = Task(
        project_id=project.id,
        payloads=[{"type": "fix"}],
        status=TaskStatus.COMPLETED,
        context={"pr_merged": True},
    )
    reaper_db.add(task)
    await reaper_db.commit()
    await reaper_db.refresh(task)

    bug = await _make_bug_message(reaper_db, project)
    link_message_to_outcome(bug, "task", str(task.id))
    reaper_db.add(bug)
    await reaper_db.commit()

    acted = await sweep_resolve_linked_messages(reaper_db, _utcnow())

    assert acted == 0
    await reaper_db.refresh(bug)
    assert bug.status == "linked"


@pytest.mark.asyncio
async def test_resolve_linked_messages_left_linked_when_not_landed(reaper_db):
    """A non-bug 'linked' message is untouched when its owner has not landed."""
    project = await _make_project(reaper_db)
    task = Task(
        project_id=project.id,
        payloads=[{"type": "fix"}],
        status=TaskStatus.IN_PROGRESS,
        context={},
    )
    reaper_db.add(task)
    await reaper_db.commit()
    await reaper_db.refresh(task)

    msg = await _make_conversational_message(
        reaper_db, project, msg_type=MessageType.QUESTION
    )
    link_message_to_outcome(msg, "task", str(task.id))
    reaper_db.add(msg)
    await reaper_db.commit()

    acted = await sweep_resolve_linked_messages(reaper_db, _utcnow())

    assert acted == 0
    await reaper_db.refresh(msg)
    assert msg.status == "linked"


@pytest.mark.asyncio
async def test_decay_stale_open_messages_resolves_aged_note(reaper_db):
    """An OPEN NOTE with no activity past the decay window is auto-decayed."""
    project = await _make_project(reaper_db)
    old = _utcnow() - timedelta(days=STALE_OPEN_DECAY_DAYS + 1)
    msg = await _make_conversational_message(
        reaper_db, project, msg_type=MessageType.NOTE, created_at=old
    )

    acted = await sweep_decay_stale_open_messages(reaper_db, _utcnow())

    assert acted == 1
    await reaper_db.refresh(msg)
    assert msg.status == "resolved"
    assert msg.resolution == "auto-decayed (stale)"


@pytest.mark.asyncio
async def test_decay_stale_open_leaves_recent_and_live_types(reaper_db):
    """Recent NOTEs, BLOCKER, and QUESTIONs under 30d are never decayed."""
    project = await _make_project(reaper_db)
    # 19 days: past NOTE threshold (14d) but before QUESTION threshold (30d)
    old = _utcnow() - timedelta(days=STALE_OPEN_DECAY_DAYS + 5)
    recent_note = await _make_conversational_message(
        reaper_db, project, msg_type=MessageType.NOTE
    )  # created now
    live_blocker = await _make_conversational_message(
        reaper_db, project, msg_type=MessageType.BLOCKER, created_at=old
    )
    # QUESTION at 19 days: below STALE_QUESTION_DECAY_DAYS (30d), must survive
    young_question = await _make_conversational_message(
        reaper_db, project, msg_type=MessageType.QUESTION, created_at=old
    )

    acted = await sweep_decay_stale_open_messages(reaper_db, _utcnow())

    assert acted == 0
    for m in (recent_note, live_blocker, young_question):
        await reaper_db.refresh(m)
        assert m.status == "open"


@pytest.mark.asyncio
async def test_decay_stale_open_messages_resolves_aged_question(reaper_db):
    """An OPEN QUESTION past the 30-day question window is auto-decayed as 'stale_unanswered'."""
    project = await _make_project(reaper_db)
    old = _utcnow() - timedelta(days=STALE_QUESTION_DECAY_DAYS + 1)
    msg = await _make_conversational_message(
        reaper_db, project, msg_type=MessageType.QUESTION, created_at=old
    )

    acted = await sweep_decay_stale_open_messages(reaper_db, _utcnow())

    assert acted == 1
    await reaper_db.refresh(msg)
    assert msg.status == "resolved"
    assert msg.resolution == "stale_unanswered"


@pytest.mark.asyncio
async def test_decay_stale_open_question_survives_short_window(reaper_db):
    """An OPEN QUESTION older than NOTE decay window (14d) but younger than the
    question window (30d) must NOT be decayed — questions get more patience."""
    project = await _make_project(reaper_db)
    # 15 days: past NOTE threshold (14d) but before QUESTION threshold (30d)
    mildly_old = _utcnow() - timedelta(days=STALE_OPEN_DECAY_DAYS + 1)
    msg = await _make_conversational_message(
        reaper_db, project, msg_type=MessageType.QUESTION, created_at=mildly_old
    )

    acted = await sweep_decay_stale_open_messages(reaper_db, _utcnow())

    assert acted == 0
    await reaper_db.refresh(msg)
    assert msg.status == "open"


@pytest.mark.asyncio
async def test_decay_stale_open_question_respects_runtime_config(reaper_db, monkeypatch):
    """'stale_question_decay_days' runtime config overrides the default 30-day window."""
    import app.shared_state as _ss

    project = await _make_project(reaper_db)
    # 5 days old — within default window (30d) but past the custom window (3d)
    slightly_old = _utcnow() - timedelta(days=5)
    msg = await _make_conversational_message(
        reaper_db, project, msg_type=MessageType.QUESTION, created_at=slightly_old
    )

    original = dict(_ss.runtime_config)
    monkeypatch.setattr(_ss, "runtime_config", {**original, "stale_question_decay_days": 3})
    acted = await sweep_decay_stale_open_messages(reaper_db, _utcnow())
    monkeypatch.setattr(_ss, "runtime_config", original)

    assert acted == 1
    await reaper_db.refresh(msg)
    assert msg.status == "resolved"
    assert msg.resolution == "stale_unanswered"


@pytest.mark.asyncio
async def test_decay_stale_open_skips_operator_action_needed_note(reaper_db):
    """An operator-action-needed NOTE older than 14 days but under 60 days survives."""
    from app.message_audience import OPERATOR_ACTION_NEEDED

    project = await _make_project(reaper_db)
    # Older than the normal decay window (14 days) but younger than the OAN
    # threshold (60 days) — must survive.
    old = _utcnow() - timedelta(days=STALE_OPEN_DECAY_DAYS + 1)
    ctx = {"audience": OPERATOR_ACTION_NEEDED}
    msg = await _make_conversational_message(
        reaper_db, project, msg_type=MessageType.NOTE, created_at=old, context=ctx,
    )

    acted = await sweep_decay_stale_open_messages(reaper_db, _utcnow())

    assert acted == 0
    await reaper_db.refresh(msg)
    assert msg.status == "open"
    assert msg.resolution is None


@pytest.mark.asyncio
async def test_decay_stale_open_resolves_ordinary_note_with_operator_fyi(reaper_db):
    """An ordinary NOTE with operator-fyi audience still decays — only
    operator-action-needed blocks auto-resolution."""
    from app.message_audience import OPERATOR_FYI

    project = await _make_project(reaper_db)
    old = _utcnow() - timedelta(days=STALE_OPEN_DECAY_DAYS + 1)
    ctx = {"audience": OPERATOR_FYI}
    msg = await _make_conversational_message(
        reaper_db, project, msg_type=MessageType.NOTE, created_at=old, context=ctx,
    )

    acted = await sweep_decay_stale_open_messages(reaper_db, _utcnow())

    assert acted == 1
    await reaper_db.refresh(msg)
    assert msg.status == "resolved"
    assert msg.resolution == "auto-decayed (stale)"


@pytest.mark.asyncio
async def test_decay_stale_open_messages_reduces_operator_counts(reaper_db):
    """End-to-end: after ``sweep_decay_stale_open_messages`` decays aged operator-
    visible messages, ``get_operator_message_counts`` reflects the decrease.

    Today decay and counts are tested independently (each mocks the other's
    read-model or write-model).  This test runs both in sequence with real
    DB rows to prove the write-model decay actually moves the read-model
    needle.
    """
    from app.operator_attention import get_operator_message_counts

    project = await _make_project(reaper_db)

    # Two operator-visible messages old enough to decay — one FEEDBACK (counted
    # directly by get_operator_message_counts) and one NOTE with operator-fyi
    # audience (contributes to unread).
    old = _utcnow() - timedelta(days=STALE_OPEN_DECAY_DAYS + 1)

    # FEEDBACK with explicit operator-fyi audience.
    feedback_msg = await _make_conversational_message(
        reaper_db, project,
        msg_type=MessageType.FEEDBACK,
        created_at=old,
        context={"audience": "operator-fyi", "event_type": "cycle_summary"},
    )

    # NOTE with operator-fyi audience (decayable).
    note_msg = await _make_conversational_message(
        reaper_db, project,
        msg_type=MessageType.NOTE,
        created_at=old,
        context={"audience": "operator-fyi", "event_type": "cycle_summary"},
    )

    # A CONTROL: operator-action-needed NOTE that should NOT decay at this age
    # (it survives past 14d up to the 60d OAN threshold).
    from app.message_audience import OPERATOR_ACTION_NEEDED
    control_msg = await _make_conversational_message(
        reaper_db, project,
        msg_type=MessageType.NOTE,
        created_at=old,
        context={"audience": OPERATOR_ACTION_NEEDED, "event_type": "auto_fix_escalated"},
    )

    # Pre-decay count: FEEDBACK and operator-fyi NOTE are operator-visible,
    # so they appear in the operator attention read model.
    pre_counts = await get_operator_message_counts(reaper_db, project.id)

    # Run the decay sweep.
    acted = await sweep_decay_stale_open_messages(reaper_db, _utcnow())

    assert acted == 2, (
        f"Expected 2 aged messages decayed (FEEDBACK + NOTE), got {acted}"
    )

    # Post-decay count: the two aged operator-visibles are now resolved,
    # so the read model must show fewer operator-visible messages.
    post_counts = await get_operator_message_counts(reaper_db, project.id)

    # The FEEDBACK count must decrease by 1 (our decayed FEEDBACK was open
    # and operator-visible, now resolved).
    assert post_counts["feedback"] == pre_counts["feedback"] - 1, (
        f"FEEDBACK count did not decrease: "
        f"pre={pre_counts['feedback']}, post={post_counts['feedback']}"
    )

    # The control OAN message must NOT have been decayed (it stays open and
    # operator-visible, so the needs_operator count still includes it).
    await reaper_db.refresh(control_msg)
    assert control_msg.status == "open", (
        "operator-action-needed NOTE must survive 14-day decay window"
    )

    # Unread count: both decayed messages were of types counted in unread
    # (FEEDBACK is in _HIGH_SIGNAL_MESSAGE_TYPES; the NOTE contributes to
    # the unread note query).  After decay both are resolved, so unread
    # should be stable or decrease (it can increase if other messages were
    # created by sweep machinery, but it must not increase due to decayed
    # messages remaining visible).
    assert post_counts["unread"] <= pre_counts["unread"], (
        f"unread count increased after sweeping aged operator-visible messages: "
        f"pre={pre_counts['unread']}, post={post_counts['unread']}"
    )

    # Verify the decayed messages are resolved.
    await reaper_db.refresh(feedback_msg)
    assert feedback_msg.status == "resolved", (
        "FEEDBACK must be resolved after sweep"
    )
    await reaper_db.refresh(note_msg)
    assert note_msg.status == "resolved", (
        "NOTE must be resolved after sweep"
    )


# ── Step 18d: stale-open bug-class decay ─────────────────────────────────────

from app.reaper_bug_message_sweeps import (  # noqa: E402
    STALE_OPEN_BUG_CLASS_DECAY_DAYS,
    sweep_decay_stale_open_bug_messages,
)


@pytest.mark.asyncio
async def test_sweep_decay_stale_open_bug_messages_decays_unowned_bug_report(reaper_db):
    """An OPEN unowned BUG_REPORT older than the threshold is auto-decayed."""
    project = await _make_project(reaper_db)
    old = _utcnow() - timedelta(days=STALE_OPEN_BUG_CLASS_DECAY_DAYS + 1)
    msg = ProjectMessage(
        project_id=project.id,
        message_type=MessageType.BUG_REPORT,
        from_type="system",
        content="old unowned bug report",
        status="open",
        context={},
        created_at=old,
    )
    reaper_db.add(msg)
    await reaper_db.commit()
    await reaper_db.refresh(msg)

    acted = await sweep_decay_stale_open_bug_messages(reaper_db, _utcnow())

    assert acted == 1
    await reaper_db.refresh(msg)
    assert msg.status == "resolved"
    assert msg.resolution == "auto-decayed (stale)"
    assert msg.resolved_at is not None


@pytest.mark.asyncio
async def test_sweep_decay_stale_open_bug_messages_decays_unowned_blocker(reaper_db):
    """An OPEN unowned BLOCKER older than the threshold is auto-decayed."""
    project = await _make_project(reaper_db)
    old = _utcnow() - timedelta(days=STALE_OPEN_BUG_CLASS_DECAY_DAYS + 5)
    msg = ProjectMessage(
        project_id=project.id,
        message_type=MessageType.BLOCKER,
        from_type="agent",
        content="old unowned blocker",
        status="open",
        context={},
        created_at=old,
    )
    reaper_db.add(msg)
    await reaper_db.commit()
    await reaper_db.refresh(msg)

    acted = await sweep_decay_stale_open_bug_messages(reaper_db, _utcnow())

    assert acted == 1
    await reaper_db.refresh(msg)
    assert msg.status == "resolved"
    assert msg.resolution == "auto-decayed (stale)"


@pytest.mark.asyncio
async def test_sweep_decay_stale_open_bug_messages_decays_unowned_strategic_bug_report(
    reaper_db,
):
    """An OPEN unowned strategist-filed (from_type='strategic') BUG_REPORT older
    than the threshold is auto-decayed like a 'system'/'agent' one — agent-filed
    bug reports authored via strategic_completion.py must not be permanently
    invisible to this sweep."""
    project = await _make_project(reaper_db)
    old = _utcnow() - timedelta(days=STALE_OPEN_BUG_CLASS_DECAY_DAYS + 1)
    msg = ProjectMessage(
        project_id=project.id,
        message_type=MessageType.BUG_REPORT,
        from_type="strategic",
        content="old unowned strategist-filed bug report",
        status="open",
        context={},
        created_at=old,
    )
    reaper_db.add(msg)
    await reaper_db.commit()
    await reaper_db.refresh(msg)

    acted = await sweep_decay_stale_open_bug_messages(reaper_db, _utcnow())

    assert acted == 1
    await reaper_db.refresh(msg)
    assert msg.status == "resolved"
    assert msg.resolution == "auto-decayed (stale)"
    assert msg.resolved_at is not None


@pytest.mark.asyncio
async def test_sweep_decay_stale_open_bug_messages_skips_recent(reaper_db):
    """A BUG_REPORT younger than the threshold is left open."""
    project = await _make_project(reaper_db)
    recent = _utcnow() - timedelta(days=STALE_OPEN_BUG_CLASS_DECAY_DAYS - 1)
    msg = ProjectMessage(
        project_id=project.id,
        message_type=MessageType.BUG_REPORT,
        from_type="system",
        content="recent bug report",
        status="open",
        context={},
        created_at=recent,
    )
    reaper_db.add(msg)
    await reaper_db.commit()
    await reaper_db.refresh(msg)

    acted = await sweep_decay_stale_open_bug_messages(reaper_db, _utcnow())

    assert acted == 0
    await reaper_db.refresh(msg)
    assert msg.status == "open"


@pytest.mark.asyncio
async def test_sweep_decay_stale_open_bug_messages_skips_user_authored(reaper_db):
    """Operator-authored (user) BUG_REPORTs are never auto-decayed."""
    project = await _make_project(reaper_db)
    old = _utcnow() - timedelta(days=STALE_OPEN_BUG_CLASS_DECAY_DAYS + 10)
    msg = ProjectMessage(
        project_id=project.id,
        message_type=MessageType.BUG_REPORT,
        from_type="user",
        content="operator-authored bug report",
        status="open",
        context={},
        created_at=old,
    )
    reaper_db.add(msg)
    await reaper_db.commit()
    await reaper_db.refresh(msg)

    acted = await sweep_decay_stale_open_bug_messages(reaper_db, _utcnow())

    assert acted == 0
    await reaper_db.refresh(msg)
    assert msg.status == "open"


@pytest.mark.asyncio
async def test_sweep_decay_stale_open_bug_messages_skips_linked(reaper_db):
    """A BUG_REPORT with a live convergence link is skipped (Step-15 owns it)."""
    project = await _make_project(reaper_db)
    old = _utcnow() - timedelta(days=STALE_OPEN_BUG_CLASS_DECAY_DAYS + 5)
    msg = ProjectMessage(
        project_id=project.id,
        message_type=MessageType.BUG_REPORT,
        from_type="system",
        content="linked bug report",
        status="open",
        context={"_convergence_link": {"kind": "direction", "id": "some-direction"}},
        created_at=old,
    )
    reaper_db.add(msg)
    await reaper_db.commit()
    await reaper_db.refresh(msg)

    acted = await sweep_decay_stale_open_bug_messages(reaper_db, _utcnow())

    assert acted == 0
    await reaper_db.refresh(msg)
    assert msg.status == "open"


@pytest.mark.asyncio
async def test_sweep_decay_stale_open_bug_messages_skips_direction_owned(reaper_db):
    """An old BUG_REPORT with direction_id ownership is skipped (Step 17 owns it)."""
    project = await _make_project(reaper_db)
    old = _utcnow() - timedelta(days=STALE_OPEN_BUG_CLASS_DECAY_DAYS + 10)
    msg = ProjectMessage(
        project_id=project.id,
        message_type=MessageType.BUG_REPORT,
        from_type="system",
        content="old direction-owned bug report",
        status="open",
        context={"direction_id": "d-still-active"},
        created_at=old,
    )
    reaper_db.add(msg)
    await reaper_db.commit()
    await reaper_db.refresh(msg)

    acted = await sweep_decay_stale_open_bug_messages(reaper_db, _utcnow())

    assert acted == 0
    await reaper_db.refresh(msg)
    assert msg.status == "open"


@pytest.mark.asyncio
async def test_sweep_decay_stale_open_bug_messages_skips_task_owned(reaper_db):
    """An old BLOCKER with pr_number+source_task_id ownership is skipped (Step 17 owns it)."""
    project = await _make_project(reaper_db)
    old = _utcnow() - timedelta(days=STALE_OPEN_BUG_CLASS_DECAY_DAYS + 20)
    msg = ProjectMessage(
        project_id=project.id,
        message_type=MessageType.BLOCKER,
        from_type="agent",
        content="old task-owned blocker",
        status="open",
        context={"pr_number": 1234, "source_task_id": "00000000-0000-0000-0000-000000000001"},
        created_at=old,
    )
    reaper_db.add(msg)
    await reaper_db.commit()
    await reaper_db.refresh(msg)

    acted = await sweep_decay_stale_open_bug_messages(reaper_db, _utcnow())

    assert acted == 0
    await reaper_db.refresh(msg)
    assert msg.status == "open"


@pytest.mark.asyncio
async def test_decay_stale_open_operator_action_needed_eventually_decays(reaper_db):
    """An operator-action-needed NOTE older than 60 days is eventually auto-decayed."""
    from app.message_audience import OPERATOR_ACTION_NEEDED

    project = await _make_project(reaper_db)
    # Older than the long threshold (60 days)
    very_old = _utcnow() - timedelta(days=61)
    ctx = {"audience": OPERATOR_ACTION_NEEDED}
    msg = await _make_conversational_message(
        reaper_db, project, msg_type=MessageType.NOTE, created_at=very_old, context=ctx,
    )

    acted = await sweep_decay_stale_open_messages(reaper_db, _utcnow())

    assert acted == 1
    await reaper_db.refresh(msg)
    assert msg.status == "resolved"
    assert msg.resolution == "auto-decayed (stale)"


@pytest.mark.asyncio
async def test_decay_stale_open_operator_action_needed_preserved_within_threshold(reaper_db):
    """An operator-action-needed NOTE younger than 60 days is preserved."""
    from app.message_audience import OPERATOR_ACTION_NEEDED

    project = await _make_project(reaper_db)
    # Older than the normal 14-day window but younger than the 60-day OAN threshold
    mid_age = _utcnow() - timedelta(days=30)
    ctx = {"audience": OPERATOR_ACTION_NEEDED}
    msg = await _make_conversational_message(
        reaper_db, project, msg_type=MessageType.NOTE, created_at=mid_age, context=ctx,
    )

    acted = await sweep_decay_stale_open_messages(reaper_db, _utcnow())

    assert acted == 0
    await reaper_db.refresh(msg)
    assert msg.status == "open"
    assert msg.resolution is None


# ── Step 16: converge duplicate foundations ───────────────────────────────


async def _conv_task(
    db,
    project,
    *,
    ttype="create",
    files=None,
    parent=None,
    ctx=None,
    created_at=None,
    status=TaskStatus.IN_PROGRESS,
    depends_on=None,
):
    """Create an active write-task for the convergence sweep tests."""
    t = Task(
        project_id=project.id,
        payloads=[{"type": ttype, "related_files": files or []}],
        status=status,
        parent_task_id=parent,
        context=ctx or {},
        depends_on=depends_on or [],
        created_at=created_at or _utcnow(),
    )
    db.add(t)
    await db.commit()
    await db.refresh(t)
    return t


@pytest.fixture()
def _no_broadcast(mocker):
    mocker.patch(
        "app.broadcast.broadcast_to_project", new_callable=AsyncMock, return_value=None
    )


@pytest.fixture(autouse=True)
def _reset_fleet_scan_cadence_globals():
    """Reset fleet-scan cadence globals before each test so earlier tests that invoke
    the reaper sweep train (which sets these module-level timestamps) cannot gate
    functional unit tests that call the sweeps directly within the 15-min window."""
    _rs._last_converge_duplicate_scan = None
    _rs._last_merge_gate_degradation_scan = None
    yield
    _rs._last_converge_duplicate_scan = None
    _rs._last_merge_gate_degradation_scan = None


@pytest.mark.asyncio
async def test_converge_genuine_cross_parent_duplicate_wires_edge(
    reaper_db, _no_broadcast
):
    """A genuine cross-parent duplicate (near-identical file sets) is serialized:
    the later task gets a depends_on edge onto the earlier (canonical) one."""
    from app.reaper_sweeps import sweep_converge_duplicate_foundations

    project = await _make_project(reaper_db)
    files = ["backend/app/foundation.py", "backend/app/x.py", "backend/app/y.py"]
    canonical = await _conv_task(
        reaper_db,
        project,
        files=files,
        created_at=_utcnow() - timedelta(hours=2),
    )
    dependent = await _conv_task(
        reaper_db, project, files=files, created_at=_utcnow()
    )

    n = await sweep_converge_duplicate_foundations(reaper_db, _utcnow())
    await reaper_db.refresh(canonical)
    await reaper_db.refresh(dependent)

    assert n == 1
    assert str(canonical.id) in [str(d) for d in dependent.depends_on]
    assert not canonical.depends_on
    assert dependent.context.get("_foundation_converged", {}).get(
        "foundation_id"
    ) == str(canonical.id)


@pytest.mark.asyncio
async def test_converge_shared_finding_origin_wires_edge(reaper_db, _no_broadcast):
    """Shared finding origin is a genuine-duplicate signal even when file sets differ."""
    from app.reaper_sweeps import sweep_converge_duplicate_foundations

    project = await _make_project(reaper_db)
    canonical = await _conv_task(
        reaper_db,
        project,
        files=["backend/app/a.py"],
        ctx={"finding_hash": "deadbeefcafe0001"},
        created_at=_utcnow() - timedelta(hours=1),
    )
    dependent = await _conv_task(
        reaper_db,
        project,
        files=["backend/app/b.py", "backend/app/c.py"],
        ctx={"finding_hash": "deadbeefcafe0001"},
        created_at=_utcnow(),
    )

    n = await sweep_converge_duplicate_foundations(reaper_db, _utcnow())
    await reaper_db.refresh(dependent)

    assert n == 1
    assert str(canonical.id) in [str(d) for d in dependent.depends_on]


@pytest.mark.asyncio
async def test_no_converge_when_only_one_shared_infra_file(reaper_db, _no_broadcast):
    """Two independent tasks that merely share one infra file are NOT converged."""
    from app.reaper_sweeps import sweep_converge_duplicate_foundations

    project = await _make_project(reaper_db)
    t1 = await _conv_task(
        reaper_db,
        project,
        files=["backend/app/shared.py", "backend/app/a.py", "backend/app/b.py"],
    )
    t2 = await _conv_task(
        reaper_db,
        project,
        files=["backend/app/shared.py", "backend/app/c.py", "backend/app/d.py"],
    )

    n = await sweep_converge_duplicate_foundations(reaper_db, _utcnow())
    await reaper_db.refresh(t1)
    await reaper_db.refresh(t2)

    assert n == 0
    assert not t1.depends_on
    assert not t2.depends_on


@pytest.mark.asyncio
async def test_no_converge_same_parent(reaper_db, _no_broadcast):
    """Same-parent siblings reconcile by design and are never auto-serialized."""
    from app.reaper_sweeps import sweep_converge_duplicate_foundations

    project = await _make_project(reaper_db)
    parent = _uuid_mod.uuid4()
    files = ["backend/app/foundation.py", "backend/app/x.py"]
    t1 = await _conv_task(reaper_db, project, files=files, parent=parent)
    t2 = await _conv_task(reaper_db, project, files=files, parent=parent)

    n = await sweep_converge_duplicate_foundations(reaper_db, _utcnow())
    await reaper_db.refresh(t1)
    await reaper_db.refresh(t2)

    assert n == 0
    assert not t1.depends_on
    assert not t2.depends_on


@pytest.mark.asyncio
async def test_converge_refuses_cycle(reaper_db, _no_broadcast):
    """The sweep never closes a depends_on cycle: if the earlier (canonical) task
    already depends on the later one, no reverse edge is wired."""
    from app.reaper_sweeps import sweep_converge_duplicate_foundations

    project = await _make_project(reaper_db)
    files = ["backend/app/foundation.py", "backend/app/x.py", "backend/app/y.py"]
    later = await _conv_task(
        reaper_db, project, files=files, created_at=_utcnow()
    )
    # earlier=canonical, but it already depends on the later task → reverse edge cycles
    earlier = await _conv_task(
        reaper_db,
        project,
        files=files,
        created_at=_utcnow() - timedelta(hours=2),
        depends_on=[str(later.id)],
    )

    n = await sweep_converge_duplicate_foundations(reaper_db, _utcnow())
    await reaper_db.refresh(later)
    await reaper_db.refresh(earlier)

    assert n == 0
    assert not later.depends_on
    assert earlier.depends_on == [str(later.id)]


# ── Spawn-time foundation convergence (research_completion) ────────────────────
# These exercise converge_spawn_batch_foundations, which serializes GENUINE
# duplicate foundations BEFORE the just-spawned children begin executing — reusing
# the SAME predicate + apply-with-guards helper as the reaper Step 15 sweep above,
# so the early and late paths can never drift.


@pytest.mark.asyncio
async def test_spawn_converge_within_batch_genuine_duplicate(reaper_db):
    """(a) Two just-spawned siblings with an identical finding origin are the
    SAME finding, not a foundation pair — serializing the later one produced
    rebase-then-duplicate PRs, so it is DROPPED from the batch instead, stamped
    with the surviving sibling for the caller's skip receipt."""
    from app.research_completion import converge_spawn_batch_foundations
    from app.research_sibling_dependencies import DUPLICATE_FINDING_DROP_KEY

    project = await _make_project(reaper_db)
    research_parent = _uuid_mod.uuid4()
    canonical = await _conv_task(
        reaper_db,
        project,
        files=["backend/app/a.py"],
        parent=research_parent,
        ctx={"finding_hash": "spawnhash00000001"},
        created_at=_utcnow() - timedelta(minutes=5),
        status=TaskStatus.QUEUED,
    )
    dependent = await _conv_task(
        reaper_db,
        project,
        files=["backend/app/b.py", "backend/app/c.py"],
        parent=research_parent,
        ctx={"finding_hash": "spawnhash00000001"},
        created_at=_utcnow(),
        status=TaskStatus.QUEUED,
    )

    batch = [canonical, dependent]
    wired = converge_spawn_batch_foundations(batch, [])

    assert wired == []
    assert [t.id for t in batch] == [canonical.id]
    assert not dependent.depends_on
    assert not canonical.depends_on
    marker = dependent.context[DUPLICATE_FINDING_DROP_KEY]
    assert marker["surviving_task_id"] == str(canonical.id)
    assert marker["finding_hash"] == "spawnhash00000001"


@pytest.mark.asyncio
async def test_spawn_drops_duplicate_finding_child_vs_active_task(reaper_db):
    """A just-spawned child whose finding_hash matches an already-active task is
    dropped from the batch — the active task IS the finding's implementation, so
    serializing behind it only manufactures a duplicate PR after rebase."""
    from app.research_completion import converge_spawn_batch_foundations
    from app.research_sibling_dependencies import DUPLICATE_FINDING_DROP_KEY

    project = await _make_project(reaper_db)
    active = await _conv_task(
        reaper_db,
        project,
        files=["backend/app/a.py"],
        parent=_uuid_mod.uuid4(),
        ctx={"finding_hash": "activehash0000001"},
        created_at=_utcnow() - timedelta(hours=1),
        status=TaskStatus.IN_PROGRESS,
    )
    child = await _conv_task(
        reaper_db,
        project,
        files=["backend/app/b.py"],
        parent=_uuid_mod.uuid4(),
        ctx={"finding_hash": "activehash0000001"},
        created_at=_utcnow(),
        status=TaskStatus.QUEUED,
    )

    batch = [child]
    wired = converge_spawn_batch_foundations(batch, [active])

    assert wired == []
    assert batch == []
    assert not child.depends_on
    assert child.context[DUPLICATE_FINDING_DROP_KEY]["surviving_task_id"] == str(
        active.id
    )


@pytest.mark.asyncio
async def test_spawn_converge_new_finding_vs_active_task(reaper_db):
    """(b) A just-spawned child whose foundation duplicates an already-active task
    is serialized behind that active task (the earlier = canonical one), rather
    than running as an independent peer until the reaper cleans it up."""
    from app.research_completion import converge_spawn_batch_foundations

    project = await _make_project(reaper_db)
    active = await _conv_task(
        reaper_db,
        project,
        files=["backend/app/cap.py"],
        parent=_uuid_mod.uuid4(),
        ctx={"provides_capability": "token-budgeter"},
        created_at=_utcnow() - timedelta(hours=1),
        status=TaskStatus.IN_PROGRESS,
    )
    child = await _conv_task(
        reaper_db,
        project,
        files=["backend/app/other.py"],
        parent=_uuid_mod.uuid4(),
        ctx={"provides_capability": "token-budgeter"},
        created_at=_utcnow(),
        status=TaskStatus.QUEUED,
    )

    wired = converge_spawn_batch_foundations([child], [active])

    assert [t.id for t in wired] == [child.id]
    assert str(active.id) in [str(d) for d in child.depends_on]
    assert not active.depends_on


@pytest.mark.asyncio
async def test_spawn_no_converge_subthreshold_incidental_overlap(reaper_db):
    """Negative: two just-spawned children that merely share one incidental infra
    file (Jaccard below the convergence threshold, no shared origin/capability) are
    left to proceed in parallel and reconcile at merge — NOT serialized."""
    from app.research_completion import converge_spawn_batch_foundations

    project = await _make_project(reaper_db)
    research_parent = _uuid_mod.uuid4()
    a = await _conv_task(
        reaper_db,
        project,
        files=["backend/app/shared.py", "backend/app/a.py", "backend/app/b.py"],
        parent=research_parent,
        created_at=_utcnow() - timedelta(minutes=1),
    )
    b = await _conv_task(
        reaper_db,
        project,
        files=["backend/app/shared.py", "backend/app/c.py", "backend/app/d.py"],
        parent=research_parent,
        created_at=_utcnow(),
    )

    wired = converge_spawn_batch_foundations([a, b], [])

    assert wired == []
    assert not a.depends_on
    assert not b.depends_on


@pytest.mark.asyncio
async def test_spawn_converge_skips_active_vs_active_pairs(reaper_db):
    """The spawn helper only ever makes a just-spawned child the dependent. A
    genuine-duplicate pair of pre-existing active tasks (neither in the batch) is
    left for the reaper sweep — the spawn path never re-points an active peer."""
    from app.research_completion import converge_spawn_batch_foundations

    project = await _make_project(reaper_db)
    files = ["backend/app/f.py", "backend/app/g.py", "backend/app/h.py"]
    active_a = await _conv_task(
        reaper_db,
        project,
        files=files,
        parent=_uuid_mod.uuid4(),
        created_at=_utcnow() - timedelta(hours=2),
    )
    active_b = await _conv_task(
        reaper_db,
        project,
        files=files,
        parent=_uuid_mod.uuid4(),
        created_at=_utcnow() - timedelta(hours=1),
    )
    # The batch child is unrelated, so no child-dependent action is emitted.
    child = await _conv_task(
        reaper_db,
        project,
        files=["backend/app/unrelated.py"],
        parent=_uuid_mod.uuid4(),
        created_at=_utcnow(),
    )

    wired = converge_spawn_batch_foundations([child], [active_a, active_b])

    assert wired == []
    assert not active_a.depends_on
    assert not active_b.depends_on
    assert not child.depends_on


@pytest.mark.asyncio
async def test_spawn_converge_refuses_cycle_for_coordinated_siblings(reaper_db):
    """A genuine-duplicate batch pair already linked by explicit depends_on (the
    canonical declared to depend on the later child) is left alone: the reverse
    convergence edge would close a cycle, so the apply-with-guards helper refuses
    it — mirroring constraint #3 (coordinated siblings stay untouched)."""
    from app.research_completion import converge_spawn_batch_foundations

    project = await _make_project(reaper_db)
    research_parent = _uuid_mod.uuid4()
    later = await _conv_task(
        reaper_db,
        project,
        files=["backend/app/a.py"],
        parent=research_parent,
        ctx={"finding_hash": "cyclehash00000001"},
        created_at=_utcnow(),
    )
    # earlier=canonical, but already declared to depend on `later` → reverse cycles.
    earlier = await _conv_task(
        reaper_db,
        project,
        files=["backend/app/a.py"],
        parent=research_parent,
        ctx={"finding_hash": "cyclehash00000001"},
        created_at=_utcnow() - timedelta(hours=1),
        depends_on=[str(later.id)],
    )

    wired = converge_spawn_batch_foundations([later, earlier], [])

    assert wired == []
    assert earlier.depends_on == [str(later.id)]
    assert not later.depends_on


# ── Step 17: auto-resolve OPEN owned bug-class messages on owner success ───────

from app.reaper_sweeps import sweep_resolve_open_owned_bug_messages  # noqa: E402


async def _make_landed_task(db, project, *, status=TaskStatus.COMPLETED, landed=True):
    """Create a task; when landed=True it is a merged completion (is_merged_completion)."""
    ctx = {"pr_merged": True, "landed_on_integration": True} if landed else {}
    task = Task(
        project_id=project.id,
        payloads=[{"type": "fix"}],
        status=status,
        context=ctx,
    )
    db.add(task)
    await db.commit()
    await db.refresh(task)
    return task


@pytest.mark.asyncio
async def test_open_owned_resolves_when_direction_completed(reaper_db):
    """An open owned BUG_REPORT resolves when its owning direction is 'completed'."""
    project = await _make_project(reaper_db)
    await _make_direction_row(
        reaper_db, project, direction_key="d-done1", status="completed"
    )
    msg = await _make_bug_message(
        reaper_db, project, context={"direction_id": "d-done1"}
    )

    resolved = await sweep_resolve_open_owned_bug_messages(reaper_db, _utcnow())

    assert resolved == 1
    await reaper_db.refresh(msg)
    assert msg.status == "resolved"
    assert "d-done1" in (msg.resolution or "")
    assert "completed" in (msg.resolution or "")
    assert msg.resolved_at is not None
    assert msg.context.get("_owned_auto_resolved_at") is not None
    assert msg.context.get("_owned_auto_resolution") == msg.resolution


@pytest.mark.asyncio
async def test_open_owned_resolves_strategic_authored_bug_report(reaper_db):
    """A strategist-filed (from_type='strategic') BUG_REPORT — the shape produced
    by strategic_completion.py for agent-filed bug reports — resolves on owner
    success the same as a 'system'/'agent'-authored one. Without 'strategic' in
    SYSTEM_AUTHORED_FROM_TYPES this row would never be selected by any sweep."""
    project = await _make_project(reaper_db)
    await _make_direction_row(
        reaper_db, project, direction_key="d-done-strategic", status="completed"
    )
    msg = await _make_bug_message(
        reaper_db,
        project,
        context={"direction_id": "d-done-strategic"},
        from_type="strategic",
    )

    resolved = await sweep_resolve_open_owned_bug_messages(reaper_db, _utcnow())

    assert resolved == 1
    await reaper_db.refresh(msg)
    assert msg.status == "resolved"
    assert "d-done-strategic" in (msg.resolution or "")
    assert msg.resolved_at is not None


@pytest.mark.asyncio
async def test_open_owned_resolution_broadcasts_workbench_envelope(reaper_db, mocker):
    """Owner-success auto-resolution publishes a resolved message envelope."""
    project = await _make_project(reaper_db)
    await _make_direction_row(
        reaper_db, project, direction_key="d-done-env", status="completed"
    )
    msg = await _make_bug_message(
        reaper_db, project, context={"direction_id": "d-done-env"}
    )
    broadcast = mocker.patch(
        "app.broadcast.broadcast_to_project", new_callable=AsyncMock
    )

    resolved = await sweep_resolve_open_owned_bug_messages(reaper_db, _utcnow())

    assert resolved == 1
    await reaper_db.refresh(msg)
    broadcast.assert_awaited_once()
    project_id, event, payload = broadcast.await_args.args
    assert project_id == str(project.id)
    assert event == "message_updated"
    _assert_message_update_event_payload(
        payload,
        project=project,
        msg=msg,
        status="resolved",
        action="resolved",
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("dir_status", ["retired", "rejected", "expired", "superseded"])
async def test_open_owned_stays_open_when_direction_ended_without_landing(
    reaper_db, dir_status
):
    """A direction that ended without landing the fix (retired/rejected/expired/
    superseded) must NOT resolve the message — only genuine success resolves."""
    project = await _make_project(reaper_db)
    await _make_direction_row(
        reaper_db, project, direction_key=f"d-{dir_status}", status=dir_status
    )
    msg = await _make_bug_message(
        reaper_db, project, context={"direction_id": f"d-{dir_status}"}
    )

    resolved = await sweep_resolve_open_owned_bug_messages(reaper_db, _utcnow())

    assert resolved == 0
    await reaper_db.refresh(msg)
    assert msg.status == "open"
    assert msg.resolution is None


@pytest.mark.asyncio
async def test_open_owned_resolves_when_owning_pr_merged(reaper_db):
    """An open owned BLOCKER resolves when its owning PR's source task is a merged
    completion (COMPLETED + landed signal)."""
    project = await _make_project(reaper_db)
    task = await _make_landed_task(reaper_db, project, landed=True)
    msg = await _make_bug_message(
        reaper_db,
        project,
        msg_type=MessageType.BLOCKER,
        context={"pr_number": 4567, "source_task_id": str(task.id)},
    )

    resolved = await sweep_resolve_open_owned_bug_messages(reaper_db, _utcnow())

    assert resolved == 1
    await reaper_db.refresh(msg)
    assert msg.status == "resolved"
    assert "#4567" in (msg.resolution or "")
    assert "merged" in (msg.resolution or "")


@pytest.mark.asyncio
async def test_open_owned_stays_open_when_owning_task_completed_but_unmerged(reaper_db):
    """COMPLETED != merged: a COMPLETED owner with no landed signal has NOT landed
    the fix, so the message stays open (the false-resolution this guards against)."""
    project = await _make_project(reaper_db)
    task = await _make_landed_task(reaper_db, project, landed=False)
    msg = await _make_bug_message(
        reaper_db,
        project,
        context={"pr_number": 99, "source_task_id": str(task.id)},
    )

    # verify_pr_merged stubbed to False so the secondary check can't merge it either.
    async def _never_merged(_task, _session):
        return False

    resolved = await sweep_resolve_open_owned_bug_messages(
        reaper_db, _utcnow(), verify_pr_merged=_never_merged
    )

    assert resolved == 0
    await reaper_db.refresh(msg)
    assert msg.status == "open"


@pytest.mark.asyncio
@pytest.mark.parametrize("task_status", [TaskStatus.FAILED, TaskStatus.CANCELLED])
async def test_open_owned_stays_open_when_owning_task_failed_or_cancelled(
    reaper_db, task_status
):
    """An owner that failed/was cancelled never delivered the fix — stays open."""
    project = await _make_project(reaper_db)
    task = await _make_landed_task(reaper_db, project, status=task_status, landed=False)
    msg = await _make_bug_message(
        reaper_db,
        project,
        context={"pr_number": 7, "source_task_id": str(task.id)},
    )

    resolved = await sweep_resolve_open_owned_bug_messages(reaper_db, _utcnow())

    assert resolved == 0
    await reaper_db.refresh(msg)
    assert msg.status == "open"


@pytest.mark.asyncio
async def test_open_owned_stays_open_when_owner_missing(reaper_db):
    """A source_task_id pointing at a non-existent task is fail-safe — stays open."""
    project = await _make_project(reaper_db)
    msg = await _make_bug_message(
        reaper_db,
        project,
        context={"pr_number": 3, "source_task_id": str(_uuid_mod.uuid4())},
    )

    resolved = await sweep_resolve_open_owned_bug_messages(reaper_db, _utcnow())

    assert resolved == 0
    await reaper_db.refresh(msg)
    assert msg.status == "open"


@pytest.mark.asyncio
async def test_open_owned_stays_open_when_owner_malformed(reaper_db):
    """A malformed source_task_id (not a UUID) is fail-safe — stays open, no abort."""
    project = await _make_project(reaper_db)
    msg = await _make_bug_message(
        reaper_db,
        project,
        context={"pr_number": 3, "source_task_id": "not-a-uuid"},
    )

    resolved = await sweep_resolve_open_owned_bug_messages(reaper_db, _utcnow())

    assert resolved == 0
    await reaper_db.refresh(msg)
    assert msg.status == "open"


@pytest.mark.asyncio
async def test_open_owned_stays_open_when_no_ownership_context(reaper_db):
    """A bug message with no direction_id / pr ownership context stays open."""
    project = await _make_project(reaper_db)
    msg = await _make_bug_message(reaper_db, project, context={"source": "misc"})

    resolved = await sweep_resolve_open_owned_bug_messages(reaper_db, _utcnow())

    assert resolved == 0
    await reaper_db.refresh(msg)
    assert msg.status == "open"


@pytest.mark.asyncio
async def test_open_owned_stays_open_when_direction_still_active(reaper_db):
    """A still-active owning direction has not landed — message stays open."""
    project = await _make_project(reaper_db)
    await _make_direction_row(
        reaper_db, project, direction_key="d-active1", status="active"
    )
    msg = await _make_bug_message(
        reaper_db, project, context={"direction_id": "d-active1"}
    )

    resolved = await sweep_resolve_open_owned_bug_messages(reaper_db, _utcnow())

    assert resolved == 0
    await reaper_db.refresh(msg)
    assert msg.status == "open"


@pytest.mark.asyncio
async def test_open_owned_resolution_is_idempotent(reaper_db):
    """Re-running the sweep on an already-resolved message is a no-op (status off 'open')."""
    project = await _make_project(reaper_db)
    await _make_direction_row(
        reaper_db, project, direction_key="d-idem1", status="completed"
    )
    msg = await _make_bug_message(
        reaper_db, project, context={"direction_id": "d-idem1"}
    )

    first = await sweep_resolve_open_owned_bug_messages(reaper_db, _utcnow())
    assert first == 1
    second = await sweep_resolve_open_owned_bug_messages(reaper_db, _utcnow())
    assert second == 0
    await reaper_db.refresh(msg)
    assert msg.status == "resolved"


@pytest.mark.asyncio
async def test_open_owned_sweep_ignores_linked_status_messages(reaper_db):
    """The Step-17 sweep is DISJOINT from the Step-15 linked sweep: a 'linked'
    message with a completed owning direction is NOT touched here."""
    project = await _make_project(reaper_db)
    await _make_direction_row(
        reaper_db, project, direction_key="d-linked1", status="completed"
    )
    # status='linked' (the linked sweep's domain), not 'open'.
    msg = await _make_bug_message(
        reaper_db,
        project,
        status="linked",
        context={"direction_id": "d-linked1"},
    )

    resolved = await sweep_resolve_open_owned_bug_messages(reaper_db, _utcnow())

    assert resolved == 0
    await reaper_db.refresh(msg)
    assert msg.status == "linked"


@pytest.mark.asyncio
async def test_open_owned_sweep_ignores_user_authored_messages(reaper_db):
    """Operator-authored (from_type='user') bug reports are never auto-resolved,
    even with a completed owning direction."""
    project = await _make_project(reaper_db)
    await _make_direction_row(
        reaper_db, project, direction_key="d-user1", status="completed"
    )
    msg = ProjectMessage(
        project_id=project.id,
        message_type=MessageType.BUG_REPORT,
        from_type="user",
        content="operator bug",
        status="open",
        context={"direction_id": "d-user1"},
    )
    reaper_db.add(msg)
    await reaper_db.commit()
    await reaper_db.refresh(msg)

    resolved = await sweep_resolve_open_owned_bug_messages(reaper_db, _utcnow())

    assert resolved == 0
    await reaper_db.refresh(msg)
    assert msg.status == "open"


@pytest.mark.asyncio
async def test_open_owned_one_bad_row_does_not_abort_batch(reaper_db):
    """A malformed owner row is skipped (fail-safe) while a sibling resolvable
    message in the same batch still resolves."""
    project = await _make_project(reaper_db)
    await _make_direction_row(
        reaper_db, project, direction_key="d-good1", status="completed"
    )
    bad = await _make_bug_message(
        reaper_db, project, context={"pr_number": 1, "source_task_id": "bad-uuid"}
    )
    good = await _make_bug_message(
        reaper_db, project, context={"direction_id": "d-good1"}
    )

    resolved = await sweep_resolve_open_owned_bug_messages(reaper_db, _utcnow())

    assert resolved == 1
    await reaper_db.refresh(bad)
    await reaper_db.refresh(good)
    assert bad.status == "open"
    assert good.status == "resolved"


# ── Step 18: negatively resolve OPEN bug-class messages confirmed stale on HEAD ─

from app.reaper_sweeps import sweep_resolve_stale_open_bug_messages  # noqa: E402


@pytest.mark.asyncio
async def test_stale_open_bug_resolves_when_citations_absent(reaper_db, monkeypatch):
    """An open bug message whose citations re-validate as ABSENT on HEAD is
    resolved with the distinct stale reason + distinct audit keys + stored signal."""
    project = await _make_project(reaper_db)
    msg = await _make_bug_message(reaper_db, project, context={"source": "x"})

    absent_signal = {
        "citation_state": "absent",
        "broken_refs": ["app/gone.py"],
        "ungrounded_refs": ["app/gone.py:vanished"],
    }

    async def _absent(_session, _project, _content, *, structured_context=None):
        return absent_signal

    monkeypatch.setattr(_rs, "compute_message_citation_warning", _absent, raising=False)
    monkeypatch.setattr(
        "app.source_ref_validation.compute_message_citation_warning",
        _absent,
        raising=False,
    )

    resolved = await sweep_resolve_stale_open_bug_messages(reaper_db, _utcnow())

    assert resolved == 1
    await reaper_db.refresh(msg)
    assert msg.status == "resolved"
    assert "stale" in (msg.resolution or "").lower()
    assert "no longer reproduce" in (msg.resolution or "")
    assert msg.resolved_at is not None
    # Distinct audit keys — NOT the Step-17 owner-landed keys.
    assert msg.context.get("_stale_auto_resolved_at") is not None
    assert msg.context.get("_stale_auto_resolution") == msg.resolution
    assert msg.context.get("_stale_auto_citation_signal") == absent_signal
    assert "_owned_auto_resolution" not in msg.context


@pytest.mark.asyncio
async def test_stale_open_bug_resolves_strategic_authored_when_citations_absent(
    reaper_db, monkeypatch
):
    """A strategist-filed (from_type='strategic') bug message whose citations
    re-validate as ABSENT on HEAD is resolved the same as a 'system'/'agent'
    one — strategic-authored rows must be selected by this sweep too."""
    project = await _make_project(reaper_db)
    msg = await _make_bug_message(
        reaper_db, project, context={"source": "x"}, from_type="strategic"
    )

    async def _absent(_session, _project, _content, *, structured_context=None):
        return {
            "citation_state": "absent",
            "broken_refs": ["app/gone.py"],
            "ungrounded_refs": ["app/gone.py:vanished"],
        }

    monkeypatch.setattr(_rs, "compute_message_citation_warning", _absent, raising=False)
    monkeypatch.setattr(
        "app.source_ref_validation.compute_message_citation_warning",
        _absent,
        raising=False,
    )

    resolved = await sweep_resolve_stale_open_bug_messages(reaper_db, _utcnow())

    assert resolved == 1
    await reaper_db.refresh(msg)
    assert msg.status == "resolved"
    assert "stale" in (msg.resolution or "").lower()
    assert msg.resolved_at is not None


@pytest.mark.asyncio
async def test_stale_open_bug_resolution_broadcasts_workbench_envelope(
    reaper_db, monkeypatch, mocker
):
    """Stale auto-resolution publishes the same resolved message envelope."""
    project = await _make_project(reaper_db)
    msg = await _make_bug_message(reaper_db, project, context={"source": "x"})

    async def _absent(_session, _project, _content, *, structured_context=None):
        return {
            "citation_state": "absent",
            "broken_refs": ["app/gone.py"],
            "ungrounded_refs": [],
        }

    monkeypatch.setattr(_rs, "compute_message_citation_warning", _absent, raising=False)
    monkeypatch.setattr(
        "app.source_ref_validation.compute_message_citation_warning",
        _absent,
        raising=False,
    )
    broadcast = mocker.patch(
        "app.broadcast.broadcast_to_project", new_callable=AsyncMock
    )

    resolved = await sweep_resolve_stale_open_bug_messages(reaper_db, _utcnow())

    assert resolved == 1
    await reaper_db.refresh(msg)
    broadcast.assert_awaited_once()
    project_id, event, payload = broadcast.await_args.args
    assert project_id == str(project.id)
    assert event == "message_updated"
    _assert_message_update_event_payload(
        payload,
        project=project,
        msg=msg,
        status="resolved",
        action="resolved",
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("state", ["present", "indeterminate"])
async def test_stale_open_bug_stays_open_when_not_absent(reaper_db, monkeypatch, state):
    """A truthy non-absent citation signal still leaves the message OPEN."""
    project = await _make_project(reaper_db)
    msg = await _make_bug_message(reaper_db, project)

    async def _not_absent(_session, _project, _content, *, structured_context=None):
        return {
            "citation_state": state,
            "broken_refs": ["app/gone.py"] if state == "present" else [],
            "ungrounded_refs": [],
        }

    monkeypatch.setattr(
        _rs, "compute_message_citation_warning", _not_absent, raising=False
    )
    monkeypatch.setattr(
        "app.source_ref_validation.compute_message_citation_warning",
        _not_absent,
        raising=False,
    )

    resolved = await sweep_resolve_stale_open_bug_messages(reaper_db, _utcnow())

    assert resolved == 0
    await reaper_db.refresh(msg)
    assert msg.status == "open"
    assert msg.resolution is None


@pytest.mark.asyncio
async def test_stale_open_bug_rechecks_fresh_not_ingestion_stamp(
    reaper_db, monkeypatch
):
    """A message carrying an ingestion-time absent stamp is NOT trusted: the code
    is PRESENT on HEAD now, the fresh re-check returns None, and it stays open."""
    project = await _make_project(reaper_db)
    # Stale ingestion stamp claims absent, but fresh grounding (below) says present.
    msg = await _make_bug_message(
        reaper_db,
        project,
        context={"_provenance_warnings": {"citation_state": "absent"}},
    )

    async def _present_now(_session, _project, _content, *, structured_context=None):
        return None  # present on HEAD → helper returns None

    monkeypatch.setattr(
        _rs, "compute_message_citation_warning", _present_now, raising=False
    )
    monkeypatch.setattr(
        "app.source_ref_validation.compute_message_citation_warning",
        _present_now,
        raising=False,
    )

    resolved = await sweep_resolve_stale_open_bug_messages(reaper_db, _utcnow())

    assert resolved == 0
    await reaper_db.refresh(msg)
    assert msg.status == "open"


@pytest.mark.asyncio
async def test_stale_open_bug_resolution_is_idempotent(reaper_db, monkeypatch):
    """Re-running the sweep on an already-stale-resolved message is a no-op."""
    project = await _make_project(reaper_db)
    msg = await _make_bug_message(reaper_db, project)

    async def _absent(_session, _project, _content, *, structured_context=None):
        return {
            "citation_state": "absent",
            "broken_refs": ["x.py"],
            "ungrounded_refs": [],
        }

    monkeypatch.setattr(_rs, "compute_message_citation_warning", _absent, raising=False)
    monkeypatch.setattr(
        "app.source_ref_validation.compute_message_citation_warning",
        _absent,
        raising=False,
    )

    first = await sweep_resolve_stale_open_bug_messages(reaper_db, _utcnow())
    assert first == 1
    second = await sweep_resolve_stale_open_bug_messages(reaper_db, _utcnow())
    assert second == 0
    await reaper_db.refresh(msg)
    assert msg.status == "resolved"


@pytest.mark.asyncio
async def test_stale_open_bug_degrades_safely_on_grounding_error(
    reaper_db, monkeypatch
):
    """A grounding exception leaves the offending message open and never aborts the
    batch — a sibling absent message in the same batch still resolves."""
    project = await _make_project(reaper_db)
    boom = await _make_bug_message(reaper_db, project, context={"tag": "boom"})
    good = await _make_bug_message(reaper_db, project, context={"tag": "good"})

    async def _maybe_raise(_session, _project, content, *, structured_context=None):
        if (structured_context or {}).get("tag") == "boom":
            raise RuntimeError("grounding backend exploded")
        return {
            "citation_state": "absent",
            "broken_refs": ["g.py"],
            "ungrounded_refs": [],
        }

    monkeypatch.setattr(
        _rs, "compute_message_citation_warning", _maybe_raise, raising=False
    )
    monkeypatch.setattr(
        "app.source_ref_validation.compute_message_citation_warning",
        _maybe_raise,
        raising=False,
    )

    resolved = await sweep_resolve_stale_open_bug_messages(reaper_db, _utcnow())

    assert resolved == 1
    await reaper_db.refresh(boom)
    await reaper_db.refresh(good)
    assert boom.status == "open"
    assert good.status == "resolved"


@pytest.mark.asyncio
async def test_stale_open_bug_sweep_ignores_non_eligible_messages(
    reaper_db, monkeypatch
):
    """Disjointness: non-open, non-bug-class, and user-authored messages are never
    touched even when grounding would report absent."""
    project = await _make_project(reaper_db)
    linked = await _make_bug_message(reaper_db, project, status="linked")
    note = await _make_bug_message(reaper_db, project, msg_type=MessageType.NOTE)
    user_msg = ProjectMessage(
        project_id=project.id,
        message_type=MessageType.BUG_REPORT,
        from_type="user",
        content="operator bug",
        status="open",
        context={},
    )
    reaper_db.add(user_msg)
    await reaper_db.commit()
    await reaper_db.refresh(user_msg)

    async def _absent(_session, _project, _content, *, structured_context=None):
        return {
            "citation_state": "absent",
            "broken_refs": ["x.py"],
            "ungrounded_refs": [],
        }

    monkeypatch.setattr(_rs, "compute_message_citation_warning", _absent, raising=False)
    monkeypatch.setattr(
        "app.source_ref_validation.compute_message_citation_warning",
        _absent,
        raising=False,
    )

    resolved = await sweep_resolve_stale_open_bug_messages(reaper_db, _utcnow())

    assert resolved == 0
    for m in (linked, note, user_msg):
        await reaper_db.refresh(m)
    assert linked.status == "linked"
    assert note.status == "open"
    assert user_msg.status == "open"


# ── sweep_merge_gate_verification_degradation (fleet-wide alert) ──────────────


async def _make_gate_project(db, *, outcomes):
    """Persist a project carrying merge_gate_runtime.recent_outcomes."""
    project = Project(
        name="gate-unit",
        user_id="user1",
        merge_gate_runtime={"recent_outcomes": outcomes},
    )
    db.add(project)
    await db.commit()
    await db.refresh(project)
    return project


def _outcome(verified, reason=None):
    return {"at": _utcnow().isoformat(), "verified": verified, "reason": reason}


@pytest.mark.asyncio
async def test_merge_gate_degradation_sweep_alerts_when_rate_collapses(
    reaper_db, mocker
):
    mocker.patch("app.alert_dispatch.broadcast_to_project", new_callable=AsyncMock)
    # 3 degraded projects, 12 recent merges, only 3 verified -> rate 0.25 < 0.5.
    projects = []
    for _ in range(3):
        p = await _make_gate_project(
            reaper_db,
            outcomes=[
                _outcome(True),
                _outcome(False, "verify_setup_failed"),
                _outcome(False, "verify_setup_failed"),
                _outcome(False, "verify_setup_failed"),
            ],
        )
        projects.append(p)

    created = await _rs.sweep_merge_gate_verification_degradation(reaper_db)
    assert created == 3

    from app.alert_dispatch import _MERGE_GATE_DEGRADATION_ALERT_TITLE

    alerts = (
        (
            await reaper_db.execute(
                _select(Alert).where(Alert.title == _MERGE_GATE_DEGRADATION_ALERT_TITLE)
            )
        )
        .scalars()
        .all()
    )
    assert len(alerts) == 3
    # Dominant reason is a degraded-environment failure -> ERROR severity.
    assert all(a.severity == AlertSeverity.ERROR for a in alerts)
    assert all("could not be run" in a.message for a in alerts)

    # Second sweep dedups on the stable title — no new alerts.
    again = await _rs.sweep_merge_gate_verification_degradation(reaper_db)
    assert again == 0


@pytest.mark.asyncio
async def test_merge_gate_degradation_sweep_silent_when_healthy(reaper_db, mocker):
    mocker.patch("app.alert_dispatch.broadcast_to_project", new_callable=AsyncMock)
    # Healthy fleet: ample sample, high verification rate, no degraded projects.
    for _ in range(3):
        await _make_gate_project(
            reaper_db,
            outcomes=[_outcome(True), _outcome(True), _outcome(True), _outcome(True)],
        )

    created = await _rs.sweep_merge_gate_verification_degradation(reaper_db)
    assert created == 0


@pytest.mark.asyncio
async def test_merge_gate_degradation_sweep_ignores_tiny_sample(reaper_db, mocker):
    mocker.patch("app.alert_dispatch.broadcast_to_project", new_callable=AsyncMock)
    # Rate is terrible (0%) but only 2 samples — below min_samples, so no alert.
    await _make_gate_project(
        reaper_db,
        outcomes=[
            _outcome(False, "verify_setup_failed"),
            _outcome(False, "verify_setup_failed"),
        ],
    )

    created = await _rs.sweep_merge_gate_verification_degradation(reaper_db)
    assert created == 0


# ── sweep_migration_slot_contention (per-project alert) ──────────────────────


def _mk_migration_inprogress(project_id, *, started_delta, title="mig-holder"):
    from app.time_utils import _utcnow

    return Task(
        project_id=project_id,
        title=title,
        description="m",
        status=TaskStatus.IN_PROGRESS,
        created_by="t",
        requires_approval=False,
        payloads=[{"type": "migrate"}],
        context={},
        status_log=[],
        started_at=_utcnow() - started_delta,
    )


def _mk_migration_waiter(project_id, *, status=TaskStatus.QUEUED, updated_delta=None):
    from app.time_utils import _utcnow

    kwargs = {}
    if updated_delta is not None:
        kwargs["updated_at"] = _utcnow() - updated_delta
    return Task(
        project_id=project_id,
        title="mig-waiter",
        description="w",
        status=status,
        created_by="t",
        requires_approval=False,
        payloads=[{"type": "migrate"}],
        context={},
        status_log=[],
        **kwargs,
    )


@pytest.mark.asyncio
async def test_migration_slot_contention_sweep_alerts_past_threshold(reaper_db, mocker):
    mocker.patch("app.alert_dispatch.broadcast_to_project", new_callable=AsyncMock)
    project = Project(name="slot-unit", user_id="user1")
    reaper_db.add(project)
    await reaper_db.commit()
    await reaper_db.refresh(project)

    holder = _mk_migration_inprogress(project.id, started_delta=timedelta(hours=2))
    waiter = _mk_migration_waiter(project.id, updated_delta=timedelta(minutes=45))
    reaper_db.add_all([holder, waiter])
    await reaper_db.commit()

    created = await _rs.sweep_migration_slot_contention(reaper_db)
    assert created == 1

    from app.alert_dispatch import _MIGRATION_SLOT_CONTENTION_ALERT_TITLE

    alerts = (
        (
            await reaper_db.execute(
                _select(Alert).where(
                    Alert.title == _MIGRATION_SLOT_CONTENTION_ALERT_TITLE
                )
            )
        )
        .scalars()
        .all()
    )
    assert len(alerts) == 1
    assert alerts[0].project_id == project.id
    assert alerts[0].task_id == holder.id
    assert "parked waiting" in alerts[0].message

    # Dedups on the stable title — a second sweep raises no new alert.
    again = await _rs.sweep_migration_slot_contention(reaper_db)
    assert again == 0


@pytest.mark.asyncio
async def test_migration_slot_contention_sweep_silent_below_threshold(
    reaper_db, mocker
):
    mocker.patch("app.alert_dispatch.broadcast_to_project", new_callable=AsyncMock)
    project = Project(name="slot-fresh", user_id="user1")
    reaper_db.add(project)
    await reaper_db.commit()
    await reaper_db.refresh(project)

    # Fresh holder (5m) with a waiter — contended but under the 1h threshold.
    holder = _mk_migration_inprogress(project.id, started_delta=timedelta(minutes=5))
    waiter = _mk_migration_waiter(project.id, updated_delta=timedelta(minutes=3))
    reaper_db.add_all([holder, waiter])
    await reaper_db.commit()

    created = await _rs.sweep_migration_slot_contention(reaper_db)
    assert created == 0


@pytest.mark.asyncio
async def test_migration_slot_contention_sweep_silent_with_no_waiters(
    reaper_db, mocker
):
    mocker.patch("app.alert_dispatch.broadcast_to_project", new_callable=AsyncMock)
    project = Project(name="slot-solo", user_id="user1")
    reaper_db.add(project)
    await reaper_db.commit()
    await reaper_db.refresh(project)

    # Long-held solo migration, nobody parked -> not a contention event.
    holder = _mk_migration_inprogress(project.id, started_delta=timedelta(hours=4))
    reaper_db.add(holder)
    await reaper_db.commit()

    created = await _rs.sweep_migration_slot_contention(reaper_db)
    assert created == 0


# ── event_driven_unblock_dependents ──────────────────────────────────────────

from app.reaper_sweeps import event_driven_unblock_dependents  # noqa: E402


@pytest.mark.asyncio
async def test_landing_event_resumes_blocked_dependent_immediately(reaper_db, mocker):
    """A BLOCKED dependent whose capability need is satisfied by a just-landed
    foundation is immediately resumed to QUEUED by event_driven_unblock_dependents
    — without waiting for the next periodic sweep, even while new autonomous root
    spawning is disabled."""
    mocker.patch("app.broadcast.broadcast_to_project", new_callable=AsyncMock)
    project = await _make_project(reaper_db)
    now = _utcnow()

    # Foundation: COMPLETED and carries the merge-unblock signal.
    foundation = Task(
        project_id=project.id,
        title="build auth layer",
        description="x",
        status=TaskStatus.COMPLETED,
        created_by="u",
        requires_approval=False,
        payloads=[{"type": "create"}],
        context={"pr_merged": True, "provides_capability": "auth-layer"},
        status_log=[],
    )
    reaper_db.add(foundation)
    await reaper_db.commit()
    await reaper_db.refresh(foundation)

    # Dependent: BLOCKED waiting on the capability "auth-layer".
    # recheck_after is in the future — the periodic sweep would NOT pick this up.
    dependent = await _blocked_task(
        reaper_db,
        project,
        blocker_kind="precondition",
        blocked_reason="BLOCKER: needs auth layer",
        depends_on=[],
        context={
            "blocker": {
                "source": "agent",
                "waiting_on": "auth-layer",
                "reason": "BLOCKER: needs auth layer",
                "need": {"kind": "capability", "ref": "auth-layer"},
            }
        },
        recheck_after=now + timedelta(hours=1),  # not yet due for the sweep
    )
    project.autonomous_enabled = False
    reaper_db.add(project)
    await reaper_db.commit()

    # The periodic sweep would NOT pick this up (recheck_after is in the future).
    # The event-driven path must resume it immediately.
    resumed = await event_driven_unblock_dependents(foundation, reaper_db)

    assert resumed == 1
    await reaper_db.refresh(dependent)
    assert dependent.status == TaskStatus.QUEUED
    assert "blocker" not in (dependent.context or {})
    assert len(dependent.attempt_history or []) == 1


@pytest.mark.asyncio
async def test_event_driven_unblock_respects_project_drain_fence(reaper_db, mocker):
    """A landing event cannot resurrect BLOCKED work while the project drains."""

    mocker.patch("app.broadcast.broadcast_to_project", new_callable=AsyncMock)
    project = await _make_project(reaper_db)
    foundation = Task(
        project_id=project.id,
        title="build auth layer",
        description="x",
        status=TaskStatus.COMPLETED,
        created_by="u",
        requires_approval=False,
        payloads=[{"type": "create"}],
        context={"pr_merged": True, "provides_capability": "auth-layer"},
        status_log=[],
    )
    reaper_db.add(foundation)
    await reaper_db.commit()
    await reaper_db.refresh(foundation)
    dependent = await _blocked_task(
        reaper_db,
        project,
        blocker_kind="precondition",
        blocked_reason="BLOCKER: needs auth layer",
        depends_on=[],
        context={
            "blocker": {
                "source": "agent",
                "waiting_on": "auth-layer",
                "need": {"kind": "capability", "ref": "auth-layer"},
            }
        },
        recheck_after=_utcnow() + timedelta(hours=1),
    )
    project.paused = True
    reaper_db.add(project)
    await reaper_db.commit()

    resumed = await event_driven_unblock_dependents(foundation, reaper_db)

    assert resumed == 0
    await reaper_db.refresh(dependent)
    assert dependent.status == TaskStatus.BLOCKED
    assert not (dependent.attempt_history or [])


@pytest.mark.asyncio
async def test_event_driven_unblock_skips_without_merge_signal(reaper_db, mocker):
    """If the foundation is COMPLETED but has NO merge-unblock signal, the
    event-driven path returns 0 and leaves the dependent BLOCKED."""
    mocker.patch("app.broadcast.broadcast_to_project", new_callable=AsyncMock)
    project = await _make_project(reaper_db)
    now = _utcnow()

    # Foundation: COMPLETED but NO pr_merged signal.
    foundation = Task(
        project_id=project.id,
        title="incomplete foundation",
        description="x",
        status=TaskStatus.COMPLETED,
        created_by="u",
        requires_approval=False,
        payloads=[{"type": "create"}],
        context={"provides_capability": "auth-layer"},  # no pr_merged
        status_log=[],
    )
    reaper_db.add(foundation)
    await reaper_db.commit()
    await reaper_db.refresh(foundation)

    dependent = await _blocked_task(
        reaper_db,
        project,
        blocker_kind="precondition",
        blocked_reason="BLOCKER: needs auth layer",
        depends_on=[],
        context={
            "blocker": {
                "source": "agent",
                "waiting_on": "auth-layer",
                "reason": "BLOCKER: needs auth layer",
                "need": {"kind": "capability", "ref": "auth-layer"},
            }
        },
        recheck_after=now + timedelta(hours=1),
    )

    resumed = await event_driven_unblock_dependents(foundation, reaper_db)

    assert resumed == 0
    await reaper_db.refresh(dependent)
    assert dependent.status == TaskStatus.BLOCKED


@pytest.mark.asyncio
async def test_event_driven_unblock_no_match_leaves_dependent_blocked(
    reaper_db, mocker
):
    """A foundation that does NOT provide the needed capability leaves the
    dependent BLOCKED (no false-positive resume)."""
    mocker.patch("app.broadcast.broadcast_to_project", new_callable=AsyncMock)
    project = await _make_project(reaper_db)
    now = _utcnow()

    # Foundation provides a DIFFERENT capability.
    foundation = Task(
        project_id=project.id,
        title="unrelated foundation",
        description="x",
        status=TaskStatus.COMPLETED,
        created_by="u",
        requires_approval=False,
        payloads=[{"type": "create"}],
        context={"pr_merged": True, "provides_capability": "database-schema"},
        status_log=[],
    )
    reaper_db.add(foundation)
    await reaper_db.commit()
    await reaper_db.refresh(foundation)

    dependent = await _blocked_task(
        reaper_db,
        project,
        blocker_kind="precondition",
        blocked_reason="BLOCKER: needs auth layer",
        depends_on=[],
        context={
            "blocker": {
                "source": "agent",
                "waiting_on": "auth-layer",
                "reason": "BLOCKER: needs auth layer",
                "need": {"kind": "capability", "ref": "auth-layer"},
            }
        },
        recheck_after=now + timedelta(hours=1),
    )

    resumed = await event_driven_unblock_dependents(foundation, reaper_db)

    assert resumed == 0
    await reaper_db.refresh(dependent)
    assert dependent.status == TaskStatus.BLOCKED


@pytest.mark.asyncio
async def test_event_driven_unblock_fail_open(reaper_db, mocker, monkeypatch):
    """An exception inside the disposition logic is swallowed; the function
    returns 0 rather than propagating the error (fail-open guarantee)."""
    mocker.patch("app.broadcast.broadcast_to_project", new_callable=AsyncMock)
    project = await _make_project(reaper_db)
    now = _utcnow()

    foundation = Task(
        project_id=project.id,
        title="error-prone foundation",
        description="x",
        status=TaskStatus.COMPLETED,
        created_by="u",
        requires_approval=False,
        payloads=[{"type": "create"}],
        context={"pr_merged": True, "provides_capability": "some-cap"},
        status_log=[],
    )
    reaper_db.add(foundation)
    await reaper_db.commit()
    await reaper_db.refresh(foundation)

    await _blocked_task(
        reaper_db,
        project,
        blocker_kind="precondition",
        blocked_reason="BLOCKER: needs some-cap",
        depends_on=[],
        context={
            "blocker": {
                "source": "agent",
                "waiting_on": "some-cap",
                "reason": "BLOCKER: needs some-cap",
                "need": {"kind": "capability", "ref": "some-cap"},
            }
        },
        recheck_after=now + timedelta(hours=1),
    )

    # Inject a failure into _classify_blocker_needs to simulate an unexpected error.
    # Patch the module where the function is defined (blocked_task_unblock_sweeps)
    # since _event_driven_unblock_project references it as a module-local there.
    import app.blocked_task_unblock_sweeps as _btu_mod

    async def _raise(*args, **kwargs):
        raise RuntimeError("simulated classification error")

    monkeypatch.setattr(_btu_mod, "_classify_blocker_needs", _raise)

    # Must not raise; must return 0.
    result = await event_driven_unblock_dependents(foundation, reaper_db)
    assert result == 0


# ── event_driven_unblock_on_knowledge (knowledge-doc creation trigger) ─────────
#
# A KnowledgeDocument making a BLOCKED task's kind='task' / finding-hash
# prerequisite durably satisfiable must resume the waiter immediately — without
# waiting for the slow periodic Step-12 sweep (recheck_after may be far out, or
# only every 6h once escalated). These exercise the new fast path AND assert the
# conservative matching + all-needs-resolved gate are preserved.


async def _knowledge_doc(db, project, source_task_id, *, topic):
    """Persist a durable KnowledgeDocument whose substance can satisfy a need.

    The finding-hash substance is read by the index from the SOURCE TASK's
    ``context['finding_hash']`` (mirroring production), so the caller controls
    finding-hash matching via the source task; this helper supplies the topic."""
    from app.models import KnowledgeDocument

    doc = KnowledgeDocument(
        project_id=project.id,
        source_task_id=source_task_id,
        source_kind="web_research",
        title="finding doc",
        summary="substance summary",
        body="full body",
        topic=topic,
    )
    db.add(doc)
    await db.commit()
    await db.refresh(doc)
    return doc


@pytest.mark.asyncio
async def test_knowledge_event_resumes_blocked_task_immediately(reaper_db, mocker):
    """A BLOCKED task whose kind='task' / finding-hash need is now satisfied by a
    freshly-created KnowledgeDocument is resumed to QUEUED by
    event_driven_unblock_on_knowledge — WITHOUT running the periodic sweep, and
    even though recheck_after is far in the future."""
    mocker.patch("app.broadcast.broadcast_to_project", new_callable=AsyncMock)
    project = await _make_project(reaper_db)
    now = _utcnow()

    # Source task: CANCELLED, but its finding_hash is the substance the doc carries.
    source_task = Task(
        project_id=project.id,
        title="research",
        description="x",
        status=TaskStatus.CANCELLED,
        created_by="u",
        requires_approval=False,
        payloads=[{"type": "web_research"}],
        context={"finding_hash": "abc123def4561111", "failure_category": "SCOPE_EMPTY"},
        status_log=[],
    )
    reaper_db.add(source_task)
    await reaper_db.commit()
    await reaper_db.refresh(source_task)
    await _knowledge_doc(
        reaper_db,
        project,
        source_task.id,
        topic="some-topic",
    )

    # Dependent BLOCKED on that finding_hash; recheck_after in the future, so the
    # periodic sweep would NOT pick it up this tick.
    dependent = await _blocked_task(
        reaper_db,
        project,
        blocker_kind="upstream_dependency",
        blocked_reason="BLOCKER: needs the research finding",
        depends_on=[],
        context={
            "blocker": {
                "source": "agent",
                "waiting_on": "abc123def4561111",
                "reason": "BLOCKER: needs the research finding",
                "need": {"kind": "task", "ref": "abc123def4561111"},
            }
        },
        recheck_after=now + timedelta(hours=6),
    )

    resumed = await event_driven_unblock_on_knowledge(project.id, reaper_db)

    assert resumed == 1
    await reaper_db.refresh(dependent)
    assert dependent.status == TaskStatus.QUEUED
    # No edge wired (knowledge satisfaction has no live foundation).
    assert not (dependent.depends_on or [])
    # Blocker metadata cleared.
    assert "blocker" not in (dependent.context or {})


@pytest.mark.asyncio
async def test_knowledge_event_resumes_escalated_parked_task(reaper_db, mocker):
    """A PARKED (escalated) BLOCKED task — which the sweep only re-checks every 6h —
    is resumed immediately when matching knowledge lands (the incident shape)."""
    mocker.patch("app.broadcast.broadcast_to_project", new_callable=AsyncMock)
    mocker.patch(
        "app.alert_dispatch._resolve_blocked_escalation_alert",
        new_callable=AsyncMock,
    )
    project = await _make_project(reaper_db)
    now = _utcnow()

    source_task = Task(
        project_id=project.id,
        title="research",
        description="x",
        status=TaskStatus.CANCELLED,
        created_by="u",
        requires_approval=False,
        payloads=[{"type": "web_research"}],
        context={"finding_hash": "9999888877776666", "failure_category": "SCOPE_EMPTY"},
        status_log=[],
    )
    reaper_db.add(source_task)
    await reaper_db.commit()
    await reaper_db.refresh(source_task)
    await _knowledge_doc(
        reaper_db,
        project,
        source_task.id,
        topic="parked-topic",
    )

    dependent = await _blocked_task(
        reaper_db,
        project,
        blocker_kind="upstream_dependency",
        blocked_reason="BLOCKER: needs the research finding",
        depends_on=[],
        context={
            "blocker": {
                "source": "agent",
                "waiting_on": "9999888877776666",
                "reason": "BLOCKER: needs the research finding",
                "need": {"kind": "task", "ref": "9999888877776666"},
            },
            "_blocked_escalated": True,  # parked — 6h re-eval cadence
        },
        recheck_after=now + timedelta(hours=6),
    )

    resumed = await event_driven_unblock_on_knowledge(project.id, reaper_db)

    assert resumed == 1
    await reaper_db.refresh(dependent)
    assert dependent.status == TaskStatus.QUEUED
    assert "blocker" not in (dependent.context or {})


@pytest.mark.asyncio
async def test_knowledge_event_no_match_leaves_task_blocked(reaper_db, mocker):
    """A KnowledgeDocument whose substance (topic AND finding_hash) does NOT match
    the need's ref leaves the dependent BLOCKED — no false-positive resume."""
    mocker.patch("app.broadcast.broadcast_to_project", new_callable=AsyncMock)
    project = await _make_project(reaper_db)
    now = _utcnow()

    # Source task whose finding_hash is UNRELATED to the need.
    source_task = Task(
        project_id=project.id,
        title="other research",
        description="x",
        status=TaskStatus.CANCELLED,
        created_by="u",
        requires_approval=False,
        payloads=[{"type": "web_research"}],
        context={"finding_hash": "1111222233334444", "failure_category": "SCOPE_EMPTY"},
        status_log=[],
    )
    reaper_db.add(source_task)
    await reaper_db.commit()
    await reaper_db.refresh(source_task)
    await _knowledge_doc(
        reaper_db,
        project,
        source_task.id,
        topic="unrelated-topic",
    )

    dependent = await _blocked_task(
        reaper_db,
        project,
        blocker_kind="upstream_dependency",
        blocked_reason="BLOCKER: needs the research finding",
        depends_on=[],
        context={
            "blocker": {
                "source": "agent",
                "waiting_on": "aaaabbbbccccdddd",
                "reason": "BLOCKER: needs the research finding",
                "need": {"kind": "task", "ref": "aaaabbbbccccdddd"},
            }
        },
        recheck_after=now + timedelta(hours=6),
    )

    resumed = await event_driven_unblock_on_knowledge(project.id, reaper_db)

    assert resumed == 0
    await reaper_db.refresh(dependent)
    assert dependent.status == TaskStatus.BLOCKED
    assert "blocker" in (dependent.context or {})


@pytest.mark.asyncio
async def test_knowledge_event_holds_task_with_unsatisfied_sibling_need(
    reaper_db, mocker
):
    """A multi-need BLOCKED task whose new knowledge satisfies ONLY one of its two
    needs must stay BLOCKED — the all-needs-resolved resume gate is preserved. The
    satisfied need is dropped; the still-unsatisfied one is re-persisted."""
    mocker.patch("app.broadcast.broadcast_to_project", new_callable=AsyncMock)
    project = await _make_project(reaper_db)
    now = _utcnow()

    source_task = Task(
        project_id=project.id,
        title="research",
        description="x",
        status=TaskStatus.CANCELLED,
        created_by="u",
        requires_approval=False,
        payloads=[{"type": "web_research"}],
        context={"finding_hash": "5555666677778888", "failure_category": "SCOPE_EMPTY"},
        status_log=[],
    )
    reaper_db.add(source_task)
    await reaper_db.commit()
    await reaper_db.refresh(source_task)
    # Doc satisfies ONLY the first need's ref.
    await _knowledge_doc(
        reaper_db,
        project,
        source_task.id,
        topic="satisfied-topic",
    )

    # Two needs: one satisfied by the doc, one (still-missing-foundation) is not.
    dependent = await _blocked_task(
        reaper_db,
        project,
        blocker_kind="upstream_dependency",
        blocked_reason="BLOCKER: needs two findings",
        depends_on=[],
        context={
            "blocker": {
                "source": "agent",
                "waiting_on": "5555666677778888",
                "reason": "BLOCKER: needs two findings",
                "need": {"kind": "task", "ref": "5555666677778888"},
                "needs": [
                    {"kind": "task", "ref": "5555666677778888"},
                    {"kind": "task", "ref": "eeeeffff00001111"},
                ],
            }
        },
        recheck_after=now + timedelta(hours=6),
    )

    resumed = await event_driven_unblock_on_knowledge(project.id, reaper_db)

    assert resumed == 0
    await reaper_db.refresh(dependent)
    # Still BLOCKED — the second need is unsatisfied.
    assert dependent.status == TaskStatus.BLOCKED
    # The structured blocker is retained and now carries ONLY the unsatisfied need.
    from app.task_blocked import get_blocker_needs

    residual_refs = {n["ref"] for n in get_blocker_needs(dependent)}
    assert residual_refs == {"eeeeffff00001111"}


@pytest.mark.asyncio
async def test_knowledge_completion_path_unblocks_waiter_end_to_end(reaper_db, mocker):
    """End-to-end: driving the real knowledge-completion delivery chokepoint
    (_deliver_knowledge_to_dependents) fires the event-driven unblock and resumes a
    BLOCKED waiter — proving the trigger is wired at the shared persistence seam."""
    from app.models import KnowledgeDocument
    from app.knowledge_completion import _deliver_knowledge_to_dependents

    mocker.patch("app.broadcast.broadcast_to_project", new_callable=AsyncMock)
    project = await _make_project(reaper_db)
    now = _utcnow()

    # Producing task carrying the finding_hash, with a freshly-committed doc.
    producer = Task(
        project_id=project.id,
        title="producer",
        description="x",
        status=TaskStatus.COMPLETED,
        created_by="u",
        requires_approval=False,
        payloads=[{"type": "web_research"}],
        context={"finding_hash": "abcabcabc1234567"},
        status_log=[],
    )
    reaper_db.add(producer)
    await reaper_db.commit()
    await reaper_db.refresh(producer)
    doc = KnowledgeDocument(
        project_id=project.id,
        source_task_id=producer.id,
        source_kind="web_research",
        title="e2e doc",
        summary="e2e summary",
        body="e2e body",
        topic="e2e-topic",
    )
    reaper_db.add(doc)
    await reaper_db.commit()
    await reaper_db.refresh(doc)

    dependent = await _blocked_task(
        reaper_db,
        project,
        blocker_kind="upstream_dependency",
        blocked_reason="BLOCKER: needs e2e finding",
        depends_on=[],
        context={
            "blocker": {
                "source": "agent",
                "waiting_on": "abcabcabc1234567",
                "reason": "BLOCKER: needs e2e finding",
                "need": {"kind": "task", "ref": "abcabcabc1234567"},
            }
        },
        recheck_after=now + timedelta(hours=6),
    )

    # Drive the real delivery chokepoint (where the trigger is wired).
    await _deliver_knowledge_to_dependents(
        producer, doc, {"title": "e2e doc", "summary": "e2e summary"}, reaper_db
    )

    await reaper_db.refresh(dependent)
    assert dependent.status == TaskStatus.QUEUED
    assert "blocker" not in (dependent.context or {})


@pytest.mark.asyncio
async def test_knowledge_completion_unblocks_waiter_with_document_pointer(
    reaper_db, mocker
):
    """A BLOCKED dependent task that lists the producer in its depends_on receives
    a knowledge_prerequisites pointer AND transitions to QUEUED after
    _deliver_knowledge_to_dependents fires.

    This validates both halves of the fix:
    1. The SQL filter Task.depends_on.contains([task_id_str]) correctly identifies
       the BLOCKED waiter (not just QUEUED tasks).
    2. event_driven_unblock_on_knowledge resumes the waiter with the pointer already
       stamped so it launches with the produced knowledge.
    """
    from app.models import KnowledgeDocument
    from app.knowledge_completion import (
        _deliver_knowledge_to_dependents,
        KNOWLEDGE_PREREQUISITES_KEY,
    )

    mocker.patch("app.broadcast.broadcast_to_project", new_callable=AsyncMock)
    project = await _make_project(reaper_db)
    now = _utcnow()

    # Producing task that just completed a web_research document.
    producer = Task(
        project_id=project.id,
        title="producer",
        description="x",
        status=TaskStatus.COMPLETED,
        created_by="u",
        requires_approval=False,
        payloads=[{"type": "web_research"}],
        context={"finding_hash": "deadbeef12345678"},
        status_log=[],
    )
    reaper_db.add(producer)
    await reaper_db.commit()
    await reaper_db.refresh(producer)

    doc = KnowledgeDocument(
        project_id=project.id,
        source_task_id=producer.id,
        source_kind="web_research",
        title="pointer doc",
        summary="pointer summary",
        body="pointer body",
        topic="pointer-topic",
    )
    reaper_db.add(doc)
    await reaper_db.commit()
    await reaper_db.refresh(doc)

    # BLOCKED waiter that explicitly depends on the producer and has a knowledge
    # need satisfied by the producer's finding_hash.
    waiter = await _blocked_task(
        reaper_db,
        project,
        blocker_kind="upstream_dependency",
        blocked_reason="BLOCKER: waiting on knowledge from producer",
        depends_on=[str(producer.id)],
        context={
            "blocker": {
                "source": "agent",
                "waiting_on": "deadbeef12345678",
                "reason": "BLOCKER: waiting on knowledge from producer",
                "need": {"kind": "task", "ref": "deadbeef12345678"},
            }
        },
        recheck_after=now + timedelta(hours=6),
    )

    await _deliver_knowledge_to_dependents(
        producer, doc, {"title": "pointer doc", "summary": "pointer summary"}, reaper_db
    )

    await reaper_db.refresh(waiter)

    # The pointer must be stamped on the waiter's context before resume.
    prereqs = (waiter.context or {}).get(KNOWLEDGE_PREREQUISITES_KEY, [])
    assert any(
        isinstance(p, dict) and p.get("doc_id") == str(doc.id) for p in prereqs
    ), f"knowledge_prerequisites pointer missing; got {prereqs}"

    # The event-driven unblock should have resumed the waiter.
    assert waiter.status == TaskStatus.QUEUED
    assert "blocker" not in (waiter.context or {})


@pytest.mark.asyncio
async def test_hollow_compute_doc_leaves_waiter_blocked(reaper_db, mocker):
    """A hollow compute KnowledgeDocument must not event-unblock a waiting task.

    The durable compute predicate rejects the doc, so the BLOCKED waiter stays
    blocked and no knowledge-prerequisite pointer is stamped.
    """
    from app.models import KnowledgeDocument
    from app.reaper_sweeps import event_driven_unblock_on_knowledge
    from app.knowledge_completion import KNOWLEDGE_PREREQUISITES_KEY

    mocker.patch("app.broadcast.broadcast_to_project", new_callable=AsyncMock)
    project = await _make_project(reaper_db)
    now = _utcnow()

    producer = Task(
        project_id=project.id,
        title="compute producer",
        description="x",
        status=TaskStatus.COMPLETED,
        created_by="u",
        requires_approval=False,
        payloads=[{"type": "compute_experiment"}],
        context={"finding_hash": "compute-hollow-need"},
        status_log=[],
    )
    reaper_db.add(producer)
    await reaper_db.commit()
    await reaper_db.refresh(producer)

    doc = KnowledgeDocument(
        project_id=project.id,
        source_task_id=producer.id,
        source_kind="compute_experiment",
        title="hollow compute doc",
        summary="hollow compute doc",
        body="hollow compute doc",
        topic="compute-hollow-need",
        source_citations=[],
        forward_quality_verdict={
            "analysis_mode": "fallback",
            "citation_grounding_miss": True,
            "probes": {
                "grounding": {
                    "checked": True,
                    "file_citations": 1,
                    "grounded": 0,
                    "unresolved": ["https://signed.example.com/jobs/result.json"],
                }
            },
        },
    )
    reaper_db.add(doc)
    await reaper_db.commit()
    await reaper_db.refresh(doc)

    waiter = await _blocked_task(
        reaper_db,
        project,
        blocker_kind="upstream_dependency",
        blocked_reason="BLOCKER: waiting on compute knowledge",
        depends_on=[str(producer.id)],
        context={
            "blocker": {
                "source": "agent",
                "waiting_on": "compute-hollow-need",
                "reason": "BLOCKER: waiting on compute knowledge",
                "need": {"kind": "task", "ref": "compute-hollow-need"},
            }
        },
        recheck_after=now + timedelta(hours=6),
    )

    resumed = await event_driven_unblock_on_knowledge(project.id, reaper_db)

    assert resumed == 0
    await reaper_db.refresh(waiter)
    assert waiter.status == TaskStatus.BLOCKED
    assert (waiter.context or {}).get(KNOWLEDGE_PREREQUISITES_KEY) is None


@pytest.mark.asyncio
async def test_knowledge_completion_trigger_is_fail_open(
    reaper_db, mocker, monkeypatch
):
    """If the event-driven unblock raises, the knowledge-completion chokepoint
    swallows it — document delivery must never break the producing task."""
    from app.models import KnowledgeDocument
    from app.knowledge_completion import _deliver_knowledge_to_dependents
    import app.reaper_sweeps as _rs_mod

    mocker.patch("app.broadcast.broadcast_to_project", new_callable=AsyncMock)
    project = await _make_project(reaper_db)

    producer = Task(
        project_id=project.id,
        title="producer",
        description="x",
        status=TaskStatus.COMPLETED,
        created_by="u",
        requires_approval=False,
        payloads=[{"type": "web_research"}],
        context={},
        status_log=[],
    )
    reaper_db.add(producer)
    await reaper_db.commit()
    await reaper_db.refresh(producer)
    doc = KnowledgeDocument(
        project_id=project.id,
        source_task_id=producer.id,
        source_kind="web_research",
        title="d",
        summary="s",
        body="b",
        topic="t",
    )
    reaper_db.add(doc)
    await reaper_db.commit()
    await reaper_db.refresh(doc)

    async def _raise(*args, **kwargs):
        raise RuntimeError("simulated unblock failure")

    monkeypatch.setattr(_rs_mod, "event_driven_unblock_on_knowledge", _raise)

    # Must not raise — the chokepoint is best-effort.
    await _deliver_knowledge_to_dependents(
        producer, doc, {"title": "d", "summary": "s"}, reaper_db
    )


# ── Fix: file_conflict blockers must not hard-park or survive requeue ──────────
#
# Regression tests for the three-part fix:
#   Fix 1: clear_blocker_metadata clears _blocked_escalated / _precondition_escalated_key
#   Fix 2: transient blockers (file_conflict) never hard-park at +10y;
#           need-less parked transient blockers re-evaluate and requeue when cleared
#   Fix 3: recovery sweep resets already-parked transient blockers to bounded re-eval

from app.reaper_sweeps import (  # noqa: E402
    sweep_recover_stranded_transient_blockers,
    _BLOCKED_PARKED_REEVAL_INTERVAL,
    _TRANSIENT_BLOCKER_PARK_SENTINEL,
)
# _BLOCKED_ESCALATED_PARK already imported in the Step-12 import block above.


# ── Fix 1: clear_blocker_metadata wipes escalation markers ────────────────────


def test_clear_blocker_metadata_clears_escalation_markers():
    """clear_blocker_metadata must wipe _blocked_escalated and _precondition_escalated_key
    so a requeued task never immediately re-parks on its next block episode."""
    from app.task_blocked import clear_blocker_metadata
    from testing.factories import make_task

    t = make_task(
        status=TaskStatus.BLOCKED,
        blocker_kind="file_conflict",
        blocked_reason="Waiting: file conflict on a.py",
        blocked_at=_utcnow(),
        recheck_after=_utcnow() + timedelta(days=3650),
        context={
            "blocker": {"source": "filter", "waiting_on": "a.py"},
            "failure_class": "BLOCKED_PRECONDITION",
            "failure_category": "BLOCKED_PRECONDITION",
            "_blocked_escalated": True,
            "_precondition_escalated_key": "file conflict on a.py",
        },
    )

    clear_blocker_metadata(t)

    ctx = t.context or {}
    assert "_blocked_escalated" not in ctx
    assert "_precondition_escalated_key" not in ctx
    assert "blocker" not in ctx
    assert "failure_class" not in ctx
    assert "failure_category" not in ctx
    assert t.recheck_after is None
    assert t.blocker_kind is None


@pytest.mark.asyncio
async def test_file_conflict_requeue_does_not_re_park(reaper_db, mocker):
    """The key regression: a file_conflict blocker whose context carries
    _blocked_escalated=True is requeued by the operator → on the next reblock
    and sweep cycle the task must NOT immediately re-park at +10y.

    Concrete shape: task has _blocked_escalated=True (operator requeued it),
    becomes BLOCKED again (conflict still live when it runs), and the sweep
    is called.  With the fix, the sweep rechecks state and requeues (clear)
    instead of re-parking at the +10y sentinel."""
    mocker.patch("app.broadcast.broadcast_to_project", new_callable=AsyncMock)
    project = await _make_project(reaper_db)
    now = _utcnow()

    # Simulate a file_conflict blocker that was re-blocked AFTER a requeue:
    # the requeue cleared the escalation markers (Fix 1), but here we test
    # the sweep path directly — task has _blocked_escalated=True and is due.
    t = await _blocked_task(
        reaper_db,
        project,
        blocker_kind="file_conflict",
        blocked_reason="Waiting: file conflict on a.py",
        blocked_at=now - timedelta(hours=7),
        depends_on=[],
        context={
            "blocker": {"source": "filter", "waiting_on": "a.py"},
            "_blocked_escalated": True,
            "_blocked_first_at": {
                "key": "file conflict on a.py",
                "at": (now - timedelta(hours=7)).isoformat(),
            },
        },
        recheck_after=now - timedelta(minutes=1),  # due
    )

    requeued = await sweep_unblock_blocked_tasks(reaper_db, now)

    # Transient blocker with _blocked_escalated=True should be requeued
    # (conflict clears since there are no in-flight tasks holding it),
    # NOT re-parked at +10y.
    assert requeued == 1
    await reaper_db.refresh(t)
    assert t.status == TaskStatus.QUEUED
    # Must NOT have a far-future recheck_after (the +10y sentinel).
    far_future = now + _BLOCKED_ESCALATED_PARK - timedelta(days=1)
    assert t.recheck_after is None or t.recheck_after < far_future


@pytest.mark.asyncio
async def test_file_conflict_escalation_uses_bounded_recheck_not_10y(reaper_db, mocker):
    """When a file_conflict blocker crosses the 6h escalation deadline, it must be
    parked at _BLOCKED_PARKED_REEVAL_INTERVAL (bounded) not _BLOCKED_ESCALATED_PARK
    (+10y), so it self-heals when the overlapping in-flight task merges."""
    mocker.patch("app.broadcast.broadcast_to_project", new_callable=AsyncMock)
    mocker.patch(
        "app.alert_dispatch._maybe_create_blocked_escalation_alert",
        new_callable=AsyncMock,
    )
    project = await _make_project(reaper_db)
    now = _utcnow()

    # A file_conflict blocker past the 6h escalation deadline, not yet escalated.
    t = await _blocked_task(
        reaper_db,
        project,
        blocker_kind="file_conflict",
        blocked_reason="Waiting: file conflict on a.py",
        blocked_at=now - timedelta(hours=7),  # 7h > 6h threshold
        depends_on=[],
        context={
            "blocker": {"source": "filter", "waiting_on": "a.py"},
            "_blocked_first_at": {
                "key": "file conflict on a.py",
                "at": (now - timedelta(hours=7)).isoformat(),
            },
        },
        recheck_after=now - timedelta(minutes=1),
    )

    requeued = await sweep_unblock_blocked_tasks(reaper_db, now)

    # Escalation fires; task stays BLOCKED.
    assert requeued == 0
    await reaper_db.refresh(t)
    assert t.status == TaskStatus.BLOCKED
    assert t.context.get("_blocked_escalated") is True

    # recheck_after must be bounded (PARKED_REEVAL_INTERVAL) NOT the +10y sentinel.
    assert t.recheck_after is not None
    far_future = now + _BLOCKED_ESCALATED_PARK - timedelta(days=1)
    assert t.recheck_after < far_future
    # Specifically within a few minutes of _BLOCKED_PARKED_REEVAL_INTERVAL from now.
    expected_max = now + _BLOCKED_PARKED_REEVAL_INTERVAL + timedelta(minutes=5)
    assert t.recheck_after <= expected_max


@pytest.mark.asyncio
async def test_parked_file_conflict_re_evaluates_on_next_cycle(reaper_db, mocker):
    """A file_conflict blocker with _blocked_escalated=True and a due
    recheck_after re-evaluates its precondition state instead of re-parking at
    +10y.  When the conflict has cleared (no in-flight tasks holding it), the
    task is requeued and the escalated marker is cleared."""
    mocker.patch("app.broadcast.broadcast_to_project", new_callable=AsyncMock)
    project = await _make_project(reaper_db)
    now = _utcnow()

    t = await _blocked_task(
        reaper_db,
        project,
        blocker_kind="file_conflict",
        blocked_reason="Waiting: file conflict on a.py",
        blocked_at=now - timedelta(hours=8),
        depends_on=[],
        context={
            "blocker": {"source": "filter", "waiting_on": "a.py"},
            "_blocked_escalated": True,
        },
        recheck_after=now - timedelta(minutes=1),  # due
    )

    requeued = await sweep_unblock_blocked_tasks(reaper_db, now)

    assert requeued == 1
    await reaper_db.refresh(t)
    assert t.status == TaskStatus.QUEUED
    ctx = t.context or {}
    # Escalation marker cleared on the unblock transition.
    assert "_blocked_escalated" not in ctx


# ── Fix 3: recovery sweep un-strands hard-parked transient blockers ────────────


@pytest.mark.asyncio
async def test_recovery_sweep_resets_hard_parked_file_conflict(reaper_db, mocker):
    """The recovery sweep finds a file_conflict blocker hard-parked at +10y and
    resets its recheck_after to the bounded re-eval cadence so it self-heals."""
    mocker.patch("app.broadcast.broadcast_to_project", new_callable=AsyncMock)
    project = await _make_project(reaper_db)
    now = _utcnow()

    t = await _blocked_task(
        reaper_db,
        project,
        blocker_kind="file_conflict",
        blocked_reason="Waiting: file conflict on a.py",
        blocked_at=now - timedelta(hours=8),
        depends_on=[],
        context={
            "blocker": {"source": "filter", "waiting_on": "a.py"},
            "_blocked_escalated": True,
        },
        # Hard-parked at the +10y sentinel.
        recheck_after=now + timedelta(days=3650),
    )

    recovered = await sweep_recover_stranded_transient_blockers(reaper_db, now)

    assert recovered == 1
    await reaper_db.refresh(t)
    assert t.status == TaskStatus.BLOCKED  # still BLOCKED
    # recheck_after reset to bounded re-eval (within 2× PARKED_REEVAL_INTERVAL).
    assert t.recheck_after is not None
    far_future = now + _TRANSIENT_BLOCKER_PARK_SENTINEL
    assert t.recheck_after < far_future
    expected_max = now + _BLOCKED_PARKED_REEVAL_INTERVAL + timedelta(minutes=5)
    assert t.recheck_after <= expected_max


@pytest.mark.asyncio
async def test_recovery_sweep_ignores_non_transient_blockers(reaper_db, mocker):
    """The recovery sweep must not touch hard-parked blockers that are NOT
    transient kinds (e.g. upstream_dependency) — those stay for the operator."""
    mocker.patch("app.broadcast.broadcast_to_project", new_callable=AsyncMock)
    project = await _make_project(reaper_db)
    now = _utcnow()
    far = now + timedelta(days=3650)

    t = await _blocked_task(
        reaper_db,
        project,
        blocker_kind="upstream_dependency",
        blocked_reason="Waiting: upstream dependency",
        blocked_at=now - timedelta(hours=8),
        depends_on=[],
        context={"blocker": {"source": "agent"}, "_blocked_escalated": True},
        recheck_after=far,
    )

    recovered = await sweep_recover_stranded_transient_blockers(reaper_db, now)

    assert recovered == 0
    await reaper_db.refresh(t)
    # recheck_after must be unchanged.
    assert t.recheck_after is not None
    assert abs((t.recheck_after - far).total_seconds()) < 5


@pytest.mark.asyncio
async def test_recovery_sweep_then_unblock_sweep_requeues(reaper_db, mocker):
    """End-to-end: recovery sweep resets a hard-parked transient blocker,
    then the unblock sweep requeues it when the conflict has cleared."""
    mocker.patch("app.broadcast.broadcast_to_project", new_callable=AsyncMock)
    project = await _make_project(reaper_db)
    now = _utcnow()

    t = await _blocked_task(
        reaper_db,
        project,
        blocker_kind="file_conflict",
        blocked_reason="Waiting: file conflict on a.py",
        blocked_at=now - timedelta(hours=8),
        depends_on=[],
        context={
            "blocker": {"source": "filter", "waiting_on": "a.py"},
            "_blocked_escalated": True,
        },
        recheck_after=now + timedelta(days=3650),
    )

    # Step 1: recovery sweep moves the row back into the due window
    # (recheck_after → now + _BLOCKED_PARKED_REEVAL_INTERVAL).
    recovered = await sweep_recover_stranded_transient_blockers(reaper_db, now)
    assert recovered == 1

    # Step 2: unblock sweep re-evaluates when the reset recheck_after is due.
    # Advance "now" past the new recheck_after so the row is due.
    future_now = now + _BLOCKED_PARKED_REEVAL_INTERVAL + timedelta(minutes=1)
    requeued = await sweep_unblock_blocked_tasks(reaper_db, future_now)
    assert requeued == 1
    await reaper_db.refresh(t)
    assert t.status == TaskStatus.QUEUED


# ── Fix 4: need-less NON-transient escalated blockers get bounded backoff ──────
#
# Regression coverage for the 2026-08-18 incident (5 tasks stranded BLOCKED
# with recheck_after stamped ~2036): a need-less, non-transient escalated
# blocker (e.g. a missing runner capability, a repo-wide gate bug) used to be
# parked at _BLOCKED_ESCALATED_PARK (+10y) — a fake far-future timestamp that
# evicted the row from the due-query forever. It must now stay on the bounded
# _BLOCKED_ESCALATED_RECHECK_MAX (24h) cadence, self-healing when the fix
# lands outside the dependency graph, and its BLOCKED status message must
# name the blocker and the next recheck time.


@pytest.mark.asyncio
async def test_needless_escalated_blocker_recheck_capped_at_24h_not_10y(
    reaper_db, mocker
):
    """An already-escalated, need-less, NON-transient blocker (blocker_kind not
    in {file_conflict, pending_rollup}) whose recheck comes due must be
    rescheduled on the bounded _BLOCKED_ESCALATED_RECHECK_MAX (24h) cadence —
    never the old _BLOCKED_ESCALATED_PARK (+10y) sentinel — and the status
    message must name the blocker and the next recheck time."""
    from app.blocked_task_unblock_sweeps import _BLOCKED_ESCALATED_RECHECK_MAX

    mocker.patch("app.broadcast.broadcast_to_project", new_callable=AsyncMock)
    project = await _make_project(reaper_db)
    now = _utcnow()

    t = await _blocked_task(
        reaper_db,
        project,
        blocker_kind="runner_capability",
        blocked_reason="Waiting: runner capability playwright_chromium_usable",
        blocked_at=now - timedelta(hours=8),
        depends_on=[],
        context={
            "blocker": {
                "source": "gate",
                "waiting_on": "runner_capability",
                "escalation_detail": "no runner advertises playwright_chromium_usable",
            },
            "_blocked_escalated": True,
            # Many prior escalated-recheck cycles: proves the cadence is
            # capped at 24h, not merely under the old +10y sentinel (a bug
            # that used the ordinary 1h cap here would also pass a
            # "< 10y" assertion, so this pins the cap specifically).
            "_blocked_escalated_recheck_count": 10,
        },
        recheck_after=now - timedelta(minutes=1),  # due
    )

    requeued = await sweep_unblock_blocked_tasks(reaper_db, now)

    # Need-less non-transient blocker never auto-resumes — it stays parked
    # for the operator, but on the bounded cadence.
    assert requeued == 0
    await reaper_db.refresh(t)
    assert t.status == TaskStatus.BLOCKED
    assert t.context.get("_blocked_escalated") is True

    assert t.recheck_after is not None
    # Bounded at the 24h escalated cap (with slack for the doubled backoff
    # at count=11 exceeding the cap and the small alert/commit window).
    expected_min = now + _BLOCKED_ESCALATED_RECHECK_MAX - timedelta(minutes=5)
    expected_max = now + _BLOCKED_ESCALATED_RECHECK_MAX + timedelta(minutes=5)
    assert expected_min <= t.recheck_after <= expected_max, (
        f"expected recheck_after within 5min of the 24h escalated cap, got "
        f"{t.recheck_after} (now={now})"
    )
    # Never the old multi-year tombstone.
    far_future = now + _BLOCKED_ESCALATED_PARK - timedelta(days=1)
    assert t.recheck_after < far_future

    # Surfaced-reasons requirement: the status message names the blocker and
    # the next recheck time, not a silent/unexplained stall.
    msg = t.latest_status_message or ""
    assert "runner_capability" in msg
    assert "next automatic recheck at" in msg
    assert t.recheck_after.strftime("%Y-%m-%d %H:%M") in msg


@pytest.mark.asyncio
async def test_needless_escalated_blocker_resumes_when_capability_need_satisfied(
    reaper_db, mocker
):
    """An already-escalated blocker that DOES carry a structured (machine-
    resolvable) need must automatically resume to QUEUED once that need is
    satisfied by completed work — the exact 2026-08-18 incident shape (runner
    gains a capability outside the dependency graph) — with NO operator
    action required. This is the "recheck after the blocker clears actually
    requeues the task" half of the regression."""
    mocker.patch("app.broadcast.broadcast_to_project", new_callable=AsyncMock)
    project = await _make_project(reaper_db)
    now = _utcnow()

    # The foundation that supplies the capability the blocked task needs —
    # already COMPLETED, advertising the capability via the marker convention
    # (app.capability_registry / _classify_blocker_needs conservative match).
    provider = Task(
        project_id=project.id,
        title="capability provider",
        description="lands the missing runner capability",
        status=TaskStatus.COMPLETED,
        created_by="test-user",
        requires_approval=False,
        payloads=[{"type": "create"}],
        context={"provides_capability": "playwright-chromium", "pr_merged": True},
        status_log=[],
    )
    reaper_db.add(provider)
    await reaper_db.commit()

    need = {"kind": "capability", "ref": "playwright-chromium"}
    t = await _blocked_task(
        reaper_db,
        project,
        blocker_kind="runner_capability",
        blocked_reason="Waiting: runner capability playwright-chromium",
        blocked_at=now - timedelta(hours=8),
        depends_on=[],
        context={
            "blocker": {
                "source": "agent",
                "waiting_on": "capability",
                "needs": [need],
                "need": need,
            },
            "_blocked_escalated": True,
            "_blocked_escalated_recheck_count": 3,
        },
        recheck_after=now - timedelta(minutes=1),  # due
    )

    requeued = await sweep_unblock_blocked_tasks(reaper_db, now)

    assert requeued == 1, (
        "an already-escalated (parked) blocker with a satisfiable need must "
        "auto-resume once the need is met — never wait for a manual operator "
        "requeue"
    )
    await reaper_db.refresh(t)
    assert t.status == TaskStatus.QUEUED


@pytest.mark.asyncio
async def test_recovery_sweep_resets_hard_parked_non_transient_escalated_blocker(
    reaper_db, mocker
):
    """sweep_recover_stranded_escalated_blockers un-strands a NON-transient
    escalated blocker a prior deploy already hard-parked at the old +10y
    sentinel, resetting it to the bounded backoff cadence."""
    from app.reaper_sweeps import sweep_recover_stranded_escalated_blockers

    mocker.patch("app.broadcast.broadcast_to_project", new_callable=AsyncMock)
    project = await _make_project(reaper_db)
    now = _utcnow()

    t = await _blocked_task(
        reaper_db,
        project,
        blocker_kind="runner_capability",
        blocked_reason="Waiting: runner capability playwright_chromium_usable",
        blocked_at=now - timedelta(hours=8),
        depends_on=[],
        context={
            "blocker": {"source": "gate"},
            "_blocked_escalated": True,
            "_blocked_escalated_recheck_count": 40,
        },
        # Hard-parked at the old +10y sentinel by a prior deploy.
        recheck_after=now + timedelta(days=3650),
    )

    recovered = await sweep_recover_stranded_escalated_blockers(reaper_db, now)

    assert recovered == 1
    await reaper_db.refresh(t)
    assert t.status == TaskStatus.BLOCKED  # still parked, not requeued blind
    assert t.context.get("_blocked_escalated") is True
    # Backoff counter restarted so the recovered row re-earns its cadence
    # from the bottom instead of instantly re-parking near the cap.
    assert t.context.get("_blocked_escalated_recheck_count") == 0
    far_future = now + _TRANSIENT_BLOCKER_PARK_SENTINEL
    assert t.recheck_after is not None
    assert t.recheck_after < far_future


@pytest.mark.asyncio
async def test_wake_delivery_sweep_stall_signal_across_cycles(reaper_db, mocker):
    """A FIRED registration the sweep can't deliver, across consecutive no-progress
    cycles, trips the cross-cycle delivery-stall signal; a delivering cycle resets it.

    Simulates a wedged-but-not-crashed delivery path by stubbing ``deliver_wake``
    to a no-op that leaves the registration FIRED — so the sweep makes no progress
    while a FIRED backlog persists, exactly the regression the per-cycle reporting
    is meant to surface.
    """
    import app.task_lifecycle as _tl
    from app.models import TaskWakeRegistration, WakeAction, WakeDeliveryState
    from app.reaper_sweeps import (
        _WAKE_DELIVERY_STALL_CYCLE_THRESHOLD,
        WakeSweepResult,
        reset_wake_delivery_stall_state,
        sweep_deliver_task_wakes,
    )

    reset_wake_delivery_stall_state()
    project = await _make_project(reaper_db)

    now = _utcnow()
    task = Task(
        project_id=project.id,
        title="park-stall",
        status=TaskStatus.PARKED,
        parked_at=now - timedelta(minutes=1),
        payloads=[],
    )
    reaper_db.add(task)
    await reaper_db.commit()
    await reaper_db.refresh(task)
    reg = TaskWakeRegistration(
        task_id=task.id,
        project_id=project.id,
        wake_source_kind="compute_job_terminal",
        wake_action=WakeAction.RESUME,
        delivery_state=WakeDeliveryState.FIRED,
        fired_at=now - timedelta(minutes=1),
    )
    reaper_db.add(reg)
    await reaper_db.commit()

    real_deliver = _tl.deliver_wake

    async def _wedged_deliver(session, registration, *, now=None):
        # Leaves the registration FIRED — no progress, no state change.
        return False

    mocker.patch.object(_tl, "deliver_wake", new=_wedged_deliver)

    # No-progress cycles with a persistent FIRED backlog climb the stall counter.
    for cycle in range(1, _WAKE_DELIVERY_STALL_CYCLE_THRESHOLD + 1):
        result = await sweep_deliver_task_wakes(
            reaper_db, _utcnow(), batch_size=1
        )
        assert isinstance(result, WakeSweepResult)
        assert result.delivered == 0
        assert result.fired_undelivered == 1
        assert result.stall_cycles == cycle
        # Trips only once it sustains across the threshold.
        assert result.delivery_stalled is (
            cycle >= _WAKE_DELIVERY_STALL_CYCLE_THRESHOLD
        )

    # A genuinely-delivering cycle drains the backlog and resets the signal.
    mocker.patch.object(_tl, "deliver_wake", new=real_deliver)
    recovered = await sweep_deliver_task_wakes(
        reaper_db, _utcnow(), batch_size=1
    )
    assert recovered.delivered == 1
    assert recovered.fired_undelivered == 0
    assert recovered.stall_cycles == 0
    assert recovered.delivery_stalled is False

    reset_wake_delivery_stall_state()


@pytest.mark.asyncio
async def test_wake_delivery_stall_raises_operator_alert(reaper_db, mocker):
    """A stalled wake delivery raises a deduped operator Alert; recovery clears it.

    Drives a wedged delivery path to trip the cross-cycle stall signal, then
    exercises sweep_wake_delivery_stall_alerts to assert:
      1. The first stalled call creates exactly one WARNING Alert row per project.
      2. Repeat stalled calls are deduped (no second Alert row).
      3. A recovering call (delivery_stalled=False) dismisses the open Alert.
    """
    import app.task_lifecycle as _tl
    from app.models import TaskWakeRegistration, WakeAction, WakeDeliveryState
    from app.reaper_sweeps import (
        _WAKE_DELIVERY_STALL_CYCLE_THRESHOLD,
        get_wake_delivery_stall_cycles,
        reset_wake_delivery_stall_state,
        sweep_deliver_task_wakes,
        sweep_wake_delivery_stall_alerts,
    )

    reset_wake_delivery_stall_state()
    project = await _make_project(reaper_db)

    now = _utcnow()
    task = Task(
        project_id=project.id,
        title="park-alert-stall",
        status=TaskStatus.PARKED,
        parked_at=now - timedelta(minutes=1),
        payloads=[],
    )
    reaper_db.add(task)
    await reaper_db.commit()
    await reaper_db.refresh(task)

    reg = TaskWakeRegistration(
        task_id=task.id,
        project_id=project.id,
        wake_source_kind="compute_job_terminal",
        wake_action=WakeAction.RESUME,
        delivery_state=WakeDeliveryState.FIRED,
        fired_at=now - timedelta(minutes=1),
    )
    reaper_db.add(reg)
    await reaper_db.commit()

    real_deliver = _tl.deliver_wake

    async def _wedged_deliver(session, registration, *, now=None):
        return False  # no progress — leaves the registration FIRED

    mocker.patch.object(_tl, "deliver_wake", new=_wedged_deliver)

    # Drive the stall counter up to threshold so delivery_stalled trips.
    for _ in range(_WAKE_DELIVERY_STALL_CYCLE_THRESHOLD):
        await sweep_deliver_task_wakes(reaper_db, _utcnow(), batch_size=1)

    assert get_wake_delivery_stall_cycles() >= _WAKE_DELIVERY_STALL_CYCLE_THRESHOLD
    stall_cycles = get_wake_delivery_stall_cycles()

    # First stalled alert sweep → one WARNING Alert created per project.
    created = await sweep_wake_delivery_stall_alerts(
        reaper_db, delivery_stalled=True, stall_cycles=stall_cycles
    )
    assert created == 1

    alerts = (
        (
            await reaper_db.execute(
                select(Alert).where(
                    Alert.project_id == project.id,
                    Alert.dismissed_at.is_(None),
                )
            )
        )
        .scalars()
        .all()
    )
    assert len(alerts) == 1
    assert alerts[0].severity == AlertSeverity.WARNING
    assert alerts[0].event_type == "deployment.wake_delivery_stalled"
    # Verify the event_type passes operator_visible_alert_filter so the alert
    # is visible in the operator badge/preview (not hidden as STRATEGIST_INTERNAL).
    from app.message_audience import get_operator_event_types
    assert alerts[0].event_type in get_operator_event_types(), (
        f"event_type {alerts[0].event_type!r} not in operator_visible_alert_filter "
        f"allowlist — stall alert would be hidden from operator surfaces"
    )

    # Second call with delivery still stalled → deduped, no new Alert row.
    created2 = await sweep_wake_delivery_stall_alerts(
        reaper_db, delivery_stalled=True, stall_cycles=stall_cycles
    )
    assert created2 == 0

    still_one = (
        (
            await reaper_db.execute(
                select(Alert).where(
                    Alert.project_id == project.id,
                    Alert.dismissed_at.is_(None),
                )
            )
        )
        .scalars()
        .all()
    )
    assert len(still_one) == 1

    # Recovery: allow delivery to proceed and verify the alert is cleared.
    mocker.patch.object(_tl, "deliver_wake", new=real_deliver)
    recovered = await sweep_deliver_task_wakes(
        reaper_db, _utcnow(), batch_size=1
    )
    assert recovered.delivered == 1
    assert recovered.delivery_stalled is False

    await sweep_wake_delivery_stall_alerts(
        reaper_db, delivery_stalled=False, stall_cycles=0
    )

    dismissed = (
        (
            await reaper_db.execute(
                select(Alert).where(
                    Alert.project_id == project.id,
                    Alert.dismissed_at.is_not(None),
                )
            )
        )
        .scalars()
        .all()
    )
    assert len(dismissed) == 1
    assert dismissed[0].dismissed_at is not None

    reset_wake_delivery_stall_state()


# ── Infra-recovery guard: reaper PR-close loop ────────────────────────────────
# These tests verify that the reaper skips closing PRs owned by infra-reaped
# fix tasks when the review-loop still has iteration budget, and correctly
# closes PRs for genuine merit failures or budget-exhausted infra failures.


def _make_review_loop_fix_task(
    project,
    *,
    pr_number: int,
    iteration: int,
    max_iterations: int,
    attempt_count: int = 3,
    failure_class_history: list | None = None,
) -> Task:
    """Return an IN_PROGRESS fix task with a stale heartbeat and review_loop
    context, but NO PULL_REQUEST artifact (so the reaper doesn't redirect it
    to the EVALUATING open-PR handoff path)."""
    ctx = {
        "review_loop": {
            "pr_number": pr_number,
            "iteration": iteration,
            "max_iterations": max_iterations,
            "branch": f"blueprint/fix-{pr_number}",
            "base_branch": "main",
        },
    }
    if failure_class_history is not None:
        ctx["_failure_class_history"] = failure_class_history
    return Task(
        project_id=project.id,
        payloads=[{"type": "fix"}],
        vm_instance_id=f"vm-fix-{pr_number}",
        status=TaskStatus.IN_PROGRESS,
        attempt_count=attempt_count,
        last_heartbeat=_utcnow() - timedelta(minutes=10),
        context=ctx,
    )


@pytest.mark.asyncio
async def test_reaper_preserves_pr_for_infra_reaped_fix_with_budget_remaining(
    reaper_db, stop_after_one_cycle, reaper_side_effects, mocker
):
    """(a) Infra-reaped fix task + open PR + iterations remaining → PR NOT closed.

    The real incident: fix task 33cdac3e was reaped 3x on AGENT_TIMEOUT (the
    agent never ran), yet the reaper closed the PR, discarding valid work.
    The infra-recovery guard must skip close_pull_request and let the
    eval-sweep reaped-owner reconcile re-drive the chain.
    """
    import app.failure_classification as fc

    mocker.patch.object(
        fc,
        "classify_failure_with_confidence",
        return_value=fc.FailureClassification(
            failure_class="AGENT_TIMEOUT", confidence=0.9
        ),
    )
    mock_close = mocker.patch(
        "app.github.close_pull_request", new_callable=AsyncMock, return_value=True
    )
    mocker.patch(
        "app.routers.agent.get_github_token_for_task",
        new_callable=AsyncMock,
        return_value=("tok", "owner", "repo"),
    )
    mocker.patch("app.github.create_pr_comment", new_callable=AsyncMock)

    project = await _make_project(reaper_db)
    t = _make_review_loop_fix_task(
        project,
        pr_number=10152,
        iteration=2,
        max_iterations=12,
        attempt_count=3,
    )
    reaper_db.add(t)
    await reaper_db.commit()

    await _run_reaper_once()
    await reaper_db.refresh(t)

    assert t.status == TaskStatus.FAILED, "task must reach terminal-fail path"
    assert (t.context or {}).get("failure_category") == "AGENT_TIMEOUT"
    mock_close.assert_not_called()


@pytest.mark.asyncio
async def test_reaper_closes_pr_for_merit_failure(
    reaper_db, stop_after_one_cycle, reaper_side_effects, mocker
):
    """(b) Genuine merit failure (CODE_ERROR) → PR IS closed as before.

    The infra-recovery guard must not interfere with the existing close path
    for code-quality failures.
    """
    import app.failure_classification as fc

    mocker.patch.object(
        fc,
        "classify_failure_with_confidence",
        return_value=fc.FailureClassification(
            failure_class="CODE_ERROR", confidence=0.9
        ),
    )
    mock_close = mocker.patch(
        "app.github.close_pull_request", new_callable=AsyncMock, return_value=True
    )
    mocker.patch(
        "app.routers.agent.get_github_token_for_task",
        new_callable=AsyncMock,
        return_value=("tok", "owner", "repo"),
    )
    mocker.patch("app.github.create_pr_comment", new_callable=AsyncMock)

    project = await _make_project(reaper_db)
    t = _make_review_loop_fix_task(
        project,
        pr_number=20001,
        iteration=3,
        max_iterations=12,
        attempt_count=3,
    )
    reaper_db.add(t)
    await reaper_db.commit()

    await _run_reaper_once()
    await reaper_db.refresh(t)

    assert t.status == TaskStatus.FAILED
    assert (t.context or {}).get("failure_category") == "CODE_ERROR"
    mock_close.assert_called_once()


@pytest.mark.asyncio
async def test_reaper_closes_pr_when_iteration_cap_exhausted(
    reaper_db, stop_after_one_cycle, reaper_side_effects, mocker
):
    """(b) Even an AGENT_TIMEOUT failure closes the PR when the iteration cap
    is exhausted (iteration == max_iterations). No unbounded re-drive loop."""
    import app.failure_classification as fc

    mocker.patch.object(
        fc,
        "classify_failure_with_confidence",
        return_value=fc.FailureClassification(
            failure_class="AGENT_TIMEOUT", confidence=0.9
        ),
    )
    mock_close = mocker.patch(
        "app.github.close_pull_request", new_callable=AsyncMock, return_value=True
    )
    mocker.patch(
        "app.routers.agent.get_github_token_for_task",
        new_callable=AsyncMock,
        return_value=("tok", "owner", "repo"),
    )
    mocker.patch("app.github.create_pr_comment", new_callable=AsyncMock)

    project = await _make_project(reaper_db)
    t = _make_review_loop_fix_task(
        project,
        pr_number=20002,
        iteration=12,
        max_iterations=12,
        attempt_count=3,
    )
    reaper_db.add(t)
    await reaper_db.commit()

    await _run_reaper_once()
    await reaper_db.refresh(t)

    assert t.status == TaskStatus.FAILED
    mock_close.assert_called_once()


@pytest.mark.asyncio
async def test_reaper_closes_pr_when_infra_retry_budget_exhausted(
    reaper_db, stop_after_one_cycle, reaper_side_effects, mocker
):
    """(c) AGENT_TIMEOUT with 3 prior infra entries in _failure_class_history
    → budget exhausted → PR IS still closed (no infinite re-drive)."""
    import app.failure_classification as fc
    from datetime import timedelta

    mocker.patch.object(
        fc,
        "classify_failure_with_confidence",
        return_value=fc.FailureClassification(
            failure_class="AGENT_TIMEOUT", confidence=0.9
        ),
    )
    mock_close = mocker.patch(
        "app.github.close_pull_request", new_callable=AsyncMock, return_value=True
    )
    mocker.patch(
        "app.routers.agent.get_github_token_for_task",
        new_callable=AsyncMock,
        return_value=("tok", "owner", "repo"),
    )
    mocker.patch("app.github.create_pr_comment", new_callable=AsyncMock)

    recent = (_utcnow() - timedelta(hours=1)).isoformat()
    project = await _make_project(reaper_db)
    t = _make_review_loop_fix_task(
        project,
        pr_number=20003,
        iteration=2,
        max_iterations=12,
        attempt_count=3,
        failure_class_history=[
            {"class": "AGENT_TIMEOUT", "at": recent},
            {"class": "AGENT_TIMEOUT", "at": recent},
            {"class": "AGENT_TIMEOUT", "at": recent},
        ],
    )
    reaper_db.add(t)
    await reaper_db.commit()

    await _run_reaper_once()
    await reaper_db.refresh(t)

    assert t.status == TaskStatus.FAILED
    mock_close.assert_called_once()


# ── Terminal pipeline-death re-arm (incident 2026-08-19, review 20712c3f) ───
# A review task reaped to terminal FAILED (AGENT_TIMEOUT) never reports agent
# status and is skipped by the resurrection sweep, so without a producer the
# EVALUATING original parked with review_loop.ci_waiting falsy strands its PR
# un-reviewed indefinitely. The reaper's terminal branch must stage
# _pr_state_check_pending on the original — bounded, with an operator-facing
# WHY on exhaustion.


def _make_dead_review_chain(project, pr_number: int, *, orig_ctx_extra=None):
    """Return (original EVALUATING create task, doomed review task).

    The review task has a stale heartbeat, empty logs, and attempt_count high
    enough to exhaust any AGENT_TIMEOUT budget, so the reaper takes the
    terminal-FAIL branch (not the retry branch). It has NO PULL_REQUEST
    artifact, so the open-PR handoff never applies (reviews don't author PRs).
    """
    orig_ctx = {
        "review_loop": {
            "pr_number": pr_number,
            "iteration": 2,
            "max_iterations": 12,
            "branch": f"blueprint/task-{pr_number}",
            "base_branch": "main",
            # ci_waiting falsy → outside the CI monitor poll set and the
            # review-wait backstop: exactly the stranded park.
            "ci_waiting": False,
        },
    }
    if orig_ctx_extra:
        orig_ctx.update(orig_ctx_extra)
    original = Task(
        project_id=project.id,
        payloads=[{"type": "create"}],
        status=TaskStatus.EVALUATING,
        context=orig_ctx,
    )
    review = Task(
        project_id=project.id,
        payloads=[
            {
                "type": "review",
                "pr_number": pr_number,
                "prompt": "Review the PR",
            }
        ],
        vm_instance_id=f"vm-review-{pr_number}",
        status=TaskStatus.IN_PROGRESS,
        attempt_count=8,  # ≥ any AGENT_TIMEOUT budget → terminal, not retry
        last_heartbeat=_utcnow() - timedelta(minutes=10),
        context={},
    )
    return original, review


def _wire_review_to_original(review: Task, original: Task) -> None:
    review.payloads = [dict(review.payloads[0], original_task_id=str(original.id))]


@pytest.mark.asyncio
async def test_reaper_terminal_agent_timeout_review_rearms_original_pr_check(
    reaper_db, stop_after_one_cycle, reaper_side_effects, mocker
):
    """A review reaped to terminal FAILED (AGENT_TIMEOUT) must re-arm the
    canonical _pr_state_check_pending consumer on its EVALUATING original so
    the PR gets reviewed again — instead of stranding it indefinitely."""
    import app.failure_classification as fc

    mocker.patch.object(
        fc,
        "classify_failure_with_confidence",
        return_value=fc.FailureClassification(
            failure_class="AGENT_TIMEOUT", confidence=0.9
        ),
    )
    mocker.patch(
        "app.routers.agent.get_github_token_for_task",
        new_callable=AsyncMock,
        return_value=("tok", "owner", "repo"),
    )

    project = await _make_project(reaper_db)
    original, review = _make_dead_review_chain(project, pr_number=30001)
    reaper_db.add(original)
    await reaper_db.commit()
    await reaper_db.refresh(original)
    _wire_review_to_original(review, original)
    reaper_db.add(review)
    await reaper_db.commit()

    await _run_reaper_once()
    await reaper_db.refresh(review)
    await reaper_db.refresh(original)

    assert review.status == TaskStatus.FAILED, "review must reach terminal-fail"
    assert (review.context or {}).get("failure_category") == "AGENT_TIMEOUT"
    octx = original.context or {}
    assert octx.get("_pr_state_check_pending") is True, (
        "terminal review death must stage the canonical PR re-drive marker "
        "on the EVALUATING original"
    )
    assert octx.get("_pipeline_terminal_rearm_count") == 1
    assert original.status == TaskStatus.EVALUATING, (
        "the original stays parked; the eval-sweep consumer owns the re-drive"
    )
    msg = original.latest_status_message or ""
    assert "#30001" in msg and "re-armed" in msg.lower(), (
        f"re-arm must surface WHY on the original, got: {msg!r}"
    )


@pytest.mark.asyncio
async def test_reaper_terminal_rearm_skips_non_evaluating_original(
    reaper_db, stop_after_one_cycle, reaper_side_effects, mocker
):
    """A COMPLETED original (rollup child — re-driven by rollup convergence)
    must NOT be touched by the terminal re-arm, and the reap must not crash."""
    import app.failure_classification as fc

    mocker.patch.object(
        fc,
        "classify_failure_with_confidence",
        return_value=fc.FailureClassification(
            failure_class="AGENT_TIMEOUT", confidence=0.9
        ),
    )

    project = await _make_project(reaper_db)
    original, review = _make_dead_review_chain(project, pr_number=30002)
    original.status = TaskStatus.COMPLETED
    reaper_db.add(original)
    await reaper_db.commit()
    await reaper_db.refresh(original)
    _wire_review_to_original(review, original)
    reaper_db.add(review)
    await reaper_db.commit()

    await _run_reaper_once()
    await reaper_db.refresh(review)
    await reaper_db.refresh(original)

    assert review.status == TaskStatus.FAILED
    octx = original.context or {}
    assert "_pr_state_check_pending" not in octx
    assert "_pipeline_terminal_rearm_count" not in octx
    assert original.status == TaskStatus.COMPLETED


@pytest.mark.asyncio
async def test_reaper_terminal_rearm_cap_exhausted_emits_operator_why(
    reaper_db, stop_after_one_cycle, reaper_side_effects, mocker
):
    """With the re-arm budget already spent, another terminal review death must
    NOT re-arm again (no infinite respawn loop) and must surface an explicit
    operator-facing reason + failure alert on the original instead of silently
    parking it."""
    import app.failure_classification as fc
    from app.reaper_pipeline_rearm import (
        PIPELINE_TERMINAL_REARM_CAP,
        PIPELINE_TERMINAL_REARM_COUNT_KEY,
        PIPELINE_TERMINAL_REARM_EXHAUSTED_KEY,
    )

    # Literal pin: the automatic re-review budget is a bounded small constant.
    assert PIPELINE_TERMINAL_REARM_CAP == 2

    mocker.patch.object(
        fc,
        "classify_failure_with_confidence",
        return_value=fc.FailureClassification(
            failure_class="AGENT_TIMEOUT", confidence=0.9
        ),
    )
    # Re-patch over the fixture's alert mock so this test can inspect calls.
    alert_mock = mocker.patch(
        "app.alert_dispatch._maybe_create_failure_alert", new_callable=AsyncMock
    )

    project = await _make_project(reaper_db)
    original, review = _make_dead_review_chain(
        project,
        pr_number=30003,
        orig_ctx_extra={
            PIPELINE_TERMINAL_REARM_COUNT_KEY: PIPELINE_TERMINAL_REARM_CAP
        },
    )
    reaper_db.add(original)
    await reaper_db.commit()
    await reaper_db.refresh(original)
    _wire_review_to_original(review, original)
    reaper_db.add(review)
    await reaper_db.commit()

    await _run_reaper_once()
    await reaper_db.refresh(review)
    await reaper_db.refresh(original)

    assert review.status == TaskStatus.FAILED
    octx = original.context or {}
    assert "_pr_state_check_pending" not in octx, (
        "exhausted budget must stop the re-arm — no unbounded respawn loop"
    )
    assert octx.get(PIPELINE_TERMINAL_REARM_COUNT_KEY) == (
        PIPELINE_TERMINAL_REARM_CAP
    ), "exhaustion must not keep incrementing the counter"
    exhausted = octx.get(PIPELINE_TERMINAL_REARM_EXHAUSTED_KEY)
    assert isinstance(exhausted, dict), (
        "exhaustion must stamp operator-facing provenance on the original"
    )
    assert exhausted.get("pr_number") == 30003
    assert exhausted.get("failure_class") == "AGENT_TIMEOUT"
    assert exhausted.get("dead_task_id") == str(review.id)
    msg = original.latest_status_message or ""
    assert "exhausted" in msg.lower() and "operator" in msg.lower(), (
        f"exhaustion must emit WHY on the original, got: {msg!r}"
    )
    # A failure alert must be raised FOR THE ORIGINAL (the dead review gets
    # its own alert from the ordinary terminal path).
    original_alert_calls = [
        c
        for c in alert_mock.await_args_list
        if c.args and getattr(c.args[0], "id", None) == original.id
    ]
    assert original_alert_calls, "exhaustion must raise an alert on the original"
    alert_snippet = original_alert_calls[0].args[2] if len(
        original_alert_calls[0].args
    ) > 2 else original_alert_calls[0].kwargs.get("error_snippet", "")
    assert "exhausted" in (alert_snippet or "").lower()


# ── Fleet-scan cadence gates and limits (Part A) ────────────────────────────


@pytest.mark.asyncio
async def test_sweep_converge_duplicate_foundations_cadence_gate(mocker):
    """Second call within _FLEET_SCAN_INTERVAL_SECONDS is a no-op (returns 0)."""
    _rs._last_converge_duplicate_scan = None  # ensure first call is allowed
    mocker.patch(
        "app.digest_metrics.compute_foundation_convergence_actions",
        return_value=[],
    )
    mock_session = AsyncMock()
    mock_session.execute = AsyncMock(
        return_value=AsyncMock(scalars=lambda: AsyncMock(all=lambda: []))
    )
    first = await _rs.sweep_converge_duplicate_foundations(mock_session)
    second = await _rs.sweep_converge_duplicate_foundations(mock_session)
    assert first == 0, "first call returns 0 when no convergence actions"
    assert second == 0, "cadence gate must suppress second call within 15 min"
    # Confirm it was actually gated: execute was only called once (first pass).
    assert mock_session.execute.await_count == 1, (
        "second call must not hit the DB at all"
    )
    _rs._last_converge_duplicate_scan = None  # restore for test isolation


@pytest.mark.asyncio
async def test_sweep_converge_duplicate_foundations_scan_carries_limit(mocker):
    """The active-task scan must include LIMIT 500 so it cannot read the full fleet."""
    _rs._last_converge_duplicate_scan = None
    mocker.patch(
        "app.digest_metrics.compute_foundation_convergence_actions",
        return_value=[],
    )
    captured_queries: list = []

    class _CapturingResult:
        def scalars(self):
            return self

        def all(self):
            return []

    async def _execute(stmt, *a, **kw):
        captured_queries.append(stmt)
        return _CapturingResult()

    mock_session = AsyncMock()
    mock_session.execute = _execute
    await _rs.sweep_converge_duplicate_foundations(mock_session)

    assert captured_queries, "expected at least one SELECT"
    import sqlalchemy.dialects.sqlite

    dialect = sqlalchemy.dialects.sqlite.dialect()
    compiled = str(captured_queries[0].compile(dialect=dialect)).upper()
    assert "LIMIT" in compiled, (
        "sweep_converge_duplicate_foundations must apply LIMIT to active-task scan"
    )
    _rs._last_converge_duplicate_scan = None


@pytest.mark.asyncio
async def test_sweep_merge_gate_degradation_cadence_gate(mocker):
    """Second call within _FLEET_SCAN_INTERVAL_SECONDS is a no-op (returns 0)."""
    _rs._last_merge_gate_degradation_scan = None
    # Return a below-minimum-sample result so the function returns 0 cleanly on the
    # first call without needing to mock the alert-dispatch path.
    fleet_health_mock = mocker.patch(
        "app.digest_metrics.compute_merge_gate_fleet_health",
        new_callable=AsyncMock,
        return_value={
            "verified_count": 0,
            "fell_open_count": 0,
            "verification_rate": None,
            "degraded_project_count": 0,
            "project_count": 0,
            "most_common_reason": None,
        },
    )
    mock_session = AsyncMock()
    await _rs.sweep_merge_gate_verification_degradation(mock_session)
    second = await _rs.sweep_merge_gate_verification_degradation(mock_session)
    assert second == 0, "cadence gate must suppress second call within 15 min"
    fleet_health_mock.assert_awaited_once()  # only called on first pass; gate suppresses second
    _rs._last_merge_gate_degradation_scan = None


# ── run_infra_recovery_and_hold_maintenance LIMIT 200 ───────────────────────


@pytest.mark.asyncio
async def test_infra_recovery_query_carries_limit(reaper_db, mocker):
    """_infra_query must cap at 200 rows so large FAILED backlogs don't stall."""
    from app.reaper_infra_recovery import run_infra_recovery_and_hold_maintenance

    captured_queries: list = []
    _orig_execute = reaper_db.execute

    async def _spy_execute(stmt, *a, **kw):
        captured_queries.append(stmt)
        return await _orig_execute(stmt, *a, **kw)

    mocker.patch.object(reaper_db, "execute", side_effect=_spy_execute)
    mocker.patch(
        "app.vm_lifecycle._ensure_vm_deleted", new_callable=AsyncMock, return_value=True
    )

    now = _utcnow()
    await run_infra_recovery_and_hold_maintenance(reaper_db, now)

    import sqlalchemy.dialects.sqlite

    dialect = sqlalchemy.dialects.sqlite.dialect()
    failed_scans = [
        str(q.compile(dialect=dialect)).upper()
        for q in captured_queries
        if hasattr(q, "compile")
        and "TASK" in str(q.compile(dialect=dialect)).upper()
        and "FAILED" in str(q.compile(dialect=dialect)).upper()
    ]
    assert failed_scans, "expected at least one FAILED task scan"
    assert any("LIMIT" in s for s in failed_scans), (
        "run_infra_recovery_and_hold_maintenance must apply LIMIT 200 to the FAILED task scan"
    )


# ── sweep_unblock_blocked_tasks: per-row lock (no batch lock during GitHub) ──


@pytest.mark.asyncio
async def test_sweep_unblock_per_row_commit_releases_lock_between_tasks(
    reaper_db, mocker
):
    """Each task must be committed (lock released) before the next task is processed.

    The lock-across-network anti-pattern held a batch FOR UPDATE across 200 rows
    for the entire GitHub-call window.  The fix: lock-free pre-scan collects IDs,
    then per-row FOR UPDATE is acquired and released (committed) per task.

    Verify the per-row commit property: for 2 due BLOCKED tasks, the session must
    commit once per task (>= 2 commits), proving each row's lock is released before
    the next iteration begins rather than being held for the whole batch.
    """
    mocker.patch("app.broadcast.broadcast_to_project", new_callable=AsyncMock)
    project = await _make_project(reaper_db)

    # Two BLOCKED tasks due now, escalated transient blockers — both flow through
    # the full iteration body and each exit via _commit_task_row().
    common = dict(
        project_id=project.id,
        title="unblock-locktest",
        description="x",
        status=TaskStatus.BLOCKED,
        created_by="test-user",
        requires_approval=False,
        payloads=[{"type": "fix"}],
        status_log=[],
        blocker_kind="pending_rollup",
        blocked_reason="Waiting: pending rollup",
        blocked_at=_utcnow(),
        recheck_after=_utcnow() - timedelta(minutes=1),
        context={"_blocked_escalated": True},
        depends_on=[],
    )
    t1 = Task(**common)
    t2 = Task(**{**common, "title": "unblock-locktest-2"})
    reaper_db.add(t1)
    reaper_db.add(t2)
    await reaper_db.commit()

    commit_count = [0]
    _orig_commit = reaper_db.commit

    async def _counting_commit():
        commit_count[0] += 1
        return await _orig_commit()

    mocker.patch.object(reaper_db, "commit", side_effect=_counting_commit)

    await sweep_unblock_blocked_tasks(reaper_db, _utcnow())

    # Each of the 2 tasks must produce its own commit so the FOR UPDATE lock
    # is released before the next task's processing begins.
    assert commit_count[0] >= 2, (
        f"expected at least 2 per-row commits for 2 tasks, got {commit_count[0]}; "
        "the per-row lock is not being released between tasks"
    )


# ── Message supersession mode defaults to enforce ────────────────────────────


@pytest.mark.asyncio
async def test_message_supersession_defaults_to_enforce_mode(reaper_db):
    """The message supersession mode defaults to 'enforce', so stale messages
    from a prior version are auto-resolved even when no runtime config override
    is set. This is the acceptance test for the observe->enforce graduation."""
    from app.message_supersession import supersede_stale_messages_on_version_close
    from app.models import MessageType, ProjectMessage
    from app.shared_state import runtime_config as _rc

    NOW = _utcnow()
    V1_OPENED = NOW - timedelta(days=180)
    V2_OPENED = NOW - timedelta(days=150)
    # Message in v1's window (created before V2_OPENED) and older than 14d.
    MSG_TIME = NOW - timedelta(days=165)

    saved = _rc.pop("message_supersession_mode", None)
    try:
        project = await _make_project(reaper_db)

        reaper_db.add(ReleaseVersion(
            project_id=project.id, version_number=1,
            status="open", opened_at=V1_OPENED,
        ))
        reaper_db.add(ReleaseVersion(
            project_id=project.id, version_number=2,
            status="open", opened_at=V2_OPENED,
        ))
        msg = ProjectMessage(
            project_id=project.id,
            message_type=MessageType.NOTE,
            from_type="strategic",
            content="stale status update",
            status="open",
            created_at=MSG_TIME,
        )
        reaper_db.add(msg)
        await reaper_db.commit()
        await reaper_db.refresh(msg)

        summary = await supersede_stale_messages_on_version_close(
            reaper_db,
            project_id=project.id,
            closed_version_number=2,
            closed_version_opened_at=V2_OPENED,
            terminal_status="promoted",
            now=NOW,
        )
        await reaper_db.commit()

        assert summary["mode"] == "enforce", (
            "supersession mode should default to 'enforce'"
        )
        assert summary["acted"] is True, (
            "supersession should act (resolve messages) by default"
        )
        assert summary["superseded"] == 1, (
            "the stale v1 message should be superseded"
        )
        await reaper_db.refresh(msg)
        assert msg.status == "resolved", (
            "message should be resolved when mode defaults to enforce"
        )
    finally:
        if saved is None:
            _rc.pop("message_supersession_mode", None)
        else:
            _rc["message_supersession_mode"] = saved


# ── Acceptance: extracted sweeps re-exported from reaper_sweeps ──────────────


def test_extracted_sweeps_reexported_from_reaper_sweeps():
    """Symbols extracted to reaper_bug_message_sweeps must remain importable
    from app.reaper_sweeps so no existing caller needs to change.

    This is the acceptance gate for the reaper_sweeps extraction refactor:
    it verifies both the symbol contract (importable) and the identity
    contract (the re-exports resolve to the same objects defined in the
    sub-module, not stale copies).
    """
    import app.reaper_bug_message_sweeps as _sub
    import app.reaper_sweeps as _rs

    exported_names = [
        "WakeSweepResult",
        "_WAKE_DELIVERY_STALL_CYCLE_THRESHOLD",
        "get_wake_delivery_stall_cycles",
        "reset_wake_delivery_stall_state",
        "sweep_converge_linked_bug_messages",
        "sweep_deliver_task_wakes",
        "sweep_resolve_open_owned_bug_messages",
        "sweep_resolve_stale_open_bug_messages",
        "sweep_wake_delivery_stall_alerts",
    ]
    for name in exported_names:
        assert hasattr(_rs, name), (
            f"app.reaper_sweeps is missing re-export '{name}' — "
            "callers that import it directly from reaper_sweeps will break"
        )
        # The re-export must resolve to the same object as the sub-module def,
        # not a stale local copy left behind by a partial extraction.
        rs_obj = getattr(_rs, name)
        sub_obj = getattr(_sub, name)
        assert rs_obj is sub_obj, (
            f"app.reaper_sweeps.{name} is NOT the same object as "
            f"app.reaper_bug_message_sweeps.{name} — the re-export is a "
            "stale copy, not a live reference to the sub-module symbol"
        )


def test_blocked_unblock_helpers_reexport_identity():
    """Helpers extracted to blocked_task_unblock_sweeps must remain importable from
    app.reaper_sweeps and resolve to the SAME objects (identity check), so every
    existing import site keeps working without change.
    """
    import app.blocked_task_unblock_sweeps as _sub
    import app.reaper_sweeps as _rs

    helper_names = [
        "_apply_needs_disposition",
        "_blocked_precondition_state",
        "_cancel_blocker_as_dead",
        "_classify_blocker_needs",
        "_clear_blocked_fields",
        "_fetch_active_candidates",
        "_fetch_completed_candidates",
        "_fetch_dead_candidates",
        "_fetch_knowledge_substance_index",
        "_fetch_working_capability_names",
        "_next_recheck_delay",
        "_partition_completed_needs_base_safe",
        "_persist_residual_needs",
        "_wire_edges",
    ]
    for name in helper_names:
        assert hasattr(_rs, name), (
            f"app.reaper_sweeps is missing re-export '{name}' — "
            "callers that import it from reaper_sweeps will break"
        )
        assert hasattr(_sub, name), (
            f"app.blocked_task_unblock_sweeps is missing '{name}'"
        )
        rs_obj = getattr(_rs, name)
        sub_obj = getattr(_sub, name)
        assert rs_obj is sub_obj, (
            f"app.reaper_sweeps.{name} is NOT the same object as "
            f"app.blocked_task_unblock_sweeps.{name} — the re-export is a "
            "stale copy, not a live reference to the sub-module symbol"
        )


# ── _apply_rollup_merge_classification helper ────────────────────────────────


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "terminal_rollup_merge",
    [True, False],
    ids=["terminal", "non-terminal"],
)
async def test_apply_rollup_merge_classification_hotfix_lane_bool(
    terminal_rollup_merge, mocker
):
    """hotfix_lane=False → the CORROBORATING twin
    corroborate_and_apply_merged_landed_signal is awaited when record_merged
    (the sync helper would record the acceptance verdict without the backend's
    own corroboration marker); task.status, vm_instance_id, last_heartbeat, and
    message set correctly."""
    import app.services  # noqa: F401 — prime circular-import chain before importing helpers
    from app.recovery_evaluating_helpers import _apply_rollup_merge_classification
    from app.models import Task, TaskStatus

    task = Task(
        status=TaskStatus.EVALUATING,
        context={"some_key": "value"},
        vm_instance_id="vm-old",
        last_heartbeat=datetime(2026, 1, 1),
    )
    task.payloads = [{"type": "create", "hotfix": False}]

    mock_classify = mocker.patch(
        "app.branch_resolution.classify_rollup_merge_outcome",
        new_callable=AsyncMock,
        return_value=(True, terminal_rollup_merge, "main"),
    )
    mock_apply = mocker.patch(
        "app.landed_signal.corroborate_and_apply_merged_landed_signal",
        new_callable=AsyncMock,
        return_value=True,
    )
    mock_sync_apply = mocker.patch(
        "app.landed_signal.apply_merged_landed_signal",
        return_value=True,
    )
    mocker.patch(
        "app.landed_signal.corroborate_and_mark_landed_on_integration",
        new_callable=AsyncMock,
    )

    mock_proj = object()
    mock_session = AsyncMock()
    ctx_override = {"staged": True}

    record_merged, terminal, landed = await _apply_rollup_merge_classification(
        task,
        mock_proj,
        mock_session,
        pr_number=42,
        merged_base_branch="main",
        source="test",
        msg_terminal="PR merged out-of-band",
        msg_non_terminal="PR merged into {landed}; awaiting downstream landing",
        hotfix_lane=False,
        ctx_override=ctx_override,
        non_merge_reason="merged_pr_non_integration_branch",
        merge_commit_sha="c" * 40,
    )

    assert record_merged is True
    assert terminal is terminal_rollup_merge
    assert landed == "main"

    assert task.status == (
        TaskStatus.COMPLETED if terminal_rollup_merge else TaskStatus.EVALUATING
    )
    assert task.vm_instance_id is None
    assert task.last_heartbeat is None
    if terminal_rollup_merge:
        assert task.latest_status_message == "PR merged out-of-band"
    else:
        assert (
            task.latest_status_message
            == "PR merged into main; awaiting downstream landing"
        )

    mock_classify.assert_awaited_once_with(
        task,
        mock_proj,
        mock_session,
        pr_number=42,
        merged_base_branch="main",
        source="test",
    )
    mock_apply.assert_awaited_once_with(
        task,
        mock_proj,
        mock_session,
        base_branch="main",
        hotfix_lane=False,
        non_merge_reason="merged_pr_non_integration_branch",
        # The caller's merge sha must be forwarded so the acceptance-test
        # corroboration reads the immutable merge tree instead of the
        # 60s-TTL branch-tip cache (which can still serve the pre-merge tip).
        merge_commit_sha="c" * 40,
    )
    # The recovery path must NOT reach the sync helper directly — that is the
    # variant that lands without stamping the acceptance-test corroboration.
    mock_sync_apply.assert_not_called()
    assert task.context is ctx_override


@pytest.mark.asyncio
async def test_apply_rollup_merge_classification_no_signal_when_not_record_merged(
    mocker,
):
    """When record_merged=False, neither landing write (guarded twin nor
    mark-landed twin) is called."""
    import app.services  # noqa: F401 — prime circular-import chain before importing helpers
    from app.recovery_evaluating_helpers import _apply_rollup_merge_classification
    from app.models import Task, TaskStatus

    task = Task(status=TaskStatus.EVALUATING)
    task.payloads = [{"type": "create"}]

    mocker.patch(
        "app.branch_resolution.classify_rollup_merge_outcome",
        new_callable=AsyncMock,
        return_value=(False, True, "main"),
    )
    mock_apply = mocker.patch(
        "app.landed_signal.corroborate_and_apply_merged_landed_signal",
        new_callable=AsyncMock,
    )
    mock_mark = mocker.patch(
        "app.landed_signal.corroborate_and_mark_landed_on_integration",
        new_callable=AsyncMock,
    )

    record_merged, terminal, landed = await _apply_rollup_merge_classification(
        task,
        object(),
        AsyncMock(),
        pr_number=7,
        merged_base_branch="main",
        source="test",
        msg_terminal="done",
        msg_non_terminal="pending {landed}",
        hotfix_lane=False,
    )

    assert record_merged is False
    assert terminal is True
    mock_apply.assert_not_called()
    mock_mark.assert_not_called()


@pytest.mark.asyncio
async def test_apply_rollup_merge_classification_hotfix_lane_none_routes_mark_landed(
    mocker,
):
    """hotfix_lane=None → corroborate_and_mark_landed_on_integration awaited (with
    the session and project it needs to corroborate); the guarded twin skipped."""
    import app.services  # noqa: F401 — prime circular-import chain before importing helpers
    from app.recovery_evaluating_helpers import _apply_rollup_merge_classification
    from app.models import Task, TaskStatus

    task = Task(status=TaskStatus.EVALUATING, context={"rollup_pr_number": 5})
    task.payloads = [{"type": "create"}]

    mocker.patch(
        "app.branch_resolution.classify_rollup_merge_outcome",
        new_callable=AsyncMock,
        return_value=(True, True, "automain"),
    )
    mock_apply = mocker.patch(
        "app.landed_signal.corroborate_and_apply_merged_landed_signal",
        new_callable=AsyncMock,
    )
    mock_mark = mocker.patch(
        "app.landed_signal.corroborate_and_mark_landed_on_integration",
        new_callable=AsyncMock,
    )
    mock_sync_mark = mocker.patch("app.landed_signal.mark_landed_on_integration")
    _proj = object()
    _session = AsyncMock()

    record_merged, terminal, landed = await _apply_rollup_merge_classification(
        task,
        _proj,
        _session,
        pr_number=5,
        merged_base_branch="automain",
        source="recovery_sweeps.lost_marker",
        msg_terminal="✓ rollup PR #5 already merged to {landed}; converged",
        msg_non_terminal="Lost-marker: merged into {landed}; awaiting",
        hotfix_lane=None,
        merge_commit_sha="d" * 40,
    )

    assert record_merged is True
    assert terminal is True
    assert landed == "automain"
    assert task.status == TaskStatus.COMPLETED
    assert task.vm_instance_id is None
    assert task.last_heartbeat is None
    assert (
        task.latest_status_message
        == "✓ rollup PR #5 already merged to automain; converged"
    )
    mock_apply.assert_not_called()
    mock_mark.assert_awaited_once_with(
        task,
        _session,
        _proj,
        branch="automain",
        # Pinned to the immutable merge commit, not the cached branch tip.
        merge_commit_sha="d" * 40,
    )
    # The project is required for corroboration (it resolves the repo's GitHub
    # credentials), so the twin must receive it rather than the old bare
    # ``mark_landed_on_integration(task)``.
    mock_sync_mark.assert_not_called()


def test_apply_rollup_merge_classification_reexported_from_recovery_evaluating():
    """_apply_rollup_merge_classification must be importable from recovery_evaluating
    so existing patch targets continue to work."""
    import app.services  # noqa: F401 — prime circular-import chain
    import app.recovery_evaluating as _re

    assert hasattr(_re, "_apply_rollup_merge_classification"), (
        "app.recovery_evaluating must re-export _apply_rollup_merge_classification"
    )


def test_recover_orphaned_tasks_reexported_from_recovery_post_commit():
    """_recover_orphaned_tasks must be importable from recovery_post_commit and
    recovery_post_commit_orphans, and both must resolve to the same callable."""
    import app.services  # noqa: F401 — prime circular-import chain
    import app.recovery_post_commit as _rpc
    import app.recovery_post_commit_orphans as _rpo

    assert callable(_rpc._recover_orphaned_tasks), (
        "app.recovery_post_commit must re-export _recover_orphaned_tasks"
    )
    assert _rpc._recover_orphaned_tasks is _rpo._recover_orphaned_tasks, (
        "re-export must be the same object as the canonical definition"
    )
    assert callable(_rpc._spawn_review_for_recovered_task), (
        "app.recovery_post_commit must re-export _spawn_review_for_recovered_task"
    )
    assert (
        _rpc._spawn_review_for_recovered_task is _rpo._spawn_review_for_recovered_task
    ), "re-export must be the same object as the canonical definition"


@pytest.mark.asyncio
async def test_recover_orphaned_tasks_skips_non_write_type(reaper_db, mocker):
    """_recover_orphaned_tasks must skip candidates whose task type is not in
    WRITE_TASK_TYPES — no GitHub token fetch should occur for research tasks."""
    from app.models import Task, TaskStatus, Project
    from app.recovery_post_commit_orphans import _recover_orphaned_tasks

    project = Project(
        name="orphan-test-proj",
        user_id="user1",
        github_repo="owner/repo",
        github_installation_id="inst-1",
    )
    reaper_db.add(project)
    await reaper_db.commit()
    await reaper_db.refresh(project)

    now = _utcnow()
    task = Task(
        project_id=project.id,
        status=TaskStatus.COMPLETED,
        created_by="ORCHESTRATOR",
        description="research task",
        payloads=[{"type": "research", "prompt": "investigate something"}],
        created_at=now - timedelta(minutes=10),
    )
    reaper_db.add(task)
    await reaper_db.commit()

    mock_get_token = mocker.AsyncMock(return_value=("gh_token", "owner", "repo"))
    mocker.patch("app.routers.agent.get_github_token_for_task", mock_get_token)

    await _recover_orphaned_tasks()

    # research type is not in WRITE_TASK_TYPES — no GitHub lookup should occur
    mock_get_token.assert_not_called()


def test_unblock_sweep_entrypoints_reexport_identity():
    """The four moved sweep entry points accessed via reaper_sweeps ARE the same
    objects as in blocked_task_unblock_sweeps (not copies or re-definitions)."""
    import app.reaper_sweeps as rs
    import app.blocked_task_unblock_sweeps as btu

    assert rs.sweep_unblock_blocked_tasks is btu.sweep_unblock_blocked_tasks
    assert rs._event_driven_unblock_project is btu._event_driven_unblock_project
    assert rs.event_driven_unblock_dependents is btu.event_driven_unblock_dependents
    assert rs.event_driven_unblock_on_knowledge is btu.event_driven_unblock_on_knowledge


def test_snap_all_terminal_and_snap_any_completed():
    """Unit test for _snap_all_terminal and _snap_any_completed helpers."""
    import uuid
    from app.models import TaskStatus

    tid = uuid.uuid4()
    snap_empty = []
    snap_all_done = [
        (tid, TaskStatus.FAILED, {}),
        (uuid.uuid4(), TaskStatus.CANCELLED, {}),
    ]
    snap_with_completed = [
        (tid, TaskStatus.COMPLETED, {}),
        (uuid.uuid4(), TaskStatus.FAILED, {}),
    ]
    snap_running = [
        (tid, TaskStatus.IN_PROGRESS, {}),
        (uuid.uuid4(), TaskStatus.FAILED, {}),
    ]

    # empty snapshot: vacuously all-terminal, none completed
    assert _snap_all_terminal(snap_empty) is True
    assert _snap_any_completed(snap_empty) is False

    # all terminal, none completed
    assert _snap_all_terminal(snap_all_done) is True
    assert _snap_any_completed(snap_all_done) is False

    # has a completed child
    assert _snap_all_terminal(snap_with_completed) is True
    assert _snap_any_completed(snap_with_completed) is True

    # one child still running
    assert _snap_all_terminal(snap_running) is False
    assert _snap_any_completed(snap_running) is False


# ── _run_orphan_io / OrphanIoSnapshot ────────────────────────────────────────


@pytest.mark.asyncio
async def test_orphan_external_io_gather_result_bundle(mocker):
    """_run_orphan_io() bundles all four external I/O calls into an
    OrphanIoSnapshot with the correct field values.

    Acceptance gate: exercises the extracted production function, not just
    the dataclass — verifies PR-state lookup and spot-preemption outcomes
    are wired into the returned snapshot.
    """
    from app.reaper_orphan_io import OrphanIoSnapshot, _run_orphan_io
    from datetime import datetime, timezone

    # ── task / fresh_task stubs ──────────────────────────────────────────
    class _Task:
        id = "task-abc"
        project_id = "proj-1"
        context = {}
        first_payload = {"type": "create"}
        vm_instance_id = None
        vm_spot = False
        last_heartbeat = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
        started_at = datetime(2026, 1, 1, 11, 0, 0, tzinfo=timezone.utc)
        status = "IN_PROGRESS"

    task = _Task()
    fresh_task = _Task()

    # ── mock the DB-backed PR artifact check (no PR) ─────────────────────
    mocker.patch(
        "app.reaper_orphan_io._task_has_open_pr",
        new=AsyncMock(return_value=(False, None)),
    )

    # ── mock the max_plan_pool deferred imports ───────────────────────────
    mocker.patch(
        "app.max_plan_pool.get_task_llm_account_id",
        return_value=None,
    )
    session = object()  # not used for real DB calls in this path

    snapshot = await _run_orphan_io(
        task=task,
        fresh_task=fresh_task,
        reap_reason_code="heartbeat_timeout_post_claim",
        is_provision_failure=False,
        is_rate_limited=False,
        is_dead_auth=False,
        session=session,
        runtime_config={},
    )

    assert isinstance(snapshot, OrphanIoSnapshot)
    # Pre-IO status captured from fresh_task before any external call
    assert snapshot.pre_io_status == "IN_PROGRESS"
    assert snapshot.pre_io_last_hb == fresh_task.last_heartbeat
    # No spot VM, no preemption
    assert snapshot.is_spot_preemption is False
    # No rate-limit signal — remains False
    assert snapshot.is_rate_limited is False
    # No PR artifact — GH lookup skipped
    assert snapshot.has_pr_artifact is False
    assert snapshot.pr_num is None
    assert snapshot.gh_pr_state is None
    assert snapshot.gh_pr_merged is False
    # No provision failure — serial capture skipped
    assert snapshot.io_serial_log is None
    assert snapshot.io_prov_zone is None


def test_reaper_active_scan_projection_invariant():
    """The active-scan stmt must exclude plan_output and retain the columns
    required by event listeners and _heartbeat_stale_cutoff."""
    from sqlalchemy.dialects import postgresql

    stmt = _build_active_scan_stmt()
    sql_text = str(stmt.compile(dialect=postgresql.dialect()))

    # plan_output must NOT appear in the projected columns
    assert "plan_output" not in sql_text, (
        "Active-scan projection must exclude plan_output JSONB. "
        "Re-adding it re-introduces the over-fetch this guard protects against."
    )

    # These columns are required by Task event listeners (_heartbeat_stale_cutoff
    # / status assignment); removing any triggers MissingGreenlet under async sessions.
    for required in ("status_log", "context", "payloads"):
        assert required in sql_text, (
            f"Active-scan projection must include '{required}' — "
            "removing it will trigger MissingGreenlet under async sessions."
        )


# ---------------------------------------------------------------------------
# Step 20: sweep_orphaned_cross_base_children
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_converge_guard_cancels_unreachable_cross_base_child(reaper_db):
    """A QUEUED child of a COMPLETED parent whose cross-base code-dep is
    permanently stranded (STRANDED_LANDING_REASON_KEY set on dep, no landed
    signal) is cancelled and stamped _orphaned_cross_base_cancel.

    This is the primary acceptance test for the converge guard (Step 20).
    """
    from app.reaper_sweeps import sweep_orphaned_cross_base_children

    project = await _make_project(reaper_db)

    # Dep: cross-base code task, COMPLETED, definitively stranded (never lands).
    dep = Task(
        project_id=project.id,
        payloads=[{"type": "create", "base_branch": "plan/dep-branch"}],
        status=TaskStatus.COMPLETED,
        context={
            "related_files": ["backend/app/foo.py"],
            "landed_on_integration": False,
            "rollup_landing_stranded": "not_ancestor_of_integration",
        },
    )
    reaper_db.add(dep)
    await reaper_db.commit()
    await reaper_db.refresh(dep)

    # Parent: research/plan direction, COMPLETED.
    parent = Task(
        project_id=project.id,
        payloads=[{"type": "research"}],
        status=TaskStatus.COMPLETED,
    )
    reaper_db.add(parent)
    await reaper_db.commit()
    await reaper_db.refresh(parent)

    # Child: QUEUED, on a different base branch, with file overlap so the dep
    # is a genuine code dependency, and waiting on the stranded dep.
    child = Task(
        project_id=project.id,
        payloads=[{"type": "create", "base_branch": "plan/child-branch"}],
        status=TaskStatus.QUEUED,
        parent_task_id=parent.id,
        depends_on=[str(dep.id)],
        context={"related_files": ["backend/app/foo.py"]},
    )
    reaper_db.add(child)
    await reaper_db.commit()
    await reaper_db.refresh(child)

    cancelled = await sweep_orphaned_cross_base_children(reaper_db)

    assert cancelled == 1
    await reaper_db.refresh(child)
    assert child.status == TaskStatus.CANCELLED
    assert child.context.get("_orphaned_cross_base_cancel") is True


@pytest.mark.asyncio
async def test_converge_guard_skips_legitimately_waiting_child(reaper_db):
    """A QUEUED child whose dep is cross-base COMPLETED but lacks
    STRANDED_LANDING_REASON_KEY (it may still land via rollup propagation) is
    left untouched — confirms the conservative 'only-cancel-when-definitive' policy.
    """
    from app.reaper_sweeps import sweep_orphaned_cross_base_children

    project = await _make_project(reaper_db)

    # Dep: COMPLETED, landed_on_integration=False BUT no stranded marker.
    dep = Task(
        project_id=project.id,
        payloads=[{"type": "create", "base_branch": "plan/dep-branch"}],
        status=TaskStatus.COMPLETED,
        context={
            "related_files": ["backend/app/foo.py"],
            "landed_on_integration": False,
            "_non_merge_unblock_reason": "staging_only_merge",
        },
    )
    reaper_db.add(dep)
    await reaper_db.commit()
    await reaper_db.refresh(dep)

    parent = Task(
        project_id=project.id,
        payloads=[{"type": "research"}],
        status=TaskStatus.COMPLETED,
    )
    reaper_db.add(parent)
    await reaper_db.commit()
    await reaper_db.refresh(parent)

    child = Task(
        project_id=project.id,
        payloads=[{"type": "create", "base_branch": "plan/child-branch"}],
        status=TaskStatus.QUEUED,
        parent_task_id=parent.id,
        depends_on=[str(dep.id)],
        context={"related_files": ["backend/app/foo.py"]},
    )
    reaper_db.add(child)
    await reaper_db.commit()
    await reaper_db.refresh(child)

    cancelled = await sweep_orphaned_cross_base_children(reaper_db)

    assert cancelled == 0
    await reaper_db.refresh(child)
    assert child.status == TaskStatus.QUEUED


@pytest.mark.asyncio
async def test_converge_guard_skips_child_of_active_parent(reaper_db):
    """A QUEUED child whose parent is still EVALUATING (not COMPLETED) is left
    untouched — the direction may still converge and handle the child normally.
    """
    from app.reaper_sweeps import sweep_orphaned_cross_base_children

    project = await _make_project(reaper_db)

    dep = Task(
        project_id=project.id,
        payloads=[{"type": "create", "base_branch": "plan/dep-branch"}],
        status=TaskStatus.COMPLETED,
        context={
            "related_files": ["backend/app/foo.py"],
            "landed_on_integration": False,
            "rollup_landing_stranded": "not_ancestor_of_integration",
        },
    )
    reaper_db.add(dep)
    await reaper_db.commit()
    await reaper_db.refresh(dep)

    parent = Task(
        project_id=project.id,
        payloads=[{"type": "research"}],
        status=TaskStatus.EVALUATING,
    )
    reaper_db.add(parent)
    await reaper_db.commit()
    await reaper_db.refresh(parent)

    child = Task(
        project_id=project.id,
        payloads=[{"type": "create", "base_branch": "plan/child-branch"}],
        status=TaskStatus.QUEUED,
        parent_task_id=parent.id,
        depends_on=[str(dep.id)],
        context={"related_files": ["backend/app/foo.py"]},
    )
    reaper_db.add(child)
    await reaper_db.commit()
    await reaper_db.refresh(child)

    cancelled = await sweep_orphaned_cross_base_children(reaper_db)

    assert cancelled == 0
    await reaper_db.refresh(child)
    assert child.status == TaskStatus.QUEUED


@pytest.mark.asyncio
async def test_converge_guard_skips_ordering_only_dep(reaper_db):
    """A dep that is ordering-only (non-code-producing type, e.g. research) is not
    a genuine code dependency, so the child waiting on it is not cancelled even
    when the dep is stranded cross-base.
    """
    from app.reaper_sweeps import sweep_orphaned_cross_base_children

    project = await _make_project(reaper_db)

    # Dep is a research task (non-code-producing) — ordering-only.
    dep = Task(
        project_id=project.id,
        payloads=[{"type": "research", "base_branch": "plan/dep-branch"}],
        status=TaskStatus.COMPLETED,
        context={
            "related_files": ["backend/app/foo.py"],
            "landed_on_integration": False,
            "rollup_landing_stranded": "not_ancestor_of_integration",
        },
    )
    reaper_db.add(dep)
    await reaper_db.commit()
    await reaper_db.refresh(dep)

    parent = Task(
        project_id=project.id,
        payloads=[{"type": "research"}],
        status=TaskStatus.COMPLETED,
    )
    reaper_db.add(parent)
    await reaper_db.commit()
    await reaper_db.refresh(parent)

    child = Task(
        project_id=project.id,
        payloads=[{"type": "create", "base_branch": "plan/child-branch"}],
        status=TaskStatus.QUEUED,
        parent_task_id=parent.id,
        depends_on=[str(dep.id)],
        context={"related_files": ["backend/app/foo.py"]},
    )
    reaper_db.add(child)
    await reaper_db.commit()
    await reaper_db.refresh(child)

    cancelled = await sweep_orphaned_cross_base_children(reaper_db)

    assert cancelled == 0
    await reaper_db.refresh(child)
    assert child.status == TaskStatus.QUEUED


# ── Metrics-integrity sweep (standing self-audit of self-reported counters) ──


@pytest.mark.asyncio
async def test_metrics_integrity_sweep_flags_counter_drift_without_mutating_sources(
    reaper_db, mocker
):
    """The standing metrics-integrity sweep must FLAG a seeded self-reported
    counter drift, leave the audited source counter byte-identical, and respect
    both the mode gate (off => no run) and the cadence gate (second immediate
    call inside the interval => no run).

    Ground truth exercised: a playbook entry cannot be confirmed more times than
    it was ever referenced/shown. We seed confirmed_count=7 > reference_count=2
    (the exact over-credit class the shown-key gate closed) and assert the sweep
    reports the drift as an independent recompute while NEVER writing back to
    ``confirmed_count``.
    """
    import app.metrics_integrity_sweep as mis
    from app.shared_state import runtime_config as _rc

    mis._reset_observability_for_tests()

    project = await _make_project(reaper_db)
    # A drifted playbook entry (confirmed > reference) plus a clean one.
    project.design_doc = {
        "playbook/drifted-key": {
            "value": "seeded drift",
            "confirmed_count": 7,
            "reference_count": 2,
            "followed_count": 3,
        },
        "playbook/clean-key": {
            "value": "consistent",
            "confirmed_count": 1,
            "reference_count": 4,
            "followed_count": 4,
        },
    }
    from sqlalchemy.orm.attributes import flag_modified

    flag_modified(project, "design_doc")
    reaper_db.add(project)
    await reaper_db.commit()

    # --- Mode gate: off => sweep does not run and flags nothing. --------------
    _saved = {
        k: _rc.get(k)
        for k in (
            "metrics_integrity_sweep_mode",
            "metrics_integrity_sweep_interval_minutes",
        )
    }
    try:
        _rc["metrics_integrity_sweep_mode"] = "off"
        gated = await mis.run_metrics_integrity_sweep(reaper_db, now=_utcnow())
        assert gated is None
        assert mis.get_metrics_integrity_observability()["ticks"] == 0

        # --- Observe mode: sweep runs and flags the drift. -------------------
        _rc["metrics_integrity_sweep_mode"] = "observe"
        _rc["metrics_integrity_sweep_interval_minutes"] = 60
        now = _utcnow()
        tick = await mis.run_metrics_integrity_sweep(reaper_db, now=now)
        assert tick is not None
        assert tick["mode"] == "observe"

        pid = str(project.id)
        playbook_flags = [
            f
            for f in tick["flags"]
            if f["check"] == "playbook_confirmation_integrity" and f["subject"] == pid
        ]
        assert len(playbook_flags) == 1, tick["flags"]
        flag = playbook_flags[0]
        assert flag["status"] == "flagged"
        # self_reported = sum of confirmed_count (7 + 1), ground truth = sum of
        # reference_count (2 + 4); delta = number of bad entries (1 over-credit).
        assert flag["self_reported"] == 8
        assert flag["ground_truth"] == 6
        assert flag["delta"] == 1
        assert "drifted-key" in flag["detail"]

        # --- Non-mutation: the audited source counter is byte-identical. -----
        await reaper_db.refresh(project)
        entries = project.design_doc
        assert entries["playbook/drifted-key"]["confirmed_count"] == 7
        assert entries["playbook/drifted-key"]["reference_count"] == 2
        assert entries["playbook/clean-key"]["confirmed_count"] == 1

        # --- Observability snapshot carries the flag. ------------------------
        snap = mis.get_metrics_integrity_observability()
        assert snap["ticks"] == 1
        assert snap["flags_total"] >= 1
        assert any(
            f["check"] == "playbook_confirmation_integrity"
            for f in snap["recent_flags"]
        )

        # --- Cadence gate: a second call inside the interval does NOT run. ----
        again = await mis.run_metrics_integrity_sweep(
            reaper_db, now=now + timedelta(minutes=1)
        )
        assert again is None
        assert mis.get_metrics_integrity_observability()["ticks"] == 1
    finally:
        for k, v in _saved.items():
            if v is None:
                _rc.pop(k, None)
            else:
                _rc[k] = v
        mis._reset_observability_for_tests()


# ── _projects_batch expiry-guard regression tests ────────────────────────────
#
# Regression: the orphan loop batch-loads Project objects BEFORE the per-row
# processing loop (reaper.py ~line 757).  A per-row exception on one orphan
# triggers session.rollback(), which expires ALL session-tracked ORM objects —
# including the _projects_batch items.  A subsequent iteration that reads
# attributes on the cached-but-expired Project raises MissingGreenlet
# (synchronous lazy-load in async context), crash-quarantining a healthy task
# as a side effect of an unrelated per-row failure.
#
# The fix (applied at the three _projects_batch.get() call sites) mirrors the
# existing Task-object guard: ``if _proj is None or inspect(_proj).expired:
# _proj = await session.get(Project, task.project_id)``.
#
# These tests exercise two of the three guarded call sites:
#   Site 1 (~line 1589): merged-PR landing classification
#   Site 2 (~line 1730): open-PR review-spawn branch derivation


@pytest.mark.asyncio
@pytest.mark.skipif(
    not USE_POSTGRES,
    reason="rollback-triggered ORM expiry (the MissingGreenlet fault) is no-oped "
    "on aiosqlite; only asyncpg exercises the re-fetch guard faithfully.",
)
async def test_orphan_project_batch_expired_before_merged_pr_classification(
    reaper_db, stop_after_one_cycle, reaper_side_effects, clean_crash_streaks, mocker
):
    """Regression for Site 1: _projects_batch Project expired by a prior-row
    rollback must be re-fetched before _apply_rollup_merge_classification.

    Task A (PROVISIONING, processed first) crashes on session.get — triggering
    session.rollback() which expires all ORM objects including _projects_batch.
    Task B (IN_PROGRESS, merged PR) is processed second.  Without the expiry
    guard at the merged-PR classification site, ``get_project_integration_branch``
    accesses an expired Project attribute, raising MissingGreenlet and
    crash-quarantining a healthy task.  With the guard, a fresh Project is
    re-fetched and Task B completes normally.
    """
    mocker.patch(
        "app.routers.agent.get_github_token_for_task",
        new_callable=AsyncMock,
        return_value=("tok", "owner", "repo"),
    )
    mocker.patch(
        "app.github.get_pull_request",
        new_callable=AsyncMock,
        return_value={"state": "closed", "merged": True, "base": {"ref": "main"}},
    )
    mocker.patch(
        "app.review_pipeline._spawn_pipeline_task",
        new_callable=AsyncMock,
        return_value=(None, True),
    )

    project = await _make_project(reaper_db)

    # Task A: PROVISIONING (priority bucket 2) — processed first.
    # started_at > 15 min ago satisfies the absolute PROVISIONING catch-all.
    # Crashes immediately via patched_get, expiring all session objects.
    task_a = Task(
        project_id=project.id,
        payloads=[{"type": "create"}],
        vm_instance_id="vm-expiry-crash-merged-pr-site1",
        status=TaskStatus.PROVISIONING,
        started_at=_utcnow() - timedelta(minutes=20),
    )
    # Task B: IN_PROGRESS (priority bucket 3) — processed second.
    # Has a merged-PR artifact; should still complete after the batch expiry.
    task_b = Task(
        project_id=project.id,
        payloads=[{"type": "create", "base_branch": "main"}],
        vm_instance_id="vm-merged-pr-after-batch-expiry",
        status=TaskStatus.IN_PROGRESS,
        attempt_count=0,
        last_heartbeat=_utcnow() - timedelta(minutes=10),
        context={"scope": "small"},
    )
    reaper_db.add(task_a)
    reaper_db.add(task_b)
    await reaper_db.commit()
    await reaper_db.refresh(task_a)
    await reaper_db.refresh(task_b)
    task_a_id = task_a.id
    task_b_id = task_b.id

    reaper_db.add(
        Artifact(
            task_id=task_b_id,
            artifact_type=ArtifactType.PULL_REQUEST,
            pr_url="https://github.com/HenryHamster/BlueprintAIStudio/pull/7701",
        )
    )
    await reaper_db.commit()

    # Crash Task A inside _run_orphan_io.  This function is called at line 1136
    # for every orphan including PROVISIONING tasks, inside the per-orphan
    # try/except block.  The crash triggers the except-block's
    # session.rollback() (line 2265), expiring ALL cached objects including
    # _projects_batch entries.  _run_orphan_io was chosen because (a) it is
    # guaranteed to be called for every orphan, (b) it runs AFTER the fresh_task
    # guard (so the task instance is valid), and (c) it runs BEFORE the three
    # _projects_batch call sites.
    import app.reaper as _reaper_mod

    _real_run_orphan_io = _reaper_mod._run_orphan_io

    async def patched_run_orphan_io(*args, **kwargs):
        task = kwargs.get("task")
        if task is not None and task.id == task_a_id:
            raise RuntimeError("injected-expiry-crash-projects-batch-site1")
        return await _real_run_orphan_io(*args, **kwargs)

    mocker.patch.object(
        _reaper_mod, "_run_orphan_io", side_effect=patched_run_orphan_io
    )

    _cycles_before = _reaper_mod._reaper_cycles_completed_count
    await _run_reaper_once()
    await reaper_db.refresh(task_b)

    # Cycle completes cleanly despite Task A's crash.
    assert _reaper_mod._reaper_cycles_completed_count == _cycles_before + 1, (
        "reaper cycle must complete cleanly despite Task A crashing and expiring "
        "_projects_batch"
    )
    # Task B must be COMPLETED: the expiry guard re-fetched a fresh Project and
    # _apply_rollup_merge_classification classified the landing correctly.
    # Without the guard, MissingGreenlet would crash-quarantine Task B to
    # IN_PROGRESS (mutations rolled back by the per-row exception handler).
    assert task_b.status == TaskStatus.COMPLETED, (
        "Task B (merged-PR) must be COMPLETED — the _projects_batch expiry guard "
        "must re-fetch a fresh Project before _apply_rollup_merge_classification "
        "(reaper.py Site 1: merged-PR classification)"
    )
    assert (task_b.context or {}).get("pr_merged") is True, (
        "Task B context must carry pr_merged=True after expiry-guarded merge classification"
    )


@pytest.mark.asyncio
@pytest.mark.skipif(
    not USE_POSTGRES,
    reason="rollback-triggered ORM expiry (the MissingGreenlet fault) is no-oped "
    "on aiosqlite; only asyncpg exercises the re-fetch guard faithfully.",
)
async def test_orphan_project_batch_expired_before_open_pr_review_spawn(
    reaper_db, stop_after_one_cycle, reaper_side_effects, clean_crash_streaks, mocker
):
    """Regression for Site 2: _projects_batch Project expired by a prior-row
    rollback must be re-fetched before _get_project_task_branch_namespace at
    the open-PR review-spawn site.

    Same expiry mechanic as Site 1: Task A crashes → rollback → all session
    objects expire.  Task B (open PR) then derives its review-spawn branch via
    ``_get_project_task_branch_namespace(_proj)``, which reads
    ``_proj.build_config``.  Without the guard, that attribute access raises
    MissingGreenlet; the inner spawn-failure except catches it, sets
    ``_pr_state_check_pending = True``, and never calls _spawn_pipeline_task.
    With the guard, a fresh Project is re-fetched and the review task spawns
    normally.

    Note: no explicit GitHub token mock is needed — when get_github_token_for_task
    has no credentials in the test DB, reaper_orphan_io.py falls back to
    gh_pr_state="open", triggering the open-PR review-spawn path automatically.
    """
    mock_spawn = mocker.patch(
        "app.review_pipeline._spawn_pipeline_task",
        new_callable=AsyncMock,
        return_value=(None, True),
    )

    project = await _make_project(reaper_db)

    # Task A: PROVISIONING (priority 2, processed first) — crashes, expiring
    # all session objects including _projects_batch.
    task_a = Task(
        project_id=project.id,
        payloads=[{"type": "create"}],
        vm_instance_id="vm-expiry-crash-open-pr-site2",
        status=TaskStatus.PROVISIONING,
        started_at=_utcnow() - timedelta(minutes=20),
    )
    # Task B: IN_PROGRESS (priority 3, processed second) — has a PR artifact.
    # The GitHub fallback sets gh_pr_state="open", routing to the review-spawn
    # path that reads _proj.build_config via _get_project_task_branch_namespace.
    task_b = Task(
        project_id=project.id,
        payloads=[{"type": "create"}],
        vm_instance_id="vm-open-pr-after-batch-expiry",
        status=TaskStatus.IN_PROGRESS,
        attempt_count=0,
        last_heartbeat=_utcnow() - timedelta(minutes=10),
        context={"scope": "small"},
    )
    reaper_db.add(task_a)
    reaper_db.add(task_b)
    await reaper_db.commit()
    await reaper_db.refresh(task_a)
    await reaper_db.refresh(task_b)
    task_a_id = task_a.id
    task_b_id = task_b.id

    reaper_db.add(
        Artifact(
            task_id=task_b_id,
            artifact_type=ArtifactType.PULL_REQUEST,
            pr_url="https://github.com/HenryHamster/BlueprintAIStudio/pull/8802",
        )
    )
    await reaper_db.commit()

    # Crash Task A inside _run_orphan_io.  This function is called at line 1136
    # for every orphan including PROVISIONING tasks, inside the per-orphan
    # try/except block.  The crash triggers the except-block's
    # session.rollback() (line 2265), expiring ALL cached objects including
    # _projects_batch entries.  _run_orphan_io was chosen because (a) it is
    # guaranteed to be called for every orphan, (b) it runs AFTER the fresh_task
    # guard (so the task instance is valid), and (c) it runs BEFORE the three
    # _projects_batch call sites.
    import app.reaper as _reaper_mod

    _real_run_orphan_io = _reaper_mod._run_orphan_io

    async def patched_run_orphan_io(*args, **kwargs):
        task = kwargs.get("task")
        if task is not None and task.id == task_a_id:
            raise RuntimeError("injected-expiry-crash-projects-batch-site2")
        return await _real_run_orphan_io(*args, **kwargs)

    mocker.patch.object(
        _reaper_mod, "_run_orphan_io", side_effect=patched_run_orphan_io
    )

    _cycles_before = _reaper_mod._reaper_cycles_completed_count
    await _run_reaper_once()
    await reaper_db.refresh(task_b)

    # Cycle completes cleanly.
    assert _reaper_mod._reaper_cycles_completed_count == _cycles_before + 1, (
        "reaper cycle must complete cleanly despite Task A crashing and expiring "
        "_projects_batch"
    )
    # Task B must be EVALUATING (handed off to review pipeline).
    assert task_b.status == TaskStatus.EVALUATING, (
        "Task B (open-PR) must be EVALUATING — the _projects_batch expiry guard "
        "must re-fetch a fresh Project before _get_project_task_branch_namespace "
        "(reaper.py Site 2: open-PR review-spawn branch derivation)"
    )
    # The spawn must have been called: without the expiry guard,
    # _get_project_task_branch_namespace raises MissingGreenlet on the expired
    # project, the inner spawn-failure except catches it, and _spawn_pipeline_task
    # is never invoked (leaving _pr_state_check_pending=True and no review child).
    mock_spawn.assert_called_once()
    # With the guard and a successful spawn, the write-ahead recovery marker is
    # cleared from context (not needed since the review task was spawned).
    assert not (task_b.context or {}).get("_pr_state_check_pending"), (
        "_pr_state_check_pending must be cleared after successful review spawn"
    )


# ── Step 22 / 22b independent cadence timers ─────────────────────────────────
# Regression coverage for the shared-cadence-global bug: sweep_proof_earn_loop
# (Step 22b) MUST NOT share its cadence timestamp with sweep_capability_backing_proofs
# (Step 22). The reaper runs Step 22 then Step 22b in the same cycle with the same
# `now`; a shared timestamp global means Step 22 stamps it first, so Step 22b's
# cadence check sees "just ran" and gates the proof-earn loop out on every cycle.


@pytest.mark.asyncio
async def test_proof_earn_loop_runs_after_backing_proof_same_cycle(reaper_db, mocker):
    """Step 22b runs even after Step 22 stamps its own cadence timer this cycle.

    With the shared-global bug, sweep_capability_backing_proofs stamps the cadence
    timestamp at the top of the cycle, and sweep_proof_earn_loop — reading the SAME
    global — then sees "not due" and returns {} without ever collecting demand. This
    asserts the proof-earn loop actually executes its body (collect_missing_capability_demand
    is awaited) after the backing-proof sweep ran with the same `now`.
    """
    # Reset both cadence timers to first-run-immediate.
    _prev_backing = _rs._last_backing_proof_sweep
    _prev_earn = _rs._last_proof_earn_loop_sweep
    _rs._last_backing_proof_sweep = None
    _rs._last_proof_earn_loop_sweep = None
    try:
        project = await _make_project(reaper_db)
        project.problem_model = {
            "capabilities": [
                {
                    "name": "demanded-cap",
                    "status": "working",
                    "backing_paths": [],
                }
            ]
        }
        reaper_db.add(project)
        await reaper_db.commit()

        now = _utcnow()

        # Spy on the loop-body entry point (imported inside the sweep from
        # app.capability_demand). If Step 22b is gated out, this is never awaited.
        demand_spy = mocker.patch(
            "app.capability_demand.collect_missing_capability_demand",
            new=AsyncMock(return_value={"demands": ["demanded-cap"]}),
        )
        commission_stub = mocker.patch(
            "app.capability_proof.commission_proof_earn_loop",
            new=AsyncMock(
                return_value={
                    "total_demanded_unproven": 1,
                    "earned": 0,
                    "no_backing_paths": ["demanded-cap"],
                    "still_unproven": ["demanded-cap"],
                }
            ),
        )

        # Step 22 first — this stamps _last_backing_proof_sweep = now.
        await _rs.sweep_capability_backing_proofs(reaper_db, now)
        assert _rs._last_backing_proof_sweep == now

        # Step 22b in the SAME cycle with the SAME now. Under the bug this returns
        # {} (gated by the just-stamped shared global); with independent timers it
        # runs its body.
        result = await _rs.sweep_proof_earn_loop(reaper_db, now)

        demand_spy.assert_awaited()  # the proof-earn loop body executed
        commission_stub.assert_awaited_once()
        assert result != {}, "proof-earn loop must not be gated out by Step 22"
        assert result["total_demanded_unproven"] == 1
        assert result["still_unproven"] == ["demanded-cap"]
        # The loop stamped its OWN timer, not the backing-proof timer.
        assert _rs._last_proof_earn_loop_sweep == now
    finally:
        _rs._last_backing_proof_sweep = _prev_backing
        _rs._last_proof_earn_loop_sweep = _prev_earn


# ── Step 22e: auto-clear moot pending confirmations ──────────────────────────


async def _make_ready_llm_account(db, user_id: str):
    """Create an enabled, runtime-ready Anthropic OAuth account for a user."""
    from app.models import MaxPlanAccount

    acct = MaxPlanAccount(
        user_id=user_id,
        name="acct-1",
        enabled=True,
        oauth_token="projects/x/secrets/tok",
    )
    db.add(acct)
    await db.commit()
    return acct


@pytest.mark.asyncio
async def test_moot_confirmation_sweep_clears_credentials_when_owner_ready(reaper_db):
    """A credentials_needed item auto-clears once the owner has a runtime-ready
    LLM account, and the removal persists to the durable problem_model column."""
    _prev = _rs._last_moot_confirm_sweep
    _rs._last_moot_confirm_sweep = None
    try:
        project = await _make_project(reaper_db)  # user_id == "user1"
        await _make_ready_llm_account(reaper_db, "user1")
        project.problem_model = {
            "pending_user_confirmation": [
                {
                    "id": "p-cred",
                    "type": "credentials_needed",
                    "proposal": "Add an Anthropic Claude OAuth token for the pool.",
                }
            ]
        }
        reaper_db.add(project)
        await reaper_db.commit()

        cleared = await _rs.sweep_auto_clear_moot_pending_confirmations(
            reaper_db, _utcnow()
        )
        assert cleared == 1

        await reaper_db.refresh(project)
        assert project.problem_model["pending_user_confirmation"] == []
        # revision-log entry was appended by the pure helper
        log = project.problem_model.get("revision_log") or []
        assert any(
            e.get("changes", {}).get("auto_cleared_pending_confirmations") == ["p-cred"]
            for e in log
        )
    finally:
        _rs._last_moot_confirm_sweep = _prev


@pytest.mark.asyncio
async def test_moot_confirmation_sweep_leaves_item_when_not_ready(reaper_db):
    """No runtime-ready credential -> the credentials_needed item is left in place;
    intent_change is never auto-removed regardless."""
    _prev = _rs._last_moot_confirm_sweep
    _rs._last_moot_confirm_sweep = None
    try:
        project = await _make_project(reaper_db)  # no LLM account for user1
        project.problem_model = {
            "pending_user_confirmation": [
                {
                    "id": "p-cred",
                    "type": "credentials_needed",
                    "proposal": "Add a Claude OAuth token.",
                },
                {
                    "id": "p-intent",
                    "type": "intent_change",
                    "proposal": "Pivot the project.",
                },
            ]
        }
        reaper_db.add(project)
        await reaper_db.commit()

        cleared = await _rs.sweep_auto_clear_moot_pending_confirmations(
            reaper_db, _utcnow()
        )
        assert cleared == 0

        await reaper_db.refresh(project)
        ids = {p["id"] for p in project.problem_model["pending_user_confirmation"]}
        assert ids == {"p-cred", "p-intent"}
    finally:
        _rs._last_moot_confirm_sweep = _prev


@pytest.mark.asyncio
async def test_moot_confirmation_sweep_cadence_gated(reaper_db):
    """A just-stamped cadence timer gates the sweep out (returns 0 without work)."""
    _prev = _rs._last_moot_confirm_sweep
    _rs._last_moot_confirm_sweep = _utcnow()  # just ran -> not due
    try:
        project = await _make_project(reaper_db)
        await _make_ready_llm_account(reaper_db, "user1")
        project.problem_model = {
            "pending_user_confirmation": [
                {
                    "id": "p-cred",
                    "type": "credentials_needed",
                    "proposal": "Add an Anthropic Claude OAuth token.",
                }
            ]
        }
        reaper_db.add(project)
        await reaper_db.commit()

        cleared = await _rs.sweep_auto_clear_moot_pending_confirmations(
            reaper_db, _utcnow()
        )
        assert cleared == 0
        await reaper_db.refresh(project)
        # Untouched because the sweep was gated out.
        assert project.problem_model["pending_user_confirmation"][0]["id"] == "p-cred"
    finally:
        _rs._last_moot_confirm_sweep = _prev


@pytest.mark.asyncio
async def test_moot_confirmation_sweep_covers_all_projects_beyond_old_limit(reaper_db):
    """Sweep must cover ALL projects, not just the first 25.

    Creates 30 projects each with a clearable credentials_needed item and a
    ready owner LLM account, then asserts all 30 items are cleared in a single
    sweep invocation — verifying exhaustive pagination beyond the old LIMIT 25.
    """
    _prev = _rs._last_moot_confirm_sweep
    _rs._last_moot_confirm_sweep = None
    try:
        await _make_ready_llm_account(reaper_db, "user1")
        projects = []
        for i in range(30):
            p = Project(name=f"proj-pagination-{i}", user_id="user1")
            p.problem_model = {
                "pending_user_confirmation": [
                    {
                        "id": f"p-cred-{i}",
                        "type": "credentials_needed",
                        "proposal": "Add an Anthropic Claude OAuth token for the pool.",
                    }
                ]
            }
            reaper_db.add(p)
            projects.append(p)
        await reaper_db.commit()

        cleared = await _rs.sweep_auto_clear_moot_pending_confirmations(
            reaper_db, _utcnow()
        )
        assert cleared == 30, f"expected 30 cleared, got {cleared}"

        for p in projects:
            await reaper_db.refresh(p)
            assert p.problem_model["pending_user_confirmation"] == [], (
                f"project {p.id} still has pending confirmations"
            )
    finally:
        _rs._last_moot_confirm_sweep = _prev


# ---------------------------------------------------------------------------
# test_pending_confirmation_stale_decay
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_pending_confirmation_stale_decay_removes_old_intent_change(reaper_db):
    """An intent_change item older than PENDING_CONFIRMATION_STALE_DECAY_DAYS is
    removed by the decay sweep and the revision log is updated."""
    from app.problem_model import PENDING_CONFIRMATION_STALE_DECAY_DAYS

    _prev = _rs._last_stale_confirm_decay_sweep
    _rs._last_stale_confirm_decay_sweep = None
    try:
        project = await _make_project(reaper_db)
        old_ts = (
            _utcnow() - timedelta(days=PENDING_CONFIRMATION_STALE_DECAY_DAYS + 1)
        ).isoformat()
        project.problem_model = {
            "pending_user_confirmation": [
                {
                    "id": "p-intent-old",
                    "type": "intent_change",
                    "proposal": "Pivot the project to a new direction.",
                    "created_at": old_ts,
                }
            ]
        }
        reaper_db.add(project)
        await reaper_db.commit()

        decayed = await _rs.sweep_decay_stale_pending_confirmations(
            reaper_db, _utcnow()
        )
        assert decayed == 1

        await reaper_db.refresh(project)
        assert project.problem_model["pending_user_confirmation"] == []
        log = project.problem_model.get("revision_log") or []
        assert any(
            e.get("changes", {}).get("decayed_pending_confirmations") == ["p-intent-old"]
            for e in log
        )
    finally:
        _rs._last_stale_confirm_decay_sweep = _prev


@pytest.mark.asyncio
async def test_pending_confirmation_stale_decay_keeps_fresh_item(reaper_db):
    """An item younger than PENDING_CONFIRMATION_STALE_DECAY_DAYS is left untouched."""
    from app.problem_model import PENDING_CONFIRMATION_STALE_DECAY_DAYS

    _prev = _rs._last_stale_confirm_decay_sweep
    _rs._last_stale_confirm_decay_sweep = None
    try:
        project = await _make_project(reaper_db)
        fresh_ts = (
            _utcnow() - timedelta(days=PENDING_CONFIRMATION_STALE_DECAY_DAYS - 1)
        ).isoformat()
        project.problem_model = {
            "pending_user_confirmation": [
                {
                    "id": "p-fresh",
                    "type": "major_decision",
                    "proposal": "Adopt a new framework.",
                    "created_at": fresh_ts,
                }
            ]
        }
        reaper_db.add(project)
        await reaper_db.commit()

        decayed = await _rs.sweep_decay_stale_pending_confirmations(
            reaper_db, _utcnow()
        )
        assert decayed == 0

        await reaper_db.refresh(project)
        ids = {p["id"] for p in project.problem_model["pending_user_confirmation"]}
        assert ids == {"p-fresh"}
    finally:
        _rs._last_stale_confirm_decay_sweep = _prev


@pytest.mark.asyncio
async def test_pending_confirmation_stale_decay_removes_all_types(reaper_db):
    """All four confirmation types age out identically when past the decay window."""
    from app.problem_model import PENDING_CONFIRMATION_STALE_DECAY_DAYS

    _prev = _rs._last_stale_confirm_decay_sweep
    _rs._last_stale_confirm_decay_sweep = None
    try:
        project = await _make_project(reaper_db)
        old_ts = (
            _utcnow() - timedelta(days=PENDING_CONFIRMATION_STALE_DECAY_DAYS + 5)
        ).isoformat()
        project.problem_model = {
            "pending_user_confirmation": [
                {
                    "id": "p-cred",
                    "type": "credentials_needed",
                    "proposal": "Need an API key.",
                    "created_at": old_ts,
                },
                {
                    "id": "p-access",
                    "type": "access_needed",
                    "proposal": "Need repo access.",
                    "created_at": old_ts,
                },
                {
                    "id": "p-intent",
                    "type": "intent_change",
                    "proposal": "Pivot direction.",
                    "created_at": old_ts,
                },
                {
                    "id": "p-decision",
                    "type": "major_decision",
                    "proposal": "Adopt new infra.",
                    "created_at": old_ts,
                },
            ]
        }
        reaper_db.add(project)
        await reaper_db.commit()

        decayed = await _rs.sweep_decay_stale_pending_confirmations(
            reaper_db, _utcnow()
        )
        assert decayed == 4

        await reaper_db.refresh(project)
        assert project.problem_model["pending_user_confirmation"] == []
    finally:
        _rs._last_stale_confirm_decay_sweep = _prev


@pytest.mark.asyncio
async def test_pending_confirmation_stale_decay_cadence_gated(reaper_db):
    """A just-stamped cadence timer gates the sweep out (returns 0 without work)."""
    from app.problem_model import PENDING_CONFIRMATION_STALE_DECAY_DAYS

    _prev = _rs._last_stale_confirm_decay_sweep
    _rs._last_stale_confirm_decay_sweep = _utcnow()  # just ran -> not due
    try:
        project = await _make_project(reaper_db)
        old_ts = (
            _utcnow() - timedelta(days=PENDING_CONFIRMATION_STALE_DECAY_DAYS + 1)
        ).isoformat()
        project.problem_model = {
            "pending_user_confirmation": [
                {
                    "id": "p-gated",
                    "type": "intent_change",
                    "proposal": "Should be gated.",
                    "created_at": old_ts,
                }
            ]
        }
        reaper_db.add(project)
        await reaper_db.commit()

        decayed = await _rs.sweep_decay_stale_pending_confirmations(
            reaper_db, _utcnow()
        )
        assert decayed == 0
        await reaper_db.refresh(project)
        assert project.problem_model["pending_user_confirmation"][0]["id"] == "p-gated"
    finally:
        _rs._last_stale_confirm_decay_sweep = _prev


@pytest.mark.asyncio
async def test_pending_confirmation_stale_decay_no_created_at(reaper_db):
    """Items with a missing created_at are conservatively left in place."""
    _prev = _rs._last_stale_confirm_decay_sweep
    _rs._last_stale_confirm_decay_sweep = None
    try:
        project = await _make_project(reaper_db)
        project.problem_model = {
            "pending_user_confirmation": [
                {
                    "id": "p-no-ts",
                    "type": "intent_change",
                    "proposal": "No timestamp present.",
                    "created_at": "",
                }
            ]
        }
        reaper_db.add(project)
        await reaper_db.commit()

        decayed = await _rs.sweep_decay_stale_pending_confirmations(
            reaper_db, _utcnow()
        )
        assert decayed == 0
        await reaper_db.refresh(project)
        assert project.problem_model["pending_user_confirmation"][0]["id"] == "p-no-ts"
    finally:
        _rs._last_stale_confirm_decay_sweep = _prev


@pytest.mark.asyncio
async def test_decay_sweep_lowers_operator_needs_action_count(db_session):
    """E2E: the decay sweep removes stale pending_user_confirmation items and the
    operator attention count drops accordingly.

    Uses ``db_session`` (Postgres-backed in CI) because ``build_operator_attention``
    uses JSONB operators (``->>``, ``->``) that SQLite cannot execute.
    """
    from app.problem_model import PENDING_CONFIRMATION_STALE_DECAY_DAYS
    from app.operator_attention import build_operator_attention

    _prev = _rs._last_stale_confirm_decay_sweep
    _rs._last_stale_confirm_decay_sweep = None
    try:
        project = await _make_project(db_session)
        old_ts = (
            _utcnow() - timedelta(days=PENDING_CONFIRMATION_STALE_DECAY_DAYS + 1)
        ).isoformat()
        project.problem_model = {
            "pending_user_confirmation": [
                {
                    "id": "p-e2e-old-1",
                    "type": "intent_change",
                    "proposal": "Pivot the project.",
                    "created_at": old_ts,
                },
                {
                    "id": "p-e2e-old-2",
                    "type": "major_decision",
                    "proposal": "Adopt new infra.",
                    "created_at": old_ts,
                },
            ]
        }
        db_session.add(project)
        await db_session.commit()
        await db_session.refresh(project)

        # Baseline: operator attention includes the 2 pending confirmations.
        attention_before = await build_operator_attention(
            db_session, project, "user1"
        )
        confirm_before = attention_before["counts"]["pending_confirmations"]
        needs_op_before = attention_before["counts"]["needs_operator"]
        assert confirm_before >= 2, (
            f"test invariant: project must have stale pending confirmations, "
            f"got {confirm_before}"
        )
        assert needs_op_before >= confirm_before, (
            f"needs_operator ({needs_op_before}) must be at least "
            f"pending_confirmations ({confirm_before})"
        )

        # Sweep decays the stale items.
        decayed = await _rs.sweep_decay_stale_pending_confirmations(
            db_session, _utcnow()
        )
        assert decayed == 2

        # Post-sweep: pending_confirmations is 0, needs_operator dropped
        # by exactly the confirmation count.
        await db_session.refresh(project)
        attention_after = await build_operator_attention(
            db_session, project, "user1"
        )
        confirm_after = attention_after["counts"]["pending_confirmations"]
        needs_op_after = attention_after["counts"]["needs_operator"]

        assert confirm_after == 0, (
            f"pending_confirmations must drop from {confirm_before} to 0 "
            f"after decay sweep — got {confirm_after}"
        )
        assert needs_op_before - needs_op_after == confirm_before, (
            f"needs_operator must drop by {confirm_before} "
            f"(from {needs_op_before} to {needs_op_before - confirm_before}) "
            f"— got {needs_op_after} (delta={needs_op_before - needs_op_after})"
        )
    finally:
        _rs._last_stale_confirm_decay_sweep = _prev


@pytest.mark.asyncio
async def test_proof_earn_loop_fires_without_demand_when_unproven_caps_exist(reaper_db, mocker):
    """Step 22b fires even with NO demand when unproven working capabilities exist.

    The no-demand fallthrough breaks the circularity: demand requires blocked
    dependents, blocked dependents require enforce mode, and enforce mode
    requires proven capabilities.  Without this fallthrough the earn loop would
    never fire on a project with all capabilities unproven and no blocked
    dependents, leaving the gate permanently stuck in 'off'.

    This test asserts that when collect_missing_capability_demand returns empty
    demand, the sweep still calls commission_proof_earn_loop with a synthetic
    demand covering all unproven working capabilities.
    """
    _prev_backing = _rs._last_backing_proof_sweep
    _prev_earn = _rs._last_proof_earn_loop_sweep
    _rs._last_backing_proof_sweep = None
    _rs._last_proof_earn_loop_sweep = None
    try:
        project = await _make_project(reaper_db)
        project.problem_model = {
            "capabilities": [
                {
                    "name": "ci-pipeline",
                    "status": "working",
                    "backing_paths": ["backend/app/capability_proof.py"],
                },
                {
                    "name": "repo-map",
                    "status": "working",
                    "backing_paths": [],
                },
            ]
        }
        reaper_db.add(project)
        await reaper_db.commit()

        now = _utcnow()

        # Return NO demand from the collector.
        demand_spy = mocker.patch(
            "app.capability_demand.collect_missing_capability_demand",
            new=AsyncMock(return_value={"total": 0, "demands": []}),
        )
        # Spy on the earn loop to verify it receives a synthetic demand.
        commission_spy = mocker.patch(
            "app.capability_proof.commission_proof_earn_loop",
            new=AsyncMock(
                return_value={
                    "total_demanded_unproven": 1,
                    "earned": 0,
                    "no_backing_paths": ["repo-map"],
                    "still_unproven": [],
                    "path_proven_stuck": [],
                }
            ),
        )

        result = await _rs.sweep_proof_earn_loop(reaper_db, now)

        # The demand collector was called.
        demand_spy.assert_awaited()
        # The earn loop was called (not skipped by the demand guard).
        commission_spy.assert_awaited_once()
        # Extract the synthetic demand passed to commission_proof_earn_loop.
        _call_args = commission_spy.await_args
        assert _call_args is not None
        _synthetic_demand = _call_args[0][1] if _call_args.args else {}
        assert _synthetic_demand.get("total", 0) >= 1, (
            "synthetic demand must cover at least one unproven capability"
        )
        synthetic_refs = {
            d["ref"] for d in _synthetic_demand.get("demands", [])
            if d.get("kind") == "capability"
        }
        # Both capabilities should be in the synthetic demand.
        assert "ci-pipeline" in synthetic_refs, (
            "ci-pipeline must be in synthetic demand"
        )
        assert "repo-map" in synthetic_refs, (
            "repo-map must be in synthetic demand"
        )
        # The sweep ran (result is not {} — it's the combined summary).
        assert result != {}, "sweep must not return empty dict when it ran"
        assert _rs._last_proof_earn_loop_sweep == now
    finally:
        _rs._last_backing_proof_sweep = _prev_backing
        _rs._last_proof_earn_loop_sweep = _prev_earn

@pytest.mark.asyncio
async def test_proof_earn_loop_honors_its_own_cadence_gate(reaper_db, mocker):
    """Step 22b's cadence gate is independent of the backing-proof timer.

    The proof-earn loop must respect its OWN recent-run timestamp regardless of the
    backing-proof timer's state. Pre-stamping _last_proof_earn_loop_sweep to `now`
    while leaving _last_backing_proof_sweep=None must gate the loop out (return {})
    without collecting demand.
    """
    _prev_backing = _rs._last_backing_proof_sweep
    _prev_earn = _rs._last_proof_earn_loop_sweep
    try:
        await _make_project(reaper_db)
        now = _utcnow()

        # Backing-proof timer is "never ran"; proof-earn timer "just ran".
        _rs._last_backing_proof_sweep = None
        _rs._last_proof_earn_loop_sweep = now

        demand_spy = mocker.patch(
            "app.capability_demand.collect_missing_capability_demand",
            new=AsyncMock(return_value={"demands": ["x"]}),
        )

        result = await _rs.sweep_proof_earn_loop(reaper_db, now)

        assert result == {}, "proof-earn loop must honor its own recent-run gate"
        demand_spy.assert_not_awaited()
        # Its own timer is unchanged by a gated call; the backing timer stays None.
        assert _rs._last_proof_earn_loop_sweep == now
        assert _rs._last_backing_proof_sweep is None
    finally:
        _rs._last_backing_proof_sweep = _prev_backing
        _rs._last_proof_earn_loop_sweep = _prev_earn


@pytest.mark.asyncio
async def test_proof_earn_loop_threads_named_test_executed_and_session(reaper_db, mocker):
    """Step 22b threads the execution-trial signal (Slice 3, design 84021e2d).

    The sweep must (a) pass the DB ``session`` into ``commission_proof_earn_loop``
    so it can enroll a bounded ``capability_proof_trial``, and (b) accumulate the
    per-project ``named_test_executed`` list into the combined summary so operators
    can see which capabilities are earning the real ``proven`` tier this sweep.
    """
    _prev_backing = _rs._last_backing_proof_sweep
    _prev_earn = _rs._last_proof_earn_loop_sweep
    _rs._last_backing_proof_sweep = None
    _rs._last_proof_earn_loop_sweep = None
    try:
        project = await _make_project(reaper_db)
        project.problem_model = {
            "capabilities": [
                {
                    "name": "ci-pipeline",
                    "status": "working",
                    "backing_paths": ["backend/app/capability_proof.py"],
                }
            ]
        }
        reaper_db.add(project)
        await reaper_db.commit()

        now = _utcnow()

        mocker.patch(
            "app.capability_demand.collect_missing_capability_demand",
            new=AsyncMock(
                return_value={
                    "total": 1,
                    "demands": [{"kind": "capability", "ref": "ci-pipeline"}],
                }
            ),
        )
        commission_spy = mocker.patch(
            "app.capability_proof.commission_proof_earn_loop",
            new=AsyncMock(
                return_value={
                    "total_demanded_unproven": 1,
                    "earned": 0,
                    "no_backing_paths": [],
                    "still_unproven": [],
                    "path_proven_stuck": [],
                    "named_test_executed": ["ci-pipeline"],
                }
            ),
        )

        result = await _rs.sweep_proof_earn_loop(reaper_db, now)

        # (a) The sweep passed the DB session through (keyword arg) so the earn
        # loop can enroll a trial.
        commission_spy.assert_awaited_once()
        assert commission_spy.await_args.kwargs.get("session") is reaper_db
        # (b) named_test_executed is accumulated into the combined summary.
        assert result["named_test_executed"] == ["ci-pipeline"]
    finally:
        _rs._last_backing_proof_sweep = _prev_backing
        _rs._last_proof_earn_loop_sweep = _prev_earn
@pytest.mark.asyncio
async def test_reaper_cycle_runs_the_review_park_disposition_sweep(
    reaper_db, stop_after_one_cycle, reaper_side_effects, mocker
):
    """Registration pin: the disposition sweep must actually run in the loop.

    The whole point of ``app.review_park_disposition`` is that a review-fix park
    stops being indefinite. A sweep that exists, is unit-tested and is never
    wired into the reaper leaves every parked chain -- and its rollup parent, and
    its research direction -- stranded exactly as before, and no test of the
    sweep itself can see that. So pin BOTH halves here: the name the reaper calls
    is the real function, and one reaper cycle actually awaits it.
    """
    import app.reaper as reaper_module
    from app.review_park_disposition import sweep_dispose_expired_review_parks

    assert (
        reaper_module.sweep_dispose_expired_review_parks
        is sweep_dispose_expired_review_parks
    ), "the reaper must call the real sweep, not a shadowed local stub"

    spy = mocker.patch.object(
        reaper_module,
        "sweep_dispose_expired_review_parks",
        new_callable=AsyncMock,
        return_value=0,
    )

    await _run_reaper_once()

    assert spy.await_count == 1, (
        "one reaper cycle must run the review-park disposition sweep exactly once"
    )
    session_arg, now_arg = spy.await_args.args[:2]
    assert isinstance(session_arg, AsyncSession), (
        "the sweep must be handed the reaper's own session"
    )
    assert isinstance(now_arg, datetime), (
        "the sweep must be handed the cycle's clock, not left to read its own"
    )


# -- local_only placement holds vs the stalled-cycle reaper -------------------
#
# Live platform defect: a project with execution placement policy ``local_only``
# whose only bound runner was PAUSED had zero live runners, so its strategic
# phase task sat QUEUED under the policy's deliberately-unbounded wait. This
# sweep force-terminated it at 3h -- and that force-termination was itself the
# event that released ``_has_active_strategic_task``, letting a fresh cycle be
# cut against the identical unsatisfiable condition. 16 cycles died that way.


async def _local_only_project(db: AsyncSession) -> Project:
    project = Project(
        name="reaper-local-only",
        user_id="user1",
        build_config={
            "resource_envelope": {"execution_placement_policy": "local_only"}
        },
    )
    db.add(project)
    await db.commit()
    await db.refresh(project)
    return project


@pytest.mark.asyncio
async def test_step7_stalled_cycle_skips_migrated_local_only_placement_hold(
    reaper_db, mocker
):
    """The migrated hold must not be reaped -- and the loop must stay closed.

    The task is put into the real post-migration state by actually running
    ``_fail_stale_local_runner_holds``, which STRIPS the strategic hold keys the
    older ``task_is_strategic_local_runner_hold`` skip tests. That strip is why
    the pre-existing skip did not cover this task, so a test that hand-wrote the
    context would not track the real migration.
    """
    mocker.patch("app.broadcast.broadcast_to_project", new_callable=AsyncMock)
    mocker.patch("app.strategic_events.broadcast_to_project", new_callable=AsyncMock)
    from app.orchestrator_runner_dispatch import _fail_stale_local_runner_holds
    from app.orchestrator_runner_placement import (
        EXECUTION_PLACEMENT_WAIT_CONTEXT_KEY,
        task_is_local_only_placement_hold,
    )
    from app.strategic_runtime_work import task_is_strategic_local_runner_hold
    from app.task_lifecycle import _has_active_strategic_task
    from app.workbench_state import (
        STRATEGIC_LOCAL_RUNNER_HOLD_CONTEXT_KEY,
        STRATEGIC_LOCAL_RUNNER_REQUIRED_CONTEXT_KEY,
    )

    project = await _local_only_project(reaper_db)
    task = Task(
        project_id=project.id,
        title="strategic-phase2",
        payloads=[{"type": "strategic_review", "scope": "medium"}],
        status=TaskStatus.QUEUED,
        context={
            STRATEGIC_LOCAL_RUNNER_REQUIRED_CONTEXT_KEY: True,
            STRATEGIC_LOCAL_RUNNER_HOLD_CONTEXT_KEY: (
                _utcnow() - timedelta(hours=4)
            ).isoformat(),
        },
    )
    reaper_db.add(task)
    await reaper_db.commit()
    await reaper_db.refresh(task)

    # Real migration, not a hand-written context.
    await _fail_stale_local_runner_holds(reaper_db)
    await reaper_db.refresh(task)
    assert task.status == TaskStatus.QUEUED
    assert EXECUTION_PLACEMENT_WAIT_CONTEXT_KEY in (task.context or {})
    # The seam: the OLD skip no longer covers this task at all.
    assert task_is_strategic_local_runner_hold(task) is False
    assert task_is_local_only_placement_hold(task) is True

    cycle = StrategicCycle(
        id=_make_cycle_id(),
        project_id=project.id,
        outcome="phase2_running",
        phase2_task_id=task.id,
        phase1_started_at=_utcnow() - timedelta(hours=5),
        phase2_started_at=_utcnow() - timedelta(hours=4),
    )
    reaper_db.add(cycle)
    await reaper_db.commit()

    stalled = await _rs.sweep_stalled_strategic_cycles(
        reaper_db,
        _utcnow(),
        heartbeat_stale_cutoff_fn=lambda _task, now: now - timedelta(minutes=5),
    )

    assert stalled == 0
    await reaper_db.refresh(task)
    await reaper_db.refresh(cycle)
    assert task.status == TaskStatus.QUEUED
    assert cycle.outcome == "phase2_running"
    # The loop is broken, not merely quieter: the surviving QUEUED task keeps the
    # strategic admission guard closed, so no replacement cycle can be cut
    # against the same unsatisfiable condition.
    assert await _has_active_strategic_task(project.id, reaper_db) is True


@pytest.mark.asyncio
async def test_step7_stalled_cycle_still_reaps_strategic_task_without_placement_hold(
    reaper_db, mocker
):
    """Negative control: the skip must be narrow.

    Same project policy, same >3h phase age, same strategic task type -- the
    ONLY difference is that this task carries no placement wait. It must still
    be declared stalled and force-terminated, or the fix has simply disabled the
    watchdog.
    """
    mocker.patch("app.broadcast.broadcast_to_project", new_callable=AsyncMock)
    mocker.patch("app.strategic_events.broadcast_to_project", new_callable=AsyncMock)
    from app.orchestrator_runner_placement import EXECUTION_PLACEMENT_WAIT_CONTEXT_KEY

    project = await _local_only_project(reaper_db)
    task = Task(
        project_id=project.id,
        title="strategic-phase2",
        payloads=[{"type": "strategic_review", "scope": "medium"}],
        status=TaskStatus.IN_PROGRESS,
        last_heartbeat=_utcnow() - timedelta(hours=1),
    )
    reaper_db.add(task)
    await reaper_db.commit()
    await reaper_db.refresh(task)
    assert EXECUTION_PLACEMENT_WAIT_CONTEXT_KEY not in (task.context or {})

    cycle = StrategicCycle(
        id=_make_cycle_id(),
        project_id=project.id,
        outcome="phase2_running",
        phase2_task_id=task.id,
        phase1_started_at=_utcnow() - timedelta(hours=5),
        phase2_started_at=_utcnow() - timedelta(hours=4),
    )
    reaper_db.add(cycle)
    await reaper_db.commit()

    stalled = await _rs.sweep_stalled_strategic_cycles(
        reaper_db,
        _utcnow(),
        heartbeat_stale_cutoff_fn=lambda _task, now: now - timedelta(minutes=5),
    )

    assert stalled == 1
    await reaper_db.refresh(task)
    await reaper_db.refresh(cycle)
    assert task.status == TaskStatus.FAILED
    assert cycle.outcome == "stalled_phase2"


@pytest.mark.asyncio
async def test_step7_stalled_cycle_alert_dedupes_across_two_cycle_ids(
    reaper_db, mocker
):
    """One aging row per (project, phase) -- not one immortal row per cycle.

    The pre-fix raw ``Alert(...)`` insert titled itself with the cycle id, so N
    stalls on one project minted N rows that nothing could ever retire, and each
    row's ``created_at`` restarted the clock any age-based escalation would read.
    """
    mocker.patch("app.broadcast.broadcast_to_project", new_callable=AsyncMock)
    mocker.patch("app.strategic_events.broadcast_to_project", new_callable=AsyncMock)

    project = await _make_project(reaper_db)
    cycle_ids = []
    for _ in range(2):
        task = Task(
            project_id=project.id,
            title="strategic-phase2",
            payloads=[{"type": "strategic_review", "scope": "medium"}],
            status=TaskStatus.IN_PROGRESS,
            last_heartbeat=_utcnow() - timedelta(hours=1),
        )
        reaper_db.add(task)
        await reaper_db.commit()
        await reaper_db.refresh(task)
        cycle = StrategicCycle(
            id=_make_cycle_id(),
            project_id=project.id,
            outcome="phase2_running",
            phase2_task_id=task.id,
            phase1_started_at=_utcnow() - timedelta(hours=5),
            phase2_started_at=_utcnow() - timedelta(hours=4),
        )
        cycle_ids.append(cycle.id)
        reaper_db.add(cycle)
        await reaper_db.commit()
        assert (
            await _rs.sweep_stalled_strategic_cycles(
                reaper_db,
                _utcnow(),
                heartbeat_stale_cutoff_fn=lambda _t, now: now - timedelta(minutes=5),
            )
            == 1
        )

    rows = await reaper_db.execute(
        _select(Alert).where(
            Alert.project_id == project.id,
            Alert.event_type == "research.diagnosis_stalled",
        )
    )
    alerts = list(rows.scalars().all())
    assert len(alerts) == 1, [a.title for a in alerts]
    alert = alerts[0]
    assert alert.dedupe_key == f"research.diagnosis_stalled:{project.id}:phase2"
    assert alert.occurrence_count == 2
    # The row ages from the FIRST stall, but its message names the LATEST cycle.
    assert cycle_ids[0] not in alert.title
    assert cycle_ids[1] in alert.message


@pytest.mark.asyncio
async def test_step7_stalled_alert_names_the_reason_not_just_the_state(
    reaper_db, mocker
):
    """"Stalled in phase2" is a state. The alert must carry the CAUSE.

    A phase task that never left QUEUED means no agent ever ran, which is a
    different operator action from an agent that died mid-run — and the alert
    is the only place that distinction reaches a human.
    """
    mocker.patch("app.broadcast.broadcast_to_project", new_callable=AsyncMock)
    mocker.patch("app.strategic_events.broadcast_to_project", new_callable=AsyncMock)

    project = await _make_project(reaper_db)
    task = Task(
        project_id=project.id,
        title="strategic-phase2",
        payloads=[{"type": "strategic_review", "scope": "medium"}],
        status=TaskStatus.QUEUED,
        last_heartbeat=None,
        blocked_reason="no eligible runner bound to project",
    )
    reaper_db.add(task)
    await reaper_db.commit()
    await reaper_db.refresh(task)
    cycle = StrategicCycle(
        id=_make_cycle_id(),
        project_id=project.id,
        outcome="phase2_running",
        phase2_task_id=task.id,
        phase1_started_at=_utcnow() - timedelta(hours=5),
        phase2_started_at=_utcnow() - timedelta(hours=4),
    )
    reaper_db.add(cycle)
    await reaper_db.commit()

    # Force updated_at into the past (bypassing the onupdate hook) so the row
    # counts as untouched since long before the liveness window.
    from sqlalchemy import update as _sa_update

    await reaper_db.execute(
        _sa_update(Task)
        .where(Task.id == task.id)
        .values(
            updated_at=_utcnow() - timedelta(hours=2),
            created_at=_utcnow() - timedelta(hours=4),
        )
    )
    await reaper_db.commit()

    assert (
        await _rs.sweep_stalled_strategic_cycles(
            reaper_db,
            _utcnow(),
            heartbeat_stale_cutoff_fn=lambda _t, now: now - timedelta(minutes=5),
        )
        == 1
    )

    alert = (
        await reaper_db.execute(
            _select(Alert).where(
                Alert.project_id == project.id,
                Alert.event_type == "research.diagnosis_stalled",
            )
        )
    ).scalars().one()
    # The cause key, the human sentence, and the operator's own blocked_reason
    # all reach the alert.
    assert "phase2:queued_never_dispatched" in alert.message
    assert "never left QUEUED" in alert.message
    assert "no eligible runner bound to project" in alert.message
    # First stall of three: still a retryable WARNING, and it says so.
    assert alert.severity == AlertSeverity.WARNING
    assert "Stall 1 of 3" in alert.message

    # And the cause is durable on the cycle row, not only in the alert text.
    await reaper_db.refresh(cycle)
    assert cycle.cycle_metadata["stall_signature"] == "phase2:queued_never_dispatched"
    assert "never left QUEUED" in cycle.cycle_metadata["stall_reason"]


@pytest.mark.asyncio
async def test_step7_repeated_same_cause_stalls_escalate_one_alert_to_error(
    reaper_db, mocker
):
    """19 identical WARNINGs was the defect. One row escalates instead.

    Same dedupe key across attempts, so the occurrence count climbs on ONE row
    and the severity crosses to ERROR at the retry bound — the point at which
    the pipeline has stopped re-minting and only a human can clear it.
    """
    mocker.patch("app.broadcast.broadcast_to_project", new_callable=AsyncMock)
    mocker.patch("app.strategic_events.broadcast_to_project", new_callable=AsyncMock)

    project = await _make_project(reaper_db)
    for _ in range(_strategic_cycle.MAX_CONSECUTIVE_STALLS):
        task = Task(
            project_id=project.id,
            title="strategic-phase2",
            payloads=[{"type": "strategic_review", "scope": "medium"}],
            status=TaskStatus.QUEUED,
            last_heartbeat=None,
        )
        reaper_db.add(task)
        await reaper_db.commit()
        await reaper_db.refresh(task)
        cycle = StrategicCycle(
            id=_make_cycle_id(),
            project_id=project.id,
            outcome="phase2_running",
            phase2_task_id=task.id,
            phase1_started_at=_utcnow() - timedelta(hours=5),
            phase2_started_at=_utcnow() - timedelta(hours=4),
        )
        reaper_db.add(cycle)
        await reaper_db.commit()
        # Age the row past the liveness window so the sweep sees a task that
        # has not been touched, not one whose recovery is in flight.
        from sqlalchemy import update as _sa_update

        await reaper_db.execute(
            _sa_update(Task)
            .where(Task.id == task.id)
            .values(
                updated_at=_utcnow() - timedelta(hours=2),
                created_at=_utcnow() - timedelta(hours=4),
            )
        )
        await reaper_db.commit()
        assert (
            await _rs.sweep_stalled_strategic_cycles(
                reaper_db,
                _utcnow(),
                heartbeat_stale_cutoff_fn=lambda _t, now: now - timedelta(minutes=5),
            )
            == 1
        )

    alerts = list(
        (
            await reaper_db.execute(
                _select(Alert).where(
                    Alert.project_id == project.id,
                    Alert.event_type == "research.diagnosis_stalled",
                )
            )
        )
        .scalars()
        .all()
    )
    # ONE row, not one per stall.
    assert len(alerts) == 1, [a.title for a in alerts]
    alert = alerts[0]
    assert alert.occurrence_count == _strategic_cycle.MAX_CONSECUTIVE_STALLS
    # Escalated, and the message says retrying has stopped.
    assert alert.severity == AlertSeverity.ERROR
    assert "automatic retry stopped" in alert.title
    assert "SUPPRESSED" in alert.message
    assert "stall 3 in a row" in alert.message

    # The final cycle's failure_reason carries the attempt count and cause.
    last = (
        (
            await reaper_db.execute(
                _select(StrategicCycle)
                .where(StrategicCycle.project_id == project.id)
                .order_by(StrategicCycle.phase1_started_at.desc())
            )
        )
        .scalars()
        .first()
    )
    assert "attempt 3" in last.failure_reason
    assert "phase2:queued_never_dispatched" in last.failure_reason


@pytest.mark.asyncio
async def test_step7_stall_streak_does_not_leak_across_projects(reaper_db, mocker):
    """The cutoff is per project; one project's stalls must not bound another."""
    mocker.patch("app.broadcast.broadcast_to_project", new_callable=AsyncMock)
    mocker.patch("app.strategic_events.broadcast_to_project", new_callable=AsyncMock)

    noisy = await _make_project(reaper_db)
    for _ in range(_strategic_cycle.MAX_CONSECUTIVE_STALLS):
        reaper_db.add(
            StrategicCycle(
                id=_make_cycle_id(),
                project_id=noisy.id,
                outcome="stalled_phase2",
                phase1_started_at=_utcnow() - timedelta(hours=9),
                cycle_metadata={
                    "stall_signature": "phase2:queued_never_dispatched",
                    "stall_reason": "phase2 task never left QUEUED",
                },
            )
        )
    await reaper_db.commit()

    quiet = await _make_project(reaper_db)
    task = Task(
        project_id=quiet.id,
        title="strategic-phase2",
        payloads=[{"type": "strategic_review", "scope": "medium"}],
        status=TaskStatus.QUEUED,
        last_heartbeat=None,
    )
    reaper_db.add(task)
    await reaper_db.commit()
    await reaper_db.refresh(task)
    reaper_db.add(
        StrategicCycle(
            id=_make_cycle_id(),
            project_id=quiet.id,
            outcome="phase2_running",
            phase2_task_id=task.id,
            phase1_started_at=_utcnow() - timedelta(hours=5),
            phase2_started_at=_utcnow() - timedelta(hours=4),
        )
    )
    await reaper_db.commit()

    # Force updated_at into the past (bypassing the onupdate hook) so the row
    # counts as untouched since long before the liveness window.
    from sqlalchemy import update as _sa_update

    await reaper_db.execute(
        _sa_update(Task)
        .where(Task.id == task.id)
        .values(
            updated_at=_utcnow() - timedelta(hours=2),
            created_at=_utcnow() - timedelta(hours=4),
        )
    )
    await reaper_db.commit()

    assert (
        await _rs.sweep_stalled_strategic_cycles(
            reaper_db,
            _utcnow(),
            heartbeat_stale_cutoff_fn=lambda _t, now: now - timedelta(minutes=5),
        )
        == 1
    )

    alert = (
        await reaper_db.execute(
            _select(Alert).where(
                Alert.project_id == quiet.id,
                Alert.event_type == "research.diagnosis_stalled",
            )
        )
    ).scalars().one()
    # First stall for THIS project despite the neighbour's exhausted streak.
    assert alert.severity == AlertSeverity.WARNING
    assert "Stall 1 of 3" in alert.message


def test_stall_cutoff_skip_is_not_a_healthy_idle_receipt():
    """A cutoff skip must not suppress the strategic-starvation alert.

    The cutoff writes into the same ``_strategic_no_delta_skips`` log as an
    ordinary quiet-project skip, but it means the strategist is BLOCKED. If it
    counted as an intentional-idle receipt the cutoff would mute the one
    detector that notices a halted strategic loop.
    """
    cutoff = _utcnow() - timedelta(hours=6)
    recent = (_utcnow() - timedelta(hours=1)).isoformat()

    blocked = {
        "_strategic_no_delta_skips": [
            {
                "skipped_at": recent,
                "reason": (
                    "stall_cutoff: 3 consecutive strategic cycles stalled "
                    "with the same cause [phase2:queued_never_dispatched]"
                ),
            }
        ]
    }
    assert _rs._recent_no_delta_skip_at(blocked, cutoff) is None

    # A genuine quiet-project skip still counts.
    quiet = {
        "_strategic_no_delta_skips": [
            {
                "skipped_at": recent,
                "reason": "no_meaningful_delta_since_last_cycle",
            }
        ]
    }
    assert _rs._recent_no_delta_skip_at(quiet, cutoff) is not None

    # Legacy entries predate the reason key and must keep counting.
    legacy = {"_strategic_no_delta_skips": [{"skipped_at": recent}]}
    assert _rs._recent_no_delta_skip_at(legacy, cutoff) is not None

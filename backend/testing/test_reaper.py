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
from app.reaper import (
    reaper_service,
    _copy_analysis_artifacts_to_previous,
    _reaper_skip_counts,
    _heartbeat_stale_cutoff,
    _progress_stale_cutoff,
)
from app.shared_state import runtime_config as _runtime_config


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
        # strategic_review override sets effort=max → 720s floor overrides the
        # 300s small-scope threshold.
        assert _heartbeat_stale_cutoff(_T(), now) == now - timedelta(seconds=720)
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
    mocker.patch(
        "app.main._ensure_vm_deleted", new_callable=AsyncMock, return_value=True
    )
    mocker.patch("app.main.delete_agent_vm", return_value=True)
    mocker.patch("app.main._maybe_create_failure_alert", new_callable=AsyncMock)
    mocker.patch(
        "app.rollup_management._check_and_trigger_rollup", new_callable=AsyncMock
    )
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
    try:  # noqa: SIM105
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
        started_at=datetime.utcnow() - timedelta(minutes=10),
        last_heartbeat=datetime.utcnow() - timedelta(seconds=30),
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
        started_at=datetime.utcnow() - timedelta(minutes=3),
        last_heartbeat=datetime.utcnow() - timedelta(seconds=10),
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
    now = datetime.utcnow()
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
async def test_in_progress_no_tool_use_codex_provider_not_reaped(
    reaper_db, stop_after_one_cycle, reaper_side_effects
):
    """Codex (openai) tasks with last_tool_use_at NULL must NOT be reaped.

    The wrapper's outputMode='text' for Codex never invokes recordToolUse,
    so last_tool_use_at IS NULL for every Codex task by design — even when
    Codex is calling apply_patch happily. Pre-fix this watchdog reaped all
    healthy Codex work after the progress-stale cutoff. Pin the gate.
    """
    project = await _make_project(reaper_db)
    t = Task(
        project_id=project.id,
        payloads=[{"type": "fix", "scope": "small", "model": "gpt-5.4"}],
        vm_instance_id="vm-codex",
        status=TaskStatus.IN_PROGRESS,
        attempt_count=0,
        started_at=datetime.utcnow() - timedelta(minutes=10),
        last_heartbeat=datetime.utcnow() - timedelta(seconds=30),
        last_tool_use_at=None,
        context={"scope": "small", "llm_provider": "openai"},
    )
    reaper_db.add(t)
    await reaper_db.commit()

    await _run_reaper_once()
    await reaper_db.refresh(t)

    assert t.status == TaskStatus.IN_PROGRESS


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
        started_at=datetime.utcnow() - timedelta(minutes=10),
        # Heartbeat fresh — agent is alive and thinking
        last_heartbeat=datetime.utcnow() - timedelta(seconds=30),
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
        last_heartbeat=datetime.utcnow() - timedelta(minutes=30),
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
        last_heartbeat=datetime.utcnow() - timedelta(minutes=8),
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
        last_heartbeat=datetime.utcnow() - timedelta(minutes=16),
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
    # Audit W1: retry_after_until is a column.
    t = Task(
        project_id=project.id,
        # _extract_task_model reads payloads[0]["model"]
        payloads=[{"type": "create", "model": "sonnet"}],
        vm_instance_id="vm-rl",
        status=TaskStatus.IN_PROGRESS,
        attempt_count=1,
        last_heartbeat=datetime.utcnow() - timedelta(minutes=10),
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
async def test_reaper_account_cooldown_silent_death_free_requeue(
    reaper_db, stop_after_one_cycle, reaper_side_effects, mocker
):
    """Silent death (no rate-limit keywords) with account in cooldown → RATE_LIMIT free requeue.

    Regression guard for the account-state correlation path through the REAPER.
    The existing test triggers on a log keyword; this one drives classification
    purely via account state so a future change to keyword detection can't mask
    a broken correlation path.
    """
    disable_mock = mocker.patch(
        "app.max_plan_pool.disable_account_model", new_callable=AsyncMock
    )
    mocker.patch("app.max_plan_pool.release_max_plan_account", new_callable=AsyncMock)
    # Stub account-state correlation to return True — isolates the REAPER free-requeue
    # path from the account-pool internals (those are tested in test_failure_classification.py).
    mocker.patch(
        "app.failure_classification._account_was_rate_limited", return_value=True
    )

    account_id = "acct-rl-silent"
    project = await _make_project(reaper_db)
    t = Task(
        project_id=project.id,
        payloads=[{"type": "create", "model": "sonnet"}],
        vm_instance_id="vm-rl-silent",
        status=TaskStatus.IN_PROGRESS,
        attempt_count=1,
        last_heartbeat=datetime.utcnow() - timedelta(minutes=10),
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
    assert t.status == TaskStatus.QUEUED, "account-correlated RL must requeue"
    assert t.attempt_count == 1, "account-correlated RL must not burn attempt budget"
    assert account_id in (ctx.get("blacklisted_accounts") or [])
    assert "max_plan_account_id" not in ctx
    disable_mock.assert_awaited_once()


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

    def _rl_only_at_heartbeat(task, at_time=None):
        # Simulate cooldown active at failure instant (at_time set) but expired now.
        return at_time is not None

    mocker.patch(
        "app.failure_classification._account_was_rate_limited",
        side_effect=_rl_only_at_heartbeat,
    )

    account_id = "acct-rl-edge"
    project = await _make_project(reaper_db)
    t = Task(
        project_id=project.id,
        payloads=[{"type": "create", "model": "sonnet"}],
        vm_instance_id="vm-rl-edge",
        status=TaskStatus.IN_PROGRESS,
        attempt_count=1,
        last_heartbeat=datetime.utcnow() - timedelta(minutes=20),
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
    assert t.status == TaskStatus.QUEUED, (
        "failure at tail of rate-limit window must still classify RATE_LIMIT "
        "(at_time=last_heartbeat fix)"
    )
    assert t.attempt_count == 1, (
        "must not burn attempt budget (at_time=last_heartbeat plumbing)"
    )
    assert account_id in (ctx.get("blacklisted_accounts") or [])
    disable_mock.assert_awaited_once()


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
        last_heartbeat=datetime.utcnow() - timedelta(minutes=20),
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
        context={
            "last_error": "name resolution failed for api.github.com",
            "pr_number": 42,
        },
    )
    reaper_db.add(t)
    await reaper_db.commit()

    before = datetime.utcnow()
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
        {
            "retry_budgets_by_class": {
                "AGENT_TIMEOUT": 10,
                "CODE_ERROR": 2,
                "EXTERNAL_API": 4,
            }
        },
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
    # scope=large has a 15min heartbeat-stale threshold — need >15min of staleness
    # for the reaper to classify this task as orphaned and run the budget check.
    t = Task(
        project_id=project.id,
        payloads=[{"type": "create"}],
        vm_instance_id="vm-large-code-error",
        status=TaskStatus.IN_PROGRESS,
        attempt_count=1,
        last_heartbeat=datetime.utcnow() - timedelta(minutes=16),
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
    far_dt = datetime.utcnow() + timedelta(hours=6)
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
        updated_at=datetime.utcnow() - timedelta(minutes=30),
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
            last_heartbeat=datetime.utcnow(),
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
        updated_at=datetime.utcnow() - timedelta(minutes=30),
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
        updated_at=datetime.utcnow() - timedelta(minutes=30),
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
        updated_at=datetime.utcnow() - timedelta(minutes=30),
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
async def test_retry_after_sweep_preserves_valid(
    reaper_db, stop_after_one_cycle, reaper_side_effects
):
    """A retry_after within the 30-min ceiling must be left intact.

    Audit W1: retry_after_until is a column.
    """
    project = await _make_project(reaper_db)
    valid_dt = datetime.utcnow() + timedelta(minutes=5)
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
        last_heartbeat=datetime.utcnow() - timedelta(minutes=10),
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
        last_heartbeat=datetime.utcnow() - timedelta(minutes=10),
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
        last_heartbeat=datetime.utcnow() - timedelta(minutes=10),
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
        last_heartbeat=datetime.utcnow() - timedelta(minutes=10),
    )
    reaper_db.add(t)
    await reaper_db.commit()
    task_id = t.id
    clean_skip_counts[task_id] = 4  # one more skip will trigger force-lock

    real_execute = AsyncSession.execute
    state = {"for_update_calls": 0}
    fresh_ts = datetime.utcnow()

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
    title/message, and tier persisted in task.context."""  # noqa: RUF002
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
        created_at=datetime.utcnow() - timedelta(hours=7),
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
        created_at=datetime.utcnow() - timedelta(hours=3),
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
        created_at=datetime.utcnow() - timedelta(hours=2, minutes=30),
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
    assert persisted in {
        "CODE_ERROR",
        "AGENT_TIMEOUT",
        "EXTERNAL_API",
        "RATE_LIMIT",
        "PROVISION_FAILURE",
    }, f"failure_category must be persisted as a canonical class, got {persisted!r}"

    alert_mock.assert_awaited()
    assert alert_mock.await_args.args[0].id == t.id

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

    alerts = (
        (await reaper_db.execute(_select(Alert).where(Alert.task_id == t.id)))
        .scalars()
        .all()
    )
    assert len(alerts) == 1
    assert alerts[0].severity == AlertSeverity.INFO
    assert "PROVISION_FAILURE" in (alerts[0].message or "")


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
    from app.reaper import _sweep_orphan_direction_holds

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
    from app.reaper import _sweep_orphan_direction_holds

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
    from app.reaper import _sweep_orphan_direction_holds

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
    from app.reaper import _sweep_orphan_direction_holds

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
    from app.reaper import _sweep_stale_residual_cases

    _fc._residual_closed_fingerprints.clear()
    stale_case = ResidualRemediationCase(
        fingerprint="old-fp-abc123",
        status=ResidualCaseStatus.ACTIVE,
        last_seen_at=datetime.utcnow() - timedelta(hours=73),
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
    from app.reaper import _sweep_stale_residual_cases

    _fc._residual_closed_fingerprints.clear()
    recent_case = ResidualRemediationCase(
        fingerprint="recent-fp-xyz",
        status=ResidualCaseStatus.ACTIVE,
        last_seen_at=datetime.utcnow() - timedelta(hours=1),
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
    from app.reaper import _sweep_stale_residual_cases

    _fc._residual_closed_fingerprints.clear()
    already_closed = ResidualRemediationCase(
        fingerprint="closed-fp-already",
        status=ResidualCaseStatus.CLOSED_ORGANICALLY,
        last_seen_at=datetime.utcnow() - timedelta(hours=100),
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
    from app.reaper import _sweep_stale_residual_cases

    event_mock = mocker.patch(
        "app.notification_dispatch.post_event_message", new_callable=AsyncMock
    )
    _fc._residual_closed_fingerprints.clear()
    stale_case = ResidualRemediationCase(
        fingerprint="remediating-old-fp",
        status=ResidualCaseStatus.REMEDIATING,
        project_id=_uuid_mod.uuid4(),
        last_seen_at=datetime.utcnow() - timedelta(hours=73),
        last_spawned_at=datetime.utcnow() - timedelta(hours=73),
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
    from app.reaper import _sweep_stale_residual_cases

    event_mock = mocker.patch(
        "app.notification_dispatch.post_event_message", new_callable=AsyncMock
    )
    _fc._residual_closed_fingerprints.clear()
    recent_case = ResidualRemediationCase(
        fingerprint="remediating-recent-fp",
        status=ResidualCaseStatus.REMEDIATING,
        project_id=_uuid_mod.uuid4(),
        last_seen_at=datetime.utcnow() - timedelta(hours=1),
        last_spawned_at=datetime.utcnow() - timedelta(hours=1),
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
        last_heartbeat=datetime.utcnow() - timedelta(minutes=10),
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
        last_heartbeat=datetime.utcnow() - timedelta(minutes=10),
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
        last_heartbeat=datetime.utcnow() - timedelta(minutes=10),
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
        last_heartbeat=datetime.utcnow() - timedelta(minutes=10),
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
        last_heartbeat=datetime.utcnow() - timedelta(minutes=10),
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
        return_value={"state": "closed", "merged": True},
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
        vm_instance_id="vm-merged-pr",
        status=TaskStatus.IN_PROGRESS,
        attempt_count=0,
        last_heartbeat=datetime.utcnow() - timedelta(minutes=10),
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
        last_heartbeat=datetime.utcnow() - timedelta(minutes=10),
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
        last_heartbeat=datetime.utcnow() - timedelta(minutes=10),
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
    # wrote a log into task.context already.
    mocker.patch("app.reaper._capture_serial_for_vm", return_value=None)
    mocker.patch("app.reaper.get_vm_zone", return_value=None)

    project = await _make_project(reaper_db)
    t = Task(
        project_id=project.id,
        payloads=[{"type": "create"}],
        vm_instance_id="vm-sticky-serial",
        status=TaskStatus.IN_PROGRESS,
        attempt_count=0,
        last_heartbeat=datetime.utcnow() - timedelta(minutes=7),
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


@pytest.mark.asyncio
async def test_step9_sweep_finalizes_qualification_stuck_pending_after_reaper_kill(
    reaper_db, stop_after_one_cycle, reaper_side_effects
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

        # Both qualifier children FAILED — exactly what a reaper kill leaves
        # behind. vm_instance_id is None so the active-task sweep ignores them
        # (FAILED is already non-active; this is belt-and-suspenders).
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
    reaper_db, stop_after_one_cycle, reaper_side_effects
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

        # Audit FAILED but the smoketest child is still QUEUED — not stuck,
        # just mid-flight. vm_instance_id is None so the active sweep won't
        # reap the QUEUED child and change its status under us.
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
            status=TaskStatus.QUEUED,
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

        # started_at is FRESH (now) so the 6h qualification-timeout backstop
        # in sweep_stuck_qualifications does NOT fire — this test exercises the
        # running-child skip path, not the timeout force-finalize path. A stale
        # timestamp here would let the timeout finalize the version as
        # not_promoted and mask the running-child skip behaviour being tested.
        version.last_qualification = {
            "status": "pending",
            "candidate_sha": "abcd" * 10,
            "ci_green": False,
            "started_at": _now_for_seed().isoformat(),
            "audit_task_id": str(audit_task.id),
            "smoketest_task_id": str(smoke_task.id),
        }
        reaper_db.add(version)
        await reaper_db.commit()

        # Positive control: a SECOND version (own project — ReleaseVersion is
        # unique on (project_id, version_number)) whose audit + smoketest are
        # BOTH terminal. The same sweep cycle MUST finalize this one. If the
        # sweep's dedicated finalize session were opened against the wrong
        # engine (Round 7 regression), this finalize would silently fail and
        # the control assertion below would catch it — so "stays pending" for
        # the running-child version can no longer pass for the wrong reason.
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
        control_smoke = Task(
            project_id=control_project.id,
            title="Qualifier smoketest: automain candidate",
            payloads=[{"type": "smoketest", "base_branch": "automain"}],
            vm_instance_id=None,
            status=TaskStatus.FAILED,
            context={
                "release_version_id": str(control_version.id),
                "qualifier_smoketest": True,
            },
        )
        reaper_db.add(control_audit)
        reaper_db.add(control_smoke)
        await reaper_db.commit()
        await reaper_db.refresh(control_audit)
        await reaper_db.refresh(control_smoke)
        control_version.last_qualification = {
            "status": "pending",
            "candidate_sha": "ef01" * 10,
            "ci_green": False,
            "started_at": _now_for_seed().isoformat(),
            "audit_task_id": str(control_audit.id),
            "smoketest_task_id": str(control_smoke.id),
        }
        reaper_db.add(control_version)
        await reaper_db.commit()

        await _run_reaper_once()
        await reaper_db.refresh(version)
        await reaper_db.refresh(control_version)

        # One child still non-terminal — the version stays 'pending'.
        assert version.last_qualification is not None
        assert version.last_qualification.get("status") == "pending", (
            "Step 9 must not finalize while a qualifier child is non-terminal"
        )
        # Positive control: both-children-terminal version WAS finalized in the
        # same cycle — proves the skip above is a deliberate decision by a
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
    reaper_db, stop_after_one_cycle, reaper_side_effects
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

        # Audit COMPLETED, smoketest hung IN_PROGRESS — without the timeout
        # this would never finalize.
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
            status=TaskStatus.IN_PROGRESS,  # hung — never reaches terminal
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

        # CORRUPT started_at — _parse_started_at returns None for this.
        version.last_qualification = {
            "status": "pending",
            "candidate_sha": "abcd" * 10,
            "ci_green": False,
            "started_at": "this-is-not-a-timestamp",
            "audit_task_id": str(audit_task.id),
            "smoketest_task_id": str(smoke_task.id),
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
        assert version.last_qualification.get("status") == "not_promoted", (
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

    alerts = (
        (
            await reaper_db.execute(
                _select(_Alert).where(_Alert.project_id == project.id)
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

    alerts = (
        (
            await reaper_db.execute(
                _select(_Alert).where(_Alert.project_id == project.id)
            )
        )
        .scalars()
        .all()
    )
    assert len(alerts) == 0


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
        last_heartbeat=datetime.utcnow() - timedelta(hours=1),
    )
    reaper_db.add(task)
    await reaper_db.commit()
    await reaper_db.refresh(task)

    cycle = StrategicCycle(
        id=_make_cycle_id(),
        project_id=project.id,
        outcome="phase1_running",
        phase1_task_id=task.id,
        phase1_started_at=datetime.utcnow() - timedelta(hours=4),
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
        last_heartbeat=datetime.utcnow() - timedelta(hours=1),
    )
    reaper_db.add(task)
    await reaper_db.commit()
    await reaper_db.refresh(task)

    cycle = StrategicCycle(
        id=_make_cycle_id(),
        project_id=project.id,
        outcome="phase2_running",
        phase2_task_id=task.id,
        phase1_started_at=datetime.utcnow() - timedelta(hours=4),
        phase2_started_at=datetime.utcnow() - timedelta(hours=4),
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
        last_heartbeat=datetime.utcnow() - timedelta(seconds=30),
    )
    reaper_db.add(task)
    await reaper_db.commit()
    await reaper_db.refresh(task)

    cycle = StrategicCycle(
        id=_make_cycle_id(),
        project_id=project.id,
        outcome="phase1_running",
        phase1_task_id=task.id,
        phase1_started_at=datetime.utcnow() - timedelta(hours=4),
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
async def test_step7_stalled_cycle_already_terminal_task_untouched(
    reaper_db, stop_after_one_cycle, reaper_side_effects, mocker
):
    """Linked task already FAILED — cycle is declared stalled without re-mutating the task."""
    mocker.patch("app.broadcast.broadcast_to_project", new_callable=AsyncMock)

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
        phase1_started_at=datetime.utcnow() - timedelta(hours=4),
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
    assert cycle.outcome == "stalled_phase1", "cycle must be declared stalled"
